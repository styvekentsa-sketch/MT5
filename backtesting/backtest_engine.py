"""
Backtest de la stratégie du bot sur données historiques, bougie par bougie.

Rejoue exactement la chaîne de décision du moteur en direct :
  1. calendrier économique (annonce à fort impact -> pas de nouveau trade)
  2. signal technique  : strategies.technical_analysis.signal_from_indicators
  3. score hybride     : strategies.strategy_manager.decide (poids, seuil, confirmation technique)
  4. SL / TP           : strategies.strategy_manager.compute_sl_tp
  5. taille et risque  : risk.risk_manager.calculate_position_size / validate_trade_risk,
                         exécutés contre un broker simulé (compte, positions, pertes du jour)

Pas de biais d'anticipation : la décision prise à la clôture de la bougie i n'utilise
que les bougies <= i (les indicateurs sont causaux) ; l'entrée a lieu à l'ouverture
de la bougie i + 1, dont les plus haut/bas servent ensuite aux SL/TP.

Limites assumées : un symbole par backtest ; pas d'historique d'actualités ni de
calendrier par défaut (sentiment neutre, aucun blocage) — voir `calendar_blocker`
et `database_sentiment` pour rejouer des données historisées.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable

import pandas as pd

from backtesting.broker import SimulatedBroker, Trade
from backtesting.metrics import BacktestMetrics, compute_metrics
from brokers.mt5_client import SymbolSpecs
from config.settings import settings
from risk.risk_manager import calculate_position_size, validate_trade_risk
from sentiment.news_scraper import NEWS_BLOCK_MINUTES_AFTER, NEWS_BLOCK_MINUTES_BEFORE, EconomicEvent, symbol_currencies
from sentiment.sentiment_analyzer import analyze_news_items
from strategies.strategy_manager import compute_sl_tp, decide, pip_size
from strategies.technical_analysis import MIN_BARS, TechnicalSignal, compute_indicators, signal_from_indicators
from utils.logger import get_logger

log = get_logger(__name__)

SignalFn = Callable[[pd.DataFrame, int], TechnicalSignal]
SentimentFn = Callable[[str, datetime], float]
NewsBlockerFn = Callable[[str, datetime], bool]

DEFAULT_SPREAD_POINTS = 10
QUIET_LOGGERS = ("risk.risk_manager", "strategies.strategy_manager", "strategies.technical_analysis")

# Règle de risque responsable d'un refus, d'après le motif renvoyé par validate_trade_risk
RISK_RULES = (
    ("perte journalière", "perte_journaliere"),
    ("pertes consécutives", "pertes_consecutives"),
    ("position", "exposition"),
    ("spread", "spread"),
    ("perte au SL", "risque_par_trade"),
    ("marge", "marge"),
    ("rendement/risque", "ratio_rendement_risque"),
    ("aberrant", "sl_aberrant"),
    ("volume", "volume"),
)


def risk_rule(failure: str) -> str:
    """Catégorie stable d'un motif de refus (ex : « 3 pertes consécutives ... » -> pertes_consecutives)."""
    return next((rule for fragment, rule in RISK_RULES if fragment in failure), "sl_tp" if "SL" in failure or "TP" in failure else "autre")


@dataclass
class BacktestConfig:
    symbol: str
    initial_balance: float = 10_000.0
    currency: str = "USD"
    leverage: int = 100
    commission_per_lot: float = 0.0
    default_spread_points: int = DEFAULT_SPREAD_POINTS   # si les données n'ont pas de colonne « spread »


@dataclass
class BacktestResult:
    config: BacktestConfig
    metrics: BacktestMetrics
    trades: list[Trade]
    equity_curve: pd.Series
    stats: Counter = field(default_factory=Counter)    # décisions : signal, news_blocked, rejected:<motif>...
    start: datetime | None = None
    end: datetime | None = None


# =============================================================================
#  Fournisseurs optionnels (données fondamentales historiques)
# =============================================================================

def calendar_blocker(events: list[EconomicEvent], before: int = NEWS_BLOCK_MINUTES_BEFORE,
                     after: int = NEWS_BLOCK_MINUTES_AFTER) -> NewsBlockerFn:
    """Même règle que le moteur : annonce « High » sur une devise du symbole dans [-after, +before] minutes."""
    high_impact = [e for e in events if e.impact == "High"]

    def blocked(symbol: str, now: datetime) -> bool:
        currencies = symbol_currencies(symbol)
        start, end = now - timedelta(minutes=after), now + timedelta(minutes=before)
        return any(e.currency in currencies and start <= e.time <= end for e in high_impact)

    return blocked


def database_sentiment(articles: list[dict], lookback_hours: int | None = None) -> SentimentFn:
    """
    Sentiment rejoué depuis les articles historisés (table market_sentiment) : à chaque
    bougie, seuls les articles publiés avant elle et dans la fenêtre `lookback_hours` comptent.
    """
    window = timedelta(hours=lookback_hours or settings.news.lookback_hours)
    dated = sorted(
        ({"title": a["title"], "snippet": a.get("snippet", ""), "published": pd.Timestamp(a["published_at"]).to_pydatetime()}
         for a in articles if a.get("published_at")),
        key=lambda a: a["published"],
    )

    def sentiment(symbol: str, now: datetime) -> float:
        recent = [a for a in dated if now - window <= a["published"] <= now]
        return analyze_news_items(recent, symbol=symbol, now=now) if recent else 0.0

    return sentiment


