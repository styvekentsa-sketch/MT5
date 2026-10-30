"""Architecture multi-brokers : adaptateur ccxt (exchange simulé), MT5 (suffixes), risque par marché, comptes."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

import ccxt
import pytest

from brokers import base as broker_base
from brokers import mt5_client
from brokers.ccxt_broker import CCXTBroker
from brokers.errors import BrokerConnectionError, clear_errors, recent_errors
from brokers.models import AccountInfo, Tick, format_volume
from brokers.mt5_broker import MT5Broker
from config import settings
from config.accounts import load_accounts, validate_accounts, worker_env
from config.broker import CCXTConfig
from config.env import ConfigError
from risk import risk_manager as rm
from tests.fakes import FakeBroker

BTC = "BTC/USDT:USDT"


def _config(**changes) -> CCXTConfig:
    base = CCXTConfig(exchange="bybit", api_key="", api_secret="", api_password="", environment="demo",
                      market_type="swap", quote="USDT", leverage=5, paper_balance=10_000.0, timeout_ms=1000)
    return replace(base, **changes)


class FakeExchange:
    """Sous-ensemble de l'API synchrone ccxt utilisée par CCXTBroker."""

    precisionMode = ccxt.TICK_SIZE
    timeframes = {"1m": "1", "15m": "15", "1h": "60"}

    def __init__(self) -> None:
        self.markets = {
            "BTC/USDT": {"symbol": "BTC/USDT", "base": "BTC", "quote": "USDT", "type": "spot", "linear": None,
                         "settle": None, "active": True, "precision": {"amount": 0.00001, "price": 0.01},
                         "limits": {"amount": {"min": 0.00001}, "cost": {"min": 5}}},
            BTC: {"symbol": BTC, "base": "BTC", "quote": "USDT", "type": "swap", "linear": True, "settle": "USDT",
                  "active": True, "contractSize": 1.0, "precision": {"amount": 0.001, "price": 0.1},
                  "limits": {"amount": {"min": 0.001, "max": 100.0}, "cost": {"min": 5.0}}},
        }
        self.has = {"setLeverage": True, "fetchPositionsHistory": True}
        self.orders: list[dict] = []
        self.positions: list[dict] = []
        self.open_orders: list[dict] = []
        self.fail: dict[str, Exception] = {}
        self.bid, self.ask = 80_000.0, 80_000.1

    def _maybe_fail(self, name: str) -> None:
        if name in self.fail:
            raise self.fail[name]

    def load_markets(self, reload=False):
        self._maybe_fail("load_markets")
        return self.markets

    def fetch_balance(self):
        self._maybe_fail("fetch_balance")
        return {"total": {"USDT": 1_000.0}, "free": {"USDT": 900.0}, "used": {"USDT": 100.0}}

    def fetch_ticker(self, symbol):
        self._maybe_fail("fetch_ticker")
        return {"bid": None, "ask": None, "last": self.bid, "timestamp": 1_761_600_000_000}

    def fetch_order_book(self, symbol, limit=None):
        return {"bids": [[self.bid, 1.0]], "asks": [[self.ask, 1.0]]}

    def fetch_ohlcv(self, symbol, timeframe, limit=None):
        return [[1_761_600_000_000 + i * 900_000, 1.0, 2.0, 0.5, 1.5, 10.0] for i in range(limit)]

    def fetch_positions(self, symbols=None):
        return self.positions

    def fetch_open_orders(self, symbol, since=None, limit=None, params=None):
        return self.open_orders if not params else []

    def fetch_positions_history(self, symbols=None, since=None):
        return [{"id": "p1", "symbol": BTC, "timestamp": since, "realizedPnl": -12.5}]

    def set_leverage(self, leverage, symbol):
        self.orders.append({"leverage": leverage, "symbol": symbol})

    def create_order(self, symbol, type, side, amount, price=None, params=None):
        params = params or {}
        if "stopLossPrice" in params:
            self._maybe_fail("stop_loss")
        self.orders.append({"symbol": symbol, "side": side, "amount": amount, **params})
        return {"id": str(len(self.orders)), "filled": amount, "average": self.ask if side == "buy" else self.bid}

    def cancel_order(self, order_id, symbol, params=None):
        self.orders.append({"cancel": order_id})


