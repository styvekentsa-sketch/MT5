"""Journal d'entrée, analyse post-mortem des pertes et mémoire des erreurs (fichiers JSON)."""

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from brokers.models import ClosedDeal, OrderResult, Tick
from core import engine as eng
from core.engine import TradingEngine
from learning import trade_memory as tm
from learning.trade_memory import TradeMemory
from tests.conftest import TEST_API_KEY
from tests.fakes import FakeBroker

T0 = datetime(2026, 10, 30, 9, 0, tzinfo=timezone.utc)


def _bars(n=300, start=1.1, step=0.0, freq="1min", end=None):
    closes = start + step * np.arange(n) + 0.00003 * np.tile([1, -1], n // 2)
    index = pd.date_range(end=end or T0 + timedelta(minutes=30), periods=n, freq=freq, tz="UTC")
    return pd.DataFrame({"open": closes, "high": closes + 0.0002, "low": closes - 0.0002, "close": closes,
                         "volume": 10.0, "spread": 10, "real_volume": 0}, index=index)


@pytest.fixture
def broker(eurusd_specs):
    fake = FakeBroker()
    fake.bars = _bars()
    fake.specs = {"EURUSD": eurusd_specs}
    fake.ticks = {"EURUSD": Tick("EURUSD", 1.10000, 1.10010, 0.0, 10, T0)}
    return fake


@pytest.fixture
def memory(tmp_path):
    return TradeMemory(tmp_path)


DECISION = {"mode": "scalping", "entry_price": 1.10010, "suggested_sl": 1.09960, "suggested_tp": 1.10060,
            "sentiment_score": -0.05, "technical_score": 0.12,
            "verdict": "[SCALP] micro-signal BUY M1 (+0.12, tendance haussière (EMA5/EMA13))"}


def _open(memory, broker, ticket, dry_run=False):
    features = tm.market_features(broker, "EURUSD", DECISION, now=T0)
    result = OrderResult(True, "EURUSD", "BUY", 1.0, price=1.10010, sl=1.09960, tp=1.10060, ticket=ticket,
                         dry_run=dry_run)
    return memory.record_entry(result, DECISION, features), features


def _close(broker, ticket, profit, reason="sl", minutes=1, price=1.09960):
    broker.position_deals[ticket] = [
        ClosedDeal(ticket + 1, "EURUSD", T0, 0.0, 1, position_id=ticket, price=1.10010, reason="bot", entry="in"),
        ClosedDeal(ticket + 2, "EURUSD", T0 + timedelta(minutes=minutes), profit, 1, position_id=ticket,
                   price=price, reason=reason, entry="out"),
    ]


# ----------------------------------------------------------------- 1. journal d'entrée

def test_entry_snapshot_is_complete(memory, broker, tmp_path):
    entry, features = _open(memory, broker, 101)
    saved = json.loads((tmp_path / "trade_decisions.json").read_text(encoding="utf-8"))
    assert saved[0]["ticket"] == 101 and saved[0]["status"] == "open"
    assert {"rsi", "ema_fast", "ema_slow", "macd_hist", "atr", "spread_points", "spread_to_sl", "sentiment"} <= set(features)
    assert features["timeframe"] == "M1" and features["ema_periods"] == "5/13" and features["spread_points"] == 10
    assert features["spread_to_sl"] == pytest.approx(0.2)                   # 1 pip de spread pour 5 pips de SL
    assert entry["reason"].startswith("[SCALP]") and entry["pattern"].startswith("EURUSD|scalping|BUY|")


def test_simulated_trades_are_logged_but_never_analysed(memory, broker):
    entry, _ = _open(memory, broker, -1, dry_run=True)
    assert entry["status"] == "simulated" and memory.review(broker) == []


# ----------------------------------------------------------------- 2. post-mortem

def test_review_waits_for_closing_deal_then_analyses_loss(memory, broker, tmp_path):
    _open(memory, broker, 101)
    assert memory.review(broker) == []                                       # pas encore d'historique
    _close(broker, 101, -50.0)
    [event] = memory.review(broker)
    assert event["outcome"] == "loss" and "-50.00" in event["message"]
    analysis = json.loads((tmp_path / "loss_analysis.json").read_text(encoding="utf-8"))[0]
    assert analysis["duration_seconds"] == 60 and analysis["close_reason"] == "sl"
    assert analysis["sl_distance_pips"] == pytest.approx(5.0)
    assert analysis["adverse_excursion_pips"] is not None
    assert any("Bruit de marché" in f for f in analysis["factors"])         # stop touché en 1 minute
    assert memory.review(broker) == []                                       # déjà traité


def test_open_position_is_not_reviewed(memory, broker):
    _open(memory, broker, 101)
    broker.positions = [type("P", (), {"ticket": 101})()]
    _close(broker, 101, -50.0)
    assert memory.review(broker) == []


@pytest.mark.parametrize("features, reason, duration, expected", [
    ({"spread_to_sl": 0.4, "timeframe": "M1"}, "sl", 600, "Spread élevé"),
    ({"ema_gap_atr": 0.1, "timeframe": "M15"}, "sl", 7200, "Faux signal de cassure en range"),
    ({"rsi": 78, "timeframe": "M15"}, "sl", 7200, "mouvement épuisé"),
    ({"sentiment": -0.4, "timeframe": "M15"}, "sl", 7200, "Sentiment des actualités contraire"),
    ({"timeframe": "M15"}, "sl", 7200, "aléa de marché"),
])
def test_failure_factors(features, reason, duration, expected):
    trade = {"side": "BUY", "entry_price": 1.1, "tp": 1.101, "features": features}
    factors = tm.failure_factors(trade, reason, duration, favorable=0.0005, max_bar_range=0.0002)
    assert expected in factors[0]


def test_never_in_favor_is_detected():
    trade = {"side": "SELL", "entry_price": 1.1, "tp": 1.099, "features": {"timeframe": "H1"}}
    factors = tm.failure_factors(trade, "sl", 36_000, favorable=0.00005, max_bar_range=0.001)
    assert any("jamais allé dans le sens" in f for f in factors)


# ----------------------------------------------------------------- 3. mémoire des erreurs

def test_repeated_losing_pattern_blocks_similar_orders(memory, broker):
    _, features = _open(memory, broker, 101)
    _close(broker, 101, -50.0)
    memory.review(broker)
    assert memory.check("EURUSD", "BUY", "scalping", features) is None      # une seule perte : pas encore
    _open(memory, broker, 202)
    _close(broker, 202, -49.0)
    memory.review(broker)
    block = memory.check("EURUSD", "BUY", "scalping", features)
    assert block is not None and "pattern similaire au trade perdant #202" in block.message
    assert "2 perte(s) / 0 gain(s)" in block.message
    assert memory.check("EURUSD", "SELL", "scalping", features) is None     # autre sens : autre pattern
    assert memory.check("GBPUSD", "BUY", "scalping", features) is None


def test_wins_on_the_same_pattern_lift_the_block(memory, broker):
    _, features = _open(memory, broker, 101)
    for ticket, profit in ((101, -50.0), (202, -49.0), (303, 50.0), (404, 51.0)):
        if ticket != 101:
            _open(memory, broker, ticket)
        _close(broker, ticket, profit, reason="tp" if profit > 0 else "sl", price=1.10060 if profit > 0 else 1.09960)
        memory.review(broker)
    assert memory.check("EURUSD", "BUY", "scalping", features) is None      # 2 pertes / 4 trades = 50 % < 60 %
    report = memory.report()
    assert (report["wins"], report["losses"], report["blocking_patterns"]) == (2, 2, 0)


def test_old_mistakes_are_forgotten(memory, broker):
    pattern = {"losses": 3, "wins": 0, "last_loss_at": (datetime.now(timezone.utc) - timedelta(days=45)).isoformat()}
    assert not memory.is_blocking(pattern)
    pattern["last_loss_at"] = datetime.now(timezone.utc).isoformat()
    assert memory.is_blocking(pattern)


def test_corrupt_memory_file_is_set_aside(tmp_path, broker):
    (tmp_path / "mistakes_memory.json").write_text("{ pas du json", encoding="utf-8")
    memory = TradeMemory(tmp_path)
    assert memory.check("EURUSD", "BUY", "scalping", tm.market_features(broker, "EURUSD", DECISION)) is None
    assert list(tmp_path.glob("mistakes_memory.corrupt-*.json"))


# ----------------------------------------------------------------- intégration moteur et API

@pytest.fixture
def engine(monkeypatch, broker, memory):
    monkeypatch.setattr(eng, "is_high_impact_news_imminent", lambda symbol: False)
    monkeypatch.setattr(eng, "evaluate_market", lambda symbol, broker: dict(DECISION, action="BUY", direction="BUY",
                                                                             confidence=12, mode=None))
    monkeypatch.setattr(eng, "calculate_position_size", lambda *a, **k: 1.0)
    monkeypatch.setattr(eng, "validate_trade_risk", lambda *a, **k: {"is_approved": True, "reason": "ok"})
    engine = TradingEngine(symbols=["EURUSD"], broker=broker, trade_cooldown_seconds=0)
    engine.memory = memory
    return engine


def test_engine_logs_entries_and_blocks_on_memory(engine, broker, memory, monkeypatch):
    assert [o.status for o in engine.step()] == ["traded"]
    assert memory.trades.load()[0]["status"] == "simulated"                 # FakeBroker : ordres simulés

    block = tm.MemoryBlock("k", "Ordre rejeté par la mémoire des erreurs - pattern similaire au trade perdant #7", {})
    monkeypatch.setattr(memory, "check", lambda *a: block)
    [outcome] = engine.step()
    assert outcome.status == "memory_blocked" and "#7" in outcome.verdict
    assert len(broker.orders) == 1                                           # aucun nouvel ordre


def test_engine_records_closed_trade_and_api_report(engine, broker, memory):
    _open(memory, broker, 101)
    _close(broker, 101, -50.0)
    engine.step()
    closed = [r for r in engine.recent_decisions if r["status"] == "trade_closed"]
    assert closed and "Perte #101" in closed[0]["verdict"]

    client = TestClient(create_app(engine))
    body = client.get("/api/learning", headers={"X-API-Key": TEST_API_KEY}).json()
    assert body["enabled"] is True and body["losses"] == 1 and body["recent_losses"][0]["ticket"] == 101
    assert body["patterns"][0]["losses"] == 1 and body["patterns"][0]["blocking"] is False
    journal = client.get("/api/bot/decisions?status=actions", headers={"X-API-Key": TEST_API_KEY}).json()
    assert journal["items"][0]["stage"] == "post-mortem"


def test_learning_settings_validation():
    from config.settings import LearningConfig, ConfigError
    with pytest.raises(ConfigError):
        replace(LearningConfig(True, tm.Path("."), 2, 0.6, 30), min_loss_rate=1.5).validate()
