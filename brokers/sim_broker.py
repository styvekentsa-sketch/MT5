"""
Broker de SIMULATION (BROKER_TYPE=sim) : faire tourner le bot, le dashboard et le mode
analyste sans terminal MetaTrader 5 ni connexion internet.

  - bougies fictives : marche aléatoire déterministe (SIM_SEED) par symbole et timeframe,
    calée sur l'heure actuelle ; ou bougies locales (SIM_DATA_DIR/<SYMBOLE>_<TF>.csv, puis
    <SYMBOLE>.csv) au format des exports MT5 / backtest
  - prix en direct : prolongement de la dernière bougie, spread fixe par symbole
  - compte virtuel (SIM_BALANCE) : les ordres ouvrent des positions virtuelles, fermées
    automatiquement à leur SL / TP ; solde, fonds propres, P/L du jour et historique suivent

Tout ce qui sort de ce broker est marqué SIMULATION : nom « sim », description, ordres
`dry_run`, logs. Aucun ordre ne quitte jamais le processus.
"""

from __future__ import annotations

import itertools
import threading
import zlib
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from brokers.base import TIMEFRAME_NAMES, BaseBroker
from brokers.models import (
    POSITION_TYPE_BUY,
    POSITION_TYPE_SELL,
    AccountInfo,
    ClosedDeal,
    OrderResult,
    Position,
    SymbolSpecs,
    Tick,
)
from config import settings
from utils.logger import get_logger

log = get_logger(__name__)

TIMEFRAME_SECONDS = {"M1": 60, "M5": 300, "M15": 900, "M30": 1800, "H1": 3600, "H4": 14_400,
                     "D1": 86_400, "W1": 604_800, "MN1": 2_592_000}
OHLCV_COLUMNS = ["open", "high", "low", "close", "volume", "spread", "real_volume"]
SIM_LEVERAGE = 100


@dataclass(frozen=True)
class SimInstrument:
    """Caractéristiques d'un actif simulé (prix de départ, décimales, volatilité par minute)."""
    price: float
    digits: int
    contract_size: float
    vol_per_minute: float            # écart-type du rendement par minute
    spread_points: int


INSTRUMENTS = {
    "EURUSD": SimInstrument(1.1000, 5, 100_000, 0.00012, 10),
    "GBPUSD": SimInstrument(1.3000, 5, 100_000, 0.00015, 14),
    "USDJPY": SimInstrument(150.00, 3, 100_000, 0.00014, 12),
    "XAUUSD": SimInstrument(2400.0, 2, 100, 0.00030, 25),
}
DEFAULT_INSTRUMENT = SimInstrument(100.0, 2, 1, 0.00030, 10)


def instrument(symbol: str) -> SimInstrument:
    return INSTRUMENTS.get(symbol.upper(), DEFAULT_INSTRUMENT)


