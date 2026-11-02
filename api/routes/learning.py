"""
Boucle d'apprentissage : journal des trades ouverts, analyses post-mortem des pertes et
mémoire des patterns perdants (fichiers JSON de learning/trade_memory.py).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query

from api.dependencies import get_engine
from api.security import require_api_key
from core.engine import TradingEngine
from learning.trade_memory import TradeMemory

router = APIRouter(prefix="/api/learning", tags=["Apprentissage"], dependencies=[Depends(require_api_key)])


@router.get("", summary="Journal des trades, pertes analysées et mémoire des erreurs")
def learning_report(
    limit: int = Query(20, ge=1, le=200, description="Nombre d'éléments par liste"),
    engine: TradingEngine = Depends(get_engine),
) -> dict[str, Any]:
    """
    Compteurs (trades journalisés, ouverts, clos, gagnants / perdants, patterns bloquants),
    règles de blocage, derniers trades avec leur instantané d'entrée, dernières analyses
    post-mortem (durée, excursions, facteur d'échec) et patterns appris (les bloquants d'abord).
    """
    memory = engine.memory if engine.memory is not None else TradeMemory()
    return {"enabled": engine.memory is not None, **memory.report(limit)}