@pytest.fixture
def exchange() -> FakeExchange:
    return FakeExchange()


@pytest.fixture
def paper_broker(exchange) -> CCXTBroker:
    broker = CCXTBroker(_config(), exchange=exchange, connect_attempts=1, symbols=("BTCUSDT",))
    assert broker.connect()
    return broker


@pytest.fixture
def live_broker(exchange):
    broker = CCXTBroker(_config(api_key="k", api_secret="s"), exchange=exchange, connect_attempts=1,
                        symbols=("BTCUSDT",))
    assert broker.connect()
    broker_base.set_dry_run(False)
    yield broker
    broker_base.set_dry_run(True)


# ----------------------------------------------------------------- ccxt : marchés et données

def test_symbol_resolves_to_linear_perpetual_and_specs(paper_broker):
    specs = paper_broker.get_symbol_specs("BTCUSDT")
    assert specs.market == "crypto" and specs.volume_unit == "contrat(s)"
    assert specs.volume_step == 0.001 and specs.volume_min == 0.001 and specs.volume_max == 100.0
    assert specs.tick_size == 0.1 and specs.digits == 1 and specs.min_notional == 5.0
    assert paper_broker.is_connected() and "données publiques" in paper_broker.description


def test_unknown_symbol_refuses_connection(exchange):
    broker = CCXTBroker(_config(), exchange=exchange, connect_attempts=1, symbols=("DOGEUSDT",))
    assert broker.connect() is False and not broker.is_connected()


def test_tick_falls_back_to_order_book_and_bars_are_normalised(paper_broker):
    tick = paper_broker.get_current_tick("BTCUSDT")
    assert (tick.bid, tick.ask, tick.spread_points) == (80_000.0, 80_000.1, 1)
    bars = paper_broker.get_historical_data("BTCUSDT", count=3, timeframe="M15")
    assert list(bars.columns) == mt5_client.OHLCV_COLUMNS and len(bars) == 3 and str(bars.index.tz) == "UTC"
    with pytest.raises(ValueError):
        paper_broker.get_historical_data("BTCUSDT", count=3, timeframe="H4")     # absent de cet exchange


def test_linear_profit_and_margin(paper_broker):
    assert paper_broker.calc_profit("BUY", "BTCUSDT", 0.5, 80_000, 79_000) == -500.0
    assert paper_broker.calc_profit("SELL", "BTCUSDT", 0.5, 80_000, 79_000) == 500.0
    assert paper_broker.calc_margin("BUY", "BTCUSDT", 0.5, 80_000) == 8_000.0          # levier x5


# ----------------------------------------------------------------- ccxt : ordres

def test_paper_order_is_simulated_and_rounded_down(paper_broker, exchange):
    result = paper_broker.place_order("BTCUSDT", "BUY", 0.12345, sl=79_000.04, tp=82_000)
    assert result.success and result.dry_run and result.ticket < 0
    assert result.volume == 0.123 and result.sl == 79_000.0
    assert exchange.orders == []                                            # rien n'est envoyé


@pytest.mark.parametrize("kwargs, message", [
    ({"volume": 0.0001, "sl": 79_000}, "minimum"),
    ({"volume": 0.01, "sl": None}, "Stop Loss obligatoire"),
    ({"volume": 0.01, "sl": 81_000}, "incohérents"),
])
def test_invalid_orders_are_refused(paper_broker, kwargs, message):
    result = paper_broker.place_order("BTCUSDT", "BUY", **kwargs)
    assert not result.success and message in result.message


