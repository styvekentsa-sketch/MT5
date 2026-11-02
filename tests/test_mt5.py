"""
Client MT5 sans terminal : module MetaTrader5 simulé (constantes réelles, fonctions factices).
Connexion, reconnexion, mode terminal actif, spécifications du contrat, diagnostic de tradabilité.
"""

from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

import numpy as np
import pytest

import MetaTrader5 as real_mt5
from brokers import mt5_client
from brokers.errors import BrokerConnectionError
from brokers.mt5_broker import MT5Broker
from config import settings
from risk import risk_manager as rm

def _now() -> float:
    return datetime.now(timezone.utc).timestamp()


SERVER_OFFSET = 2 * 3600                                     # serveur en UTC+2, comme MetaQuotes-Demo


def _symbol(**changes):
    base = dict(name="EURUSD", visible=True, trade_mode=real_mt5.SYMBOL_TRADE_MODE_FULL, description="Euro vs US Dollar",
                currency_profit="USD", trade_contract_size=100_000.0, volume_min=0.01, volume_max=500.0,
                volume_step=0.01, digits=5, point=0.00001, trade_tick_size=0.00001, trade_tick_value=1.0,
                trade_stops_level=0, trade_freeze_level=0, filling_mode=2)
    return SimpleNamespace(**{**base, **changes})


class FakeMT5(SimpleNamespace):
    """Sous-ensemble de l'API MetaTrader5 ; chaque attribut se modifie depuis le test."""

    def __init__(self):
        super().__init__(**{k: getattr(real_mt5, k) for k in dir(real_mt5) if k.isupper()})
        self.init_ok, self.error = True, (1, "Success")
        self.connected, self.account = True, SimpleNamespace(login=settings.mt5.account, server=settings.mt5.server,
                                                            balance=1000.0, equity=990.0, margin=50.0, margin_free=940.0,
                                                            margin_level=1980.0, profit=-10.0, currency="USD",
                                                            leverage=100, trade_allowed=True,
                                                            trade_mode=real_mt5.ACCOUNT_TRADE_MODE_DEMO, name="Test",
                                                            company="Broker")
        self.symbols = {"EURUSD": _symbol()}
        now = datetime.now(timezone.utc).timestamp()          # au moment du test, pas de l'import du module
        self.ticks = {"EURUSD": SimpleNamespace(bid=1.10000, ask=1.10010, last=0.0, time=int(now + SERVER_OFFSET),
                                                time_msc=int((now + SERVER_OFFSET) * 1000))}
        self.spreads = [10] * 60
        self.init_calls, self.shutdowns = [], 0

    def initialize(self, *args, **kwargs):
        self.init_calls.append(kwargs)
        return self.init_ok

    def shutdown(self):
        self.shutdowns += 1

    def last_error(self):
        return self.error

    def terminal_info(self):
        return SimpleNamespace(connected=self.connected, trade_allowed=True) if self.init_ok else None

    def account_info(self):
        return self.account

    def symbol_info(self, symbol):
        return self.symbols.get(symbol)

    def symbol_select(self, symbol, enable):
        return symbol in self.symbols

    def symbol_info_tick(self, symbol):
        return self.ticks.get(symbol)

    def copy_rates_from_pos(self, symbol, timeframe, start, count):
        return np.array([(0, 1.1, 1.1, 1.1, 1.1, 1, s, 0) for s in self.spreads[:count]],
                        dtype=[("time", "i8"), ("open", "f8"), ("high", "f8"), ("low", "f8"), ("close", "f8"),
                               ("tick_volume", "i8"), ("spread", "i4"), ("real_volume", "i8")])

    def positions_get(self, symbol=None, ticket=None):
        return ()


@pytest.fixture
def mt5(monkeypatch):
    fake = FakeMT5()
    monkeypatch.setattr(mt5_client, "mt5", fake)
    monkeypatch.setattr(mt5_client, "_connected", True)
    monkeypatch.setattr(mt5_client, "_session", None)
    monkeypatch.setattr(mt5_client, "_attached", None)
    monkeypatch.setattr(mt5_client, "_offset_cache", None)
    monkeypatch.setattr(mt5_client.time, "sleep", lambda s: None)
    return fake


