"""Routes /api/data-feeds et webhooks entrants."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from api.routes import monitoring, webhooks
from config import settings
from core.engine import TradingEngine
from sentiment.news_scraper import EconomicEvent, FeedStatus, NewsArticle
from tests.conftest import TEST_API_KEY, TEST_WEBHOOK_SECRET

AUTH = {"X-API-Key": TEST_API_KEY}
NOW = datetime.now(timezone.utc)


@pytest.fixture
def client():
    webhooks.webhook_log.clear()
    yield TestClient(create_app(TradingEngine(symbols=["EURUSD"])))
    webhooks.webhook_log.clear()


# ----------------------------------------------------------------- webhooks

def test_webhook_accepted_with_token_and_secret_masked(client):
    payload = {"ticker": "eurusd", "action": "buy", "price": 1.1, "passphrase": "ne-doit-pas-fuiter"}
    response = client.post(f"/api/webhooks/tradingview?token={TEST_WEBHOOK_SECRET}", json=payload)
    assert response.status_code == 202 and response.json()["accepted"] is True

    log = client.get("/api/webhooks/events", headers=AUTH).json()
    event = log["events"][0]
    assert log["enabled"] is True and event["source"] == "tradingview"
    assert event["symbol"] == "EURUSD" and event["action"] == "BUY" and event["accepted"] is True
    assert event["payload"]["passphrase"] == "***" and event["payload"]["price"] == 1.1


def test_webhook_accepts_secret_in_json_body(client):
    body = {"symbol": "XAUUSD", "side": "sell", "secret": TEST_WEBHOOK_SECRET}
    assert client.post("/api/webhooks/custom-bot", json=body).status_code == 202


def test_webhook_rejected_without_valid_secret_is_still_logged(client):
    assert client.post("/api/webhooks/tradingview", json={"action": "buy"}).status_code == 401
    assert client.post("/api/webhooks/tradingview?token=faux", json={"action": "buy"}).status_code == 401
    events = client.get("/api/webhooks/events", headers=AUTH).json()["events"]
    assert len(events) == 2 and all(not e["accepted"] for e in events)


def test_webhook_plain_text_body(client):
    response = client.post(f"/api/webhooks/tradingview?token={TEST_WEBHOOK_SECRET}",
                           content=b"BUY EURUSD @ 1.1", headers={"Content-Type": "text/plain"})
    assert response.status_code == 202
    event = client.get("/api/webhooks/events", headers=AUTH).json()["events"][0]
    assert event["payload"] == "BUY EURUSD @ 1.1" and event["symbol"] is None


def test_webhook_limits(client):
    url = f"/api/webhooks/tradingview?token={TEST_WEBHOOK_SECRET}"
    assert client.post(url, content=b"x" * (webhooks.MAX_BODY_BYTES + 1)).status_code == 413
    assert client.post(f"/api/webhooks/Bad%20Source?token={TEST_WEBHOOK_SECRET}", json={}).status_code == 422
    for i in range(webhooks.LOG_CAPACITY + 5):
        client.post(url, json={"n": i})
    events = client.get("/api/webhooks/events", headers=AUTH).json()["events"]
    assert len(events) == webhooks.LOG_CAPACITY and events[0]["payload"]["n"] == webhooks.LOG_CAPACITY + 4


def test_webhook_disabled_without_secret(client, monkeypatch):
    monkeypatch.setattr(webhooks, "settings", replace(settings, api=replace(settings.api, webhook_secret="")))
    assert client.post("/api/webhooks/tradingview?token=x", json={}).status_code == 503


def test_webhook_journal_requires_api_key(client):
    assert client.get("/api/webhooks/events").status_code == 401


# ----------------------------------------------------------------- data-feeds

@pytest.fixture
def feeds(monkeypatch):
    article = NewsArticle("Dollar surges after strong payrolls", "Reuters", "https://example.com/a",
                          NOW - timedelta(minutes=30), "Better than expected data")
    events = [
        EconomicEvent("Non-Farm Payrolls", "USD", NOW + timedelta(minutes=10), "High", "180K", "150K"),
        EconomicEvent("ECB Rate Decision", "EUR", NOW + timedelta(hours=5), "High"),
        EconomicEvent("CPI", "USD", NOW - timedelta(hours=1), "High"),
    ]
    monkeypatch.setattr(monitoring, "get_recent_news", lambda: [article])
    monkeypatch.setattr(monitoring, "get_news_fetched_at", lambda: NOW)
    monkeypatch.setattr(monitoring, "get_upcoming_events", lambda hours_ahead, hours_back: events)
    monkeypatch.setattr(monitoring, "get_feed_statuses", lambda: {
        "https://www.reuters.com/rss": FeedStatus("https://www.reuters.com/rss", True, NOW, 12),
        "https://down.example/rss": FeedStatus("https://down.example/rss", False, NOW, 0, "timeout"),
        "https://new.example/rss": None,
    })
    monkeypatch.setattr(monitoring, "get_calendar_status", lambda: {
        "available": True, "last_fetch": NOW, "event_count": 80, "url": "https://cal.example", "ttl_seconds": 3600})


def test_data_feeds_news_enrichment(client, feeds):
    body = client.get("/api/data-feeds", headers=AUTH).json()
    news = body["news"][0]
    assert news["label"] == "bullish" and "better than expected" in news["keywords"]
    # USD fort = baissier pour EURUSD et pour XAUUSD (USD en devise de cotation dans les deux cas)
    assert news["symbols"] == {"EURUSD": -1.0, "XAUUSD": -1.0}
    assert news["fetched_at"] is not None


def test_data_feeds_calendar_status_matches_engine_rule(client, feeds):
    calendar = client.get("/api/data-feeds", headers=AUTH).json()["calendar"]
    assert [c["status"] for c in calendar] == ["imminent", "upcoming", "passed"]
    assert calendar[0]["blocks_trading"] is True and calendar[0]["symbols"] == ["EURUSD", "XAUUSD"]
    assert not calendar[1]["blocks_trading"]


def test_data_feeds_sources(client, feeds):
    sources = {s["name"]: s for s in client.get("/api/data-feeds", headers=AUTH).json()["sources"]}
    assert sources["reuters.com"]["status"] == "ok" and sources["reuters.com"]["items"] == 12
    assert sources["down.example"]["status"] == "error" and sources["down.example"]["detail"] == "timeout"
    assert sources["new.example"]["status"] == "pending"
    assert sources["Webhooks entrants"]["status"] == "ok"
    assert sources["Calendrier économique (Forex Factory)"]["next_fetch"] is not None


def test_data_feeds_requires_api_key(client):
    assert client.get("/api/data-feeds").status_code == 401


# ----------------------------------------------------------------- notifications

def test_webhook_notifications(client, monkeypatch):
    sent = []
    monkeypatch.setattr(webhooks, "notify", lambda title, message="", level=None, dedup_key=None:
                        sent.append((title, dedup_key)) or True)
    client.post(f"/api/webhooks/tradingview?token={TEST_WEBHOOK_SECRET}", json={"symbol": "EURUSD", "action": "buy"})
    client.post("/api/webhooks/tradingview?token=faux", json={})
    assert sent[0] == ("Webhook tradingview reçu", None)
    assert sent[1][0] == "Webhook tradingview rejeté" and sent[1][1].startswith("webhook-rejected:tradingview:")


# ----------------------------------------------------------------- persistance et historique

def test_webhooks_persist_beyond_memory_log(client, isolated_database):
    client.post(f"/api/webhooks/tradingview?token={TEST_WEBHOOK_SECRET}", json={"symbol": "EURUSD", "action": "buy"})
    webhooks.webhook_log.clear()                                       # simule un redémarrage
    events = client.get("/api/webhooks/events", headers=AUTH).json()["events"]
    assert len(events) == 1 and events[0]["symbol"] == "EURUSD"
    assert isolated_database.list_webhooks()[1] == 1


def test_webhook_falls_back_to_memory_when_database_down(client, isolated_database):
    isolated_database.close()
    response = client.post(f"/api/webhooks/tradingview?token={TEST_WEBHOOK_SECRET}", json={"action": "sell"})
    assert response.status_code == 202
    assert client.get("/api/webhooks/events", headers=AUTH).json()["events"][0]["action"] == "SELL"


def test_history_endpoints(client, isolated_database):
    db = isolated_database
    for i in range(3):
        db.insert_webhook(source="tradingview", symbol="EURUSD", action="BUY", accepted=i != 1, reason="r",
                          client="c", size_bytes=1, payload={"n": i})
    db.insert_decision({"symbol": "EURUSD", "status": "traded", "action": "BUY", "volume": 0.5, "dry_run": True})
    db.insert_decision({"symbol": "EURUSD", "status": "hold"})
    db.insert_decision({"symbol": "XAUUSD", "status": "rejected", "detail": "spread"})
    db.upsert_articles([{"link": "https://ex.com/a", "title": "Gold soars", "source": "R", "score": 0.6,
                         "label": "bullish", "symbols": {"XAUUSD": 1.0}}])

    webhooks_page = client.get("/api/history/webhooks?limit=2&accepted=true", headers=AUTH).json()
    assert webhooks_page["total"] == 2 and len(webhooks_page["items"]) == 2
    assert client.get("/api/history/trades?trades_only=true", headers=AUTH).json()["total"] == 2
    page = client.get("/api/history/trades?status=hold&status=traded&symbol=EURUSD", headers=AUTH).json()
    assert page["total"] == 2 and {i["status"] for i in page["items"]} == {"hold", "traded"}
    articles = client.get("/api/history/sentiment?symbol=XAUUSD", headers=AUTH).json()
    assert articles["total"] == 1 and articles["items"][0]["symbols"] == {"XAUUSD": 1.0}


def test_history_validation_and_auth(client, isolated_database):
    assert client.get("/api/history/trades").status_code == 401
    assert client.get("/api/history/trades?status=unknown", headers=AUTH).status_code == 422
    assert client.get("/api/history/trades?limit=0", headers=AUTH).status_code == 422
    isolated_database.close()
    assert client.get("/api/history/webhooks", headers=AUTH).status_code == 503