def test_live_without_api_key_is_refused(paper_broker):
    broker_base.set_dry_run(False)
    try:
        result = paper_broker.place_order("BTCUSDT", "BUY", 0.01, sl=79_000)
    finally:
        broker_base.set_dry_run(True)
    assert not result.success and "CCXT_API_KEY" in result.message


def test_live_order_sets_leverage_then_entry_sl_and_tp(live_broker, exchange):
    result = live_broker.place_order("BTCUSDT", "SELL", 0.02, sl=81_000, tp=78_000)
    assert result.success and not result.dry_run and result.order_id
    leverage, entry, sl, tp = exchange.orders
    assert leverage == {"leverage": 5, "symbol": BTC}
    assert entry == {"symbol": BTC, "side": "sell", "amount": 0.02}
    assert sl == {"symbol": BTC, "side": "buy", "amount": 0.02, "stopLossPrice": 81_000.0, "reduceOnly": True}
    assert tp == {"symbol": BTC, "side": "buy", "amount": 0.02, "takeProfitPrice": 78_000.0, "reduceOnly": True}


def test_refused_stop_loss_closes_the_position_immediately(live_broker, exchange):
    exchange.fail["stop_loss"] = ccxt.InvalidOrder("trigger price invalid")
    result = live_broker.place_order("BTCUSDT", "BUY", 0.02, sl=79_000, tp=82_000)
    assert not result.success and "refermée" in result.message
    assert exchange.orders[-1] == {"symbol": BTC, "side": "sell", "amount": 0.02, "reduceOnly": True}


def test_positions_read_stops_from_reduce_only_orders(live_broker, exchange):
    exchange.positions = [{"symbol": BTC, "contracts": 0.02, "side": "long", "entryPrice": 80_000,
                           "markPrice": 80_500, "unrealizedPnl": 10.0, "timestamp": 1_761_600_000_000}]
    exchange.open_orders = [{"id": "a", "reduceOnly": True, "triggerPrice": 79_000},
                            {"id": "b", "reduceOnly": True, "triggerPrice": 82_000}]
    [position] = live_broker.get_positions()
    assert position.symbol == "BTCUSDT" and position.side == "BUY" and position.volume == 0.02
    assert (position.sl, position.tp) == (79_000.0, 82_000.0) and position.magic == settings.trading.magic_number
    account = live_broker.get_balance()
    assert account.balance == 1_000.0 and account.equity == 1_010.0 and account.margin_free == 900.0
    assert [d.net_profit for d in live_broker.get_closed_deals(live_broker.now())] == [-12.5]

    closed = live_broker.close_order(position.ticket, force=True)
    assert closed.success and {"symbol": BTC, "side": "sell", "amount": 0.02, "reduceOnly": True} in exchange.orders


# ----------------------------------------------------------------- erreurs centralisées et isolation

def test_network_error_is_journaled_and_connection_recovers(paper_broker, exchange):
    clear_errors()
    exchange.fail["fetch_ticker"] = ccxt.NetworkError("connexion réinitialisée")
    with pytest.raises(BrokerConnectionError):
        paper_broker.get_current_tick("BTCUSDT")
    assert not paper_broker.is_connected()
    assert recent_errors()[0].kind == "network" and recent_errors()[0].broker == "bybit"

    # la tentative d'ordre suivante échoue proprement (OrderResult), sans exception
    exchange.fail["load_markets"] = ccxt.NetworkError("toujours hors ligne")
    paper_broker._connect_attempts = 1
    assert not paper_broker.place_order("BTCUSDT", "BUY", 0.01, sl=79_000).success

    exchange.fail.clear()
    paper_broker.ensure_connection()                                        # reconnexion automatique
    assert paper_broker.is_connected() and paper_broker.get_current_tick("BTCUSDT") is not None
    clear_errors()


