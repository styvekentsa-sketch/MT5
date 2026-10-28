"""
Données de marché pour le graphique du dashboard (LECTURE SEULE).

Expose les bougies MT5 des symboles configurés, via la même fonction que la stratégie
(`get_historical_data`). Aucun ordre, aucune règle de risque : rien n'est modifié.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status

from api.schemas import CandleOut, CandlesOut
from api.security import require_api_key
from brokers.mt5_client import TIMEFRAMES, get_current_tick, get_historical_data, get_symbol_specs
from config.settings import settings
from utils.logger import get_logger

log = get_logger(__name__)

router = APIRouter(prefix="/api/market", tags=["Marché"], dependencies=[Depends(require_api_key)])

MIN_CANDLES = 1
MAX_CANDLES = 1000


@router.get("/candles", response_model=CandlesOut, summary="Bougies d'un symbole suivi")
def get_candles(
    symbol: str = Query(..., description="Symbole configuré dans TRADING_SYMBOLS"),
    timeframe: str = Query("M15", description=f"Une des valeurs : {', '.join(TIMEFRAMES)}"),
    count: int = Query(300, ge=MIN_CANDLES, le=MAX_CANDLES, description="Nombre de bougies (la dernière est en cours)"),
) -> CandlesOut:
    """
    Dernières bougies OHLC (prix Bid), la plus récente étant en cours de formation.
    Pour un rafraîchissement en direct, rappeler avec un petit `count` (2 ou 3).
    """
    symbol = symbol.strip().upper()
    timeframe = timeframe.strip().upper()
    if symbol not in settings.trading.symbols:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            f"Symbole non suivi : {symbol} (disponibles : {', '.join(settings.trading.symbols)})")
    if timeframe not in TIMEFRAMES:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"Timeframe invalide : {timeframe}")

    try:
        df = get_historical_data(symbol, count=count, timeframe=timeframe)
    except ValueError as exc:                          # symbole inconnu chez le broker
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    if df.empty:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"Aucune bougie disponible pour {symbol}")

    specs = get_symbol_specs(symbol)
    tick = get_current_tick(symbol)
    return CandlesOut(
        symbol=symbol,
        timeframe=timeframe,
        digits=specs.digits if specs else 5,
        bid=tick.bid if tick else None,
        ask=tick.ask if tick else None,
        candles=[
            CandleOut(time=int(ts.timestamp()), open=row.open, high=row.high, low=row.low, close=row.close,
                      volume=float(row.volume))
            for ts, row in zip(df.index, df.itertuples(index=False))
        ],
    )
