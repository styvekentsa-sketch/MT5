"""Moteurs de sentiment, orientation par paire et robustesse."""

from datetime import datetime, timedelta, timezone

import pytest

from sentiment import sentiment_analyzer as sa
from sentiment.sentiment_analyzer import KeywordEngine, analyze_news_items, analyze_text, split_symbol


def test_empty_or_invalid_text_is_neutral():
    for value in ("", "   ", None, 42):
        assert analyze_text(value) == {"score": 0.0, "label": "neutral", "engine": "none"}


def test_financial_vocabulary_is_scored():
    assert analyze_text("Gold soars to a record high")["label"] == "bullish"
    assert analyze_text("Stocks plunge as recession fears grow")["label"] == "bearish"
    for text in ("Gold soars to a record high", "Stocks plunge as recession fears grow"):
        assert -1.0 <= analyze_text(text)["score"] <= 1.0


def test_keyword_engine_phrases_and_negation():
    engine = KeywordEngine()
    assert engine.score("Payrolls better than expected") > 0
    assert engine.score("Data worse than expected") < 0
    assert engine.score("strong growth") > 0
    assert engine.score("growth is not strong") < engine.score("growth is strong")
    assert engine.score("nothing relevant here") == 0.0


def test_engine_falls_back_when_dependency_fails(monkeypatch):
    class Broken:
        name = "broken"

        def __init__(self):
            raise ImportError("dépendance absente")

    monkeypatch.setattr(sa, "_engine", None)
    monkeypatch.setitem(sa._ENGINE_CHAIN, "vader", (Broken, KeywordEngine))
    assert sa.get_engine().name == "keywords"
    monkeypatch.setattr(sa, "_engine", None)            # le moteur réel sera rechargé au besoin


def test_engine_error_returns_neutral(monkeypatch):
    class Failing:
        name = "failing"

        def score(self, text):
            raise RuntimeError("panne")

    monkeypatch.setattr(sa, "_engine", Failing())
    assert analyze_text("Gold soars")["score"] == 0.0


@pytest.mark.parametrize("symbol, expected", [
    ("EURUSD", ("EUR", "USD")),
    ("eurusd.m", ("EUR", "USD")),
    ("XAUUSD", ("XAU", "USD")),
    ("US30.cash", ("US30", None)),
    ("GER40", ("GER40", None)),
    ("FOOBAR", (None, None)),
])
def test_split_symbol(symbol, expected):
    assert split_symbol(symbol) == expected


def test_quote_currency_news_is_inverted_for_the_pair():
    items = [{"title": "Dollar surges after strong payrolls", "snippet": ""}]
    assert analyze_news_items(items, symbol="EURUSD") < 0      # USD fort = EURUSD baissier
    assert analyze_news_items(items, symbol="USDJPY") > 0      # USD en base = haussier


def test_explicit_pair_mention_and_ambiguous_articles():
    assert analyze_news_items([{"title": "EUR/USD rallies to a multi year high"}], symbol="EURUSD") > 0
    # Les deux devises citées sans la paire : sens indéterminable -> ignoré
    assert analyze_news_items([{"title": "Euro surges while dollar slumps"}], symbol="EURUSD") == 0.0
    # Article sans rapport avec la paire
    assert analyze_news_items([{"title": "Nikkei surges"}], symbol="EURUSD") == 0.0


def test_recent_articles_weigh_more():
    now = datetime.now(timezone.utc)
    bullish_recent = {"title": "Euro surges", "published": now}
    bearish_old = {"title": "Euro plunges", "published": now - timedelta(hours=12)}
    assert analyze_news_items([bullish_recent, bearish_old], symbol="EURUSD") > 0


def test_garbage_items_never_raise():
    assert analyze_news_items([None, 123, {}, "", object()], symbol="EURUSD") == 0.0
    assert analyze_news_items(None) == 0.0
    assert analyze_news_items([{"title": "Euro surges"}], symbol="UNKNOWN") == 0.0


def test_detect_keywords():
    assert sa.detect_keywords("Payrolls better than expected, dollar surges") == ["better than expected", "surges"]
    assert sa.detect_keywords("rally rally rally", limit=8) == ["rally"]
    assert sa.detect_keywords("") == [] and sa.detect_keywords(None) == []


def test_relevant_symbols_with_direction():
    symbols = ["EURUSD", "USDJPY", "XAUUSD", "GER40"]
    assert sa.relevant_symbols("Dollar surges after payrolls", symbols) == {"EURUSD": -1.0, "USDJPY": 1.0, "XAUUSD": -1.0}
    assert sa.relevant_symbols("Gold hits record", symbols) == {"XAUUSD": 1.0}
    # La paire citée explicitement prime ; « usd » y concerne aussi les autres paires en USD
    assert sa.relevant_symbols("EUR/USD breaks out", symbols) == {
        "EURUSD": sa.PAIR_MENTION_BOOST, "USDJPY": 1.0, "XAUUSD": -1.0}
    assert sa.relevant_symbols("", symbols) == {}