def synthetic_candles(symbol: str, timeframe: str, count: int, now: datetime, seed: int) -> pd.DataFrame:
    """
    Bougies fictives reproductibles : même seed, symbole, timeframe et heure -> mêmes bougies.
    La dernière bougie est « en cours » (comme chez MT5).
    """
    spec = instrument(symbol)
    step = TIMEFRAME_SECONDS[timeframe]
    end = datetime.fromtimestamp((int(now.timestamp()) // step) * step, tz=timezone.utc)
    index = pd.date_range(end=end, periods=count, freq=pd.Timedelta(seconds=step), tz="UTC")
    # graine liée à la bougie de départ : l'historique ne « bouge » pas d'un appel à l'autre
    rng = np.random.default_rng(zlib.crc32(f"{seed}:{symbol}:{timeframe}:{int(index[0].timestamp())}".encode()))
    sigma = spec.vol_per_minute * np.sqrt(step / 60)
    drift = rng.normal(0, sigma * 0.05)                        # légère tendance propre à l'historique
    returns = rng.normal(drift, sigma, count)
    close = spec.price * np.exp(np.cumsum(returns))
    open_ = np.concatenate([[spec.price], close[:-1]])
    wick = np.abs(rng.normal(0, sigma * 0.6, (2, count))) * close
    high = np.maximum(open_, close) + wick[0]
    low = np.minimum(open_, close) - wick[1]
    df = pd.DataFrame({"open": open_, "high": high, "low": low, "close": close,
                       "volume": rng.integers(50, 500, count).astype(float),
                       "spread": spec.spread_points, "real_volume": 0.0}, index=index)
    df.index.name = "time"
    return df.round(spec.digits)


class SimBroker(BaseBroker):
    """Broker entièrement simulé : données fictives ou locales, compte et positions virtuels."""

    name = "sim"
    market = "forex"

    def __init__(self, seed: int | None = None, balance: float | None = None, data_dir: Path | None = None,
                 clock=None) -> None:
        super().__init__()
        cfg = settings.broker.sim
        self.seed = cfg.seed if seed is None else seed
        self.initial_balance = cfg.balance if balance is None else balance
        self.data_dir = data_dir if data_dir is not None else cfg.data_dir
        self.currency = "USD"
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()
        self._connected = False
        self._balance = self.initial_balance
        self._positions: dict[int, Position] = {}
        self._deals: list[ClosedDeal] = []
        self._tickets = itertools.count(1)
        self._csv_cache: dict[tuple[str, str], pd.DataFrame | None] = {}

    @property
    def description(self) -> str:
        source = f"CSV {self.data_dir}" if self.data_dir else f"données fictives, seed {self.seed}"
        return f"SIMULATION ({source})"

    def now(self) -> datetime:
        return self._clock()

    # ------------------------------------------------------------------ connexion

    def connect(self) -> bool:
        self._connected = True
        self._set_connected(True)
        log.warning("MODE SIMULATION : %s, compte virtuel de %.2f %s, aucun ordre réel",
                    self.description, self.initial_balance, self.currency)
        return True

    def disconnect(self) -> None:
        self._connected = False

    def is_connected(self) -> bool:
        return self._connected

    # ------------------------------------------------------------------ données de marché

    def _local_candles(self, symbol: str, timeframe: str) -> pd.DataFrame | None:
        if not self.data_dir:
            return None
        key = (symbol, timeframe)
        if key not in self._csv_cache:
            from backtesting.data import load_csv     # import paresseux : utile seulement en mode CSV

            frame = None
            for name in (f"{symbol}_{timeframe}.csv", f"{symbol}.csv"):
                path = Path(self.data_dir) / name
                if path.exists():
                    frame = load_csv(path)
                    log.info("Simulation : bougies %s %s lues dans %s (%d)", symbol, timeframe, path, len(frame))
                    break
            self._csv_cache[key] = frame
        return self._csv_cache[key]

    def get_historical_data(self, symbol: str, count: int = 100, timeframe: str | None = None) -> pd.DataFrame:
        if count <= 0:
            raise ValueError(f"count doit être > 0 (reçu : {count})")
        tf = (timeframe or settings.trading.timeframe).upper()
        if tf not in TIMEFRAME_NAMES:
            raise ValueError(f"Timeframe inconnu : {tf}")
        local = self._local_candles(symbol, tf)
        if local is not None:
            df = local.tail(count).copy()
            for column in OHLCV_COLUMNS:
                if column not in df.columns:
                    df[column] = 0.0
            return df[OHLCV_COLUMNS]
        return synthetic_candles(symbol, tf, count, self.now(), self.seed)

    def get_current_tick(self, symbol: str) -> Tick | None:
        spec = instrument(symbol)
        last = self.get_historical_data(symbol, count=2, timeframe="M1").iloc[-1]
        point = 10 ** -spec.digits
        # oscillation intra-minute déterministe autour de la clôture de la bougie en cours
        now = self.now()
        wobble = np.sin(now.second / 60 * 2 * np.pi + zlib.crc32(symbol.encode()) % 7) * (last["high"] - last["low"]) / 2
        bid = round(float(last["close"]) + wobble, spec.digits)
        return Tick(symbol, bid, round(bid + spec.spread_points * point, spec.digits), bid, spec.spread_points, now)

    def get_symbol_specs(self, symbol: str) -> SymbolSpecs | None:
        spec = instrument(symbol)
        point = 10 ** -spec.digits
        return SymbolSpecs(symbol=symbol, digits=spec.digits, point=point, tick_size=point,
                           tick_value=point * spec.contract_size, volume_min=0.01, volume_max=100.0,
                           volume_step=0.01, stops_level=0, freeze_level=0, trade_allowed=True,
                           contract_size=spec.contract_size)

    def calc_profit(self, side: str, symbol: str, volume: float, price_open: float, price_close: float) -> float | None:
        direction = 1 if side.upper() == "BUY" else -1
        value = (price_close - price_open) * direction * volume * instrument(symbol).contract_size
        if symbol.upper().startswith("USD") and price_close > 0:       # cotation en devise étrangère (USDJPY)
            value /= price_close
        return round(value, 2)

    def calc_margin(self, side: str, symbol: str, volume: float, price: float) -> float | None:
        notional = volume * instrument(symbol).contract_size * (1.0 if symbol.upper().startswith("USD") else price)
        return round(notional / SIM_LEVERAGE, 2)

    # ------------------------------------------------------------------ compte & positions virtuels

    def _refresh(self) -> None:
        """Met à jour les prix des positions et ferme celles dont le SL ou le TP est atteint."""
        for ticket, pos in list(self._positions.items()):
            tick = self.get_current_tick(pos.symbol)
            price = tick.bid if pos.type == POSITION_TYPE_BUY else tick.ask
            hit = None
            if pos.type == POSITION_TYPE_BUY:
                hit = "sl" if pos.sl and price <= pos.sl else "tp" if pos.tp and price >= pos.tp else None
            else:
                hit = "sl" if pos.sl and price >= pos.sl else "tp" if pos.tp and price <= pos.tp else None
            if hit:
                self._close(ticket, pos.sl if hit == "sl" else pos.tp, hit)
            else:
                profit = self.calc_profit(pos.side, pos.symbol, pos.volume, pos.price_open, price) or 0.0
                self._positions[ticket] = replace(pos, price_current=price, profit=profit)

    def _close(self, ticket: int, price: float, reason: str) -> float:
        pos = self._positions.pop(ticket)
        profit = self.calc_profit(pos.side, pos.symbol, pos.volume, pos.price_open, price) or 0.0
        self._balance = round(self._balance + profit, 2)
        self._deals.append(ClosedDeal(ticket=next(self._tickets), symbol=pos.symbol, time=self.now(), net_profit=profit,
                                      magic=pos.magic, position_id=ticket, price=price, reason=reason,
                                      entry="out", volume=pos.volume))
        log.info("[SIMULATION] Position #%d %s %s fermée (%s) @ %s : %+.2f %s",
                 ticket, pos.side, pos.symbol, reason, price, profit, self.currency)
        return profit

    def get_balance(self) -> AccountInfo | None:
        with self._lock:
            self._refresh()
            floating = round(sum(p.profit for p in self._positions.values()), 2)
            margin = round(sum(self.calc_margin(p.side, p.symbol, p.volume, p.price_open) or 0.0
                               for p in self._positions.values()), 2)
            equity = round(self._balance + floating, 2)
            return AccountInfo(login=0, balance=self._balance, equity=equity, margin=margin,
                               margin_free=round(equity - margin, 2),
                               margin_level=round(equity / margin * 100, 2) if margin else 0.0,
                               profit=floating, currency=self.currency, leverage=SIM_LEVERAGE,
                               name="Compte de simulation", company="SIMULATION", server=self.description,
                               account_type="paper")

    def get_positions(self, symbol: str | None = None, bot_only: bool = True) -> list[Position]:
        with self._lock:
            self._refresh()
            return [p for p in self._positions.values() if symbol is None or p.symbol == symbol]

    def get_closed_deals(self, since: datetime, bot_only: bool = False) -> list[ClosedDeal]:
        with self._lock:
            return [d for d in self._deals if d.time >= since and d.entry == "out"]

    def get_position_deals(self, position_id: int) -> list[ClosedDeal]:
        with self._lock:
            return [d for d in self._deals if d.position_id == position_id]

    # ------------------------------------------------------------------ ordres

    def place_order(self, symbol: str, side: str, volume: float, sl: float | None = None,
                    tp: float | None = None, comment: str = "Bot Order") -> OrderResult:
        side = side.strip().upper()
        if side not in ("BUY", "SELL") or volume <= 0:
            return OrderResult(False, symbol, side, volume, message="ordre invalide")
        with self._lock:
            tick = self.get_current_tick(symbol)
            price = tick.ask if side == "BUY" else tick.bid
            ticket = next(self._tickets)
            self._positions[ticket] = Position(
                ticket=ticket, symbol=symbol, type=POSITION_TYPE_BUY if side == "BUY" else POSITION_TYPE_SELL,
                volume=volume, price_open=price, price_current=price, sl=sl or 0.0, tp=tp or 0.0, profit=0.0,
                swap=0.0, time=int(self.now().timestamp()), magic=settings.trading.magic_number, comment=comment)
            self._deals.append(ClosedDeal(ticket=next(self._tickets), symbol=symbol, time=self.now(), net_profit=0.0,
                                          magic=settings.trading.magic_number, position_id=ticket, price=price,
                                          reason="bot", entry="in", volume=volume))
        log.info("[SIMULATION] %s %s %s @ %s | SL %s | TP %s | ticket #%d", side, volume, symbol, price, sl, tp, ticket)
        return OrderResult(True, symbol, side, volume, price, sl, tp, ticket=ticket, message="simulation", dry_run=True)

    def close_order(self, ticket: int, comment: str = "Bot Close", force: bool = False) -> OrderResult:
        with self._lock:
            pos = self._positions.get(ticket)
            if pos is None:
                return OrderResult(False, "", "CLOSE", 0.0, ticket=ticket, message=f"position #{ticket} introuvable")
            tick = self.get_current_tick(pos.symbol)
            price = tick.bid if pos.type == POSITION_TYPE_BUY else tick.ask
            self._close(ticket, price, "bot" if comment.startswith("Bot") else "manuel")
        return OrderResult(True, pos.symbol, "CLOSE", pos.volume, price, ticket=ticket, message="fermée (simulation)",
                           dry_run=True)

    def modify_stops(self, ticket: int, sl: float | None, tp: float | None) -> OrderResult:
        with self._lock:
            pos = self._positions.get(ticket)
            if pos is None:
                return OrderResult(False, "", "MODIFY", 0.0, ticket=ticket, message=f"position #{ticket} introuvable")
            self._positions[ticket] = replace(pos, sl=sl if sl is not None else pos.sl, tp=tp if tp is not None else pos.tp)
        return OrderResult(True, pos.symbol, "MODIFY", pos.volume, sl=sl, tp=tp, ticket=ticket, message="simulation",
                           dry_run=True)

    def trading_enabled(self) -> bool | None:
        return True

