"""
Gardien des positions (sorties partielles TP1 / TP2 / TP3, retour au prix d'entrée, Bouclier avant et
après annonce), suspension sur drawdown, latence par étape, confirmation des protections, journal.
"""

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from brokers.models import POSITION_TYPE_BUY, AccountInfo, ClosedDeal, OrderResult, Position, Tick
from config.settings import settings
from core import engine as eng
from core import position_guard as pg
from core.engine import TradingEngine
from core.news_pipeline import NewsPipeline, SimulatedCalendarProvider
from core.position_guard import PositionGuard, exit_levels
from learning.trade_memory import TradeMemory
from risk import drawdown as dd
from risk.drawdown import DrawdownMonitor
from tests.fakes import FakeBroker
from utils.latency import LatencyTracker

MAGIC = settings.trading.magic_number
NOW = datetime(2026, 11, 6, 13, 20, tzinfo=timezone.utc)


def _exits(monkeypatch, **changes):
    monkeypatch.setattr(pg, "settings", replace(settings, exits=replace(settings.exits, **changes)))


def _position(price=1.1021, volume=1.0, sl=1.0980, ticket=1, symbol="EURUSD"):
    return Position(ticket=ticket, symbol=symbol, type=POSITION_TYPE_BUY, volume=volume, price_open=1.1000,
                    price_current=price, sl=sl, tp=1.1060, profit=0.0, swap=0.0, time=0, magic=MAGIC)


@pytest.fixture
def broker():
    fake = FakeBroker()
    fake.ticks = {"EURUSD": Tick("EURUSD", 1.1021, 1.10212, 0.0, 2, NOW)}
    return fake


def _pipeline(minutes, **values):
    event = {"time": (NOW + timedelta(minutes=minutes)).isoformat(), "title": "Non-Farm Employment Change",
             "currency": "USD", "importance": "high", **values}
    return NewsPipeline([SimulatedCalendarProvider([event])], minutes_before=30, minutes_after=15,
                        symbols=["EURUSD"], cache_seconds=0)


# ----------------------------------------------------------------- sorties partielles

def test_levels_tp1_tp2_tp3(monkeypatch):
    _exits(monkeypatch, tp1_fraction=0.5, tp2_fraction=0.25)
    levels = exit_levels("BUY", 1.1000, 1.0980, 1.1060)
    assert levels == {"tp1": pytest.approx(1.1020), "tp2": pytest.approx(1.1040), "tp3": 1.1060}
    _exits(monkeypatch, tp1_fraction=0.0)
    assert exit_levels("SELL", 1.1, 1.102, 1.094)["tp1"] is None


def test_disabled_by_default(broker):
    broker.positions = [_position(price=1.1050)]
    assert PositionGuard(broker).run() == [] and broker.closed == []


def test_tp1_closes_half_and_moves_the_stop_to_entry_plus_spread(monkeypatch, broker):
    _exits(monkeypatch, tp1_fraction=0.5)
    broker.positions = [_position()]
    guard = PositionGuard(broker)
    guard.register(1, 1.1000, 1.0980, 1.0)
    actions = guard.run()
    assert [a.kind for a in actions] == ["tp1", "breakeven"]
    assert broker.closed_volumes == [(1, 0.5, "Bot TP1")]
    assert broker.modified[0][1] == pytest.approx(1.10002)                 # entrée + spread
    broker.positions = [replace(broker.positions[0], volume=0.5)]
    assert guard.run() == []                                               # TP1 pris une seule fois


def test_below_tp1_nothing_happens(monkeypatch, broker):
    _exits(monkeypatch, tp1_fraction=0.5)
    broker.positions = [_position(price=1.1015)]                          # 0.75 R
    assert PositionGuard(broker).run() == []


def test_tp2_after_tp1(monkeypatch, broker):
    _exits(monkeypatch, tp1_fraction=0.5, tp2_fraction=0.25)
    broker.positions = [_position(price=1.1041, volume=0.5, sl=1.10002)]
    guard = PositionGuard(broker)
    guard.register(1, 1.1000, 1.0980, 1.0)
    [action] = guard.run()
    assert action.kind == "tp2" and broker.closed_volumes == [(1, 0.25, "Bot TP2")]


