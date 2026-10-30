"""
Adaptateur exchanges crypto via ccxt (Binance USDⓈ-M, Bybit, OKX...) : contrats perpétuels
linéaires réglés en USDT.

Pourquoi uniquement les perpétuels : ce sont les seuls marchés où l'exchange conserve un
Stop Loss côté serveur pour la position et où la vente (SELL) à découvert est possible ;
le spot ne permet ni l'un ni l'autre de façon uniforme.

Unités : le volume est un nombre de contrats (`contractSize` unités de l'actif, 1 BTC pour
BTC/USDT:USDT). Le pas, le minimum, la valeur minimale d'ordre (notional) et le pas de prix
viennent des métadonnées de l'exchange (`load_markets`).

Symboles : TRADING_SYMBOLS=BTCUSDT,ETHUSDT (ou la notation ccxt BTC/USDT:USDT).

Ordres : entrée au marché, puis SL et TP en ordres conditionnels « reduce-only ». Si le SL ne
peut pas être posé, la position est refermée immédiatement : jamais de position non protégée.
Aucun renvoi automatique après une erreur réseau (l'ordre a pu passer : risque de doublon).

Sans clé API (PAPER uniquement) : données publiques réelles, capital fictif CCXT_PAPER_BALANCE.
Hypothèse : compte en mode « one-way » (une position par symbole), marge croisée.
"""

from __future__ import annotations

import itertools
import threading
import time
import zlib
from datetime import datetime, timezone
from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal
from typing import Any, Callable, TypeVar

import pandas as pd

from brokers.base import BaseBroker, is_dry_run
from brokers.errors import BrokerConnectionError, BrokerError
from brokers.models import (
    POSITION_TYPE_BUY,
    POSITION_TYPE_SELL,
    AccountInfo,
    ClosedDeal,
    OrderResult,
    Position,
    SymbolSpecs,
    Tick,
    format_volume,
)
from config import settings
from config.broker import CCXTConfig
from utils.logger import get_logger

log = get_logger(__name__)

T = TypeVar("T")

TIMEFRAMES = {"M1": "1m", "M5": "5m", "M15": "15m", "M30": "30m", "H1": "1h",
              "H4": "4h", "D1": "1d", "W1": "1w", "MN1": "1M"}
OHLCV_COLUMNS = ["open", "high", "low", "close", "volume", "spread", "real_volume"]
MAX_CONNECT_ATTEMPTS = 3
CONNECT_RETRY_DELAY_SECONDS = 5      # doublé à chaque tentative
ORDER_BOOK_DEPTH = 5
UNLIMITED_VOLUME = 1e12


def _floor_to_step(value: float, step: float) -> float:
    d_step = Decimal(str(step))
    return float((Decimal(str(round(value, 12))) / d_step).to_integral_value(rounding=ROUND_FLOOR) * d_step)


def _round_to_step(value: float, step: float) -> float:
    d_step = Decimal(str(step))
    return float((Decimal(str(value)) / d_step).to_integral_value(rounding=ROUND_HALF_UP) * d_step)


def _decimals(step: float) -> int:
    return max(0, -Decimal(str(step)).normalize().as_tuple().exponent)


