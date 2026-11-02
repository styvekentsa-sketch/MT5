"""
Mode analyste dans le dashboard (LECTURE SEULE, aucun ordre) :

  - GET /api/analysis/risk         état des limites journalières (perte max, pertes consécutives)
  - GET /api/analysis/setups       propositions des stratégies à setups, arguments pour / contre
  - GET /api/analysis/indicators   ATR, ADX, pivots confirmés, supports / résistances, régime
  - GET /api/analysis/performance  statistiques par stratégie : trades réels clos et backtests
"""

from __future__ import annotations

import json
import math
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from api.dependencies import get_active_broker
from api.security import require_api_key
from backtesting.setup_engine import REPORTS_DIR, compute_stats
from brokers.base import TIMEFRAME_NAMES as TIMEFRAMES
from brokers.base import BaseBroker
from config.settings import settings
from core.regime_detector import adx, atr, confirmed_pivots, detect_regime, previous_session_pivots
from learning.trade_memory import TradeMemory
from models.schemas import SetupStatus, TradingSetup
from risk.risk_manager import consecutive_losses, daily_loss_pct
from strategies.base import closed_candles
from utils.logger import get_logger

log = get_logger(__name__)

router = APIRouter(prefix="/api/analysis", tags=["Analyse"], dependencies=[Depends(require_api_key)])

SETUPS_CACHE_SECONDS = 15            # l'analyse complète (2 stratégies x symboles) n'est pas refaite à chaque appel
NEAR_MIN_RATIO = 0.5                 # ratio net à moins de 0.5 du minimum : argument défavorable
MAX_REPORTS = 20

_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_cache_lock = threading.Lock()


def _symbol(symbol: str) -> str:
    symbol = symbol.strip().upper()
    if symbol not in settings.trading.symbols:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            f"Symbole non suivi : {symbol} (disponibles : {', '.join(settings.trading.symbols)})")
    return symbol


def _number(value: Any, digits: int = 6) -> float | None:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return round(value, digits) if math.isfinite(value) else None


# ----------------------------------------------------------------- limites de risque

@router.get("/risk", summary="Limites de risque journalières")
def risk_limits(broker: BaseBroker = Depends(get_active_broker)) -> dict[str, Any]:
    """Perte du jour (réalisée + latente) et série de pertes du bot, comparées aux limites."""
    account = broker.get_account_info()
    if account is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"Compte {broker.description} indisponible")
    start_of_day = broker.now().replace(hour=0, minute=0, second=0, microsecond=0)
    deals = broker.get_closed_deals(start_of_day)
    loss = daily_loss_pct(account, deals)
    streak = consecutive_losses(deals)
    max_loss, max_streak = settings.risk.max_daily_loss_pct, settings.risk.max_consecutive_losses
    return {
        "daily_loss_pct": _number(loss, 3),
        "max_daily_loss_pct": max_loss,
        "daily_loss_reached": loss is not None and loss >= max_loss,
        "consecutive_losses": streak,
        "max_consecutive_losses": max_streak,
        "consecutive_losses_reached": streak >= max_streak,
        "risk_per_trade_pct": settings.risk.risk_per_trade_pct,
        "max_total_risk_pct": settings.risk.max_total_risk_pct,
        "max_open_positions": settings.risk.max_open_positions,
        "deals_today": len(deals),
    }


# ----------------------------------------------------------------- setups

def arguments(setup: TradingSetup) -> tuple[list[str], list[str]]:
    """Arguments favorables (contexte, déclencheur, ratio) et défavorables (notes, refus, limites)."""
    pros = [part.strip() for part in setup.reason.split(";") if part.strip()]
    cons = list(setup.notes)
    ratio = setup.net_reward_risk
    minimum = settings.risk.min_net_reward_risk
    if ratio is not None:
        if ratio >= minimum + NEAR_MIN_RATIO:
            pros.append(f"ratio net {ratio:.2f} (minimum {minimum:g})")
        elif ratio >= minimum:
            cons.append(f"ratio net {ratio:.2f} proche du minimum {minimum:g}")
    adx_value = setup.indicators.get("adx")
    if isinstance(adx_value, (int, float)) and adx_value > 0:
        trending = setup.regime.value.startswith("tendance")
        if trending and adx_value < 25:
            cons.append(f"ADX {adx_value:.0f} : tendance encore faible")
        elif not trending and adx_value > 25:
            cons.append(f"ADX {adx_value:.0f} : le range pourrait céder")
    return pros, cons


def _setup_out(setup: TradingSetup) -> dict[str, Any]:
    pros, cons = arguments(setup)
    data = setup.model_dump(mode="json")
    return {**data, "pros": pros, "cons": cons}


def _strategies() -> list:
    from strategies.registry import setup_strategies

    return setup_strategies()