def test_restart_reads_the_initial_volume_from_the_journal(monkeypatch, broker, tmp_path):
    _exits(monkeypatch, tp1_fraction=0.5)
    memory = TradeMemory(tmp_path)
    memory.record_entry(OrderResult(True, "EURUSD", "BUY", 1.0, 1.1000, 1.0980, 1.1060, ticket=1),
                        {"entry_price": 1.1000}, {})
    broker.positions = [_position(volume=0.5)]                             # TP1 pris avant le redémarrage
    [action] = PositionGuard(broker, memory).run()
    assert action.kind == "breakeven" and broker.closed_volumes == []      # pas de second TP1


def test_restart_without_journal_uses_the_opening_deal(monkeypatch, broker):
    _exits(monkeypatch, tp1_fraction=0.5)
    broker.position_deals = {1: [ClosedDeal(1, "EURUSD", NOW, 0.0, MAGIC, 1, 1.1, entry="in", volume=1.0)]}
    broker.positions = [_position(volume=0.5)]
    assert [a.kind for a in PositionGuard(broker).run()] == ["breakeven"]


# ----------------------------------------------------------------- Bouclier

def test_shield_breakeven_before_a_major_release_only_in_profit(monkeypatch, broker):
    _exits(monkeypatch, shield_action="breakeven")
    broker.positions = [_position(price=1.1015)]
    guard = PositionGuard(broker)
    [action] = guard.run(_pipeline(10), NOW)
    assert action.kind == "shield_breakeven" and broker.modified[0][1] == pytest.approx(1.10002)
    assert guard.run(_pipeline(10), NOW) == []                             # une seule action par annonce

    broker.modified.clear()
    broker.positions = [_position(price=1.0995, ticket=2)]                 # en perte : stop d'origine conservé
    [action] = PositionGuard(broker).run(_pipeline(10), NOW)
    assert "pas assez en gain" in action.description and broker.modified == []


@pytest.mark.parametrize("mode, expected", [("close", (1, None, "Bot SHIELD_CLOSE")),
                                            ("reduce", (1, 0.5, "Bot SHIELD_REDUCE"))])
def test_shield_close_or_reduce(monkeypatch, broker, mode, expected):
    _exits(monkeypatch, shield_action=mode)
    broker.positions = [_position(price=1.1005)]
    PositionGuard(broker).run(_pipeline(10), NOW)
    assert broker.closed_volumes == [expected]


def test_no_shield_outside_the_window(monkeypatch, broker):
    _exits(monkeypatch, shield_action="close")
    broker.positions = [_position()]
    assert PositionGuard(broker).run(_pipeline(90), NOW) == []


def test_contrary_publication_closes_the_invalidated_position(broker):
    broker.positions = [_position(price=1.1005)]                           # achat EURUSD
    pipeline = _pipeline(-5, actual="250K", consensus="180K")             # USD fort : EURUSD baissier
    [action] = PositionGuard(broker).run(pipeline, NOW)
    assert action.kind == "invalidated" and "Scénario invalidé" in action.description
    assert broker.closed_volumes == [(1, None, "Bot INVALIDATED")]


def test_favourable_publication_keeps_the_position(broker):
    broker.positions = [_position(price=1.1005)]
    assert PositionGuard(broker).run(_pipeline(-5, actual="120K", consensus="180K"), NOW) == []


# ----------------------------------------------------------------- drawdown

def _account(equity):
    return AccountInfo(login=7, balance=equity, equity=equity, margin=0, margin_free=equity, margin_level=0, profit=0,
                       currency="USD", leverage=100, account_type="demo")


