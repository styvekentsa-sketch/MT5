"""
Exécution manuelle d'un setup proposé (bouton « Exécuter ce setup en Live » du dashboard).

Le setup affiché peut dater de quelques secondes : tout est revérifié au moment du clic, avec le
prix ACTUEL, par les mêmes règles que le moteur. Refus (aucun ordre) si :

  - le bot est en mode PAPER (l'ordre ne serait pas réel) ;
  - le compte est un compte RÉEL, sauf RISK_ALLOW_MANUAL_REAL_ACCOUNT=true (démo uniquement par
    défaut) ; type de compte inconnu : refus par précaution ;
  - le setup n'est pas « prêt », a expiré, ou a déjà été exécuté (double clic) ;
  - la perte journalière maximale est atteinte (kill switch) ;
  - le prix a fui : écart entre le prix exécutable actuel et l'entrée prévue > 0.20 ATR, ou le
    prix a déjà franchi le stop ;
  - la taille recalculée (1 % de risque, risque cumulé) est nulle, ou les règles de risque /
    le vérificateur indépendant refusent l'ordre au prix actuel.

Ordre AU MARCHÉ (les setups prévoient une entrée au premier prix exécutable), avec le SL et le TP
du setup. Une position ouverte est inscrite au journal d'apprentissage (post-mortem si perte).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone

from brokers.base import BaseBroker, is_dry_run
from config.settings import settings
from models.schemas import SetupStatus, Side, TradingSetup
from risk.risk_manager import calculate_position_size, daily_stop_reached, validate_trade_risk
from risk.verifier import verify_order
from strategies.base import ENTRY_MAX_DRIFT_ATR
from strategies.registry import strategy_min_ratio
from utils.logger import get_logger
from utils.notifier import Level, notify

log = get_logger(__name__)

ORDER_COMMENT = "Setup manuel"


@dataclass(frozen=True)
class ExecutionResult:
    success: bool
    message: str
    code: str = "ok"                 # ok, refused, broker_error
    ticket: int | None = None
    volume: float = 0.0
    price: float | None = None
    sl: float | None = None
    tp: float | None = None
    checks: list[str] = field(default_factory=list)

    @classmethod
    def refused(cls, message: str) -> ExecutionResult:
        return cls(False, message, "refused")


_executed: set[str] = set()
_lock = threading.Lock()


def account_block(broker: BaseBroker) -> str | None:
    """Motif global d'interdiction (mode, type de compte), ou None : le bouton peut être actif."""
    if is_dry_run():
        return "mode PAPER : passez le bot en LIVE pour envoyer un ordre réel"
    account = broker.get_account_info()
    if account is None:
        return f"compte {broker.description} indisponible"
    if account.account_type == "real" and not settings.risk.allow_manual_real_account:
        return "compte RÉEL : exécution manuelle désactivée (RISK_ALLOW_MANUAL_REAL_ACCOUNT=false)"
    if account.account_type not in ("demo", "real", "contest"):
        return f"type de compte inconnu ({account.account_type}) : exécution refusée par précaution"
    return None


def was_executed(setup_id: str) -> bool:
    with _lock:
        return setup_id in _executed


def execute_setup(setup: TradingSetup, broker: BaseBroker, memory=None,
                  now: datetime | None = None) -> ExecutionResult:
    """Revérifie le setup au prix actuel puis envoie l'ordre au marché ; jamais d'exception."""
    now = now or datetime.now(timezone.utc)
    with _lock:                                        # un clic à la fois, et un seul ordre par setup
        if setup.id in _executed:
            return ExecutionResult.refused(f"setup {setup.id} déjà exécuté")
        try:
            result = _execute(setup, broker, memory, now)
        except Exception as exc:  # noqa: BLE001 - l'interface doit toujours recevoir une réponse
            log.exception("[%s] Exécution manuelle du setup %s impossible", setup.symbol, setup.id)
            return ExecutionResult(False, f"erreur inattendue : {type(exc).__name__}", "broker_error")
        if result.success:
            _executed.add(setup.id)
        return result


