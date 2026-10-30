"""Broker factice pour les tests : implémente `BaseBroker`, état modifiable par attribut."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable

import pandas as pd

from brokers.base import BaseBroker
from brokers.models import AccountInfo, ClosedDeal, OrderResult, Position, SymbolSpecs, Tick


class FakeBroker(BaseBroker):
    name = "fake"

    def __init__(self, market: str = "forex", connected: bool = True) -> None:
        super().__init__()
        self.market = market  # type: ignore[assignment]
        self.connected = connected
        self.account: AccountInfo | None = None
        self.positions: list[Any] = []
        self.deals: list[ClosedDeal] = []
        self.specs: dict[str, SymbolSpecs] = {}
        self.ticks: dict[str, Tick] = {}
        self.bars: pd.DataFrame = pd.DataFrame()
        self.profit_fn: Callable[..., float | None] | None = None
        self.margin_fn: Callable[..., float | None] | None = None
        self.orders: list[dict[str, Any]] = []
        self.closed: list[tuple[int, bool]] = []
        self.order_ok = True
        self.close_ok: Callable[[int], bool] = lambda ticket: True

    def connect(self) -> bool:
        return self.connected

    def disconnect(self) -> None:
        self.connected = False

    def is_connected(self) -> bool:
        return self.connected

    def get_balance(self) -> AccountInfo | None:
        return self.account

    def get_positions(self, symbol: str | None = None, bot_only: bool = True) -> list[Position]:
        return [p for p in self.positions if symbol is None or p.symbol == symbol]

    def get_closed_deals(self, since: datetime, bot_only: bool = False) -> list[ClosedDeal]:
        return list(self.deals)

    def place_order(self, symbol: str, side: str, volume: float, sl: float | None = None,
                    tp: float | None = None, comment: str = "Bot Order") -> OrderResult:
        self.orders.append({"symbol": symbol, "action": side, "volume": volume, "sl": sl, "tp": tp})
        return OrderResult(self.order_ok, symbol, side, volume, price=1.1, sl=sl, tp=tp, ticket=-1,
                           message="" if self.order_ok else "refusé par le broker", dry_run=True)

    def close_order(self, ticket: int, comment: str = "Bot Close", force: bool = False) -> OrderResult:
        self.closed.append((ticket, force))
        ok = self.close_ok(ticket)
        return OrderResult(ok, "EURUSD", "CLOSE", 0.5, ticket=ticket, message="" if ok else "marché fermé")

    def get_historical_data(self, symbol: str, count: int = 100, timeframe: str | None = None) -> pd.DataFrame:
        return self.bars

    def get_current_tick(self, symbol: str) -> Tick | None:
        return self.ticks.get(symbol)

    def get_symbol_specs(self, symbol: str) -> SymbolSpecs | None:
        return self.specs.get(symbol)

    def calc_profit(self, side: str, symbol: str, volume: float, price_open: float, price_close: float) -> float | None:
        return self.profit_fn(side, symbol, volume, price_open, price_close) if self.profit_fn else None

    def calc_margin(self, side: str, symbol: str, volume: float, price: float) -> float | None:
        return self.margin_fn(side, symbol, volume, price) if self.margin_fn else None
