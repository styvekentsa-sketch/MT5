"""
Protection automatique des positions : R figé, prix d'entrée + frais à +1 R, stop suiveur à 1 R du
meilleur prix dès +1,5 R, jamais de recul, pas minimal, contraintes du courtier, TP à 3 R, vérification
des modifications, reprise après redémarrage.
"""

from dataclasses import replace
from datetime import datetime, timezone

import pandas as pd
import pytest

from brokers.models import POSITION_TYPE_BUY, POSITION_TYPE_SELL, ClosedDeal, OrderResult, Position, SymbolSpecs, Tick
from config.settings import settings
from core import position_guard as pg
from core import position_protection as pp
from core.position_guard import PositionGuard
from core.position_protection import PositionProtection, ProtectionStore
from tests.fakes import FakeBroker

MAGIC = settings.trading.magic_number
OPENED = int(datetime(2026, 11, 7, 9, 0, tzinfo=timezone.utc).timestamp())
NOW = OPENED + 3600.0
SPECS = SymbolSpecs(symbol="EURUSD", digits=5, point=0.00001, tick_size=0.00001, tick_value=1.0, volume_min=0.01,
                    volume_max=100, volume_step=0.01, stops_level=0, freeze_level=0, trade_allowed=True)


def _protection(monkeypatch, **changes):
    cfg = replace(settings.protection, enabled=True, **changes)
    patched = replace(settings, protection=cfg)
    monkeypatch.setattr(pp, "settings", patched)
    monkeypatch.setattr(pg, "settings", patched)


@pytest.fixture(autouse=True)
def enabled(monkeypatch):
    _protection(monkeypatch)


def _buy(sl=1.0980, tp=1.1060, ticket=1, magic=MAGIC):
    return Position(ticket=ticket, symbol="EURUSD", type=POSITION_TYPE_BUY, volume=1.0, price_open=1.1000,
                    price_current=1.1000, sl=sl, tp=tp, profit=0.0, swap=0.0, time=OPENED, magic=magic)


def _sell(sl=1.1020, tp=1.0940):
    return replace(_buy(sl=sl, tp=tp), type=POSITION_TYPE_SELL)


def _at(broker, bid, spread=0.00002):
    broker.ticks = {"EURUSD": Tick("EURUSD", bid, round(bid + spread, 5), 0.0, 2, datetime.now(timezone.utc))}


@pytest.fixture
def broker():
    fake = FakeBroker()
    fake.specs = {"EURUSD": SPECS}
    return fake


@pytest.fixture
def store(tmp_path):
    return ProtectionStore(tmp_path / "protection.json")


def _run(broker, store, position=None):
    position = position or broker.positions[0]
    return PositionProtection(broker, store).protect(position, now=NOW)


def _sl(broker):
    return broker.positions[0].sl


# ----------------------------------------------------------------- R et paliers

def test_initial_risk_is_frozen_at_first_sight(broker, store):
    broker.positions = [_buy()]
    _at(broker, 1.1005)
    assert _run(broker, store) == []
    state = store.states[f"{MAGIC}:1"]
    assert state.risk == pytest.approx(0.0020) and state.initial_stop == 1.0980
    broker.positions = [_buy(sl=1.0990)]                                   # stop resserré à la main
    _run(broker, store)
    assert store.states[f"{MAGIC}:1"].risk == pytest.approx(0.0020)        # R jamais recalculé


def test_nothing_below_one_r(broker, store):
    broker.positions = [_buy()]
    _at(broker, 1.10199)                                                   # 0.995 R
    assert _run(broker, store) == [] and broker.modified == []


def test_breakeven_at_one_r_with_fee_margin(monkeypatch, broker, store):
    _protection(monkeypatch, commission_per_lot=7.0, breakeven_margin_points=3)
    broker.positions = [_buy()]
    _at(broker, 1.1020)                                                    # +1 R
    [action] = _run(broker, store)
    assert action.kind == "protect_breakeven" and action.success
    assert _sl(broker) == pytest.approx(1.10010)                           # entrée + 7 pts commission + 3 pts
    assert store.states[f"{MAGIC}:1"].stage == "breakeven"


def test_trailing_from_one_and_a_half_r_at_one_r_from_the_best_bid(broker, store):
    broker.positions = [_buy()]
    _at(broker, 1.1030)                                                    # +1,5 R
    [action] = _run(broker, store)
    assert action.kind == "protect_trailing" and _sl(broker) == pytest.approx(1.1010)