@router.get("/setups", summary="Propositions des stratégies à setups (mode analyste)")
def current_setups(request: Request, refresh: bool = Query(False, description="Ignorer le cache"),
                   broker: BaseBroker = Depends(get_active_broker)) -> dict[str, Any]:
    """
    Analyse chaque symbole suivi avec les stratégies à setups (même séquence qu'en direct :
    détection, fenêtre d'annonces, taille, risque, vérificateur indépendant). Aucun ordre.
    Les setups prêts déclenchent une alerte (une seule par setup, cf. NOTIFY_COOLDOWN_SECONDS).
    """
    key = broker.description
    with _cache_lock:
        cached = _cache.get(key)
        if cached and not refresh and time.monotonic() - cached[0] < SETUPS_CACHE_SECONDS:
            return cached[1]
        now = datetime.now(timezone.utc)
        rows, errors = [], []
        strategies = _strategies()
        for symbol in settings.trading.symbols:
            for strategy in strategies:
                try:
                    setup = strategy.analyze(broker, symbol, now)
                except Exception as exc:  # noqa: BLE001 - un symbole en erreur n'empêche pas les autres
                    log.warning("[%s] Analyse %s impossible : %s", symbol, strategy.name, exc)
                    errors.append(f"{symbol} · {strategy.name} : {type(exc).__name__}")
                    continue
                if setup is not None:
                    rows.append(_setup_out(setup))
        order = {SetupStatus.READY.value: 0, SetupStatus.WATCHING.value: 1}
        rows.sort(key=lambda s: (order.get(s["status"], 2), s["symbol"], s["strategy"]))
        result = {"generated_at": now.isoformat(timespec="seconds"), "broker": broker.description,
                  "min_net_reward_risk": settings.risk.min_net_reward_risk, "setups": rows, "errors": errors}
        _cache[key] = (time.monotonic(), result)
        return result


# ----------------------------------------------------------------- indicateurs

def _series(values) -> list[float | None]:
    return [_number(v, 8) for v in np.asarray(values, dtype=float)]


@router.get("/indicators", summary="ATR, ADX, pivots confirmés, supports / résistances et régime")
def indicators(
    symbol: str = Query(...),
    timeframe: str = Query("M15"),
    count: int = Query(300, ge=120, le=1000),
    broker: BaseBroker = Depends(get_active_broker),
) -> dict[str, Any]:
    """Calculés sur les bougies CLÔTURÉES uniquement (comme les stratégies) ; temps en secondes UTC."""
    symbol, timeframe = _symbol(symbol), timeframe.strip().upper()
    if timeframe not in TIMEFRAMES:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"Timeframe invalide : {timeframe}")
    df = closed_candles(broker.get_historical_data(symbol, count=count + 1, timeframe=timeframe))
    if df.empty:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"Aucune bougie disponible pour {symbol}")
    daily = closed_candles(broker.get_historical_data(symbol, count=3, timeframe="D1"))
    reading = detect_regime(df, daily)
    directional = adx(df)
    session = previous_session_pivots(daily)
    times = [int(ts.timestamp()) for ts in df.index]
    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "times": times,
        "atr": _series(atr(df)),
        "adx": _series(directional["adx"]),
        "plus_di": _series(directional["plus_di"]),
        "minus_di": _series(directional["minus_di"]),
        "pivots": [{"kind": p.kind, "time": int(p.time.timestamp()), "price": p.price,
                    "confirmed_at": int(p.confirmed_time.timestamp())} for p in confirmed_pivots(df)],
        "levels": [{"name": name, "price": round(price, 8)} for name, price in session.levels().items()]
        if session else [],
        "range_bounds": list(reading.range_bounds) if reading.range_bounds else None,
        "regime": reading.regime.value,
        "structure": reading.structure.value,
        "reasons": list(reading.reasons),
    }


# ----------------------------------------------------------------- performances

def _live_trade(trade: dict[str, Any]) -> SimpleNamespace:
    """Trade clos du journal, au format attendu par compute_stats (R mesuré sur la distance du stop)."""
    entry, sl, close = trade.get("entry_price"), trade.get("sl"), trade.get("close_price")
    r = 0.0
    if entry and sl and close is not None and abs(entry - sl) > 0:
        direction = 1 if trade.get("side") == "BUY" else -1
        r = direction * (close - entry) / abs(entry - sl)
    strategy = trade.get("strategy") or f"adaptive · {trade.get('mode') or '—'}"
    return SimpleNamespace(profit=float(trade.get("net_profit") or 0.0), r_multiple=r, ambiguous=False,
                           strategy=strategy, symbol=trade.get("symbol"))


def _group_stats(trades: list) -> dict[str, Any]:
    groups: dict[str, list] = {}
    for trade in trades:
        groups.setdefault(trade.strategy, []).append(trade)
    return {name: compute_stats(group).to_dict() for name, group in sorted(groups.items())}


def load_reports(directory: Path | None = None, limit: int = MAX_REPORTS) -> list[dict[str, Any]]:
    """Rapports JSON de backtest les plus récents (backtesting/setup_engine.py)."""
    directory = directory or REPORTS_DIR
    if not directory.is_dir():
        return []
    reports = []
    for path in sorted(directory.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]:
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.warning("Rapport de backtest illisible %s : %s", path.name, exc)
            continue
        reports.append({"file": path.name, **report})
    return reports


@router.get("/performance", summary="Espérance, réussite et drawdown par stratégie (réel et backtests)")
def performance(request: Request) -> dict[str, Any]:
    engine = getattr(request.app.state, "engine", None)
    memory = engine.memory if engine is not None and engine.memory is not None else TradeMemory()
    closed = [t for t in memory.trades.load() if t.get("status") == "closed"]
    trades = [_live_trade(t) for t in closed]
    return {
        "live": {"overall": compute_stats(trades).to_dict(), "by_strategy": _group_stats(trades)},
        "backtests": load_reports(),
    }
