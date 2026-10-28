"""Calendrier économique, garde-fou des annonces et agrégation RSS (réseau simulé)."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import requests

from sentiment import news_scraper as ns
from sentiment.news_scraper import EconomicEvent, NewsArticle

NOW = datetime.now(timezone.utc)


def _event(minutes_from_now: float, currency: str = "USD", impact: str = "High") -> EconomicEvent:
    return EconomicEvent(title="Non-Farm Payrolls", currency=currency, impact=impact,
                         time=NOW + timedelta(minutes=minutes_from_now))


def test_parse_event_converts_to_utc_and_rejects_invalid():
    event = ns._parse_event({"title": "CPI", "country": "usd", "date": "2026-10-28T08:30:00-04:00", "impact": "High"})
    assert event is not None and event.currency == "USD"
    assert event.time == datetime(2026, 10, 28, 12, 30, tzinfo=timezone.utc)
    assert ns._parse_event({"title": "x", "date": "pas une date"}) is None
    assert ns._parse_event({"title": "x"}) is None


def test_watched_currencies_from_symbols():
    assert ns.get_watched_currencies(["EURUSD", "XAUUSD", "US30.cash", "GBPJPY"]) == {"EUR", "USD", "GBP", "JPY"}


def test_economic_calendar_filters_impact_currency_and_day(monkeypatch):
    events = [_event(60), _event(60, impact="Medium"), _event(60, currency="AUD"), _event(60 * 24 * 3)]
    monkeypatch.setattr(ns, "_load_calendar", lambda force_refresh=False: events)
    today = ns.get_economic_calendar(day=NOW + timedelta(minutes=60))
    assert today == [events[0]]
    assert len(ns.get_economic_calendar(min_impact="Medium", day=NOW + timedelta(minutes=60))) == 2


def test_imminent_news_window_and_symbol(monkeypatch):
    monkeypatch.setattr(ns, "_load_calendar", lambda force_refresh=False: [_event(10)])
    assert ns.is_high_impact_news_imminent(minutes_buffer=15) is True
    assert ns.is_high_impact_news_imminent(minutes_buffer=5) is False
    assert ns.is_high_impact_news_imminent(symbol="EURUSD") is True
    assert ns.is_high_impact_news_imminent(symbol="GBPJPY") is False

    monkeypatch.setattr(ns, "_load_calendar", lambda force_refresh=False: [_event(-3)])
    assert ns.is_high_impact_news_imminent(minutes_after=5) is True      # volatilité post-annonce
    assert ns.is_high_impact_news_imminent(minutes_after=0) is False


def test_imminent_news_fails_safe_when_calendar_unavailable(monkeypatch):
    monkeypatch.setattr(ns, "_load_calendar", lambda force_refresh=False: None)
    assert ns.is_high_impact_news_imminent() is True
    assert ns.is_high_impact_news_imminent(block_if_unavailable=False) is False


def test_load_calendar_keeps_stale_cache_on_network_error(monkeypatch):
    cached = [_event(30)]
    monkeypatch.setattr(ns, "_calendar_cache", cached)
    monkeypatch.setattr(ns, "_calendar_fetched_at", 0.0)

    def boom(url):
        raise requests.ConnectionError("réseau coupé")

    monkeypatch.setattr(ns, "_http_get", boom)
    assert ns._load_calendar() is cached


RSS = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>FX Feed</title>
<item><title>Dollar &lt;b&gt;surges&lt;/b&gt;</title><link>https://example.com/1</link>
<description>&lt;p&gt;Strong &lt;i&gt;payrolls&lt;/i&gt; data&lt;/p&gt;</description>
<pubDate>Mon, 26 Oct 2026 10:00:00 GMT</pubDate></item>
<item><title></title><link>https://example.com/empty</link></item>
</channel></rss>"""


def test_fetch_feed_extracts_clean_articles(monkeypatch):
    monkeypatch.setattr(ns, "_http_get", lambda url: SimpleNamespace(content=RSS))
    articles = ns._fetch_feed("https://feed.example.com/rss")
    assert len(articles) == 1                                   # l'article sans titre est ignoré
    article = articles[0]
    assert article.title == "Dollar surges" and article.snippet == "Strong payrolls data"
    assert article.source == "FX Feed"
    assert article.published == datetime(2026, 10, 26, 10, 0, tzinfo=timezone.utc)


def test_fetch_feed_returns_empty_on_error(monkeypatch):
    def boom(url):
        raise requests.Timeout("timeout")

    monkeypatch.setattr(ns, "_http_get", boom)
    assert ns._fetch_feed("https://feed.example.com/rss") == []


def test_latest_news_dedup_lookback_and_sort(monkeypatch):
    fresh = NewsArticle("A", "S", "https://a", NOW - timedelta(hours=1), "")
    newer = NewsArticle("B", "S", "https://b", NOW - timedelta(minutes=5), "")
    duplicate = NewsArticle("a", "S2", "https://a-bis", NOW, "")          # même titre (casse ignorée)
    old = NewsArticle("C", "S", "https://c", NOW - timedelta(days=2), "")
    undated = NewsArticle("D", "S", "https://d", None, "")
    monkeypatch.setattr(ns, "_fetch_feed", lambda url: [fresh, old, undated, newer, duplicate])

    articles = ns.get_latest_news(feeds=["https://feed"], lookback_hours=6)
    assert [a.title for a in articles] == ["B", "A", "D"]
    assert ns.get_latest_news(feeds=[]) == []
