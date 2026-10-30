"""
Journal des décisions et rejets du moteur : pour chaque symbole et chaque tour, le sens
envisagé (ACHAT / VENTE), le verdict (ACCEPTÉ / REJETÉ) et son motif précis.

Source : table SQLite `trade_decisions` ; si la base est indisponible, copie en mémoire
des dernières décisions du moteur.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, Query

from api.dependencies import get_engine
from api.schemas import BotDecisionOut, BotDecisionsOut
from api.security import require_api_key
from core.engine import TradingEngine
from utils.database import get_database

router = APIRouter(prefix="/api/bot", tags=["Moteur"], dependencies=[Depends(require_api_key)])

MAX_DECISIONS = 200
ACCEPTED_STATUSES = ("traded",)
REJECTED_STATUSES = ("hold", "news_blocked", "cooldown", "rejected", "failed", "error")
STAGES = {
    "traded": "exécution", "hold": "signal", "news_blocked": "calendrier", "cooldown": "cooldown",
    "rejected": "risque", "failed": "broker", "error": "erreur",
}
ACTIONS = {"BUY": "ACHAT", "SELL": "VENTE"}


def _item(row: dict[str, Any]) -> BotDecisionOut:
    direction = row.get("direction") or row.get("action")
    return BotDecisionOut(
        id=row["id"],
        timestamp=row["decided_at"],
        cycle=row.get("cycle"),
        symbol=row["symbol"],
        action=ACTIONS.get(direction or ""),
        status="ACCEPTÉ" if row["status"] in ACCEPTED_STATUSES else "REJETÉ",
        stage=STAGES.get(row["status"], "erreur"),
        reason=row.get("verdict") or row.get("detail") or row["status"],
        detail=row.get("detail") or "",
        confidence=row.get("confidence"),
        technical_score=row.get("technical_score"),
        sentiment_score=row.get("sentiment_score"),
        hybrid_score=row.get("hybrid_score"),
        volume=row.get("volume"),
        ticket=row.get("ticket"),
        dry_run=row.get("dry_run"),
    )


@router.get("/decisions", response_model=BotDecisionsOut, summary="Journal des décisions et rejets")
def bot_decisions(
    limit: int = Query(50, ge=1, le=MAX_DECISIONS, description="Nombre de décisions (les plus récentes)"),
    status: Literal["accepted", "rejected"] | None = Query(None, description="accepted = ordres validés, "
                                                                               "rejected = signaux refusés"),
    symbol: str | None = Query(None, max_length=32),
    engine: TradingEngine = Depends(get_engine),
) -> BotDecisionsOut:
    """
    Dernières décisions du moteur, de la plus récente à la plus ancienne : sens envisagé,
    ACCEPTÉ / REJETÉ, étape (signal, calendrier, cooldown, risque, broker) et motif précis
    (ex : « Score hybride +0.42 sous le seuil de ±0.60 », « Risque : spread 40 pts > maximum 30 pts »).
    """
    statuses = {"accepted": ACCEPTED_STATUSES, "rejected": REJECTED_STATUSES}.get(status or "")
    symbol = symbol.strip().upper() if symbol else None
    db = get_database()
    if db.available:
        rows, _ = db.list_decisions(limit=limit, symbol=symbol, status=statuses)
        source = "sqlite"
    else:
        rows = [r for r in reversed(engine.recent_decisions)
                if (not statuses or r["status"] in statuses) and (not symbol or r["symbol"] == symbol)][:limit]
        source = "memory"
    items = [_item(r) for r in rows]
    accepted = sum(i.status == "ACCEPTÉ" for i in items)
    return BotDecisionsOut(source=source, accepted=accepted, rejected=len(items) - accepted, items=items)