def test_failing_exchange_does_not_affect_another_broker(exchange):
    """Deux brokers indépendants : la panne de l'un laisse l'autre fonctionner."""
    down, up = FakeExchange(), FakeExchange()
    broken = CCXTBroker(_config(exchange="binanceusdm"), exchange=down, connect_attempts=1, symbols=("BTCUSDT",))
    healthy = CCXTBroker(_config(), exchange=up, connect_attempts=1, symbols=("BTCUSDT",))
    assert broken.connect() and healthy.connect()
    down.fail["fetch_ticker"] = ccxt.ExchangeNotAvailable("maintenance")
    with pytest.raises(BrokerConnectionError):
        broken.get_current_tick("BTCUSDT")
    assert healthy.is_connected() and healthy.get_current_tick("BTCUSDT").bid == 80_000.0


# ----------------------------------------------------------------- risque adapté au marché

@pytest.fixture
def crypto(paper_broker):
    return paper_broker


def test_crypto_size_uses_contracts_not_forex_lot_bounds(crypto):
    # 1 % de 10 000 USDT = 100 ; SL à 40 $ -> 2.5 BTC (au-delà de RISK_MAX_LOT=1, borne réservée au Forex)
    assert rm.calculate_position_size("BTCUSDT", 80_000, 79_960, broker=crypto) == 2.5
    # SL à 1 000 $ -> 0.1 BTC, arrondi au pas de 0.001
    assert rm.calculate_position_size("BTCUSDT", 80_000, 79_000, broker=crypto) == 0.1


def test_crypto_min_notional_blocks_too_small_orders(exchange):
    broker = CCXTBroker(_config(paper_balance=5.0), exchange=exchange, connect_attempts=1, symbols=("BTCUSDT",))
    assert broker.connect()
    # budget 0.05 USDT, SL à 1 000 $ -> 0.00005 BTC < minimum de 0.001 contrat
    assert rm.calculate_position_size("BTCUSDT", 80_000, 79_000, broker=broker) == 0.0
    exchange.markets[BTC]["limits"]["cost"]["min"] = 500.0
    broker._markets.clear()
    assert broker.connect()
    # SL à 10 $ -> 0.005 BTC (au-dessus du minimum) mais 400 USDT < valeur minimale de 500
    assert rm.calculate_position_size("BTCUSDT", 80_000, 79_990, broker=broker) == 0.0


def test_crypto_spread_is_checked_in_percent(crypto, exchange):
    check = rm.validate_trade_risk("BTCUSDT", "BUY", 0.1, sl=79_000, tp=82_000, broker=crypto)
    assert check["is_approved"], check["reason"]
    exchange.ask = 80_400.0                                                 # 0.5 % > 0.15 %
    check = rm.validate_trade_risk("BTCUSDT", "BUY", 0.1, sl=79_000, tp=82_000, broker=crypto)
    assert not check["is_approved"] and "spread" in check["reason"] and "%" in check["reason"]


def test_forex_still_bounded_by_lot_settings(eurusd_specs, eurusd_tick, account):
    fake = FakeBroker()
    fake.account, fake.specs, fake.ticks = account, {"EURUSD": eurusd_specs}, {"EURUSD": eurusd_tick}
    fake.profit_fn = lambda side, symbol, volume, o, c: (c - o if side == "BUY" else o - c) / 0.00001 * volume
    # 100 EUR de risque, SL à 1 pip -> 10 lots bruts, plafonnés à RISK_MAX_LOT
    assert rm.calculate_position_size("EURUSD", 1.1001, 1.1000, broker=fake) == settings.risk.max_lot


def test_volume_formatting_follows_step():
    assert format_volume(0.1, 0.001) == "0.100" and format_volume(0.5, 0.01) == "0.50"
    assert format_volume(3, 1) == "3" and format_volume(0.025, 0.025) == "0.025"


# ----------------------------------------------------------------- MT5 : suffixe de symboles (Exness)

