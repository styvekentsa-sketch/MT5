"""
Historique persistant (SQLite) : webhooks reçus, décisions du moteur, articles analysés.

Toutes les routes sont paginées (`limit`, `offset`) et renvoient le `total` correspondant
aux filtres, pour une pagination côté dashboard. Une base indisponible renvoie 503.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status

from api.schemas import ArticleHistoryOut, DecisionHistoryOut, WebhookHistoryOut
from api.security import require_api_key
from utils.database import MAX_PAGE_SIZE, Database, get_database

router = APIRouter(prefix="/api/history", tags=["Historique"], dependencies=[Depends(require_api_key)])

DecisionStatus = Literal["traded", "hold", "news_blocked", "cooldown", "rejected", "failed", "error"]
# Décisions considérées comme des « trades » (exécutés ou tentés)
TRADE_STATUSES = ("traded", "rejected", "failed")

Limit = Query(50, ge=1, le=MAX_PAGE_SIZE, description="Nombre d'éléments par page")
Offset = Query(0, ge=0, description="Nombre d'éléments à sauter")


def _database() -> Database:
    db = get_database()
    if not db.available:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Historique indisponible (base SQLite inaccessible)")
    return db


@router.get("/webhooks", response_model=WebhookHistoryOut, summary="Historique des webhooks")
def webhook_history(
    limit: int = Limit,
    offset: int = Offset,
    source: str | None = Query(None, max_length=32, description="Filtrer par source (ex : tradingview)"),
    accepted: bool | None = Query(None, description="true = acceptés, false = refusés"),
) -> WebhookHistoryOut:
    """Webhooks reçus, du plus récent au plus ancien (payload JSON, secrets masqués)."""
    items, total = _database().list_webhooks(limit=limit, offset=offset, source=source, accepted=accepted)
    return WebhookHistoryOut(total=total, limit=limit, offset=offset, items=items)


@router.get("/trades", response_model=DecisionHistoryOut, summary="Journal des décisions du moteur")
def trade_history(
    limit: int = Limit,
    offset: int = Offset,
    symbol: str | None = Query(None, max_length=32),
    status_filter: list[DecisionStatus] | None = Query(None, alias="status",
                                                       description="Un ou plusieurs statuts (répéter le paramètre)"),
    trades_only: bool = Query(False, description="Seulement les trades exécutés, refusés ou en échec"),
    since: datetime | None = Query(None, description="Décisions depuis cette date (ISO 8601)"),
) -> DecisionHistoryOut:
    """Décisions prises par le moteur pour chaque symbole, avec scores, niveaux et volume."""
    statuses = list(status_filter or []) or (list(TRADE_STATUSES) if trades_only else None)
    items, total = _database().list_decisions(limit=limit, offset=offset, symbol=symbol, status=statuses, since=since)
    return DecisionHistoryOut(total=total, limit=limit, offset=offset, items=items)


@router.get("/sentiment", response_model=ArticleHistoryOut, summary="Historique des articles analysés")
def sentiment_history(
    limit: int = Limit,
    offset: int = Offset,
    symbol: str | None = Query(None, max_length=32, description="Articles concernant ce symbole"),
    label: Literal["bullish", "bearish", "neutral"] | None = Query(None),
) -> ArticleHistoryOut:
    """Articles scrapés et leur analyse (score, mots-clés, symboles concernés)."""
    items, total = _database().list_articles(limit=limit, offset=offset, symbol=symbol, label=label)
    return ArticleHistoryOut(total=total, limit=limit, offset=offset, items=items)
