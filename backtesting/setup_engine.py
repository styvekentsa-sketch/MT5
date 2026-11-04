"""
Backtest des stratégies à setups (pullback, rejet de range, breakout momentum...) avec EXACTEMENT la logique du
direct : `BaseStrategy.evaluate` (détection, régime causal, fenêtre d'annonces, taille, règles de
risque, vérificateur indépendant) appelée bougie par bougie sur un broker simulé.

Règles anti-anticipation :
  - à l'instant T (clôture d'une bougie du timeframe d'exécution, le plus fin des stratégies), chaque
    stratégie ne reçoit que les bougies CLÔTURÉES à T sur chaque timeframe (une bougie H1 n'est
    visible qu'une fois sa dernière minute écoulée), limitées au même nombre qu'en direct ;
    les pivots, régimes et ATR sont donc calculés comme en direct, sans bougie future
  - le prix utilisé pour décider et exécuter est le premier prix APRÈS T : ouverture de la bougie
    suivante (Bid) + spread (Ask) ; l'ordre est exécuté sur cette bougie, jamais sur une bougie
    antérieure au signal
  - calendrier : seules l'heure et l'importance des annonces (connues à l'avance) sont utilisées ;
    les résultats publiés (révisables) sont retirés avant la simulation

Coûts : spread (colonne « spread » des bougies, sinon fixe), glissement défavorable sur l'entrée et
les stops, commission par lot. SL et TP dans la même bougie : SL retenu (pire cas), trade marqué
`ambiguous`. Setup expiré au moment de l'exécution : ignoré et compté.

Statistiques : espérance nette (devise et R), taux de réussite, gains / pertes moyens, profit factor,
drawdown maximal, plus longue série de pertes ; globales et par stratégie, actif, horizon, régime, sens.

Usage :
    python -m backtesting.setup_engine --symbol EURUSD --mt5 --bars 8000

Chaque rapport est enregistré en JSON dans `reports/backtests/` (lu par le dashboard, onglet Analyse).
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd

from backtesting.broker import SimulatedBroker, Trade
from backtesting.metrics import max_drawdown
from brokers.models import SymbolSpecs
from core.news_pipeline import CalendarEvent, NewsPipeline, SimulatedCalendarProvider
from models.schemas import SetupStatus
from risk.trailing import TrailingStop, next_stop
from strategies.base import TIMEFRAME_SECONDS, BaseStrategy, Quote
from utils.logger import get_logger
from utils.server_time import mt5_server_timezone, utc_to_bar_time

log = get_logger(__name__)

DEFAULT_SPREAD_POINTS = 10
REPORTS_DIR = Path("reports/backtests")
MAX_LOADED_BARS = 100_000                    # M1 sur une longue période : plafond de chargement
EQUITY_POINTS = 400                           # courbe d'équité du rapport, sous-échantillonnée


@dataclass(frozen=True)
class CostModel:
    spread_points: int | None = None          # None : colonne « spread » des bougies, sinon DEFAULT_SPREAD_POINTS
    slippage_points: int = 1                  # glissement défavorable (entrée au marché, stops)
    commission_per_lot: float = 0.0
    swap_per_lot_night: float = 0.0           # financement payé par lot et par nuit (triple le mercredi)


def financing_nights(entry: datetime, exit_: datetime) -> int:
    """Nuits de financement : passages de minuit (heure des bougies), le mercredi compte triple (week-end)."""
    nights, day = 0, entry.date()
    while day < exit_.date():
        nights += 3 if day.weekday() == 2 else 1
        day += timedelta(days=1)
    return nights


@dataclass(frozen=True)
class SetupTrade:
    strategy: str
    symbol: str
    side: str
    style: str
    regime: str
    setup_id: str
    signal_time: datetime
    entry_time: datetime
    exit_time: datetime
    entry_price: float
    exit_price: float
    stop_loss: float
    take_profit: float | None
    volume: float
    exit_reason: str
    ambiguous: bool
    profit: float                             # net : spread, glissement et commission inclus
    planned_risk: float                       # perte prévue au SL (devise du compte) au moment du signal
    planned_net_ratio: float | None

    @property
    def r_multiple(self) -> float:
        return self.profit / self.planned_risk if self.planned_risk > 0 else 0.0


@dataclass(frozen=True)
class Stats:
    trades: int
    wins: int
    losses: int
    win_rate_pct: float
    net_profit: float
    expectancy: float                         # gain net moyen par trade (devise)
    expectancy_r: float                       # gain net moyen en multiples du risque prévu
    avg_win: float
    avg_loss: float                           # valeur positive
    profit_factor: float | None
    max_drawdown: float                       # sur la suite des trades (devise)
    max_losing_streak: int
    ambiguous: int

    def to_dict(self) -> dict[str, Any]:
        values = asdict(self)
        if values["profit_factor"] is not None and math.isinf(values["profit_factor"]):
            values["profit_factor"] = None                     # aucune perte : JSON strict, pas d'infini
        return values


def compute_stats(trades: list[SetupTrade]) -> Stats:
    profits = [t.profit for t in trades]
    wins = [p for p in profits if p > 0]
    losses = [-p for p in profits if p <= 0]
    streak = longest = 0
    for p in profits:
        streak = streak + 1 if p <= 0 else 0
        longest = max(longest, streak)
    cumulative = np.cumsum([0.0, *profits])
    drawdown, _ = max_drawdown(cumulative + 1.0e12)            # chemin des trades (décalage : % sans objet)
    gross_loss = sum(losses)
    return Stats(
        trades=len(trades), wins=len(wins), losses=len(losses),
        win_rate_pct=round(len(wins) / len(trades) * 100, 2) if trades else 0.0,
        net_profit=round(sum(profits), 2),
        expectancy=round(float(np.mean(profits)), 2) if trades else 0.0,
        expectancy_r=round(float(np.mean([t.r_multiple for t in trades])), 3) if trades else 0.0,
        avg_win=round(float(np.mean(wins)), 2) if wins else 0.0,
        avg_loss=round(float(np.mean(losses)), 2) if losses else 0.0,
        profit_factor=round(sum(wins) / gross_loss, 3) if gross_loss > 0 else (None if not wins else float("inf")),
        max_drawdown=round(drawdown, 2), max_losing_streak=longest,
        ambiguous=sum(t.ambiguous for t in trades),
    )


SEGMENTS = {"stratégie": "strategy", "actif": "symbol", "horizon": "style", "régime": "regime", "sens": "side"}


def segment(trades: list[SetupTrade]) -> dict[str, dict[str, Stats]]:
    """Statistiques par stratégie, actif, horizon, régime et sens."""
    out: dict[str, dict[str, Stats]] = {}
    for label, attribute in SEGMENTS.items():
        groups: dict[str, list[SetupTrade]] = defaultdict(list)
        for trade in trades:
            groups[str(getattr(trade, attribute))].append(trade)
        out[label] = {key: compute_stats(group) for key, group in sorted(groups.items())}
    return out


@dataclass
class SetupBacktestResult:
    trades: list[SetupTrade]
    equity: pd.Series
    initial_balance: float
    decisions: Counter = field(default_factory=Counter)        # statuts des setups et motifs d'abandon
    stats: Stats | None = None
    segments: dict[str, dict[str, Stats]] = field(default_factory=dict)
    equity_drawdown: float = 0.0
    equity_drawdown_pct: float = 0.0

    def __post_init__(self) -> None:
        self.stats = compute_stats(self.trades)
        self.segments = segment(self.trades)
        if len(self.equity):
            self.equity_drawdown, self.equity_drawdown_pct = (round(v, 2) for v in max_drawdown(self.equity.to_numpy()))

    def summary(self) -> str:
        s = self.stats
        lines = [
            f"Trades {s.trades} ({s.wins} gagnants / {s.losses} perdants), réussite {s.win_rate_pct:.1f} %",
            f"Résultat net {s.net_profit:+.2f} | espérance {s.expectancy:+.2f} par trade ({s.expectancy_r:+.2f} R)",
            f"Gain moyen {s.avg_win:.2f} | perte moyenne {s.avg_loss:.2f} | profit factor {s.profit_factor}",
            f"Drawdown max {self.equity_drawdown:.2f} ({self.equity_drawdown_pct:.2f} %) | plus longue série de pertes "
            f"{s.max_losing_streak} | SL/TP ambigus {s.ambiguous}",
            "Décisions : " + ", ".join(f"{k} {v}" for k, v in sorted(self.decisions.items())),
        ]
        for label, groups in self.segments.items():
            if len(groups) > 1 or label == "stratégie":
                lines.append(f"Par {label} : " + " | ".join(
                    f"{key} {st.trades} trades, {st.expectancy_r:+.2f} R, réussite {st.win_rate_pct:.0f} %"
                    for key, st in groups.items()))
        return "\n".join(lines)

    def walk_forward(self, folds: int = 4) -> dict[str, Any]:
        """
        Périodes successives de même durée : la première sert de référence (période où les réglages ont
        été choisis), les suivantes sont hors échantillon. Une stratégie robuste reste positive sur la
        plupart des périodes futures ; un résultat porté par une seule période est suspect.
        """
        if folds < 2 or len(self.equity) < 2:
            return {}
        start, end = self.equity.index[0], self.equity.index[-1]
        width = (end - start) / folds
        periods = []
        for k in range(folds):
            lo, hi = start + k * width, start + (k + 1) * width
            chosen = [t for t in self.trades if lo <= t.entry_time < hi or (k == folds - 1 and t.entry_time == hi)]
            stats = compute_stats(chosen)
            periods.append({"period": k + 1, "role": "référence" if k == 0 else "hors échantillon",
                            "start": lo.isoformat(), "end": hi.isoformat(), **stats.to_dict()})
        future = [p for p in periods[1:] if p["trades"]]
        positive = sum(p["net_profit"] > 0 for p in future)
        return {"folds": periods, "out_of_sample_positive": positive, "out_of_sample_traded": len(future),
                "consistency_pct": round(positive / len(future) * 100, 1) if future else None}

    def to_report(self, symbol: str, costs: CostModel, folds: int = 4) -> dict[str, Any]:
        """Rapport JSON : statistiques, segments, décisions et courbe d'équité (dashboard)."""
        step = max(1, len(self.equity) // EQUITY_POINTS)
        curve = self.equity.iloc[::step]
        return {
            "kind": "setups",
            "symbol": symbol,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "start": self.equity.index[0].isoformat() if len(self.equity) else None,
            "end": self.equity.index[-1].isoformat() if len(self.equity) else None,
            "initial_balance": self.initial_balance,
            "costs": asdict(costs),
            "stats": self.stats.to_dict(),
            "equity_drawdown": self.equity_drawdown,
            "equity_drawdown_pct": self.equity_drawdown_pct,
            "segments": {label: {key: st.to_dict() for key, st in groups.items()}
                         for label, groups in self.segments.items()},
            "decisions": dict(sorted(self.decisions.items())),
            "walk_forward": self.walk_forward(folds),
            "equity": [{"time": int(ts.timestamp()), "equity": round(float(v), 2)} for ts, v in curve.items()],
        }


def save_report(report: dict[str, Any], directory: Path = REPORTS_DIR) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    stamp = report["generated_at"].replace(":", "").replace("-", "")[:15]
    path = directory / f"setups_{report['symbol']}_{stamp}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


class TrailingState:
    """Breakeven / trailing stop d'une position simulée (risk/trailing.py), mis à jour après chaque bougie."""

    def __init__(self, config: TrailingStop, position, atr: float | None = None) -> None:
        self.config = config
        self.position = position
        self.atr = atr
        self.initial_stop = position.sl
        self.best = position.entry_price
        self.extremes: list[float] = []                # plus bas Bid (achat) / plus hauts Ask (vente)
        self.stage = "initial"

    def update(self, high: float, low: float, spread: float) -> None:
        """Bougie clôturée : meilleur prix de sortie (Bid pour un achat, Ask pour une vente), puis stop."""
        p = self.position
        if p.side == "BUY":
            self.best = max(self.best, high)
            self.extremes.append(low)
        else:
            self.best = min(self.best, low + spread)
            self.extremes.append(high + spread)
        self.extremes = self.extremes[-max(self.config.bars, 1):]
        update = next_stop(p.side, p.entry_price, self.initial_stop, p.sl, self.best, self.extremes, self.atr,
                           self.config)
        p.sl, self.stage = update.stop, update.stage


def visible(frame: pd.DataFrame, timeframe: str, at: datetime, limit: int) -> pd.DataFrame:
    """Bougies de `frame` CLÔTURÉES à l'instant `at` (ouverture + durée <= at), au plus `limit`."""
    duration = pd.Timedelta(seconds=TIMEFRAME_SECONDS[timeframe])
    closed = int((frame.index + duration).searchsorted(pd.Timestamp(at), side="right"))
    return frame.iloc[max(0, closed - limit):closed]


class SetupBacktester:
    """Rejoue des stratégies à setups sur un historique multi-timeframes, sans information future."""

    def __init__(self, strategies: Iterable[BaseStrategy], symbol: str, frames: dict[str, pd.DataFrame],
                 specs: SymbolSpecs, initial_balance: float = 10_000.0, costs: CostModel = CostModel(),
                 calendar: Iterable[CalendarEvent] = (), currency: str = "USD", leverage: int = 100,
                 bar_timezone=None) -> None:
        self.strategies = list(strategies)
        self.symbol = symbol
        self.frames = {tf: df.sort_index() for tf, df in frames.items()}
        self.specs = specs
        self.initial_balance = initial_balance
        self.costs = costs
        self.currency, self.leverage = currency, leverage
        needed = {tf for s in self.strategies for tf in s.timeframes}
        missing = needed - set(self.frames)
        if missing:
            raise ValueError(f"bougies manquantes pour les timeframes {sorted(missing)}")
        self.exec_tf = min(needed, key=lambda tf: TIMEFRAME_SECONDS[tf])
        # Bougies MT5 : heure du serveur (bar_timezone) ; stratégies et calendrier sur la même horloge
        for strategy in self.strategies:
            strategy.bar_timezone = bar_timezone
        # Calendrier : heures et importances seulement (les résultats publiés peuvent être révisés)
        events = [e.model_copy(update={"actual": None, "time": utc_to_bar_time(e.time, bar_timezone)})
                  for e in calendar]
        self.news = NewsPipeline([SimulatedCalendarProvider(events)], symbols=[symbol], cache_seconds=10**9)

    def _spread(self, bar: pd.Series) -> int:
        if self.costs.spread_points is not None:
            return self.costs.spread_points
        value = bar.get("spread") if hasattr(bar, "get") else None
        return int(value) if value is not None and not pd.isna(value) and value > 0 else DEFAULT_SPREAD_POINTS

    def run(self) -> SetupBacktestResult:
        exec_frame = self.frames[self.exec_tf]
        duration = timedelta(seconds=TIMEFRAME_SECONDS[self.exec_tf])
        broker = SimulatedBroker(self.specs, self.initial_balance, self.currency, self.leverage,
                                 self.costs.commission_per_lot, self.costs.slippage_points)
        decisions: Counter = Counter()
        meta: dict[int, dict[str, Any]] = {}
        taken: set[tuple] = set()
        trades: list[SetupTrade] = []
        equity: dict[datetime, float] = {}

        trailing: dict[int, TrailingState] = {}

        def record(closed: list[Trade]) -> None:
            for trade in closed:
                info = meta.pop(trade.ticket)
                state = trailing.pop(trade.ticket, None)
                moved = state is not None and state.stage != "initial" and trade.exit_reason == "sl"
                reason = state.stage if moved else trade.exit_reason                # breakeven / trailing
                financing = (self.costs.swap_per_lot_night * trade.volume
                             * financing_nights(trade.entry_time, trade.exit_time))
                if financing:
                    broker.balance -= financing                     # même solde que la courbe d'équité
                trades.append(SetupTrade(entry_time=trade.entry_time, exit_time=trade.exit_time,
                                         entry_price=trade.entry_price, exit_price=trade.exit_price,
                                         stop_loss=trade.sl, take_profit=trade.tp, volume=trade.volume,
                                         exit_reason=reason, ambiguous=trade.ambiguous,
                                         profit=round(trade.profit - financing, 2), symbol=self.symbol, **info))

        for i in range(len(exec_frame) - 1):
            now = (exec_frame.index[i] + duration).to_pydatetime()       # clôture de la bougie i
            bar = exec_frame.iloc[i + 1]                                  # première bougie APRÈS le signal
            spread = self._spread(bar)
            bid = float(bar["open"])
            broker.set_market(exec_frame.index[i + 1].to_pydatetime(), bid, spread)
            quote = Quote(bid=bid, ask=bid + spread * self.specs.point, tick_size=self.specs.tick_size)

            for strategy in self.strategies:
                data = {tf: visible(self.frames[tf], tf, now, strategy.bars - 1) for tf in strategy.timeframes}
                setup = strategy.evaluate(self.symbol, data, now, quote, broker, news=self.news)
                if setup is None:
                    continue
                decisions[f"{strategy.name}:{setup.status.value}"] += 1
                if setup.status != SetupStatus.READY:
                    continue
                key = (strategy.name, setup.side.value, round(setup.stop_loss, self.specs.digits))
                if key in taken:                                          # même setup, déjà exécuté
                    decisions[f"{strategy.name}:doublon"] += 1
                    continue
                if setup.expires_at is not None and broker.now() > setup.expires_at:
                    decisions[f"{strategy.name}:expiré à l'exécution"] += 1
                    continue
                taken.add(key)
                position = broker.open_position(setup.side.value, setup.volume, setup.stop_loss, setup.take_profit)
                meta[position.ticket] = dict(
                    strategy=strategy.name, side=setup.side.value, style=setup.style.value, regime=setup.regime.value,
                    setup_id=setup.id, signal_time=now, planned_risk=setup.risk_amount,
                    planned_net_ratio=setup.net_reward_risk)
                if strategy.trailing is not None:
                    trailing[position.ticket] = TrailingState(strategy.trailing, position, setup.indicators.get("atr"))

            record(broker.process_bar(broker.now(), bid, float(bar["high"]), float(bar["low"]), spread))
            # trailing : le stop est déplacé APRÈS la bougie (sorties jugées avec l'ancien stop), il ne
            # s'applique qu'à partir de la bougie suivante : aucune information intra-bougie supposée
            for state in trailing.values():
                state.update(float(bar["high"]), float(bar["low"]), spread * self.specs.point)
            equity[broker.now()] = round(broker.equity(bid=float(bar["close"])), 2)

        if len(exec_frame):
            last = exec_frame.iloc[-1]
            record(broker.close_all(exec_frame.index[-1].to_pydatetime(), float(last["close"])))
        curve = pd.Series(equity, name="equity", dtype=float)
        return SetupBacktestResult(trades, curve, self.initial_balance, decisions)


# =============================================================================
#  Ligne de commande (historique MT5)
# =============================================================================

def main(argv: list[str] | None = None) -> int:
    from backtesting.data import load_mt5
    from strategies.registry import SETUP_STRATEGIES, setup_strategies

    parser = argparse.ArgumentParser(prog="python -m backtesting.setup_engine")
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--mt5", action="store_true", required=True, help="historique du terminal MT5")
    parser.add_argument("--bars", type=int, default=6000, help="bougies M15 (les H1 sont 4 fois moins nombreuses)")
    parser.add_argument("--balance", type=float, default=10_000.0)
    parser.add_argument("--slippage", type=int, default=1, help="glissement en points")
    parser.add_argument("--spread", type=int, default=None, help="spread fixe en points (défaut : historique)")
    parser.add_argument("--commission", type=float, default=0.0, help="commission par lot (aller-retour)")
    parser.add_argument("--swap", type=float, default=0.0, help="financement payé par lot et par nuit (devise du compte)")
    parser.add_argument("--folds", type=int, default=4, help="périodes successives de la validation walk-forward")
    parser.add_argument("--strategies", default=None,
                        help=f"liste séparée par des virgules parmi : {', '.join(SETUP_STRATEGIES)} (défaut : SETUP_STRATEGIES)")
    parser.add_argument("--out", type=Path, default=REPORTS_DIR, help="dossier du rapport JSON (dashboard)")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    names = [n.strip() for n in args.strategies.split(",")] if args.strategies else None
    strategies = setup_strategies(names)
    # même période pour tous les timeframes : `--bars` bougies M15, plus l'historique de préchauffage
    span = args.bars * TIMEFRAME_SECONDS["M15"]
    frames, specs = {}, None
    for timeframe in sorted({tf for s in strategies for tf in s.timeframes}, key=TIMEFRAME_SECONDS.get):
        count = span // TIMEFRAME_SECONDS[timeframe] + max(s.bars for s in strategies)
        frames[timeframe], specs = load_mt5(args.symbol, min(count, MAX_LOADED_BARS), timeframe)
    costs = CostModel(args.spread, args.slippage, args.commission, args.swap)
    result = SetupBacktester(strategies, args.symbol, frames, specs, args.balance, costs,
                             bar_timezone=mt5_server_timezone()).run()
    finest = frames[min(frames, key=TIMEFRAME_SECONDS.get)]
    print(f"Backtest setups {args.symbol} · {finest.index[0]:%Y-%m-%d} → {finest.index[-1]:%Y-%m-%d}")
    print(result.summary())
    report = result.to_report(args.symbol, costs, args.folds)
    walk = report["walk_forward"]
    for fold in walk.get("folds", []):
        print(f"Période {fold['period']} ({fold['role']}, {fold['start'][:10]} → {fold['end'][:10]}) : "
              f"{fold['trades']} trades, {fold['net_profit']:+.2f}, {fold['expectancy_r']:+.2f} R")
    if walk.get("consistency_pct") is not None:
        print(f"Walk-forward : {walk['out_of_sample_positive']}/{walk['out_of_sample_traded']} périodes futures "
              f"positives ({walk['consistency_pct']:.0f} %)")
    print(f"Rapport : {save_report(report, args.out)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
