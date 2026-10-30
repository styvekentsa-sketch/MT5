"""
Adaptateur MetaTrader 5 : Forex, métaux, indices et CFD, quel que soit le broker MT5
(MetaQuotes-Demo, Exness, IC Markets...).

Toute la logique d'accès au terminal (verrou, reconnexion, order_check, retry sur requote,
DRY_RUN) reste dans `brokers.mt5_client` ; cette classe l'expose via `BaseBroker` et
traduit les noms de symboles quand le broker leur ajoute un suffixe :

    MT5_SYMBOL_SUFFIX=m     # Exness (compte Standard) : EURUSD -> EURUSDm, XAUUSD -> XAUUSDm

Le bot, la base, les news et le dashboard gardent les noms canoniques (EURUSD).
"""

from __future__ import annotations

from datetime import datetime

import pandas as pd

from brokers import mt5_client
from brokers.base import BaseBroker
from brokers.errors import BrokerConnectionError
from brokers.models import AccountInfo, ClosedDeal, OrderResult, Position, SymbolSpecs, Tick
from config import settings
from utils.logger import get_logger

log = get_logger(__name__)


class MT5Broker(BaseBroker):
    """Terminal MetaTrader 5 du processus (la bibliothèque MT5 ne gère qu'un terminal par processus)."""

    name = "mt5"
    market = "forex"

    def __init__(self, symbol_suffix: str | None = None) -> None:
        super().__init__()
        self.symbol_suffix = settings.broker.mt5_symbol_suffix if symbol_suffix is None else symbol_suffix

    @property
    def description(self) -> str:
        return f"MT5 {settings.mt5.account} @ {settings.mt5.server}"

    # ------------------------------------------------------------------ symboles

    def to_broker_symbol(self, symbol: str) -> str:
        return f"{symbol}{self.symbol_suffix}" if self.symbol_suffix else symbol

    def to_bot_symbol(self, broker_symbol: str) -> str:
        if self.symbol_suffix and broker_symbol.endswith(self.symbol_suffix):
            return broker_symbol[: -len(self.symbol_suffix)].upper()
        return broker_symbol.upper()

    # ------------------------------------------------------------------ connexion

    def connect(self) -> bool:
        connected = mt5_client.connect_mt5()
        if not connected:
            self._report("connexion", BrokerConnectionError("terminal MT5 injoignable ou identifiants refusés"))
        self._set_connected(connected, "" if connected else "terminal MT5 injoignable ou identifiants refusés")
        return connected

    def disconnect(self) -> None:
        mt5_client.shutdown_mt5()

    def is_connected(self) -> bool:
        return mt5_client.is_connected()

    def ensure_connection(self) -> None:
        try:
            mt5_client.ensure_connection()
        except BrokerConnectionError as exc:
            self._report("connexion", exc)
            self._set_connected(False, str(exc))
            raise
        self._set_connected(True)

    # ------------------------------------------------------------------ compte & positions

    def get_balance(self) -> AccountInfo | None:
        return mt5_client.get_account_info()

    def get_positions(self, symbol: str | None = None, bot_only: bool = True) -> list[Position]:
        positions = mt5_client.get_open_positions(
            symbol=self.to_broker_symbol(symbol) if symbol else None, bot_only=bot_only)
        return [Position.from_mt5(p, self.to_bot_symbol(p.symbol)) for p in positions]

    def get_closed_deals(self, since: datetime, bot_only: bool = False) -> list[ClosedDeal]:
        return mt5_client.get_closed_deals(since, bot_only=bot_only)

    # ------------------------------------------------------------------ ordres

    def place_order(self, symbol: str, side: str, volume: float, sl: float | None = None,
                    tp: float | None = None, comment: str = "Bot Order") -> OrderResult:
        result = mt5_client.execute_order(self.to_broker_symbol(symbol), side, volume, sl=sl, tp=tp, comment=comment)
        if not result.success:
            self._report(f"ordre {side} {symbol}", result.message)
        return result

    def close_order(self, ticket: int, comment: str = "Bot Close", force: bool = False) -> OrderResult:
        result = mt5_client.close_position(ticket, comment=comment, force=force)
        if not result.success:
            self._report(f"clôture #{ticket}", result.message)
        return result

    # ------------------------------------------------------------------ marché

    def get_historical_data(self, symbol: str, count: int = 100, timeframe: str | None = None) -> pd.DataFrame:
        return mt5_client.get_historical_data(self.to_broker_symbol(symbol), count=count, timeframe=timeframe)

    def get_current_tick(self, symbol: str) -> Tick | None:
        return mt5_client.get_current_tick(self.to_broker_symbol(symbol))

    def get_symbol_specs(self, symbol: str) -> SymbolSpecs | None:
        return mt5_client.get_symbol_specs(self.to_broker_symbol(symbol))

    def calc_profit(self, side: str, symbol: str, volume: float, price_open: float, price_close: float) -> float | None:
        return mt5_client.calc_profit(side, self.to_broker_symbol(symbol), volume, price_open, price_close)

    def calc_margin(self, side: str, symbol: str, volume: float, price: float) -> float | None:
        return mt5_client.calc_margin(side, self.to_broker_symbol(symbol), volume, price)
