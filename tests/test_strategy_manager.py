"""Fusion technique + sentiment, filtres de décision et calcul SL/TP."""

from dataclasses import replace

import pandas as pd
import pytest

from config import settings
from strategies import strategy_manager as sm
from strategies.technical_analysis import TechnicalSignal
from tests.fakes import FakeBroker


def _tech(signal="BUY", score=1.0, close=1.1, atr=0.001, swing_low=1.0, swing_high=1.2) -> TechnicalSignal:
    return TechnicalSignal(signal=signal, score=score, close=close, atr=atr,
                           swing_low=swing_low, swing_high=swing_high, reasons=("test",))


# ----------------------------------------------------------------- SL / TP

def test_atr_stop_and_reward_ratio_for_buy():
    sl, tp = sm.compute_sl_tp("BUY", 1.1, _tech())
    # SL = 1.5 ATR, TP = SL x (3.0 / 1.5)
    assert sl == pytest.approx(1.1 - 0.0015) and tp == pytest.approx(1.1 + 0.003)


def test_stop_widened_to_nearby_swing_low():
    sl, _ = sm.compute_sl_tp("BUY", 1.1, _tech(swing_low=1.098))
    assert sl == pytest.approx(1.098 - 0.0001)               # plus bas - marge de 0.1 ATR


def test_swing_too_far_falls_back_to_atr_stop():
    sl, _ = sm.compute_sl_tp("BUY", 1.1, _tech(swing_low=1.09))   # 10 ATR : trop loin
    assert sl == pytest.approx(1.1 - 0.0015)


def test_sell_levels_are_mirrored():
    sl, tp = sm.compute_sl_tp("SELL", 1.1, _tech(signal="SELL"))
    assert sl > 1.1 > tp


@pytest.mark.parametrize("action, entry, tech", [
    ("HOLD", 1.1, _tech()),
    ("BUY", None, _tech()),
    ("BUY", 1.1, _tech(atr=None)),
    ("BUY", 1.1, _tech(atr=0.0)),
])
def test_sl_tp_impossible_returns_none(action, entry, tech):
    assert sm.compute_sl_tp(action, entry, tech) == (None, None)


# ----------------------------------------------------------------- evaluate_market

@pytest.fixture
def market(monkeypatch):
    """Dépendances de evaluate_market simulées ; modifier `state` pour chaque scénario."""
    state = {"tech": _tech(), "sentiment": 0.0, "news_blocked": False, "articles": ["article"]}
    fake = FakeBroker()
    fake.bars = pd.DataFrame({"close": [1.0]})
    monkeypatch.setattr(sm, "get_broker", lambda: fake)
    monkeypatch.setattr(sm, "technical_signal", lambda df: state["tech"])
    monkeypatch.setattr(sm, "is_high_impact_news_imminent", lambda symbol: state["news_blocked"])
    monkeypatch.setattr(sm, "get_recent_news", lambda: state["articles"])
    monkeypatch.setattr(sm, "analyze_news_items", lambda articles, symbol: state["sentiment"])
    state["broker"] = fake
    return state


def test_strong_technical_signal_trades(market):
    decision = sm.evaluate_market("EURUSD")
    assert decision["action"] == "BUY"
    assert decision["confidence"] == 70                      # 0.7 x 1.0
    assert decision["suggested_sl"] < decision["entry_price"] < decision["suggested_tp"]
    assert decision["reason"]


def test_high_impact_news_blocks_trade(market):
    market["news_blocked"] = True
    decision = sm.evaluate_market("EURUSD")
    assert decision["action"] == "HOLD" and decision["news_blocked"] is True


def test_score_below_threshold_holds(market):
    market["tech"] = _tech(score=0.5)
    assert sm.evaluate_market("EURUSD")["action"] == "HOLD"


def test_sentiment_alone_never_triggers_trade(market, monkeypatch):
    news_heavy = replace(settings, strategy=replace(settings.strategy, technical_weight=0.2,
                                                    sentiment_weight=0.8, signal_threshold=0.5))
    monkeypatch.setattr(sm, "settings", news_heavy)
    market["tech"] = _tech(signal="NEUTRAL", score=0.1)
    market["sentiment"] = 1.0                               # score hybride 0.82 > seuil
    decision = sm.evaluate_market("EURUSD")
    assert decision["action"] == "HOLD" and "non confirmé" in decision["reason"]


def test_opposite_sentiment_can_veto(market):
    market["tech"] = _tech(score=0.9)
    market["sentiment"] = -1.0                              # 0.63 - 0.3 = 0.33 < 0.6
    assert sm.evaluate_market("EURUSD")["action"] == "HOLD"


def test_market_data_failure_holds(market, monkeypatch):
    def boom(symbol, count):
        raise ConnectionError("MT5 indisponible")

    monkeypatch.setattr(market["broker"], "get_historical_data", boom)
    decision = sm.evaluate_market("EURUSD")
    assert decision["action"] == "HOLD" and "indisponibles" in decision["reason"]


def test_unexpected_error_holds(market, monkeypatch):
    def boom(articles, symbol):
        raise RuntimeError("bug")

    monkeypatch.setattr(sm, "analyze_news_items", boom)
    assert sm.evaluate_market("EURUSD")["action"] == "HOLD"


def test_invalid_technical_data_holds(market):
    market["tech"] = TechnicalSignal(signal="NEUTRAL", score=0.0, reasons=("historique insuffisant",))
    assert sm.evaluate_market("EURUSD")["action"] == "HOLD"


def test_fresh_articles_are_persisted(monkeypatch, isolated_database):
    from datetime import datetime, timezone

    from sentiment.news_scraper import NewsArticle

    article = NewsArticle("Gold soars to a record high", "Reuters", "https://ex.com/gold",
                          datetime.now(timezone.utc), "")
    monkeypatch.setattr(sm, "get_latest_news", lambda: [article])
    monkeypatch.setattr(sm, "_news_fetched_at", None)
    monkeypatch.setattr(sm, "_news_cache", [])
    assert sm.get_recent_news() == [article]
    stored = isolated_database.list_articles()[0][0]
    assert stored["label"] == "bullish" and "record high" in stored["keywords"]
    assert stored["symbols"] == {"XAUUSD": 1.0}