def test_sell_trails_the_best_ask(broker, store):
    broker.positions = [_sell()]
    _at(broker, 1.09698)                                                   # Ask 1.09700 : +1,5 R
    [action] = _run(broker, store)
    assert action.kind == "protect_trailing" and _sl(broker) == pytest.approx(1.0990)


def test_stop_never_moves_back_and_respects_the_minimum_step(broker, store):
    broker.positions = [_buy()]
    _at(broker, 1.1030)
    _run(broker, store)
    assert _sl(broker) == pytest.approx(1.1010)
    _at(broker, 1.1022)                                                    # le marché revient
    assert _run(broker, store) == [] and _sl(broker) == pytest.approx(1.1010)
    _at(broker, 1.10315)                                                   # +0,075 R de mieux : sous le pas
    assert _run(broker, store) == []
    _at(broker, 1.10325)                                                   # +0,125 R depuis le dernier stop
    [action] = _run(broker, store)
    assert action.success and _sl(broker) == pytest.approx(1.10125)
    assert len(broker.modified) == 2


# ----------------------------------------------------------------- contraintes du courtier

def test_stop_is_rounded_to_the_tick_on_the_safe_side(broker, store):
    broker.specs = {"EURUSD": replace(SPECS, tick_size=0.00005)}
    broker.positions = [_buy()]
    _at(broker, 1.10313)                                                   # stop idéal 1.10113
    _run(broker, store)
    assert _sl(broker) == pytest.approx(1.10110)                           # arrondi vers le bas (BUY)


def test_stop_is_kept_at_the_broker_minimum_distance(monkeypatch, broker, store):
    _protection(monkeypatch, trailing_distance_r=0.1, trailing_start_r=1.5)
    broker.specs = {"EURUSD": replace(SPECS, stops_level=50)}
    broker.positions = [_buy()]
    _at(broker, 1.1030)                                                    # stop idéal 1.1028 (0,1 R)
    _run(broker, store)
    assert _sl(broker) == pytest.approx(1.1025)                            # Bid - 50 points


def test_no_modification_inside_the_freeze_zone(broker, store):
    broker.specs = {"EURUSD": replace(SPECS, freeze_level=100)}
    broker.positions = [_buy(tp=1.1035)]
    _at(broker, 1.1030)                                                    # 50 points du TP < zone de gel
    assert _run(broker, store) == [] and broker.modified == []


def test_symbol_not_tradable_is_left_alone(broker, store):
    broker.specs = {"EURUSD": replace(SPECS, trade_allowed=False)}
    broker.positions = [_buy()]
    _at(broker, 1.1030)
    assert _run(broker, store) == []


# ----------------------------------------------------------------- TP et stop absent

def test_missing_take_profit_is_set_at_three_r_existing_one_is_kept(broker, store):
    broker.positions = [_buy(tp=0.0)]
    _at(broker, 1.1005)
    [action] = _run(broker, store)
    assert action.kind == "protect_tp" and broker.positions[0].tp == pytest.approx(1.1060)
    broker.positions = [_buy(tp=1.1045, ticket=2)]
    _at(broker, 1.1030)
    _run(broker, store)
    assert broker.modified[-1] == (2, pytest.approx(1.1010), None)         # TP de l'ordre jamais touché


def test_missing_stop_is_restored_at_the_initial_stop(broker, store):
    broker.positions = [_buy()]
    _at(broker, 1.1005)
    _run(broker, store)
    broker.positions = [_buy(sl=0.0)]                                      # stop supprimé
    [action] = _run(broker, store)
    assert action.kind == "protect_stop" and _sl(broker) == pytest.approx(1.0980)


# ----------------------------------------------------------------- refus et vérification

def test_refused_modification_is_logged_and_retried_later(broker, store):
    broker.positions = [_buy()]
    broker.modify_stops = lambda *a: OrderResult(False, "EURUSD", "MODIFY", 0.0, message="Invalid stops")
    _at(broker, 1.1030)
    protection = PositionProtection(broker, store)
    [action] = protection.protect(broker.positions[0], now=NOW)
    assert action.kind == "protect_error" and not action.success and "Invalid stops" in action.description
    assert protection.protect(broker.positions[0], now=NOW + 10) == []     # pas de rafale de requêtes
    assert ProtectionStore(store.path).states[f"{MAGIC}:1"].last_error == "Invalid stops"


