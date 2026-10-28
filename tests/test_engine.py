"""Orchestration du moteur : séquence A-D, cooldown, isolation des erreurs, pause."""

import pytest

from brokers.mt5_client import OrderResult
from core import engine as eng
from core.engine import TradingEngine


@pytest.fixture
def pipeline(monkeypatch):
    """Dépendances du moteur simulées ; `calls` enregistre les ordres envoyés."""
    state = {"news": False, "decision": {"action": "BUY", "confidence": 72, "entry_price": 1.1,
                                         "suggested_sl": 1.098, "suggested_tp": 1.104, "reason": "test"},
             "volume": 0.5, "approved": True, "order_ok": True}
    calls: list[dict] = []

    def execute(symbol, action, volume, sl=None, tp=None, comment=""):
        calls.append({"symbol": symbol, "action": action, "volume": volume, "sl": sl, "tp": tp})
        return OrderResult(state["order_ok"], symbol, action, volume, price=1.1, sl=sl, tp=tp, ticket=-1,
                           message="" if state["order_ok"] else "refusé par le broker", dry_run=True)

    monkeypatch.setattr(eng, "ensure_connection", lambda: None)
    monkeypatch.setattr(eng, "is_high_impact_news_imminent", lambda symbol: state["news"])
    monkeypatch.setattr(eng, "evaluate_market", lambda symbol: dict(state["decision"], symbol=symbol))
    monkeypatch.setattr(eng, "calculate_position_size", lambda symbol, entry, sl: state["volume"])
    monkeypatch.setattr(eng, "validate_trade_risk",
                        lambda symbol, action, volume, sl, tp: {"is_approved": state["approved"], "reason": "test"})
    monkeypatch.setattr(eng, "execute_order", execute)
    return state, calls


def _statuses(outcomes):
    return [o.status for o in outcomes]


def test_buy_signal_is_executed_with_risk_checked_volume(pipeline):
    state, calls = pipeline
    engine = TradingEngine(symbols=["EURUSD"])
    assert _statuses(engine.step()) == ["traded"]
    assert calls == [{"symbol": "EURUSD", "action": "BUY", "volume": 0.5, "sl": 1.098, "tp": 1.104}]
    assert engine.last_outcomes[0].status == "traded" and engine.last_cycle_at is not None


def test_cooldown_prevents_second_trade_on_same_candle(pipeline):
    _, calls = pipeline
    engine = TradingEngine(symbols=["EURUSD"], trade_cooldown_seconds=900)
    engine.step()
    assert _statuses(engine.step()) == ["cooldown"]
    assert len(calls) == 1


@pytest.mark.parametrize("change, expected", [
    ({"news": True}, "news_blocked"),
    ({"decision": {"action": "HOLD", "confidence": 10, "reason": "faible"}}, "hold"),
    ({"volume": 0.0}, "rejected"),
    ({"approved": False}, "rejected"),
    ({"order_ok": False}, "failed"),
])
def test_non_trading_outcomes(pipeline, change, expected):
    state, calls = pipeline
    state.update(change)
    assert _statuses(TradingEngine(symbols=["EURUSD"]).step()) == [expected]
    if expected != "failed":
        assert calls == []


def test_missing_sl_is_rejected(pipeline):
    state, calls = pipeline
    state["decision"] = dict(state["decision"], suggested_sl=None)
    assert _statuses(TradingEngine(symbols=["EURUSD"]).step()) == ["rejected"]
    assert calls == []


def test_error_on_one_symbol_does_not_stop_the_others(pipeline, monkeypatch):
    state, calls = pipeline

    def evaluate(symbol):
        if symbol == "EURUSD":
            raise RuntimeError("bug")
        return dict(state["decision"], symbol=symbol)

    monkeypatch.setattr(eng, "evaluate_market", evaluate)
    assert _statuses(TradingEngine(symbols=["EURUSD", "XAUUSD"]).step()) == ["error", "traded"]
    assert [c["symbol"] for c in calls] == ["XAUUSD"]


def test_cycle_skipped_when_mt5_unavailable(pipeline, monkeypatch):
    def boom():
        raise ConnectionError("MT5 indisponible")

    monkeypatch.setattr(eng, "ensure_connection", boom)
    assert TradingEngine(symbols=["EURUSD"]).step() == []


def test_pause_resume_and_stop_flags():
    engine = TradingEngine(symbols=["EURUSD"])
    assert not engine.is_running and not engine.is_paused
    engine.pause()
    assert engine.is_paused
    engine.resume()
    assert not engine.is_paused


def test_run_returns_false_when_initial_connection_fails(monkeypatch):
    monkeypatch.setattr(eng, "connect_mt5", lambda: False)
    monkeypatch.setattr(eng.TradingEngine, "_install_signal_handlers", lambda self: None)
    assert TradingEngine(symbols=["EURUSD"]).run() is False


def test_run_loop_stops_cleanly(pipeline, monkeypatch):
    shutdowns = []
    monkeypatch.setattr(eng, "connect_mt5", lambda: True)
    monkeypatch.setattr(eng, "shutdown_mt5", lambda: shutdowns.append(True))
    monkeypatch.setattr(eng.TradingEngine, "_install_signal_handlers", lambda self: None)
    engine = TradingEngine(symbols=["EURUSD"], interval_seconds=1)
    original_step = engine.step

    def step_then_stop():
        outcomes = original_step()
        engine.stop()
        return outcomes

    engine.step = step_then_stop
    assert engine.run() is True
    assert engine.cycle_count == 1 and shutdowns == [True] and not engine.is_running
