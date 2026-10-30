"""
Routes de contrôle : pause/reprise du moteur, kill-switch, bascule PAPER/LIVE.

Chaque action est loguée en WARNING ou CRITICAL : elles modifient le comportement
d'un bot qui peut engager de l'argent réel.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from api.dependencies import build_status, get_active_broker, get_engine
from api.schemas import (
    ClosedPositionOut,
    ControlOut,
    KillSwitchIn,
    KillSwitchOut,
    ModeIn,
    ModeOut,
)
from api.security import require_api_key
from brokers.base import BaseBroker, is_dry_run, set_dry_run
from core.engine import TradingEngine
from utils.logger import get_logger

log = get_logger(__name__)

router = APIRouter(prefix="/api/control", tags=["Contrôle"], dependencies=[Depends(require_api_key)])

LIVE_CONFIRMATION = "LIVE"
KILL_SWITCH_COMMENT = "Kill switch"


@router.post("/start", response_model=ControlOut, summary="Démarrer / reprendre le moteur")
def start_engine(engine: TradingEngine = Depends(get_engine)) -> ControlOut:
    """Reprend l'évaluation des symboles au prochain tour de boucle."""
    if not engine.is_running:
        return ControlOut(success=False, message="La boucle du moteur est arrêtée : relancer le bot",
                          status=build_status(engine))
    engine.resume()
    log.warning("API : reprise du moteur demandée")
    return ControlOut(success=True, message="Moteur actif", status=build_status(engine))


@router.post("/stop", response_model=ControlOut, summary="Mettre le moteur en pause")
def pause_engine(engine: TradingEngine = Depends(get_engine)) -> ControlOut:
    """
    Suspend l'ouverture de nouveaux trades. Les positions ouvertes restent en place
    (gérées par leurs SL/TP) ; utiliser le kill-switch pour les fermer.
    """
    engine.pause()
    log.warning("API : pause du moteur demandée")
    return ControlOut(success=True, message="Moteur en pause", status=build_status(engine))


@router.post("/kill-switch", response_model=KillSwitchOut, summary="Fermeture d'urgence des positions")
def kill_switch(payload: KillSwitchIn = KillSwitchIn(), engine: TradingEngine = Depends(get_engine),
                broker: BaseBroker = Depends(get_active_broker)) -> KillSwitchOut:
    """
    Met le moteur en pause puis ferme immédiatement les positions ouvertes (celles du
    bot, ou toutes avec `include_manual`). Les clôtures sont RÉELLES, y compris en mode
    PAPER : un kill-switch ne doit jamais être simulé.
    """
    log.critical("API : KILL-SWITCH déclenché (positions manuelles incluses : %s)", payload.include_manual)
    if payload.pause_engine:
        engine.pause()

    results: list[ClosedPositionOut] = []
    for position in broker.get_positions(bot_only=not payload.include_manual):
        result = broker.close_order(position.ticket, comment=KILL_SWITCH_COMMENT, force=True)
        results.append(ClosedPositionOut(
            ticket=position.ticket,
            symbol=position.symbol,
            success=result.success,
            message=result.message or ("fermée" if result.success else "échec"),
        ))

    closed = sum(r.success for r in results)
    failed = len(results) - closed
    if failed:
        log.critical("KILL-SWITCH : %d position(s) fermée(s), %d ÉCHEC(S) : vérifier le terminal", closed, failed)
    else:
        log.warning("KILL-SWITCH : %d position(s) fermée(s)", closed)
    return KillSwitchOut(closed=closed, failed=failed, engine_paused=engine.is_paused, results=results)


@router.post("/positions/{ticket}/close", response_model=ClosedPositionOut, summary="Fermer une position")
def close_single_position(ticket: int, broker: BaseBroker = Depends(get_active_broker)) -> ClosedPositionOut:
    """
    Ferme une position par son ticket (bot ou manuelle). Comme le kill-switch, la
    clôture est RÉELLE même en mode PAPER : l'utilisateur agit sur le compte réel.
    """
    position = next((p for p in broker.get_positions(bot_only=False) if p.ticket == ticket), None)
    if position is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Position #{ticket} introuvable")

    log.warning("API : clôture manuelle de la position #%d (%s)", ticket, position.symbol)
    result = broker.close_order(ticket, comment="Dashboard close", force=True)
    return ClosedPositionOut(
        ticket=ticket,
        symbol=position.symbol,
        success=result.success,
        message=result.message or ("fermée" if result.success else "échec"),
    )


@router.post("/mode", response_model=ModeOut, summary="Basculer PAPER / LIVE")
def switch_mode(payload: ModeIn) -> ModeOut:
    """
    Bascule entre PAPER (`dry_run: true`, ordres simulés) et LIVE (`dry_run: false`,
    ordres réels). Le passage en LIVE exige `confirm: "LIVE"`. Effectif dès l'ordre suivant.
    """
    if not payload.dry_run and payload.confirm != LIVE_CONFIRMATION:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Passage en LIVE refusé : ajouter \"confirm\": \"{LIVE_CONFIRMATION}\" pour confirmer",
        )
    previous = "PAPER" if is_dry_run() else "LIVE"
    set_dry_run(payload.dry_run)
    mode = "PAPER" if is_dry_run() else "LIVE"
    log.warning("API : mode %s -> %s", previous, mode)
    return ModeOut(mode=mode, message="Mode inchangé" if mode == previous else f"Mode {mode} activé")