def test_mt5_broker_translates_symbol_suffix(monkeypatch):
    seen = []
    monkeypatch.setattr(mt5_client, "get_current_tick", lambda symbol: seen.append(symbol) or Tick(
        symbol, 1.1, 1.1001, 0.0, 10, datetime.now(timezone.utc)))
    monkeypatch.setattr(mt5_client, "get_open_positions", lambda symbol=None, bot_only=True: [
        type("P", (), dict(ticket=7, symbol="EURUSDm", type=1, volume=0.1, price_open=1.1, price_current=1.09,
                           sl=1.12, tp=1.05, profit=3.0, swap=0.0, time=1, magic=1, comment=""))()])
    broker = MT5Broker(symbol_suffix="m")
    broker.get_current_tick("EURUSD")
    assert seen == ["EURUSDm"]
    [position] = broker.get_positions()
    assert position.symbol == "EURUSD" and position.side == "SELL"


# ----------------------------------------------------------------- comptes multi-plateformes

def test_accounts_mix_mt5_and_ccxt():
    env = {
        "MT5_ACCOUNTS": "main,bybit",
        "ACCOUNT_MAIN_LOGIN": "111", "ACCOUNT_MAIN_PASSWORD": "pw", "ACCOUNT_MAIN_SERVER": "Exness-MT5Trial",
        "ACCOUNT_MAIN_PATH": __file__, "ACCOUNT_MAIN_SYMBOL_SUFFIX": "m",
        "ACCOUNT_BYBIT_BROKER": "ccxt", "ACCOUNT_BYBIT_EXCHANGE": "Bybit", "ACCOUNT_BYBIT_API_KEY": "key",
        "ACCOUNT_BYBIT_API_SECRET": "secret", "ACCOUNT_BYBIT_TRADING_SYMBOLS": "BTCUSDT",
    }
    main, bybit = load_accounts(env)
    validate_accounts((main, bybit))
    assert (main.broker, bybit.broker, bybit.login, bybit.server) == ("mt5", "ccxt", 0, "bybit")

    worker = worker_env(bybit, {**env, "MT5_PASSWORD": "ne-doit-pas-fuiter"})
    assert worker["BROKER_TYPE"] == "ccxt" and worker["CCXT_EXCHANGE"] == "bybit"
    assert worker["CCXT_API_KEY"] == "key" and worker["TRADING_SYMBOLS"] == "BTCUSDT"
    assert not any(k.startswith("ACCOUNT_") for k in worker)
    assert worker_env(main, env)["MT5_SYMBOL_SUFFIX"] == "m" and worker_env(main, env)["BROKER_TYPE"] == "mt5"


def test_ccxt_account_requires_exchange_and_valid_broker():
    with pytest.raises(ConfigError, match="EXCHANGE"):
        load_accounts({"MT5_ACCOUNTS": "x", "ACCOUNT_X_BROKER": "ccxt"})
    with pytest.raises(ConfigError, match="BROKER invalide"):
        load_accounts({"MT5_ACCOUNTS": "x", "ACCOUNT_X_BROKER": "ib"})


@pytest.mark.parametrize("changes, message", [
    ({"market_type": "spot"}, "swap"),
    ({"environment": "prod"}, "CCXT_ENVIRONMENT"),
    ({"api_key": "k"}, "ensemble"),
    ({"leverage": 50}, "CCXT_LEVERAGE"),
])
def test_ccxt_config_validation(changes, message):
    with pytest.raises(ConfigError, match=message):
        _config(**changes).validate()


def test_paper_account_without_keys_uses_fictitious_capital(paper_broker):
    account = paper_broker.get_balance()
    assert isinstance(account, AccountInfo) and account.balance == 10_000.0 and account.currency == "USDT"
    assert paper_broker.get_positions() == [] and paper_broker.get_closed_deals(paper_broker.now()) == []


def test_ccxt_account_identity_is_normalised(paper_broker, live_broker):
    paper = paper_broker.get_balance()
    assert (paper.account_type, paper.name, paper.server) == ("paper", "Compte PAPER", "bybit · données publiques")
    live = live_broker.get_balance()
    assert live.account_type == "demo" and live.server == "bybit · demo" and live.name.startswith("Compte API")