# =============================================================================
#  Moteur de backtest
# =============================================================================

class Backtester:
    """
    Args:
        candles: bougies clôturées (index UTC ; colonnes open, high, low, close[, spread]).
        specs: spécifications du symbole (volume min/max/pas, tick, stops level...).
        config: capital, devise, levier, commission, spread par défaut.
        sentiment_fn: sentiment(symbole, instant) dans [-1, 1] ; défaut : neutre.
        news_blocker: blocage calendrier(symbole, instant) ; défaut : aucun.
        signal_fn: signal technique(indicateurs, position) ; défaut : celui du bot.
    """

    def __init__(self, candles: pd.DataFrame, specs: SymbolSpecs, config: BacktestConfig,
                 sentiment_fn: SentimentFn | None = None, news_blocker: NewsBlockerFn | None = None,
                 signal_fn: SignalFn | None = None) -> None:
        self.candles = candles
        self.specs = specs
        self.config = config
        self.sentiment_fn = sentiment_fn or (lambda symbol, now: 0.0)
        self.news_blocker = news_blocker or (lambda symbol, now: False)
        self.signal_fn = signal_fn or signal_from_indicators
        self.has_news = sentiment_fn is not None

    def _spread(self, row: pd.Series) -> int:
        value = row.get("spread")
        return int(value) if value is not None and pd.notna(value) and value > 0 else self.config.default_spread_points

    def run(self, quiet: bool = True) -> BacktestResult:
        """Exécute le backtest ; `quiet` réduit les logs des règles de risque (une ligne par signal sinon)."""
        levels = {name: logging.getLogger(name).level for name in QUIET_LOGGERS}
        if quiet:
            for name in QUIET_LOGGERS:
                logging.getLogger(name).setLevel(logging.WARNING)
        try:
            return self._run()
        finally:
            for name, level in levels.items():
                logging.getLogger(name).setLevel(level)

    def _run(self) -> BacktestResult:
        symbol, candles = self.config.symbol, self.candles
        if len(candles) <= MIN_BARS + 1:
            raise ValueError(f"au moins {MIN_BARS + 2} bougies nécessaires ({len(candles)} fournies)")

        broker = SimulatedBroker(self.specs, self.config.initial_balance, self.config.currency,
                                 self.config.leverage, self.config.commission_per_lot)
        indicators = compute_indicators(candles)
        stats: Counter = Counter()
        equity: dict[datetime, float] = {}

        for i in range(MIN_BARS, len(candles) - 1):
            bar = candles.iloc[i + 1]                         # bougie suivante : entrée à son ouverture
            now = candles.index[i + 1].to_pydatetime()
            spread = self._spread(bar)
            broker.set_market(now, float(bar["open"]), spread)

            self._evaluate(symbol, indicators, i, now, broker, stats)

            broker.process_bar(now, float(bar["open"]), float(bar["high"]), float(bar["low"]), spread)
            equity[now] = round(broker.equity(bid=float(bar["close"])), 2)

        last_time = candles.index[-1].to_pydatetime()
        broker.close_all(last_time, float(candles.iloc[-1]["close"]))
        equity[last_time] = round(broker.balance, 2)

        curve = pd.Series(equity, name="equity")
        metrics = compute_metrics(broker.trades, curve, self.config.initial_balance)
        log.info("Backtest %s : %d trades, rendement %+.2f%%, drawdown max %.2f%%",
                 symbol, metrics.trades, metrics.total_return_pct, metrics.max_drawdown_pct)
        return BacktestResult(self.config, metrics, broker.trades, curve, stats,
                              candles.index[MIN_BARS].to_pydatetime(), last_time)

    def _evaluate(self, symbol: str, indicators: pd.DataFrame, i: int, now: datetime,
                  broker: SimulatedBroker, stats: Counter) -> None:
        """Décision à la clôture de la bougie i, exécutée à l'ouverture de la bougie i + 1."""
        tech = self.signal_fn(indicators, i)
        if not tech.is_valid:
            stats["invalid_signal"] += 1
            return
        news_blocked = self.news_blocker(symbol, now)
        decision = decide(tech, self.sentiment_fn(symbol, now), news_blocked, has_news=self.has_news)
        if decision.action not in ("BUY", "SELL"):
            stats["news_blocked" if news_blocked else "hold"] += 1
            return

        stats["signal"] += 1
        entry = broker.ask if decision.action == "BUY" else broker.bid
        sl, tp = compute_sl_tp(decision.action, entry, tech, broker.ask - broker.bid, pip_size(self.specs))
        if sl is None:
            stats["rejected:sl_impossible"] += 1
            return
        volume = calculate_position_size(symbol, entry, sl, broker=broker)
        if volume <= 0:
            stats["rejected:taille_nulle"] += 1
            return
        check = validate_trade_risk(symbol, decision.action, volume, sl, tp, broker=broker)
        if not check["is_approved"]:
            for failure in check["failed_checks"]:
                stats[f"rejected:{risk_rule(failure)}"] += 1
            return
        broker.open_position(decision.action, volume, sl, tp)
        stats["opened"] += 1
