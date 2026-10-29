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
    monkeypatch.setattr(ns, "_fetch_feed", lambda url, name="", category="": [fresh, old, undated, newer, duplicate])

    articles = ns.get_latest_news(feeds=["https://feed"], lookback_hours=6)
    assert [a.title for a in articles] == ["B", "A", "D"]
    assert ns.get_latest_news(feeds=[]) == []


def test_upcoming_events_window(monkeypatch):
    events = [_event(-180), _event(-60), _event(30), _event(60 * 30), _event(30, currency="AUD"),
              _event(30, impact="Low")]
    monkeypatch.setattr(ns, "_load_calendar", lambda force_refresh=False: events)
    assert ns.get_upcoming_events(hours_ahead=24, hours_back=2) == [events[1], events[2]]
    monkeypatch.setattr(ns, "_load_calendar", lambda force_refresh=False: None)
    assert ns.get_upcoming_events() == []


def test_feed_status_is_recorded(monkeypatch):
    monkeypatch.setattr(ns, "_feed_status", {})
    monkeypatch.setattr(ns, "_http_get", lambda url: SimpleNamespace(content=RSS))
    ns._fetch_feed("https://ok.example/rss")

    def boom(url):
        raise requests.ConnectionError("refusé")

    monkeypatch.setattr(ns, "_http_get", boom)
    ns._fetch_feed("https://ko.example/rss")
    statuses = ns.get_feed_statuses(["https://ok.example/rss", "https://ko.example/rss", "https://never.example"])
    assert statuses["https://ok.example/rss"].ok and statuses["https://ok.example/rss"].article_count == 1
    assert not statuses["https://ko.example/rss"].ok and "refusé" in statuses["https://ko.example/rss"].error
    assert statuses["https://never.example"] is None


# ----------------------------------------------------------------- collecte multi-sources

from dataclasses import replace as _replace  # noqa: E402

from config import settings as _settings  # noqa: E402

GOOGLE_RSS = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>Google News</title>
<item><title>Dollar rises on Fed bets - Reuters</title><link>https://news.google.com/a</link>
<source url="https://www.reuters.com">Reuters</source><pubDate>Wed, 28 Oct 2026 10:00:00 GMT</pubDate></item>
<item><title>Futur</title><link>https://news.google.com/b</link><pubDate>Mon, 01 Jan 2035 10:00:00 GMT</pubDate></item>
</channel></rss>"""


def _articles(prefix, count, minutes_ago=0):
    return [NewsArticle(f"{prefix} {i}", prefix, f"https://{prefix}/{i}", NOW - timedelta(minutes=minutes_ago + i), "")
            for i in range(count)]


def test_aggregator_normalization(monkeypatch):
    monkeypatch.setattr(ns, "_feed_status", {})
    monkeypatch.setattr(ns, "_http_get", lambda url: SimpleNamespace(content=GOOGLE_RSS))
    first, future = ns._fetch_feed("https://news.google.com/rss", "Reuters (via Google News)", "macro")
    assert first.title == "Dollar rises on Fed bets" and first.source == "Reuters"     # suffixe retiré, éditeur réel
    assert first.feed == "Reuters (via Google News)" and first.category == "macro"
    assert future.published <= datetime.now(timezone.utc)                               # date future ramenée


def test_one_failing_source_does_not_block_the_others(monkeypatch):
    def fetch(url, name="", category=""):
        if "down" in url:
            raise RuntimeError("site en panne")
        return _articles(url.split("//")[1], 2)

    monkeypatch.setattr(ns, "_fetch_feed", fetch)
    articles = ns.get_latest_news(feeds=["https://a.example", "https://down.example", "https://b.example"])
    assert {a.source for a in articles} == {"a.example", "b.example"}


def test_slow_source_is_abandoned_after_global_deadline(monkeypatch):
    import threading

    release = threading.Event()

    def fetch(url, name="", category=""):
        if "slow" in url:
            release.wait(10)
        return _articles(url.split("//")[1], 1)

    monkeypatch.setattr(ns, "_feed_status", {})
    monkeypatch.setattr(ns, "_fetch_feed", fetch)
    monkeypatch.setattr(ns, "settings", _replace(_settings, news=_replace(_settings.news, request_timeout_seconds=1)))
    monkeypatch.setattr(ns, "FEED_DEADLINE_FACTOR", 1)
    started = datetime.now(timezone.utc)
    articles = ns.get_latest_news(feeds=["https://fast.example", "https://slow.example"])
    release.set()
    assert [a.source for a in articles] == ["fast.example"]
    assert (datetime.now(timezone.utc) - started).total_seconds() < 5
    assert ns._feed_status["https://slow.example"].error == "délai global de collecte dépassé"


def test_balanced_aggregation_caps_each_source(monkeypatch):
    feeds = {"https://prolific.example": _articles("prolific", 40), "https://small.example": _articles("small", 3, 30)}
    monkeypatch.setattr(ns, "_fetch_feed", lambda url, name="", category="": feeds[url])
    articles = ns.get_latest_news(feeds=list(feeds), max_per_feed=5)
    counts = {s: sum(a.source == s for a in articles) for s in ("prolific", "small")}
    assert counts == {"prolific": 5, "small": 3}


def test_cross_source_dedup_on_normalized_title(monkeypatch):
    a = NewsArticle("Gold hits record high!", "A", "https://a/1", NOW, "")
    b = NewsArticle("gold hits  record high", "B", "https://b/1", NOW - timedelta(minutes=1), "")
    monkeypatch.setattr(ns, "_fetch_feed", lambda url, name="", category="": [a] if "a." in url else [b])
    assert len(ns.get_latest_news(feeds=["https://a.example", "https://b.example"])) == 1


def test_repeatedly_failing_feed_is_paused(monkeypatch):
    monkeypatch.setattr(ns, "_feed_status", {})
    for _ in range(ns.FEED_FAILURE_THRESHOLD):
        ns._record_feed("https://down.example", ok=False, error="timeout")
    assert ns._paused("https://down.example") is True
    calls = []
    monkeypatch.setattr(ns, "_fetch_feed", lambda url, name="", category="": calls.append(url) or [])
    ns.get_latest_news(feeds=["https://down.example", "https://ok.example"])
    assert calls == ["https://ok.example"]
    ns._record_feed("https://down.example", ok=True, count=3)                   # rétabli : compteur remis à zéro
    assert ns._paused("https://down.example") is False


def test_configured_feeds_catalogue_symbols_and_custom(monkeypatch):
    news = _replace(_settings.news, use_default_feeds=True,
                    rss_feeds=("https://www.fxstreet.com/rss/news", "https://custom.example/rss"))
    trading = _replace(_settings.trading, symbols=("EURUSD", "XAUUSD", "US30.cash"))
    monkeypatch.setattr(ns, "settings", _replace(_settings, news=news, trading=trading))
    feeds = ns.configured_feeds()
    urls = [f.url for f in feeds]
    assert len(urls) == len(set(urls))                                          # FXStreet en double fusionné
    assert len(feeds) == len(ns.DEFAULT_FEEDS) + 2 + 1                          # catalogue + Yahoo x2 + perso
    yahoo = [f.url for f in feeds if f.category == "symbol"]
    assert any("EURUSD=X" in u for u in yahoo) and any("GC=F" in u for u in yahoo)
    assert feeds[-1].name == "custom.example" and feeds[-1].category == "custom"


def test_catalogue_can_be_disabled(monkeypatch):
    monkeypatch.setattr(ns, "settings", _replace(_settings, news=_replace(_settings.news, use_default_feeds=False)))
    assert ns.configured_feeds() == []
    assert ns.get_latest_news() == []