def _execute(setup: TradingSetup, broker: BaseBroker, memory, now: datetime) -> ExecutionResult:
    symbol, side = setup.symbol, setup.side.value
    block = account_block(broker)
    if block:
        return ExecutionResult.refused(block)
    if setup.status != SetupStatus.READY:
        return ExecutionResult.refused(f"setup au statut « {setup.status.value} » : seul un setup prêt s'exécute")
    if setup.expires_at is not None and now > setup.expires_at:
        return ExecutionResult.refused(f"setup expiré à {setup.expires_at:%H:%M:%S} UTC : relancez l'analyse")
    if setup.take_profit is None:
        return ExecutionResult.refused("setup sans take profit")
    stop = daily_stop_reached(broker)
    if stop:
        return ExecutionResult.refused(f"limite journalière atteinte : {stop}")

    tick = broker.get_current_tick(symbol)
    if tick is None:
        return ExecutionResult.refused(f"aucun prix disponible pour {symbol}")
    entry = tick.ask if setup.side == Side.BUY else tick.bid
    atr = setup.indicators.get("atr")
    if isinstance(atr, (int, float)) and atr > 0 and abs(entry - setup.entry_price) > ENTRY_MAX_DRIFT_ATR * atr:
        return ExecutionResult.refused(
            f"le prix a bougé : {entry:.5g} contre {setup.entry_price:.5g} prévu "
            f"({abs(entry - setup.entry_price) / atr:.2f} ATR, maximum {ENTRY_MAX_DRIFT_ATR:g})")
    beyond_stop = entry <= setup.stop_loss if setup.side == Side.BUY else entry >= setup.stop_loss
    beyond_target = entry >= setup.take_profit if setup.side == Side.BUY else entry <= setup.take_profit
    if beyond_stop or beyond_target:
        return ExecutionResult.refused(f"prix actuel {entry:.5g} hors des niveaux du setup "
                                       f"(SL {setup.stop_loss:.5g}, TP {setup.take_profit:.5g})")

    volume = calculate_position_size(symbol, entry, setup.stop_loss, broker=broker)
    if volume <= 0:
        return ExecutionResult.refused("taille de position nulle : le risque autorisé est déjà utilisé")
    check = validate_trade_risk(symbol, side, volume, setup.stop_loss, setup.take_profit, broker=broker)
    if not check.get("is_approved"):
        return ExecutionResult.refused(f"règles de risque : {check.get('reason', 'refus')}")
    verdict = verify_order(symbol, side, entry, setup.stop_loss, setup.take_profit, broker,
                           min_ratio=strategy_min_ratio(setup.strategy))
    if not verdict.approved:
        return ExecutionResult.refused(f"vérificateur indépendant : {verdict.reason}")

    comment = f"{ORDER_COMMENT} {setup.strategy}"[:31]             # commentaire MT5 : 31 caractères
    result = broker.place_order(symbol, side, volume, sl=setup.stop_loss, tp=setup.take_profit, comment=comment)
    if not result.success:
        log.error("[%s] Exécution manuelle refusée par le broker : %s", symbol, result.message)
        return ExecutionResult(False, f"ordre refusé par le broker : {result.message}", "broker_error")
    if result.dry_run:                                 # filet : jamais de « succès » simulé présenté comme réel
        return ExecutionResult(False, "ordre simulé (mode PAPER) : aucun ordre réel envoyé", "refused")

    log.warning("[%s] EXÉCUTION MANUELLE du setup %s (%s) : %s %s @ %s | SL %s | TP %s | ticket #%s",
                symbol, setup.id, setup.strategy, side, result.volume, result.price, result.sl, result.tp,
                result.ticket)
    notify(f"Setup exécuté · {symbol} {side}",
           f"{setup.strategy} · {result.volume:g} lot @ {result.price} | SL {result.sl} | TP {result.tp} | "
           f"ticket #{result.ticket}", Level.INFO)
    if memory is not None:
        try:
            memory.record_entry(result, {"mode": setup.style.value, "strategy": setup.strategy,
                                         "verdict": f"setup manuel : {setup.reason}"},
                                {f"setup_{k}": v for k, v in setup.indicators.items() if isinstance(v, (int, float))})
        except Exception:  # noqa: BLE001 - le journal ne doit pas masquer un ordre passé
            log.exception("[%s] Journal d'entrée impossible pour le ticket #%s", symbol, result.ticket)
    return ExecutionResult(True, f"ordre {side} {result.volume:g} {symbol} exécuté (ticket #{result.ticket})",
                           ticket=result.ticket, volume=result.volume, price=result.price, sl=result.sl,
                           tp=result.tp, checks=[f"ratio net {verdict.net_reward_risk:.2f}" if verdict.net_reward_risk
                                                 else "ratio net vérifié", f"volume {volume:g} (risque recalculé)"])
