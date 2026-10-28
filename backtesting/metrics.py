"""Métriques de performance d'un backtest : rendement, taux de réussite, profit factor, drawdown."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any, Iterable

import pandas as pd

from backtesting.broker import Trade


@dataclass(frozen=True)
class BacktestMetrics:
    initial_balance: float
    final_balance: float
    net_profit: float
    total_return_pct: float
    trades: int
    wins: int
    losses: int
    winrate_pct: float
    gross_profit: float
    gross_loss: float                # valeur positive
    profit_factor: float | None      # inf si aucune perte ; None si aucun trade gagnant ni perdant
    avg_win: float
    avg_loss: float                  # valeur positive
    expectancy: float                # gain moyen par trade
    largest_win: float
    largest_loss: float              # valeur positive
    max_drawdown: float              # valeur positive, devise du compte
    max_drawdown_pct: float          # en % du plus haut précédent

    def to_dict(self) -> dict[str, Any]:
        """Dictionnaire sérialisable en JSON (profit factor infini -> None)."""
        data = asdict(self)
        if data["profit_factor"] is not None and math.isinf(data["profit_factor"]):
            data["profit_factor"] = None
        return data


def max_drawdown(equity: Iterable[float]) -> tuple[float, float]:
    """(plus forte baisse depuis un plus haut, la même en % de ce plus haut)."""
    peak = -math.inf
    worst, worst_pct = 0.0, 0.0
    for value in equity:
        peak = max(peak, value)
        drawdown = peak - value
        if drawdown > worst:
            worst = drawdown
            worst_pct = drawdown / peak * 100 if peak > 0 else 0.0
    return round(worst, 2), round(worst_pct, 2)


def compute_metrics(trades: list[Trade], equity_curve: pd.Series | list[float], initial_balance: float) -> BacktestMetrics:
    """Métriques à partir des trades clôturés et de la courbe de fonds propres (une valeur par bougie)."""
    profits = [t.profit for t in trades]
    wins = [p for p in profits if p > 0]
    losses = [-p for p in profits if p < 0]
    gross_profit, gross_loss = sum(wins), sum(losses)
    if gross_loss > 0:
        profit_factor: float | None = round(gross_profit / gross_loss, 3)
    else:
        profit_factor = math.inf if gross_profit > 0 else None

    final_balance = initial_balance + sum(profits)
    equity = list(equity_curve) if len(equity_curve) else [initial_balance, final_balance]
    drawdown, drawdown_pct = max_drawdown([initial_balance, *equity])

    return BacktestMetrics(
        initial_balance=round(initial_balance, 2),
        final_balance=round(final_balance, 2),
        net_profit=round(final_balance - initial_balance, 2),
        total_return_pct=round((final_balance - initial_balance) / initial_balance * 100, 2),
        trades=len(trades),
        wins=len(wins),
        losses=len(losses),
        winrate_pct=round(len(wins) / len(trades) * 100, 2) if trades else 0.0,
        gross_profit=round(gross_profit, 2),
        gross_loss=round(gross_loss, 2),
        profit_factor=profit_factor,
        avg_win=round(gross_profit / len(wins), 2) if wins else 0.0,
        avg_loss=round(gross_loss / len(losses), 2) if losses else 0.0,
        expectancy=round(sum(profits) / len(trades), 2) if trades else 0.0,
        largest_win=round(max(wins), 2) if wins else 0.0,
        largest_loss=round(max(losses), 2) if losses else 0.0,
        max_drawdown=drawdown,
        max_drawdown_pct=drawdown_pct,
    )
