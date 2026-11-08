"""
Analyse descendante : règle des 3 bougies (W1), zones d'offre / demande majeures (H4 sur niveau D1),
raffinement de l'entrée H1 -> M30 -> M15 (-> M5) pour tenir le stop, objectif bloqué par une
résistance D1, intégration au registre et au moteur.
"""

from dataclasses import replace
from datetime import datetime, timezone

import pandas as pd
import pytest

from config.settings import settings
from core import engine as eng
from core.engine import TradingEngine
from models.schemas import SetupStatus, Side
from strategies import top_down as td
from strategies.base import Quote
from strategies.market_structure import Level, Zone, supply_demand_zones, three_candle_direction
from strategies.registry import SETUP_STRATEGIES
from strategies.top_down import TopDownStrategy, is_fine_symbol
from tests.fakes import FakeBroker

T0 = pd.Timestamp("2026-11-02 00:00", tz="UTC")
NOW = datetime(2026, 11, 3, 14, 0, tzinfo=timezone.utc)

# Baisse en sommets décroissants jusqu'à 1.1000, puis rebond qui clôture au-dessus du dernier sommet (1.1044)
PATH = [1.1100, 1.1104, 1.1098, 1.1103, 1.1099, 1.1102, 1.1097, 1.1101, 1.1098, 1.1100,
        1.1090, 1.1080, 1.1070, 1.1075, 1.1080, 1.1072, 1.1060, 1.1050, 1.1055, 1.1062,
        1.1052, 1.1040, 1.1030, 1.1035, 1.1042, 1.1030, 1.1020, 1.1010, 1.1000, 1.1008,
        1.1015, 1.1025, 1.1035, 1.1045]


def _bars(closes, start=T0, freq="1h", wick=0.0002):
    """Ouverture au milieu de la clôture précédente et de la clôture : sommets / creux stricts."""
    opens = [closes[0]] + [(a + b) / 2 for a, b in zip(closes, closes[1:])]
    index = pd.date_range(start, periods=len(closes), freq=freq)
    return pd.DataFrame({"open": opens, "high": [max(o, c) + wick for o, c in zip(opens, closes)],
                         "low": [min(o, c) - wick for o, c in zip(opens, closes)], "close": closes}, index=index)


def _flat(n, freq, price, span, end=T0):
    index = pd.date_range(end=end, periods=n, freq=freq)
    return pd.DataFrame({"open": price, "high": price + span / 2, "low": price - span / 2, "close": price},
                        index=index)


def _candle(o, h, l, c):
    return {"open": o, "high": h, "low": l, "close": c}


def _week(*candles):
    return pd.DataFrame(candles, index=pd.date_range("2026-10-12", periods=len(candles), freq="7D", tz="UTC"))


# ----------------------------------------------------------------- règle des 3 bougies (W1)

@pytest.mark.parametrize("candles, direction", [
    # doji, clôture sous son plus bas, bougie suivante encore en dessous : baissier (exemple de la vidéo)
    ((_candle(1.10, 1.12, 1.08, 1.101), _candle(1.10, 1.10, 1.06, 1.07), _candle(1.07, 1.08, 1.05, 1.06)), "baissier"),
    ((_candle(1.10, 1.12, 1.08, 1.101), _candle(1.10, 1.14, 1.10, 1.13), _candle(1.13, 1.15, 1.12, 1.14)), "haussier"),
    # la dernière bougie revient de l'autre côté du doji : pas de direction
    ((_candle(1.10, 1.12, 1.08, 1.101), _candle(1.10, 1.14, 1.10, 1.13), _candle(1.13, 1.13, 1.05, 1.07)), "range"),
    # doji sur la dernière bougie : confirmation attendue
    ((_candle(1.00, 1.05, 0.99, 1.04), _candle(1.04, 1.08, 1.03, 1.07), _candle(1.07, 1.09, 1.05, 1.071)), "range"),
    # sans doji : majorité des bougies et clôture au-delà de l'ouverture de la première
    ((_candle(1.00, 1.05, 0.99, 1.04), _candle(1.04, 1.08, 1.03, 1.07), _candle(1.075, 1.09, 1.055, 1.06)), "haussier"),
])
def test_three_candle_rule(candles, direction):
    assert three_candle_direction(_week(*candles))["direction"] == direction


def test_three_candle_rule_needs_three_closed_candles():
    assert three_candle_direction(_week(_candle(1, 2, 0.5, 1.5)))["direction"] == "range"


# ----------------------------------------------------------------- offre / demande H4

def test_demand_zone_is_major_only_on_a_daily_support():
    h4 = _bars(PATH, freq="4h")
    major = supply_demand_zones(h4, [Level("support", 1.0999, 3, False)], tolerance=0.0005)
    minor = supply_demand_zones(h4, [Level("support", 1.0500, 3, False)], tolerance=0.0005)
    # la baisse laisse 3 zones d'offre actives (la 4e, reprise par la remontée, est invalidée), le rebond une demande
    assert [(z.kind, z.major) for z in major] == [("offre", False)] * 3 + [("demande", True)]
    assert [z.major for z in minor if z.kind == "demande"] == [False]


@pytest.mark.parametrize("symbol, fine", [("XAUUSD", True), ("BTCUSD", True), ("US30.cash", True),
                                          ("NAS100", True), ("EURUSD", False)])
def test_fine_refinement_for_metals_crypto_and_indices(symbol, fine):
    assert is_fine_symbol(symbol, settings.strategy.topdown_fine_symbols) is fine


# ----------------------------------------------------------------- stratégie complète