# ----------------------------------------------------------------- session et reconnexion

def test_connect_with_explicit_credentials(mt5):
    assert mt5_client.connect_mt5(max_attempts=1)
    assert mt5.init_calls[-1]["login"] == settings.mt5.account and "password" in mt5.init_calls[-1]


def test_failed_initialize_returns_false_without_crashing(mt5):
    mt5.init_ok, mt5.error = False, (-10005, "IPC timeout")
    assert mt5_client.connect_mt5(max_attempts=2) is False
    assert len(mt5.init_calls) == 2 and mt5.shutdowns == 2                 # nettoyage après chaque échec


def test_wrong_account_in_terminal_is_rejected(mt5):
    mt5.account = replace_ns(mt5.account, login=999)
    assert mt5_client.connect_mt5(max_attempts=1) is False


def test_lost_session_triggers_reconnection_then_error(mt5, monkeypatch):
    mt5.connected = False                                                 # terminal coupé du serveur
    with pytest.raises(BrokerConnectionError):
        mt5_client.ensure_connection()
    mt5.connected = True
    mt5_client.ensure_connection()                                         # retour du serveur : reconnexion
    assert mt5_client.is_connected()


def test_broker_reports_connection_loss_without_raising_to_engine(mt5):
    broker = MT5Broker(symbol_suffix="")
    mt5.connected = False
    with pytest.raises(BrokerConnectionError):
        broker.ensure_connection()
    assert broker.connect() is False                                      # connect() ne lève jamais


def test_terminal_session_mode_attaches_to_open_account(mt5, monkeypatch):
    monkeypatch.setattr(mt5_client, "settings", replace(settings, mt5=replace(settings.mt5, account=0, password="")))
    mt5.account = replace_ns(mt5.account, login=4242, server="Broker-Live")
    assert mt5_client.connect_mt5(max_attempts=1)
    assert "login" not in mt5.init_calls[-1]                               # aucun identifiant envoyé
    assert mt5_client.active_credentials().login == 4242 and mt5_client.env_credentials().server == "Broker-Live"


def test_shutdown_closes_the_api(mt5):
    mt5_client.shutdown_mt5()
    assert mt5.shutdowns == 1 and mt5_client._connected is False


# ----------------------------------------------------------------- compte et positions

def test_account_state(mt5):
    account = mt5_client.get_account_info()
    assert (account.balance, account.equity, account.margin, account.margin_free, account.currency) == \
        (1000.0, 990.0, 50.0, 940.0, "USD")
    assert mt5_client.get_open_positions() == []


# ----------------------------------------------------------------- contrat et diagnostic

def test_symbol_specs_include_contract_size(mt5):
    specs = mt5_client.get_symbol_specs("EURUSD")
    assert specs.contract_size == 100_000 and specs.volume_step == 0.01 and specs.digits == 5


def test_healthy_symbol_is_tradable_with_full_contract(mt5):
    diag = mt5_client.diagnose_symbol("EURUSD", entry=1.10010, sl=1.09810)              # SL 20 pips
    assert diag.tradable and diag.failures == []
    assert (diag.contract_size, diag.volume_min, diag.volume_max, diag.volume_step) == (100_000, 0.01, 500.0, 0.01)
    assert (diag.digits, diag.tick_size, diag.spread_points, diag.median_spread_points_60m) == (5, 0.00001, 10, 10)
    assert diag.spread_to_sl_pct == pytest.approx(5.0) and abs(diag.tick_age_seconds) < 5


