"""
Structures de données communes à tous les brokers (MT5, exchanges crypto via ccxt, broker simulé).

Les valeurs monétaires sont toujours exprimées dans la devise du compte (USD, EUR, USDT...).
Le « volume » d'un ordre est en lots pour le Forex / CFD MT5 et en contrats pour la crypto
(1 contrat = `contract_size` unités de l'actif, 1 BTC pour BTC/USDT:USDT sur Bybit).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

Market = Literal["forex", "crypto"]
AccountType = Literal["demo", "real", "contest", "paper", "unknown"]

POSITION_TYPE_BUY = 0                # valeurs de mt5.POSITION_TYPE_BUY / SELL, reprises pour tous les brokers
POSITION_TYPE_SELL = 1


@dataclass(frozen=True)
class Tick:
    symbol: str
    bid: float
    ask: float
    last: float
    spread_points: int
    time: datetime

    @property
    def spread(self) -> float:
        return self.ask - self.bid

    @property
    def spread_pct(self) -> float:
        """Spread en % du prix moyen (comparable d'un actif à l'autre, utilisé pour la crypto)."""
        mid = (self.ask + self.bid) / 2
        return (self.ask - self.bid) / mid * 100 if mid > 0 else 0.0


@dataclass(frozen=True)
class AccountInfo:
    login: int
    balance: float
    equity: float
    margin: float
    margin_free: float
    margin_level: float
    profit: float
    currency: str
    leverage: int
    # Identité du compte (MT5 : account_info ; exchange : normalisée par l'adaptateur)
    name: str = ""                   # titulaire du compte, ou nom du compte API
    company: str = ""                # broker / exchange (ex : « Exness Technologies Ltd », « Bybit »)
    server: str = ""                 # serveur MT5 (ex : « Exness-MT5Real8 ») ou environnement de l'exchange
    account_type: AccountType = "unknown"


@dataclass(frozen=True)
class OrderResult:
    success: bool
    symbol: str
    order_type: str
    volume: float
    price: float = 0.0
    sl: float | None = None
    tp: float | None = None
    ticket: int = 0
    retcode: int = 0
    message: str = ""
    dry_run: bool = False
    order_id: str = ""               # identifiant natif de l'exchange (chaîne chez ccxt)


@dataclass(frozen=True)
class SymbolSpecs:
    symbol: str
    digits: int
    point: float
    tick_size: float
    tick_value: float                # valeur d'un tick pour 1 lot / 1 contrat, en devise du compte
    volume_min: float
    volume_max: float
    volume_step: float
    stops_level: int                 # distance minimale SL/TP - prix, en points
    freeze_level: int                # zone de gel autour du prix, en points
    trade_allowed: bool
    market: Market = "forex"
    contract_size: float = 1.0       # unités de l'actif par lot / contrat
    min_notional: float = 0.0        # valeur minimale d'un ordre (exchanges crypto), 0 = aucune

    @property
    def volume_unit(self) -> str:
        return "lot(s)" if self.market == "forex" else "contrat(s)"


@dataclass(frozen=True)
class ClosedDeal:
    ticket: int
    symbol: str
    time: datetime
    net_profit: float                # profit + commission + swap + frais
    magic: int


@dataclass(frozen=True)
class Position:
    """Position ouverte, même forme pour tous les brokers (attributs identiques aux positions MT5)."""
    ticket: int
    symbol: str
    type: int                        # POSITION_TYPE_BUY / POSITION_TYPE_SELL
    volume: float
    price_open: float
    price_current: float
    sl: float                        # 0.0 = aucun SL (convention MT5)
    tp: float
    profit: float
    swap: float
    time: int                        # ouverture, timestamp Unix (secondes)
    magic: int
    comment: str = ""

    @property
    def side(self) -> str:
        return "BUY" if self.type == POSITION_TYPE_BUY else "SELL"

    @classmethod
    def from_mt5(cls, p: Any, symbol: str | None = None) -> Position:
        return cls(ticket=p.ticket, symbol=symbol or p.symbol, type=p.type, volume=p.volume,
                   price_open=p.price_open, price_current=p.price_current, sl=p.sl, tp=p.tp,
                   profit=p.profit, swap=p.swap, time=int(p.time), magic=p.magic, comment=p.comment)


def format_volume(volume: float, step: float) -> str:
    """Volume affiché avec autant de décimales que le pas (0.01 lot, 0.001 BTC...)."""
    decimals = max(0, -Decimal(str(step)).normalize().as_tuple().exponent) if step > 0 else 2
    return f"{volume:.{decimals}f}"
