"""Route en lecture seule /api/market/candles (MT5 simulé)."""

from datetime import datetime, timezone

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from brokers.mt5_client import MT5ConnectionError
from core.engine import TradingEngine
from tests.conftest import TEST_API_KEY
from tests.fakes import FakeBroker

AUTH = {"X-API-Key": TEST_API_KEY}


@pytest.fixture
def client(monkeypatch, eurusd_specs, eurusd_tick):
    calls = []

    def history(symbol, count, timeframe):
        calls.append((symbol, count, timeframe))
        index = pd.date_range("2026-10-28 10:00", periods=count, freq="15min", tz="UTC")
        return pd.DataFrame({"open": 1.1, "high": 1.102, "low": 1.099, "close": 1.101, "volume": 42,
                             "spread": 10, "real_volume": 0}, index=index)

    broker = FakeBroker()
    broker.get_historical_data = history
    broker.specs = {"EURUSD": eurusd_specs}
    broker.ticks = {"EURUSD": eurusd_tick}
    test_client = TestClient(create_app(TradingEngine(symbols=["EURUSD"], broker=broker)))
    test_client.broker = broker
    test_client.calls = calls
    return test_client


def test_candles(client):
    body = client.get("/api/market/candles?symbol=eurusd&timeframe=m5&count=3", headers=AUTH).json()
    assert body["symbol"] == "EURUSD" and body["timeframe"] == "M5" and body["digits"] == 5
    assert len(body["candles"]) == 3 and body["candles"][0]["time"] == int(
        datetime(2026, 10, 28, 10, 0, tzinfo=timezone.utc).timestamp())
    assert body["bid"] == 1.1 and body["ask"] == pytest.approx(1.1001)
    assert client.calls == [("EURUSD", 3, "M5")]


@pytest.mark.parametrize("query, code", [
    ("symbol=USDJPY", 404),                      # symbole non suivi par le bot
    ("symbol=EURUSD&timeframe=M7", 422),
    ("symbol=EURUSD&count=0", 422),
    ("symbol=EURUSD&count=5000", 422),
    ("timeframe=M15", 422),                      # symbole obligatoire
])
def test_candles_validation(client, query, code):
    assert client.get(f"/api/market/candles?{query}", headers=AUTH).status_code == code


def test_candles_requires_api_key(client):
    assert client.get("/api/market/candles?symbol=EURUSD").status_code == 401


def test_candles_mt5_errors(client, monkeypatch):
    monkeypatch.setattr(client.broker, "get_historical_data", lambda symbol, count, timeframe: pd.DataFrame())
    assert client.get("/api/market/candles?symbol=EURUSD", headers=AUTH).status_code == 503

    def unknown(symbol, count, timeframe):
        raise ValueError("Symbole inconnu chez le broker")

    monkeypatch.setattr(client.broker, "get_historical_data", unknown)
    assert client.get("/api/market/candles?symbol=EURUSD", headers=AUTH).status_code == 404

    def disconnected(symbol, count, timeframe):
        raise MT5ConnectionError("terminal fermé")

    monkeypatch.setattr(client.broker, "get_historical_data", disconnected)
    assert client.get("/api/market/candles?symbol=EURUSD", headers=AUTH).status_code == 503


def test_tick_unavailable_is_tolerated(client, monkeypatch):
    monkeypatch.setattr(client.broker, "get_current_tick", lambda symbol: None)
    monkeypatch.setattr(client.broker, "get_symbol_specs", lambda symbol: None)
    body = client.get("/api/market/candles?symbol=EURUSD", headers=AUTH).json()
    assert body["bid"] is None and body["digits"] == 5