def test_modification_not_found_at_the_broker_is_an_error(broker, store):
    broker.positions = [_buy()]
    broker.modify_stops = lambda *a: OrderResult(True, "EURUSD", "MODIFY", 1.0, dry_run=False)   # rien appliqué
    _at(broker, 1.1030)
    [action] = _run(broker, store)
    assert not action.success and "non appliquée" in action.description


def test_positions_of_another_robot_are_ignored(broker, store):
    broker.positions = [_buy(magic=MAGIC + 1)]
    _at(broker, 1.1030)
    assert _run(broker, store) == [] and store.states == {}


# ----------------------------------------------------------------- redémarrage

def test_restart_resumes_with_the_saved_best_price(broker, store):
    broker.positions = [_buy()]
    _at(broker, 1.1032)
    _run(broker, store)                                                    # meilleur Bid 1.1032 sauvegardé
    broker.modified.clear()
    restarted = ProtectionStore(store.path)
    assert restarted.states[f"{MAGIC}:1"].best == pytest.approx(1.1032)
    _at(broker, 1.1025)
    assert PositionProtection(broker, restarted).protect(broker.positions[0], now=NOW) == []
    assert _sl(broker) == pytest.approx(1.1012)                           # stop conservé, pas de recul


def test_best_price_reached_while_stopped_is_caught_up_from_m1_bars(broker, store):
    broker.positions = [_buy()]
    times = pd.to_datetime([OPENED, OPENED + 60, OPENED + 120], unit="s", utc=True)
    broker.bars = pd.DataFrame({"high": [1.1090, 1.1034, 1.1021], "low": [1.0990, 1.1010, 1.1015],
                                "spread": [2, 2, 2]}, index=times)        # 1re bougie : minute d'ouverture, ignorée
    _at(broker, 1.1020)
    [action] = _run(broker, store)
    assert action.kind == "protect_trailing" and _sl(broker) == pytest.approx(1.1014)


def test_state_is_forgotten_once_the_position_is_closed(broker, store):
    broker.positions = [_buy()]
    _at(broker, 1.1005)
    protection = PositionProtection(broker, store)
    protection.protect(broker.positions[0], now=NOW)
    protection.forget_closed([], now=NOW)
    assert f"{MAGIC}:1" in store.states                                   # disparue mais clôture inconnue
    broker.position_deals = {1: [ClosedDeal(9, "EURUSD", datetime.now(timezone.utc), 5.0, MAGIC, 1, entry="out")]}
    protection.forget_closed([], now=NOW)
    assert ProtectionStore(store.path).states == {}


# ----------------------------------------------------------------- gardien des positions

def test_guard_applies_the_protection_each_cycle(broker, tmp_path):
    broker.positions = [_buy()]
    _at(broker, 1.1030)
    guard = PositionGuard(broker, protection_store=ProtectionStore(tmp_path / "p.json"))
    guard.register(1, 1.1000, 1.0980, 1.0)
    [action] = guard.run()
    assert action.kind == "protect_trailing" and _sl(broker) == pytest.approx(1.1010)


def test_guard_skips_the_protection_when_tp1_already_moved_the_stop(monkeypatch, broker, tmp_path):
    monkeypatch.setattr(pg, "settings", replace(pg.settings, exits=replace(settings.exits, tp1_fraction=0.5)))
    broker.positions = [replace(_buy(), price_current=1.1030)]
    _at(broker, 1.1030)
    guard = PositionGuard(broker, protection_store=ProtectionStore(tmp_path / "p.json"))
    guard.register(1, 1.1000, 1.0980, 1.0)
    assert [a.kind for a in guard.run()] == ["tp1", "breakeven"]           # protection au tour suivant


def test_disabled_protection_does_nothing(monkeypatch, broker, tmp_path):
    _protection(monkeypatch)
    monkeypatch.setattr(pg, "settings", replace(pg.settings, protection=replace(settings.protection, enabled=False)))
    broker.positions = [_buy()]
    _at(broker, 1.1030)
    assert PositionGuard(broker, protection_store=ProtectionStore(tmp_path / "p.json")).run() == []
