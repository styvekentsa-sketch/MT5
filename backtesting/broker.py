"""
Broker simulé pour le backtest : compte, positions, exécution des SL/TP bougie par bougie.

Implémente le protocole `risk.risk_manager.Broker` : les règles de risque du bot
(`calculate_position_size`, `validate_trade_risk`) s'exécutent donc sans modification,
avec l'horloge et le compte simulés (pertes journalières, pertes consécutives, marge...).

Conventions d'exécution (prudentes) :
  - les bougies sont en prix Bid ; Ask = Bid + spread
  - un BUY s'ouvre à l'Ask et se clôture au Bid ; un SELL l'inverse
  - ouverture de bougie au-delà du SL/TP (gap) : exécution au prix d'ouverture
  - SL et TP touchés dans la même bougie : le SL est retenu (pire cas)
  - profit = écart / tick_size x tick_value x volume (devise de cotation = devise du compte)
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from brokers.mt5_client import AccountInfo, ClosedDeal, SymbolSpecs, Tick
from config.settings import settings

Side = Literal["BUY", "SELL"]
ExitReason = Literal["sl", "tp", "end"]


@dataclass
class SimPosition:
    ticket: int
    symbol: str
    side: Side
    volume: float
    entry_price: float
    sl: float
    tp: float | None
    opened_at: datetime
    magic: int


@dataclass(frozen=True)
class Trade:
    ticket: int
    symbol: str
    side: Side
    volume: float
    entry_time: datetime
    entry_price: float
    exit_time: datetime
    exit_price: float
    sl: float
    tp: float | None
    exit_reason: ExitReason
    commission: float
    profit: float                    # net, commission déduite


class SimulatedBroker:
    """Compte et carnet de positions simulés pour un symbole."""

    def __init__(self, specs: SymbolSpecs, initial_balance: float, currency: str = "USD", leverage: int = 100,
                 commission_per_lot: float = 0.0) -> None:
        if initial_balance <= 0:
            raise ValueError("le capital initial doit être > 0")
        self.specs = specs
        self.currency = currency
        self.leverage = leverage
        self.commission_per_lot = commission_per_lot
        self.balance = float(initial_balance)
        self.positions: list[SimPosition] = []
        self.trades: list[Trade] = []
        self._deals: list[ClosedDeal] = []
        self._tickets = itertools.count(1)
        self._clock = datetime.fromtimestamp(0, tz=timezone.utc)
        self._bid = 0.0
        self._spread_points = 0

    # ------------------------------------------------------------------ marché

    def set_market(self, time: datetime, bid: float, spread_points: int) -> None:
        """Horloge et prix courants (ouverture de la bougie en cours)."""
        self._clock, self._bid, self._spread_points = time, bid, max(0, int(spread_points))

    @property
    def spread(self) -> float:
        return self._spread_points * self.specs.point

    @property
    def bid(self) -> float:
        return self._bid

    @property
    def ask(self) -> float:
        return self._bid + self.spread

    # ------------------------------------------------------------------ protocole Broker (risk_manager)

    def now(self) -> datetime:
        return self._clock

    def get_account_info(self) -> AccountInfo:
        floating = self.floating_profit()
        margin = sum(self.calc_margin(p.side, p.symbol, p.volume, p.entry_price) or 0.0 for p in self.positions)
        equity = self.balance + floating
        return AccountInfo(
            login=0, balance=round(self.balance, 2), equity=round(equity, 2), margin=round(margin, 2),
            margin_free=round(equity - margin, 2), margin_level=round(equity / margin * 100, 2) if margin else 0.0,
            profit=round(floating, 2), currency=self.currency, leverage=self.leverage,
        )

    def get_symbol_specs(self, symbol: str) -> SymbolSpecs | None:
        return self.specs if symbol == self.specs.symbol else None

    def get_current_tick(self, symbol: str) -> Tick | None:
        if symbol != self.specs.symbol or self._bid <= 0:
            return None
        return Tick(symbol=symbol, bid=self.bid, ask=self.ask, last=0.0, spread_points=self._spread_points,
                    time=self._clock)

    def get_open_positions(self) -> list[SimPosition]:
        return list(self.positions)

    def get_closed_deals(self, since: datetime) -> list[ClosedDeal]:
        return [d for d in self._deals if d.time >= since]

    def calc_profit(self, side: str, symbol: str, volume: float, price_open: float, price_close: float) -> float:
        diff = price_close - price_open if side == "BUY" else price_open - price_close
        return diff / self.specs.tick_size * self.specs.tick_value * volume

    def calc_margin(self, side: str, symbol: str, volume: float, price: float) -> float:
        contract_value_per_unit = self.specs.tick_value / self.specs.tick_size      # ex : 100 000 pour EURUSD
        return volume * price * contract_value_per_unit / self.leverage

    # ------------------------------------------------------------------ positions

    def floating_profit(self) -> float:
        """P/L latent au prix courant (BUY clôturé au Bid, SELL à l'Ask)."""
        return sum(self.calc_profit(p.side, p.symbol, p.volume, p.entry_price,
                                    self.bid if p.side == "BUY" else self.ask) for p in self.positions)

    def equity(self, bid: float | None = None) -> float:
        if bid is not None:
            self._bid = bid
        return self.balance + self.floating_profit()

    def open_position(self, side: Side, volume: float, sl: float, tp: float | None) -> SimPosition:
        """Ouvre au prix courant : Ask pour un achat, Bid pour une vente."""
        position = SimPosition(
            ticket=next(self._tickets), symbol=self.specs.symbol, side=side, volume=volume,
            entry_price=self.ask if side == "BUY" else self.bid, sl=sl, tp=tp, opened_at=self._clock,
            magic=settings.trading.magic_number,
        )
        self.positions.append(position)
        return position

    def process_bar(self, time: datetime, open_: float, high: float, low: float, spread_points: int) -> list[Trade]:
        """Déclenche les SL/TP touchés pendant la bougie ; retourne les trades clôturés."""
        spread = max(0, int(spread_points)) * self.specs.point
        closed: list[Trade] = []
        for position in list(self.positions):
            exit_price, reason = self._exit_level(position, open_, high, low, spread)
            if exit_price is not None and reason is not None:
                closed.append(self._close(position, exit_price, time, reason))
        return closed

    @staticmethod
    def _exit_level(p: SimPosition, open_: float, high: float, low: float,
                    spread: float) -> tuple[float | None, ExitReason | None]:
        if p.side == "BUY":                          # clôture au Bid
            if open_ <= p.sl:
                return open_, "sl"
            if p.tp is not None and open_ >= p.tp:
                return open_, "tp"
            if low <= p.sl:
                return p.sl, "sl"                    # SL prioritaire si SL et TP dans la même bougie
            if p.tp is not None and high >= p.tp:
                return p.tp, "tp"
            return None, None
        ask_open, ask_high, ask_low = open_ + spread, high + spread, low + spread   # SELL : clôture à l'Ask
        if ask_open >= p.sl:
            return ask_open, "sl"
        if p.tp is not None and ask_open <= p.tp:
            return ask_open, "tp"
        if ask_high >= p.sl:
            return p.sl, "sl"
        if p.tp is not None and ask_low <= p.tp:
            return p.tp, "tp"
        return None, None

    def close_all(self, time: datetime, bid: float) -> list[Trade]:
        """Clôture les positions restantes au dernier prix (fin des données)."""
        self._bid = bid
        return [self._close(p, bid if p.side == "BUY" else self.ask, time, "end") for p in list(self.positions)]

    def _close(self, position: SimPosition, exit_price: float, time: datetime, reason: ExitReason) -> Trade:
        commission = self.commission_per_lot * position.volume
        profit = self.calc_profit(position.side, position.symbol, position.volume, position.entry_price,
                                  exit_price) - commission
        self.balance += profit
        self.positions.remove(position)
        trade = Trade(
            ticket=position.ticket, symbol=position.symbol, side=position.side, volume=position.volume,
            entry_time=position.opened_at, entry_price=position.entry_price, exit_time=time,
            exit_price=exit_price, sl=position.sl, tp=position.tp, exit_reason=reason,
            commission=round(commission, 2), profit=round(profit, 2),
        )
        self.trades.append(trade)
        self._deals.append(ClosedDeal(ticket=position.ticket, symbol=position.symbol, time=time,
                                      net_profit=trade.profit, magic=position.magic))
        return trade
