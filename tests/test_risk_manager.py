"""Taille de position et validation du risque (MT5 simulé)."""

from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from brokers.mt5_client import ClosedDeal, MT5ConnectionError
from config import settings
from risk import risk_manager as rm
from tests.conftest import fake_calc_profit


@pytest.fixture
def broker(monkeypatch, account, eurusd_specs, eurusd_tick):
    """État du broker simulé ; modifier `state` pour chaque scénario."""
    state = {"account": account, "specs": eurusd_specs, "tick": eurusd_tick,
             "positions": [], "deals": [], "margin": 100.0}
    monkeypatch.setattr(rm, "get_account_info", lambda: state["account"])
    monkeypatch.setattr(rm, "get_symbol_specs", lambda symbol: state["specs"])
    monkeypatch.setattr(rm, "get_current_tick", lambda symbol: state["tick"])
    monkeypatch.setattr(rm, "get_open_positions", lambda: state["positions"])
    monkeypatch.setattr(rm, "get_closed_deals", lambda since: state["deals"])
    monkeypatch.setattr(rm, "calc_profit", fake_calc_profit)
    monkeypatch.setattr(rm, "calc_margin", lambda side, symbol, volume, price: state["margin"])
    return state


# ----------------------------------------------------------------- taille de position

@pytest.mark.parametrize("volume, step, expected", [
    (0.379, 0.01, 0.37),
    (1.0, 0.1, 1.0),
    (0.3, 0.1, 0.3),
    (0.29999, 0.1, 0.2),
    (100 / (0.002 / 0.00001), 0.01, 0.5),     # 0.4999999999999996 : erreur flottante absorbée
])
def test_floor_to_step(volume, step, expected):
    assert rm._floor_to_step(volume, step) == expected


def test_position_size_risks_configured_percentage(broker):
    # 1 % de 10 000 = 100 ; SL à 200 ticks = 200 par lot -> 0.5 lot
    assert rm.calculate_position_size("EURUSD", 1.1000, 1.0980) == 0.5
    assert rm.calculate_position_size("EURUSD", 1.1000, 1.1020) == 0.5     # vente


def test_position_size_uses_lowest_of_balance_and_equity(broker, account):
    broker["account"] = replace(account, equity=5_000.0)
    assert rm.calculate_position_size("EURUSD", 1.1000, 1.0980) == 0.25


def test_position_size_capped_by_max_lot(broker):
    assert rm.calculate_position_size("EURUSD", 1.1000, 1.0999) == settings.risk.max_lot


def test_minimum_lot_exceeding_risk_returns_zero(broker):
    assert rm.calculate_position_size("EURUSD", 1.1, 0.1) == 0.0


def test_position_size_falls_back_on_tick_value(broker, monkeypatch):
    monkeypatch.setattr(rm, "calc_profit", lambda *args: None)
    assert rm.calculate_position_size("EURUSD", 1.1000, 1.0980) == 0.5


@pytest.mark.parametrize("entry, sl", [(1.1, 1.1), (0, 1.0), (1.1, -1), (float("nan"), 1.0), (None, 1.0), (True, 1.0)])
def test_invalid_prices_return_zero(broker, entry, sl):
    assert rm.calculate_position_size("EURUSD", entry, sl) == 0.0


def test_broker_failures_return_zero(broker, monkeypatch):
    broker["account"] = None
    assert rm.calculate_position_size("EURUSD", 1.1, 1.098) == 0.0

    def boom():
        raise MT5ConnectionError("terminal fermé")

    monkeypatch.setattr(rm, "get_account_info", boom)
    assert rm.calculate_position_size("EURUSD", 1.1, 1.098) == 0.0


# ----------------------------------------------------------------- validation avant ordre

VALID_ORDER = dict(symbol="EURUSD", action="BUY", volume=0.5, sl=1.0985, tp=1.1035)


def _validate(**overrides):
    return rm.validate_trade_risk(**{**VALID_ORDER, **overrides})


def test_valid_order_is_approved(broker):
    result = _validate()
    assert result["is_approved"] is True, result["reason"]
    assert result["failed_checks"] == []
    assert result["entry_price"] == 1.10010 and result["risk_amount"] == pytest.approx(80.0)


@pytest.mark.parametrize("overrides, fragment", [
    ({"sl": None}, "Stop Loss"),
    ({"sl": 1.2}, "SL"),                                       # SL du mauvais côté
    ({"tp": 1.0}, "TP"),
    ({"tp": 1.1015}, "rendement/risque"),
    ({"sl": 0.95}, "aberrant"),
    ({"volume": 0.555}, "pas"),
    ({"volume": 5.0}, "maximum"),
    ({"volume": 0.0}, "volume invalide"),
    ({"action": "HOLD"}, "action invalide"),
])
def test_invalid_order_parameters_are_rejected(broker, overrides, fragment):
    result = _validate(**overrides)
    assert result["is_approved"] is False
    assert fragment in result["reason"]


def test_spread_limit(broker, eurusd_tick):
    broker["tick"] = replace(eurusd_tick, ask=1.10050, spread_points=50)
    assert "spread" in _validate()["reason"]


def test_exposure_limits(broker):
    broker["positions"] = [SimpleNamespace(symbol="EURUSD")]
    assert "déjà ouverte" in _validate()["reason"]
    broker["positions"] = [SimpleNamespace(symbol=s) for s in ("GBPUSD", "USDJPY", "XAUUSD")]
    assert "positions ouvertes" in _validate()["reason"]


def test_daily_loss_limit(broker, account):
    now = datetime.now(timezone.utc)
    broker["deals"] = [ClosedDeal(1, "EURUSD", now, -400.0, magic=0)]
    broker["account"] = replace(account, balance=9_600.0, equity=9_600.0)    # -4 % > limite 3 %
    assert "perte journalière" in _validate()["reason"]


def test_consecutive_losses_limit(broker):
    now = datetime.now(timezone.utc)
    magic = settings.trading.magic_number
    broker["deals"] = [ClosedDeal(i, "EURUSD", now, -10.0, magic) for i in range(3)]
    assert "pertes consécutives" in _validate()["reason"]
    broker["deals"].append(ClosedDeal(9, "EURUSD", now, +5.0, magic))       # série interrompue
    assert _validate()["is_approved"] is True


def test_risk_and_margin_limits(broker):
    assert "perte au SL" in _validate(volume=1.0)["reason"]                  # 160 > budget 100
    broker["margin"] = 9_500.0
    assert "marge requise" in _validate()["reason"]
    broker["margin"] = None
    assert "incalculable" in _validate()["reason"]


def test_all_failures_are_reported_together(broker, eurusd_tick):
    broker["tick"] = replace(eurusd_tick, ask=1.10050, spread_points=50)
    broker["positions"] = [SimpleNamespace(symbol="EURUSD")]
    assert len(_validate()["failed_checks"]) >= 2


def test_missing_broker_data_rejects(broker, account):
    broker["tick"] = None
    assert "prix indisponible" in _validate()["reason"]
    broker["specs"] = None
    assert "symbole" in _validate()["reason"]
    broker["account"] = replace(account, equity=0.0)
    assert "capital insuffisant" in _validate()["reason"]


def test_exceptions_are_turned_into_rejections(broker, monkeypatch):
    def boom():
        raise MT5ConnectionError("terminal fermé")

    monkeypatch.setattr(rm, "get_account_info", boom)
    result = _validate()
    assert result["is_approved"] is False and "erreur interne" in result["reason"]
