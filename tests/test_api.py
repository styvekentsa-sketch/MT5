"""API FastAPI : authentification, CORS, monitoring et contrôle (MT5 simulé)."""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from api import dependencies
from api.main import create_app
from api.routes import control, monitoring
from brokers import mt5_client
from brokers.mt5_client import MT5ConnectionError, OrderResult
from config import settings
from core.engine import TradingEngine
from sentiment.news_scraper import NewsArticle
from tests.conftest import TEST_API_KEY

AUTH = {"X-API-Key": TEST_API_KEY}


def _position(ticket=1001, magic=None, sl=0.0):
    return SimpleNamespace(ticket=ticket, symbol="EURUSD", type=0, volume=0.5, price_open=1.1, price_current=1.101,
                           sl=sl, tp=1.104, profit=50.0, swap=-1.0, time=1_761_600_000,
                           magic=settings.trading.magic_number if magic is None else magic, comment="Bot BUY")


@pytest.fixture
def engine() -> TradingEngine:
    return TradingEngine(symbols=["EURUSD"])


@pytest.fixture
def client(engine, monkeypatch):
    monkeypatch.setattr(dependencies, "is_connected", lambda: True)
    yield TestClient(create_app(engine))
    mt5_client.set_dry_run(True)                             # ne jamais laisser le mode LIVE actif


# ----------------------------------------------------------------- sécurité

def test_health_is_public(client):
    assert client.get("/health").json() == {"status": "ok"}


@pytest.mark.parametrize("headers", [{}, {"X-API-Key": "mauvaise-cle"}, {"X-API-Key": ""}])
def test_api_routes_require_valid_key(client, headers):
    assert client.get("/api/status", headers=headers).status_code == 401
    assert client.post("/api/control/kill-switch", headers=headers).status_code == 401


def test_cors_only_allows_configured_origins(client):
    preflight = {"Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "x-api-key"}
    allowed = client.options("/api/status", headers={"Origin": "http://localhost:5173", **preflight})
    assert allowed.headers.get("access-control-allow-origin") == "http://localhost:5173"
    denied = client.options("/api/status", headers={"Origin": "https://evil.example", **preflight})
    assert "access-control-allow-origin" not in denied.headers


# ----------------------------------------------------------------- monitoring

def test_status(client):
    body = client.get("/api/status", headers=AUTH).json()
    assert body["state"] == "stopped" and body["mode"] == "PAPER" and body["mt5_connected"] is True
    assert body["symbols"] == ["EURUSD"]


def test_account_daily_pl(client, monkeypatch, account):
    monkeypatch.setattr(monitoring, "get_account_info", lambda: account.__class__(**{**account.__dict__, "profit": -20.0}))
    monkeypatch.setattr(monitoring, "get_closed_deals",
                        lambda since: [SimpleNamespace(net_profit=120.0)])
    body = client.get("/api/account", headers=AUTH).json()
    assert body["realized_pl_today"] == 120.0 and body["daily_pl"] == 100.0
    assert body["daily_pl_pct"] == pytest.approx(100 / (10_000 - 120) * 100, abs=1e-3)


def test_account_unavailable_returns_503(client, monkeypatch):
    monkeypatch.setattr(monitoring, "get_account_info", lambda: None)
    assert client.get("/api/account", headers=AUTH).status_code == 503

    def boom():
        raise MT5ConnectionError("terminal fermé")

    monkeypatch.setattr(monitoring, "get_account_info", boom)
    response = client.get("/api/account", headers=AUTH)
    assert response.status_code == 503 and "MT5" in response.json()["detail"]


def test_positions(client, monkeypatch):
    monkeypatch.setattr(monitoring, "get_open_positions", lambda bot_only: [_position(), _position(1002, magic=0)])
    body = client.get("/api/positions", headers=AUTH).json()
    assert [p["ticket"] for p in body] == [1001, 1002]
    assert body[0]["type"] == "BUY" and body[0]["sl"] is None and body[0]["is_bot"] is True
    assert body[1]["is_bot"] is False


def test_sentiment(client, monkeypatch):
    articles = [NewsArticle("Euro surges", "FX", "https://example.com", datetime.now(timezone.utc), "")]
    monkeypatch.setattr(monitoring, "get_recent_news", lambda: articles)
    body = client.get("/api/sentiment?limit=5", headers=AUTH).json()
    assert body["article_count"] == 1 and body["articles"][0]["label"] in ("bullish", "bearish", "neutral")
    assert set(body["by_symbol"]) == set(settings.trading.symbols)
    assert client.get("/api/sentiment?limit=0", headers=AUTH).status_code == 422


# ----------------------------------------------------------------- contrôle

def test_pause_and_start(client, engine):
    assert client.post("/api/control/stop", headers=AUTH).json()["success"] is True
    assert engine.is_paused
    response = client.post("/api/control/start", headers=AUTH).json()
    assert response["success"] is False                      # boucle non lancée : reprise impossible


def test_switching_to_live_requires_confirmation(client):
    assert client.post("/api/control/mode", headers=AUTH, json={"dry_run": False}).status_code == 400
    assert mt5_client.is_dry_run() is True
    response = client.post("/api/control/mode", headers=AUTH, json={"dry_run": False, "confirm": "LIVE"})
    assert response.json()["mode"] == "LIVE" and mt5_client.is_dry_run() is False
    assert client.post("/api/control/mode", headers=AUTH, json={"dry_run": True}).json()["mode"] == "PAPER"


def test_kill_switch_pauses_and_force_closes(client, engine, monkeypatch):
    closed = []
    monkeypatch.setattr(control, "get_open_positions", lambda bot_only: [_position(1001), _position(1002)])

    def close(ticket, comment="", force=False):
        closed.append((ticket, force))
        return OrderResult(ticket == 1001, "EURUSD", "CLOSE", 0.5, ticket=ticket,
                           message="" if ticket == 1001 else "marché fermé")

    monkeypatch.setattr(control, "close_position", close)
    body = client.post("/api/control/kill-switch", headers=AUTH, json={"include_manual": True}).json()
    assert body["closed"] == 1 and body["failed"] == 1 and body["engine_paused"] is True
    assert closed == [(1001, True), (1002, True)] and engine.is_paused


def test_close_single_position(client, monkeypatch):
    monkeypatch.setattr(control, "get_open_positions", lambda bot_only: [_position(1001)])
    monkeypatch.setattr(control, "close_position",
                        lambda ticket, comment="", force=False: OrderResult(force, "EURUSD", "CLOSE", 0.5, ticket=ticket))
    assert client.post("/api/control/positions/1001/close", headers=AUTH).json()["success"] is True
    assert client.post("/api/control/positions/9999/close", headers=AUTH).status_code == 404
    assert client.post("/api/control/positions/abc/close", headers=AUTH).status_code == 422