class CCXTBroker(BaseBroker):
    """Un compte (ou sous-compte) sur un exchange crypto."""

    market = "crypto"

    def __init__(self, config: CCXTConfig | None = None, exchange: Any = None,
                 connect_attempts: int = MAX_CONNECT_ATTEMPTS, symbols: tuple[str, ...] | None = None) -> None:
        super().__init__()
        self.cfg = config or settings.broker.ccxt
        self.symbols = symbols if symbols is not None else settings.trading.symbols
        self.name = self.cfg.exchange
        self._exchange = exchange                   # injectable (tests) ; créé à la connexion sinon
        self._connect_attempts = connect_attempts
        self._lock = threading.RLock()              # ccxt synchrone : un appel à la fois
        self._connected = False
        self._markets: dict[str, dict[str, Any]] = {}       # symbole bot -> marché ccxt
        self._leverage_done: set[str] = set()
        self._paper_tickets = itertools.count(-1, -1)

    @property
    def description(self) -> str:
        env = self.cfg.environment if self.cfg.has_credentials else "données publiques, capital fictif"
        return f"{self.name} {self.cfg.market_type} {self.cfg.quote} ({env})"

    # ------------------------------------------------------------------ appels à l'exchange

    def _call(self, operation: str, func: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        """
        Exécute un appel ccxt. Erreur réseau -> session marquée perdue + BrokerConnectionError ;
        autre erreur de l'exchange -> BrokerError. Tout incident va au journal central.
        """
        import ccxt

        with self._lock:
            try:
                result = func(*args, **kwargs)
            except ccxt.NetworkError as exc:
                self._connected = False
                self._report(operation, exc)
                self._set_connected(False, f"{type(exc).__name__} : {str(exc)[:200]}")
                raise BrokerConnectionError(f"{self.name} injoignable ({operation}) : {exc}") from exc
            except ccxt.BaseError as exc:
                self._report(operation, exc)
                raise BrokerError(f"{self.name} : {operation} refusé : {exc}") from exc
        if not self._connected and self._markets:
            self._connected = True
            self._set_connected(True)
        return result

    def _build_exchange(self) -> Any:
        import ccxt

        exchange_class = getattr(ccxt, self.cfg.exchange, None)
        if exchange_class is None:
            raise BrokerError(f"Exchange inconnu de ccxt : {self.cfg.exchange}")
        exchange = exchange_class({
            "apiKey": self.cfg.api_key,
            "secret": self.cfg.api_secret,
            "password": self.cfg.api_password,
            "enableRateLimit": True,
            "timeout": self.cfg.timeout_ms,
            "options": {"defaultType": self.cfg.market_type},
        })
        # Sans clé : seules les données publiques sont lues, toujours sur les serveurs réels
        if self.cfg.has_credentials and self.cfg.environment == "demo":
            if hasattr(exchange, "enable_demo_trading"):
                exchange.enable_demo_trading(True)
            else:
                exchange.set_sandbox_mode(True)
        elif self.cfg.has_credentials and self.cfg.environment == "testnet":
            exchange.set_sandbox_mode(True)
        return exchange

    # ------------------------------------------------------------------ connexion

    def connect(self) -> bool:
        for attempt in range(1, self._connect_attempts + 1):
            log.info("Connexion à %s (tentative %d/%d)", self.description, attempt, self._connect_attempts)
            try:
                with self._lock:
                    if self._exchange is None:
                        self._exchange = self._build_exchange()
                    self._call("chargement des marchés", self._exchange.load_markets, True)
                    self._markets.clear()
                    for symbol in self.symbols:
                        self._resolve(symbol)
                    if self.cfg.has_credentials:
                        self._call("lecture du solde", self._exchange.fetch_balance)
                    self._connected = True
                self._set_connected(True)
                log.info("Connecté à %s : %s", self.description,
                         ", ".join(f"{s} -> {m['symbol']}" for s, m in self._markets.items()))
                return True
            except BrokerConnectionError as exc:
                log.warning("Connexion à %s impossible : %s", self.name, exc)
            except BrokerError as exc:              # symbole absent, clé refusée... : inutile de réessayer
                log.error("Connexion à %s refusée : %s", self.name, exc)
                self._set_connected(False, str(exc))
                return False
            if attempt < self._connect_attempts:
                time.sleep(CONNECT_RETRY_DELAY_SECONDS * 2 ** (attempt - 1))
        self._set_connected(False, f"{self.name} injoignable après {self._connect_attempts} tentatives")
        return False

    def disconnect(self) -> None:
        with self._lock:
            self._connected = False
            session = getattr(self._exchange, "session", None)
            if session is not None:
                session.close()
        log.info("Session %s fermée", self.name)

    def is_connected(self) -> bool:
        return self._connected and bool(self._markets)

    # ------------------------------------------------------------------ symboles & spécifications

    def _resolve(self, symbol: str) -> dict[str, Any]:
        """Marché ccxt d'un symbole du bot : perpétuel linéaire réglé dans la devise du compte."""
        if symbol in self._markets:
            return self._markets[symbol]
        markets = self._exchange.markets or {}
        wanted = symbol.upper()
        candidates = [markets[symbol]] if symbol in markets else [
            m for m in markets.values()
            if (str(m.get("base", "")) + str(m.get("quote", ""))).upper() == wanted
        ]
        for market in candidates:
            if (market.get("type") == self.cfg.market_type and market.get("linear")
                    and market.get("settle") == self.cfg.quote and market.get("active") is not False):
                self._markets[symbol] = market
                return market
        raise BrokerError(f"{symbol} : aucun contrat {self.cfg.market_type} linéaire réglé en "
                          f"{self.cfg.quote} sur {self.name}")

    def _market(self, symbol: str) -> dict[str, Any]:
        if not self._markets:
            self.ensure_connection()
        try:
            return self._resolve(symbol)
        except BrokerError as exc:
            raise ValueError(str(exc)) from exc

    def _bot_symbol(self, market_symbol: str) -> str | None:
        return next((s for s, m in self._markets.items() if m["symbol"] == market_symbol), None)

    def _step(self, precision: Any) -> float:
        """Pas de prix / de volume selon le mode de précision de l'exchange."""
        import ccxt

        if precision is None:
            raise BrokerError(f"{self.name} : précision du marché inconnue")
        if self._exchange.precisionMode == ccxt.TICK_SIZE:
            return float(precision)
        if self._exchange.precisionMode == ccxt.DECIMAL_PLACES:
            return 10.0 ** -int(precision)
        raise BrokerError(f"{self.name} : mode de précision non pris en charge")

    def get_symbol_specs(self, symbol: str) -> SymbolSpecs | None:
        try:
            market = self._market(symbol)
            tick_size = self._step(market["precision"]["price"])
            volume_step = self._step(market["precision"]["amount"])
        except (ValueError, BrokerError) as exc:
            log.error("%s", exc)
            return None
        contract = float(market.get("contractSize") or 1.0)
        limits = market.get("limits") or {}
        amount = limits.get("amount") or {}
        cost = limits.get("cost") or {}
        return SymbolSpecs(
            symbol=symbol,
            digits=_decimals(tick_size),
            point=tick_size,
            tick_size=tick_size,
            tick_value=tick_size * contract,        # contrat linéaire : P/L en devise de règlement (= compte)
            volume_min=max(float(amount.get("min") or 0.0), volume_step),
            volume_max=float(amount.get("max") or UNLIMITED_VOLUME),
            volume_step=volume_step,
            stops_level=0,
            freeze_level=0,
            trade_allowed=market.get("active") is not False,
            market="crypto",
            contract_size=contract,
            min_notional=float(cost.get("min") or 0.0),
        )

    # ------------------------------------------------------------------ données de marché

    def get_historical_data(self, symbol: str, count: int = 100, timeframe: str | None = None) -> pd.DataFrame:
        if count <= 0:
            raise ValueError(f"count doit être > 0 (reçu : {count})")
        tf_name = (timeframe or settings.trading.timeframe).upper()
        tf = TIMEFRAMES.get(tf_name)
        if tf is None or tf not in (self._exchange.timeframes if self._exchange else TIMEFRAMES.values()):
            raise ValueError(f"Timeframe {tf_name} indisponible sur {self.name}")
        market = self._market(symbol)
        rows = self._call(f"bougies {symbol} {tf_name}", self._exchange.fetch_ohlcv, market["symbol"], tf,
                          limit=count)
        if not rows:
            log.error("Aucune bougie reçue pour %s %s", symbol, tf_name)
            return pd.DataFrame(columns=OHLCV_COLUMNS)
        df = pd.DataFrame(rows, columns=["time", "open", "high", "low", "close", "volume"])
        df["time"] = pd.to_datetime(df["time"], unit="ms", utc=True)
        df["spread"] = 0
        df["real_volume"] = df["volume"]
        df = df.set_index("time")[OHLCV_COLUMNS].astype(float)
        if len(df) < count:
            log.warning("Historique partiel pour %s %s : %d/%d bougies", symbol, tf_name, len(df), count)
        return df

    def get_current_tick(self, symbol: str) -> Tick | None:
        market = self._market(symbol)
        ticker = self._call(f"prix {symbol}", self._exchange.fetch_ticker, market["symbol"])
        bid, ask = ticker.get("bid"), ticker.get("ask")
        if not bid or not ask:                     # certains exchanges (Binance futures) : carnet d'ordres
            book = self._call(f"carnet {symbol}", self._exchange.fetch_order_book, market["symbol"], ORDER_BOOK_DEPTH)
            if not book.get("bids") or not book.get("asks"):
                return None
            bid, ask = book["bids"][0][0], book["asks"][0][0]
        specs = self.get_symbol_specs(symbol)
        tick_size = specs.tick_size if specs else 0.0
        timestamp = ticker.get("timestamp")
        return Tick(
            symbol=symbol,
            bid=float(bid),
            ask=float(ask),
            last=float(ticker.get("last") or bid),
            spread_points=round((ask - bid) / tick_size) if tick_size else 0,
            time=datetime.fromtimestamp(timestamp / 1000, tz=timezone.utc) if timestamp else self.now(),
        )

    # ------------------------------------------------------------------ calculs (contrats linéaires)

    def calc_profit(self, side: str, symbol: str, volume: float, price_open: float, price_close: float) -> float | None:
        contract = float(self._market(symbol).get("contractSize") or 1.0)
        direction = 1 if side.upper() == "BUY" else -1
        return round((price_close - price_open) * volume * contract * direction, 8)

    def calc_margin(self, side: str, symbol: str, volume: float, price: float) -> float | None:
        contract = float(self._market(symbol).get("contractSize") or 1.0)
        return abs(volume * contract * price) / self.cfg.leverage

    # ------------------------------------------------------------------ compte & positions

    def _identity(self) -> dict[str, str]:
        """Champs d'identité normalisés comme ceux d'un compte MT5 (nom, broker, serveur, type)."""
        company = str(getattr(self._exchange, "name", "") or self.name.capitalize())
        if not self.cfg.has_credentials:
            return {"name": settings.instance.label or "Compte PAPER", "company": company,
                    "server": f"{self.name} · données publiques", "account_type": "paper"}
        return {"name": settings.instance.label or f"Compte API {company}", "company": company,
                "server": f"{self.name} · {self.cfg.environment}",
                "account_type": "real" if self.cfg.environment == "live" else "demo"}

    def get_balance(self) -> AccountInfo | None:
        quote = self.cfg.quote
        if not self.cfg.has_credentials:
            capital = self.cfg.paper_balance
            return AccountInfo(login=0, balance=capital, equity=capital, margin=0.0, margin_free=capital,
                               margin_level=0.0, profit=0.0, currency=quote, leverage=self.cfg.leverage,
                               **self._identity())
        try:
            self.ensure_connection()
            balance = self._call("lecture du solde", self._exchange.fetch_balance)
            floating = sum(p.profit for p in self._fetch_positions(bot_only=False))
        except BrokerError as exc:
            log.error("Solde %s indisponible : %s", self.name, exc)
            return None
        total = float((balance.get("total") or {}).get(quote) or 0.0)
        used = float((balance.get("used") or {}).get(quote) or 0.0)
        free = float((balance.get("free") or {}).get(quote) or 0.0)
        equity = total + floating
        return AccountInfo(login=0, balance=total, equity=equity, margin=used, margin_free=free,
                           margin_level=equity / used * 100 if used > 0 else 0.0, profit=floating,
                           currency=quote, leverage=self.cfg.leverage, **self._identity())

    @staticmethod
    def _position_ticket(exchange: str, symbol: str, side: str) -> int:
        """Identifiant stable d'une position (les exchanges n'en donnent pas en mode one-way)."""
        return zlib.crc32(f"{exchange}:{symbol}:{side}".encode()) & 0x7FFFFFFF

    def _protective_orders(self, market_symbol: str) -> list[dict[str, Any]]:
        """Ordres conditionnels reduce-only en attente (SL / TP) sur un symbole."""
        orders: dict[str, dict[str, Any]] = {}
        for params in ({}, {"trigger": True}):
            try:
                for order in self._call(f"ordres en attente {market_symbol}", self._exchange.fetch_open_orders,
                                        market_symbol, None, None, params):
                    orders[str(order.get("id"))] = order
            except BrokerError:
                continue                           # variante non prise en charge par cet exchange
        return [o for o in orders.values() if o.get("reduceOnly") and
                (o.get("stopLossPrice") or o.get("takeProfitPrice") or o.get("triggerPrice") or o.get("stopPrice"))]

    def _stops_for(self, market_symbol: str, side: str, mark: float) -> tuple[float, float]:
        sl = tp = 0.0
        for order in self._protective_orders(market_symbol):
            if order.get("stopLossPrice"):
                sl = float(order["stopLossPrice"])
            elif order.get("takeProfitPrice"):
                tp = float(order["takeProfitPrice"])
            else:
                trigger = float(order.get("triggerPrice") or order.get("stopPrice"))
                below = trigger < mark
                if (side == "BUY") == below:
                    sl = trigger
                else:
                    tp = trigger
        return sl, tp

    def _fetch_positions(self, bot_only: bool = True) -> list[Position]:
        if not self.cfg.has_credentials:
            return []                              # PAPER sans compte : rien n'est ouvert chez l'exchange
        self.ensure_connection()
        symbols = [m["symbol"] for m in self._markets.values()] if bot_only else None
        raw = self._call("lecture des positions", self._exchange.fetch_positions, symbols)
        positions = []
        for p in raw:
            contracts = float(p.get("contracts") or 0.0)
            if contracts <= 0:
                continue
            market_symbol = p.get("symbol", "")
            bot_symbol = self._bot_symbol(market_symbol)
            if bot_only and bot_symbol is None:
                continue
            side = "BUY" if p.get("side") == "long" else "SELL"
            entry = float(p.get("entryPrice") or 0.0)
            mark = float(p.get("markPrice") or entry)
            sl, tp = float(p.get("stopLossPrice") or 0.0), float(p.get("takeProfitPrice") or 0.0)
            if not sl or not tp:
                found_sl, found_tp = self._stops_for(market_symbol, side, mark)
                sl, tp = sl or found_sl, tp or found_tp
            symbol = bot_symbol or market_symbol
            positions.append(Position(
                ticket=self._position_ticket(self.name, symbol, side),
                symbol=symbol,
                type=POSITION_TYPE_BUY if side == "BUY" else POSITION_TYPE_SELL,
                volume=contracts,
                price_open=entry,
                price_current=mark,
                sl=sl,
                tp=tp,
                profit=float(p.get("unrealizedPnl") or 0.0),
                swap=0.0,
                time=int((p.get("timestamp") or time.time() * 1000) / 1000),
                magic=settings.trading.magic_number if bot_symbol else 0,
                comment=self.name,
            ))
        return positions

    def get_positions(self, symbol: str | None = None, bot_only: bool = True) -> list[Position]:
        """
        Positions ouvertes. Un exchange ne marque pas les positions du bot : toutes celles des
        symboles de TRADING_SYMBOLS sont considérées comme les siennes (compte / sous-compte dédié conseillé).
        Lève BrokerError si l'exchange ne répond pas : le risk manager refuse alors tout nouvel ordre.
        """
        positions = self._fetch_positions(bot_only)
        return [p for p in positions if p.symbol == symbol] if symbol else positions

    def get_closed_deals(self, since: datetime, bot_only: bool = False) -> list[ClosedDeal]:
        """P/L réalisé depuis `since` : historique des positions, sinon trades (Binance : realizedPnl)."""
        if not self.cfg.has_credentials:
            return []
        self.ensure_connection()
        since_ms = int(since.timestamp() * 1000)
        magic = settings.trading.magic_number
        deals: list[ClosedDeal] = []
        if self._exchange.has.get("fetchPositionsHistory"):
            symbols = [m["symbol"] for m in self._markets.values()]
            for p in self._call("historique des positions", self._exchange.fetch_positions_history, symbols, since_ms):
                bot_symbol = self._bot_symbol(p.get("symbol", ""))
                deals.append(ClosedDeal(
                    ticket=zlib.crc32(str(p.get("id") or p.get("timestamp")).encode()) & 0x7FFFFFFF,
                    symbol=bot_symbol or p.get("symbol", ""),
                    time=datetime.fromtimestamp((p.get("lastUpdateTimestamp") or p.get("timestamp") or since_ms) / 1000,
                                                tz=timezone.utc),
                    net_profit=float(p.get("realizedPnl") or 0.0),
                    magic=magic if bot_symbol else 0,
                ))
        elif self._exchange.has.get("fetchMyTrades"):
            for bot_symbol, market in self._markets.items():
                for trade in self._call(f"trades {bot_symbol}", self._exchange.fetch_my_trades, market["symbol"], since_ms):
                    realized = float((trade.get("info") or {}).get("realizedPnl") or 0.0)
                    fee = trade.get("fee") or {}
                    fee_cost = float(fee.get("cost") or 0.0) if fee.get("currency") == self.cfg.quote else 0.0
                    deals.append(ClosedDeal(
                        ticket=zlib.crc32(str(trade.get("id")).encode()) & 0x7FFFFFFF,
                        symbol=bot_symbol,
                        time=datetime.fromtimestamp(trade["timestamp"] / 1000, tz=timezone.utc),
                        net_profit=realized - fee_cost,
                        # trade d'ouverture (aucun P/L réalisé) : ses frais comptent dans la perte du jour,
                        # mais pas dans la série de pertes consécutives
                        magic=magic if realized else 0,
                    ))
        else:
            raise BrokerError(f"{self.name} : P/L réalisé indisponible (ni historique de positions ni trades)")
        deals = [d for d in deals if not bot_only or d.magic == magic]
        return sorted(deals, key=lambda d: d.time)

    # ------------------------------------------------------------------ ordres

    def _ensure_leverage(self, market: dict[str, Any]) -> None:
        if market["symbol"] in self._leverage_done or not self._exchange.has.get("setLeverage"):
            return
        try:
            self._call(f"levier {market['symbol']}", self._exchange.set_leverage, self.cfg.leverage, market["symbol"])
        except BrokerError as exc:                 # ex : « leverage not modified » (déjà au bon niveau)
            log.info("Levier %s non modifié : %s", market["symbol"], exc)
        self._leverage_done.add(market["symbol"])

    def _cancel_protective(self, market_symbol: str) -> None:
        """Annule les SL/TP restants d'un symbole (après clôture, ou orphelins d'une position déjà fermée)."""
        for order in self._protective_orders(market_symbol):
            try:
                params = {"trigger": True} if order.get("triggerPrice") or order.get("stopPrice") else {}
                self._call(f"annulation ordre {order.get('id')}", self._exchange.cancel_order,
                           order["id"], market_symbol, params)
            except BrokerError as exc:
                log.warning("Ordre %s non annulé : %s", order.get("id"), exc)

    def place_order(self, symbol: str, side: str, volume: float, sl: float | None = None,
                    tp: float | None = None, comment: str = "Bot Order") -> OrderResult:
        side = side.strip().upper()
        if side not in ("BUY", "SELL"):
            return OrderResult(False, symbol, side, volume, message="type d'ordre invalide")
        try:
            self.ensure_connection()
            market = self._market(symbol)
            specs = self.get_symbol_specs(symbol)
            tick = self.get_current_tick(symbol)
            if specs is None or tick is None:
                raise ValueError("spécifications ou prix indisponibles")
            if not specs.trade_allowed:
                raise ValueError(f"trading désactivé sur {symbol}")
            if tick.spread_pct > settings.risk.max_spread_pct:
                raise ValueError(f"spread trop élevé ({tick.spread_pct:.3f}% > {settings.risk.max_spread_pct}%)")
            if sl is None:
                raise ValueError("Stop Loss obligatoire sur un exchange crypto")

            amount = _floor_to_step(min(volume, specs.volume_max), specs.volume_step)
            if amount < specs.volume_min:
                raise ValueError(f"volume {volume} < minimum {specs.volume_min} contrat(s)")
            price = tick.ask if side == "BUY" else tick.bid
            notional = amount * specs.contract_size * price
            if specs.min_notional and notional < specs.min_notional:
                raise ValueError(f"valeur de l'ordre {notional:.2f} {self.cfg.quote} < minimum {specs.min_notional}")
            sl = _round_to_step(sl, specs.tick_size)
            tp = _round_to_step(tp, specs.tick_size) if tp is not None else None
            if side == "BUY" and (sl >= tick.bid or (tp is not None and tp <= tick.bid)):
                raise ValueError(f"SL {sl} / TP {tp} incohérents avec un achat (Bid {tick.bid})")
            if side == "SELL" and (sl <= tick.ask or (tp is not None and tp >= tick.ask)):
                raise ValueError(f"SL {sl} / TP {tp} incohérents avec une vente (Ask {tick.ask})")

            if is_dry_run():
                ticket = next(self._paper_tickets)
                log.info("[DRY RUN] %s %s %s @ %s | SL %s | TP %s | ticket fictif #%d | %s",
                         side, format_volume(amount, specs.volume_step), symbol, price, sl, tp, ticket, comment)
                return OrderResult(True, symbol, side, amount, price, sl, tp, ticket=ticket,
                                   message="simulation", dry_run=True)
            if not self.cfg.has_credentials:
                raise ValueError("mode LIVE impossible sans CCXT_API_KEY / CCXT_API_SECRET")
            return self._send_protected_order(symbol, market, side, amount, price, sl, tp, specs)

        except (BrokerError, ValueError) as exc:
            log.error("Ordre %s %s %s refusé : %s", side, volume, symbol, exc)
            return OrderResult(False, symbol, side, volume, sl=sl, tp=tp, message=str(exc))

    def _send_protected_order(self, symbol: str, market: dict[str, Any], side: str, amount: float,
                              price: float, sl: float, tp: float | None, specs: SymbolSpecs) -> OrderResult:
        """Entrée au marché, puis SL (obligatoire) et TP en ordres reduce-only."""
        market_symbol = market["symbol"]
        exit_side = "sell" if side == "BUY" else "buy"
        if not self._fetch_positions_for(symbol):
            self._cancel_protective(market_symbol)          # SL/TP orphelins d'une position déjà fermée
        self._ensure_leverage(market)

        log.info("Envoi ordre %s %s %s @ ~%s sur %s", side, format_volume(amount, specs.volume_step),
                 symbol, price, self.name)
        order = self._call(f"ordre {side} {symbol}", self._exchange.create_order,
                           market_symbol, "market", side.lower(), amount)
        filled = float(order.get("filled") or amount)
        fill_price = float(order.get("average") or order.get("price") or price)
        ticket = self._position_ticket(self.name, symbol, side)

        try:
            self._call(f"stop loss {symbol}", self._exchange.create_order, market_symbol, "market", exit_side,
                       filled, None, {"stopLossPrice": sl, "reduceOnly": True})
        except BrokerError as exc:
            log.critical("[%s] SL impossible à poser (%s) : clôture immédiate de la position", symbol, exc)
            try:
                self._call(f"clôture d'urgence {symbol}", self._exchange.create_order, market_symbol, "market",
                           exit_side, filled, None, {"reduceOnly": True})
                message = f"SL refusé par l'exchange ({exc}) : position refermée immédiatement"
            except BrokerError as close_exc:
                message = (f"SL refusé ET clôture d'urgence échouée ({close_exc}) : "
                           f"FERMER LA POSITION {symbol} MANUELLEMENT")
            self._report(f"protection {symbol}", message)
            return OrderResult(False, symbol, side, filled, fill_price, sl, tp, ticket=ticket,
                               order_id=str(order.get("id", "")), message=message)

        message = "exécuté"
        if tp is not None:
            try:
                self._call(f"take profit {symbol}", self._exchange.create_order, market_symbol, "market", exit_side,
                           filled, None, {"takeProfitPrice": tp, "reduceOnly": True})
            except BrokerError as exc:              # la position reste protégée par son SL
                message = f"exécuté, TP non posé ({exc})"
                log.error("[%s] TP non posé : %s", symbol, exc)
        log.info("Ordre exécuté sur %s : %s %s %s @ %s | SL %s | TP %s", self.name, side,
                 format_volume(filled, specs.volume_step), symbol, fill_price, sl, tp)
        return OrderResult(True, symbol, side, filled, fill_price, sl, tp, ticket=ticket,
                           order_id=str(order.get("id", "")), message=message)

    def _fetch_positions_for(self, symbol: str) -> list[Position]:
        return [p for p in self._fetch_positions(bot_only=True) if p.symbol == symbol]

    def close_order(self, ticket: int, comment: str = "Bot Close", force: bool = False) -> OrderResult:
        try:
            if ticket < 0 and is_dry_run():
                log.info("[DRY RUN] Clôture simulée de la position fictive #%d", ticket)
                return OrderResult(True, "", "CLOSE", 0.0, ticket=ticket, message="simulation", dry_run=True)
            position = next((p for p in self._fetch_positions(bot_only=False) if p.ticket == ticket), None)
            if position is None:
                raise ValueError(f"position #{ticket} introuvable")
            exit_side = "SELL" if position.type == POSITION_TYPE_BUY else "BUY"
            if is_dry_run() and not force:
                log.info("[DRY RUN] Clôture simulée #%d : %s %s %s (P/L actuel %.2f)",
                         ticket, exit_side, position.volume, position.symbol, position.profit)
                return OrderResult(True, position.symbol, exit_side, position.volume, position.price_current,
                                   ticket=ticket, message="simulation", dry_run=True)
            market_symbol = self._resolve(position.symbol)["symbol"] if position.magic else position.symbol
            log.info("Clôture de la position #%d (%s %s, P/L %.2f) : %s",
                     ticket, position.symbol, position.volume, position.profit, comment)
            order = self._call(f"clôture {position.symbol}", self._exchange.create_order, market_symbol, "market",
                               exit_side.lower(), position.volume, None, {"reduceOnly": True})
            self._cancel_protective(market_symbol)
            return OrderResult(True, position.symbol, exit_side, position.volume,
                               float(order.get("average") or position.price_current), ticket=ticket,
                               order_id=str(order.get("id", "")), message="fermée")
        except (BrokerError, ValueError) as exc:
            log.error("Clôture de la position #%d refusée : %s", ticket, exc)
            return OrderResult(False, "", "CLOSE", 0.0, ticket=ticket, message=str(exc))