@pytest.fixture
def context(monkeypatch):
    """Direction W1 haussière, support D1 à 1.1000 (3 touches), demande H4 majeure 1.0990 - 1.1010."""
    monkeypatch.setattr(td, "three_candle_direction",
                        lambda df: {"direction": "haussier", "reason": "clôture au-dessus du doji, confirmée",
                                    "doji": 0})
    levels = [Level("support", 1.1000, 3, True), Level("résistance", 1.2000, 3, False)]
    monkeypatch.setattr(td, "key_levels", lambda *a, **k: levels)
    zone = Zone("demande", "haussier", 1.0990, 1.1010, (T0 - pd.Timedelta(days=5)).isoformat(), True, major=True)
    monkeypatch.setattr(td, "supply_demand_zones", lambda *a, **k: [zone])
    return levels


def _data(h4_span=0.01, **refinement):
    data = {"W1": _flat(10, "7D", 1.1005, 0.03), "D1": _flat(80, "1D", 1.1005, 0.012),
            "H4": _flat(80, "4h", 1.1005, h4_span, end=T0 + pd.Timedelta(hours=8))}
    start = T0 + pd.Timedelta(hours=8) - pd.Timedelta(hours=40)
    data["H1"] = refinement.pop("H1", _bars(PATH, start=start))
    data.update({tf: refinement.get(tf, pd.DataFrame(columns=["open", "high", "low", "close"]))
                 for tf in ("M30", "M15", "M5")})
    return data


def _setup(data, quote, symbol="EURUSD"):
    return TopDownStrategy().find_setup(symbol, data, NOW, quote)


def test_ready_buy_on_h1_after_the_change_of_character(context):
    setup = _setup(_data(), Quote(1.10450, 1.10452, 0.00001))
    assert setup.side == Side.BUY and setup.status == SetupStatus.READY
    assert "W1" in setup.reason and "raffinement H1" in setup.reason and "changement de caractère" in setup.reason
    assert setup.stop_loss < setup.indicators["plus_bas_reaction"] < setup.entry_price
    assert setup.indicators["declencheur"] == pytest.approx(1.1044, abs=1e-6)
    assert setup.net_reward_risk == pytest.approx(3.0, abs=0.01)
    assert any("liquidité prise" in n for n in setup.notes)                # creux sous le support aux creux égaux


def test_entry_is_refined_lower_when_the_h1_stop_is_too_wide(context):
    start = T0 + pd.Timedelta(hours=8) - pd.Timedelta(hours=40)
    tight = _bars([1.1100 + (c - 1.1100) * 0.5 for c in PATH], start=start, wick=0.0001)   # moitié moins ample
    data = _data(h4_span=0.004, M30=tight)
    setup = _setup(data, Quote(1.10722, 1.10725, 0.00001))
    assert setup.status == SetupStatus.READY and "raffinement M30" in setup.reason
    assert setup.entry_price - setup.stop_loss <= 0.004


def test_stop_too_wide_even_after_refinement_is_invalidated(context):
    setup = _setup(_data(h4_span=0.001), Quote(1.10450, 1.10452, 0.00001))
    assert setup.status == SetupStatus.INVALIDATED and "stop trop large même raffiné" in setup.notes[-1]


def test_daily_resistance_before_the_target_blocks_the_trade(context):
    context[1] = Level("résistance", 1.1060, 3, False)
    setup = _setup(_data(), Quote(1.10450, 1.10452, 0.00001))
    assert setup.status == SetupStatus.INVALIDATED and "résistance D1 avant l'objectif" in setup.notes[-1]
    assert setup.indicators["obstacle_d1"] == pytest.approx(1.1060)


def test_watching_until_the_lower_high_is_taken_out(context):
    data = _data()
    data["H1"] = data["H1"].iloc[:-1]                                        # cassure pas encore clôturée
    setup = _setup(data, Quote(1.10350, 1.10352, 0.00001))
    assert setup.status == SetupStatus.WATCHING and "attente de la clôture au-dessus" in setup.reason


def test_no_setup_without_weekly_direction(context, monkeypatch):
    monkeypatch.setattr(td, "three_candle_direction", lambda df: {"direction": "range", "reason": "", "doji": None})
    assert _setup(_data(), Quote(1.10450, 1.10452, 0.00001)) is None


def test_no_setup_without_a_major_demand_zone(context, monkeypatch):
    monkeypatch.setattr(td, "supply_demand_zones", lambda *a, **k: [])
    assert _setup(_data(), Quote(1.10450, 1.10452, 0.00001)) is None


# ----------------------------------------------------------------- direct : cache, registre, moteur

def test_higher_timeframes_are_cached_between_cycles(monkeypatch):
    broker, calls = FakeBroker(), []
    broker.get_historical_data = lambda symbol, count=100, timeframe=None: calls.append(timeframe) or _flat(5, "1h", 1, 0.1)
    clock = iter([0.0, 30.0])
    monkeypatch.setattr(td.time, "monotonic", lambda: next(clock))
    strategy = TopDownStrategy()
    strategy.load(broker, "EURUSD")
    calls.clear()
    strategy.load(broker, "EURUSD")                                        # 30 s plus tard
    assert set(calls) == {"M30", "M15", "M5"}                              # W1, D1, H4, H1 relus du cache


def test_registered_and_tried_first_by_the_engine(monkeypatch):
    assert SETUP_STRATEGIES["descendante"] is TopDownStrategy
    monkeypatch.setattr(eng, "settings", replace(settings, strategy=replace(settings.strategy, topdown_enabled=True,
                                                                            impulse_enabled=True)))
    engine = TradingEngine(symbols=["EURUSD"], broker=FakeBroker())
    assert [s.name for s in engine.opportunities][:2] == ["descendante", "impulsion"]