@pytest.mark.parametrize("change, code", [
    (lambda m: m.symbols.pop("EURUSD"), "unknown"),
    (lambda m: m.symbols.update(EURUSD=_symbol(trade_mode=real_mt5.SYMBOL_TRADE_MODE_DISABLED)), "disabled"),
    (lambda m: m.symbols.update(EURUSD=_symbol(trade_mode=real_mt5.SYMBOL_TRADE_MODE_CLOSEONLY)), "close_only"),
    (lambda m: m.ticks.pop("EURUSD"), "no_price"),
    (lambda m: m.ticks.update(EURUSD=replace_ns(m.ticks["EURUSD"], time=int(_now() + SERVER_OFFSET - 900))), "stale_tick"),
    (lambda m: m.ticks.update(EURUSD=replace_ns(m.ticks["EURUSD"], ask=1.10040)), "spread_max"),         # 40 pts > 30
    (lambda m: m.ticks.update(EURUSD=replace_ns(m.ticks["EURUSD"], ask=1.10025)), "spread_median"),      # 25 > 2 x 10
])
def test_symbol_rejections(mt5, change, code):
    change(mt5)
    mt5.ticks.setdefault("GBPUSD", SimpleNamespace(bid=1.3, ask=1.3001, last=0, time=int(_now() + SERVER_OFFSET),
                                                   time_msc=0))   # référence pour estimer l'heure du serveur
    diag = mt5_client.diagnose_symbol("EURUSD")
    assert not diag.tradable and code in [f.code for f in diag.failures], diag.failures


def test_spread_too_large_for_the_stop(mt5):
    diag = mt5_client.diagnose_symbol("EURUSD", entry=1.10010, sl=1.09960)              # SL 5 pips, spread 1 pip
    assert not diag.tradable and [f.code for f in diag.failures] == ["spread_sl"]
    assert diag.spread_to_sl_pct == pytest.approx(20.0)


def test_gold_global_cap(mt5):
    mt5.symbols["XAUUSD"] = _symbol(name="XAUUSD", digits=2, point=0.01, trade_tick_size=0.01, trade_contract_size=100)
    mt5.ticks["XAUUSD"] = SimpleNamespace(bid=2400.00, ask=2400.35, last=0, time=int(_now() + SERVER_OFFSET), time_msc=0)
    mt5.spreads = [35] * 60                                               # spread habituel, mais au-dessus du plafond
    diag = mt5_client.diagnose_symbol("XAUUSD")
    assert [f.code for f in diag.failures] == ["spread_max"] and diag.contract_size == 100


def test_configured_server_offset_wins(mt5, monkeypatch):
    monkeypatch.setattr(mt5_client, "settings", replace(settings, mt5=replace(settings.mt5, server_utc_offset_hours=3.0)))
    assert mt5_client.server_time_offset() == 3 * 3600


def test_diagnostic_never_raises_when_terminal_is_gone(mt5):
    mt5.connected, mt5.init_ok = False, False
    diag = mt5_client.diagnose_symbol("EURUSD")
    assert not diag.tradable and diag.failures[0].code == "unavailable"


# ----------------------------------------------------------------- intégration au risk manager

def test_risk_validation_uses_diagnostic_and_spread_to_stop(eurusd_specs, eurusd_tick, account):
    from tests.fakes import FakeBroker
    from models.schemas import DiagnosticFailure, SymbolDiagnostic

    broker = FakeBroker()
    broker.account, broker.specs, broker.ticks = account, {"EURUSD": eurusd_specs}, {"EURUSD": eurusd_tick}
    broker.profit_fn = lambda side, symbol, volume, o, c: (c - o if side == "BUY" else o - c) / 0.00001 * volume
    broker.margin_fn = lambda *a: 100.0
    ok = rm.validate_trade_risk("EURUSD", "BUY", 0.1, sl=1.0980, tp=1.1050, broker=broker)
    assert ok["is_approved"], ok["reason"]

    tight = rm.validate_trade_risk("EURUSD", "BUY", 0.1, sl=1.0996, tp=1.1010, broker=broker)   # SL 5 pips
    assert not tight["is_approved"] and "distance entrée-stop" in tight["reason"]

    broker.diagnose_symbol = lambda symbol, entry=None, sl=None: SymbolDiagnostic(
        symbol=symbol, found=True, failures=[DiagnosticFailure(code="stale_tick", message="prix figé depuis 900 s")])
    stale = rm.validate_trade_risk("EURUSD", "BUY", 0.1, sl=1.0980, tp=1.1050, broker=broker)
    assert not stale["is_approved"] and "prix figé" in stale["reason"]


def replace_ns(namespace, **changes):
    return SimpleNamespace(**{**vars(namespace), **changes})