def test_drawdown_peak_persists_and_suspends(monkeypatch, tmp_path):
    monkeypatch.setattr(dd, "settings", replace(settings, risk=replace(settings.risk, max_drawdown_pct=10.0)))
    path = tmp_path / "peaks.json"
    monitor = DrawdownMonitor(path)
    assert monitor.check(_account(10_000)) is None and monitor.check(_account(9_200)) is None
    restarted = DrawdownMonitor(path)                                      # plus haut relu après redémarrage
    reason = restarted.check(_account(8_900))
    assert reason and "11.00%" in reason
    restarted.reset(_account(8_900))
    assert restarted.check(_account(8_900)) is None


def test_engine_suspends_new_entries_on_drawdown(monkeypatch, tmp_path):
    monkeypatch.setattr(dd, "settings", replace(settings, risk=replace(settings.risk, max_drawdown_pct=10.0)))
    monkeypatch.setattr(eng, "evaluate_market", lambda symbol, broker: pytest.fail("aucune analyse attendue"))
    broker = FakeBroker()
    broker.account = _account(8_000)
    engine = TradingEngine(symbols=["EURUSD"], broker=broker)
    engine.drawdown = DrawdownMonitor(tmp_path / "peaks.json")
    engine.drawdown.peaks[DrawdownMonitor.key(broker.account)] = 10_000.0
    [outcome] = engine.step()
    assert outcome.status == "suspended" and "drawdown 20.00%" in outcome.verdict and broker.orders == []
    assert engine.suspension and engine.recent_decisions[-1]["status"] == "suspended"


# ----------------------------------------------------------------- latence, protections, journal

def test_latency_summary():
    tracker = LatencyTracker()
    for seconds in (0.010, 0.020, 0.300):
        tracker.record("ordre", seconds)
    with tracker.measure("analyse"):
        pass
    summary = tracker.summary()
    assert summary["ordre"] == {"last_ms": 300.0, "avg_ms": 110.0, "p95_ms": 300.0, "max_ms": 300.0, "count": 3}
    assert summary["analyse"]["count"] == 1


def test_engine_measures_each_stage(monkeypatch):
    monkeypatch.setattr(eng, "evaluate_market", lambda symbol, broker: {"action": "HOLD", "reason": "range"})
    monkeypatch.setattr(eng, "is_high_impact_news_imminent", lambda symbol=None: False)
    engine = TradingEngine(symbols=["EURUSD"], broker=FakeBroker())
    engine.step()
    assert {"actualités", "analyse", "tour"} <= set(engine.latency.summary())


def test_missing_stop_after_a_real_fill_is_restored_or_the_position_closed():
    broker = FakeBroker()
    broker.positions = [_position(sl=0.0)]
    engine = TradingEngine(symbols=["EURUSD"], broker=broker)
    fill = OrderResult(True, "EURUSD", "BUY", 1.0, 1.1, 1.098, 1.106, ticket=1, dry_run=False)
    assert "reposé" in engine._confirm_protection(fill, 1.098, 1.106)
    assert broker.modified == [(1, 1.098, 1.106)]

    broker.positions = [_position(sl=0.0)]
    broker.modify_stops = lambda *a: OrderResult(False, "EURUSD", "MODIFY", 0.0, message="refusé")
    assert "fermée" in engine._confirm_protection(fill, 1.098, 1.106) and broker.closed == [(1, True)]
    assert engine._confirm_protection(replace(fill, dry_run=True), 1.098, 1.106) is None


def test_journal_records_slippage_and_rules_version(tmp_path):
    memory = TradeMemory(tmp_path)
    entry = memory.record_entry(OrderResult(True, "EURUSD", "BUY", 0.5, 1.10012, 1.098, 1.106, ticket=3),
                                {"entry_price": 1.10002}, {})
    assert entry["planned_entry"] == 1.10002 and entry["slippage"] == pytest.approx(0.0001)   # défavorable
    assert entry["rules_version"].startswith("règles ") and "code" in entry["rules_version"]
    sell = memory.record_entry(OrderResult(True, "EURUSD", "SELL", 0.5, 1.10012, 1.102, 1.094, ticket=4),
                               {"entry_price": 1.10002}, {})
    assert sell["slippage"] == pytest.approx(-0.0001)                     # vente exécutée plus haut : favorable
