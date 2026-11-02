"""Moteur adaptatif : choix du mode, signaux scalping / day / swing, trailing stop, fin de session, kill switch."""

from dataclasses import replace
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from brokers.models import AccountInfo, Position, Tick
from core import engine as eng
from core.engine import TradingEngine
from strategies import regime
from strategies.regime import MarketContext, select_mode, trailing_stop
from tests.fakes import FakeBroker


def _frame(closes, freq="1h", spread=0.0005) -> pd.DataFrame:
    closes = np.asarray(closes, dtype=float)
    index = pd.date_range("2026-10-01", periods=len(closes), freq=freq, tz="UTC")
    return pd.DataFrame({"open": closes, "high": closes + spread, "low": closes - spread, "close": closes}, index=index)


def _up_smooth(n=200, start=1.10):                 # tendance haussière qui accélère : EMA, MACD et prix alignés
    return start + 0.00002 * np.arange(n) ** 2


def _up_zigzag(n=80, start=1.10):                  # tendance haussière avec respirations : RSI ~ 60
    steps = np.tile([0.003, -0.002], n // 2)
    return start + np.concatenate([[0], np.cumsum(steps)[:-1]])


def _flat(n=200, start=1.10):
    return start + 0.0002 * np.tile([1, -1], n // 2)


def _ctx(now, sentiment=0.0, specs=None, **frames) -> MarketContext:
    return MarketContext("EURUSD", now, frames, sentiment, specs)


AT = lambda h, m=0: datetime(2026, 10, 30, h, m, tzinfo=timezone.utc)  # noqa: E731


# ----------------------------------------------------------------- choix du mode

def test_trend_detection():
    assert regime.trend(_frame(_up_smooth())) == 1
    assert regime.trend(_frame(_up_smooth()[::-1])) == -1
    assert regime.trend(_frame(_flat())) == 0
    assert regime.trend(_frame(_up_smooth(30))) == 0                        # historique insuffisant


def test_swing_when_daily_and_hourly_trends_align():
    mode, why = select_mode(_ctx(AT(3), D1=_frame(_up_zigzag(), "1D"), H1=_frame(_up_smooth())))
    assert mode == "swing" and "haussière" in why


def test_strongly_opposite_sentiment_prevents_swing():
    mode, _ = select_mode(_ctx(AT(3), sentiment=-0.5, D1=_frame(_up_zigzag(), "1D"), H1=_frame(_up_smooth())))
    assert mode is None                                                     # 03:00 UTC : ni session ni heures liquides


@pytest.mark.parametrize("now, expected", [(AT(8), "day"), (AT(13), "day"), (AT(17), "scalping"), (AT(22), None)])
def test_mode_follows_sessions_without_background_trend(now, expected):
    calm_m5 = _frame(_flat(150), "5min")
    mode, why = select_mode(_ctx(now, D1=_frame(_flat(80), "1D"), H1=_frame(_flat()), M5=calm_m5))
    assert mode == expected, why


def test_expanding_market_suspends_scalping():
    closes = _flat(150)
    closes[-10:] += np.linspace(0, 0.02, 10)                                 # explosion de volatilité
    m5 = _frame(closes, "5min")
    m5.loc[m5.index[-10:], "high"] += 0.004
    mode, why = select_mode(_ctx(AT(17), D1=_frame(_flat(80), "1D"), H1=_frame(_flat()), M5=m5))
    assert mode is None and "expansion" in why


# ----------------------------------------------------------------- signaux par mode

def test_swing_signal_levels(eurusd_specs):
    ctx = _ctx(AT(3), specs=eurusd_specs, D1=_frame(_up_zigzag(), "1D"), H1=_frame(_up_smooth()))
    signal = regime._swing_signal(ctx)
    assert signal.action == "BUY" and "swing" in signal.verdict
    assert signal.tp_distance == pytest.approx(2 * signal.sl_distance)      # SL 2 ATR / TP 4 ATR


def test_swing_waits_when_daily_rsi_exhausted(eurusd_specs):
    ctx = _ctx(AT(3), specs=eurusd_specs, D1=_frame(_up_smooth(80), "1D"), H1=_frame(_up_smooth()))
    signal = regime._swing_signal(ctx)
    assert signal.action == "HOLD" and "RSI D1" in signal.verdict


def _m15_with_recent_cross(level):
    """Baisse puis reprise : premier nombre de bougies de hausse donnant un croisement EMA9/21 récent."""
    for k in range(1, 40):
        closes = np.concatenate([level - 0.0002 * np.arange(60), level - 0.012 + 0.0015 * np.arange(1, k + 1)])
        diff = (regime._ema(pd.Series(closes), 9) - regime._ema(pd.Series(closes), 21)).dropna()
        if any(np.sign(diff.iloc[-i]) != np.sign(diff.iloc[-i - 1]) for i in range(1, 4)) and diff.iloc[-1] > 0:
            return _frame(closes, "15min")
    raise AssertionError("aucun croisement construit")


def test_day_signal_crossover_with_hourly_trend(eurusd_specs):
    ctx = _ctx(AT(8), specs=eurusd_specs, M15=_m15_with_recent_cross(2.0), H1=_frame(_up_smooth()),
               D1=_frame(_flat(80), "1D"))
    signal = regime._day_signal(ctx, 0.0001)
    assert signal.action == "BUY", signal.verdict
    assert 10 <= signal.sl_distance / 0.0001 <= 20 and 15 <= signal.tp_distance / 0.0001 <= 30


def test_day_signal_blocked_by_nearby_resistance(eurusd_specs):
    h1 = _frame(_up_smooth())
    level = float(h1["close"].iloc[-1]) - 0.0005                             # plus haut H1 juste au-dessus
    ctx = _ctx(AT(8), specs=eurusd_specs, M15=_m15_with_recent_cross(level + 0.012 - 0.0015 * 5), H1=h1,
               D1=_frame(_flat(80), "1D"))
    signal = regime._day_signal(ctx, 0.0001)
    assert signal.action == "HOLD" and ("résistance" in signal.verdict or "croisement" in signal.verdict)


def test_scalping_signal_uses_fixed_pips(eurusd_specs):
    ctx = _ctx(AT(17), specs=eurusd_specs, M1=_frame(_up_smooth(200, 1.1), "1min"))
    signal = regime._scalping_signal(ctx, 0.0001)
    assert signal.action == "BUY" and signal.sl_distance == pytest.approx(0.0005)


class MultiFrameBroker(FakeBroker):
    def __init__(self, frames):
        super().__init__()
        self.frames = frames

    def get_historical_data(self, symbol, count=100, timeframe=None):
        df = self.frames.get(timeframe, pd.DataFrame())
        last = df.iloc[[-1]].copy()
        last.index = last.index + (df.index[-1] - df.index[-2])
        return pd.concat([df, last])                                          # + une bougie « en cours »


def test_evaluate_adaptive_builds_order(monkeypatch, eurusd_specs):
    monkeypatch.setattr(regime, "get_recent_news", lambda: [])
    broker = MultiFrameBroker({"D1": _frame(_up_zigzag(), "1D"), "H1": _frame(_up_smooth()), "M15": _frame(_flat()),
                               "M5": _frame(_flat(150), "5min"), "M1": _frame(_flat(), "1min")})
    broker.specs = {"EURUSD": eurusd_specs}
    broker.ticks = {"EURUSD": Tick("EURUSD", 1.9, 1.9001, 0.0, 10, AT(3))}
    decision = regime.evaluate_adaptive("EURUSD", broker, AT(3))
    assert decision["mode"] == "swing" and decision["action"] == "BUY", decision["verdict"]
    assert decision["order_comment"].startswith("Bot SWING BUY")
    assert decision["suggested_sl"] < 1.9001 < decision["suggested_tp"]
    assert decision["verdict"].startswith("[SWING]")


# ----------------------------------------------------------------- gestion des positions

def _position(ticket, comment, side="BUY", open_=1.1000, current=1.1000, sl=1.0950, tp=1.1100):
    return Position(ticket=ticket, symbol="EURUSD", type=0 if side == "BUY" else 1, volume=1.0, price_open=open_,
                    price_current=current, sl=sl, tp=tp, profit=5.0, swap=0.0, time=0, magic=1, comment=comment)


def test_trailing_stop_rules():
    assert trailing_stop("BUY", 1.1005, 1.1000, 1.0950, atr=0.001) is None              # gain < 1 ATR
    assert trailing_stop("BUY", 1.1030, 1.1000, 1.0950, atr=0.001) == pytest.approx(1.1010)
    assert trailing_stop("BUY", 1.1030, 1.1000, 1.1010, atr=0.001) is None              # déjà au niveau
    assert trailing_stop("SELL", 1.0970, 1.1000, 1.1050, atr=0.001) == pytest.approx(1.0990)


class ManagedBroker(FakeBroker):
    def __init__(self):
        super().__init__()
        self.modified = []

    def modify_stops(self, ticket, sl, tp):
        self.modified.append((ticket, round(sl, 5), tp))
        from brokers.models import OrderResult
        return OrderResult(True, "EURUSD", "MODIFY", 1.0, sl=sl, tp=tp, ticket=ticket)


def test_day_positions_closed_at_session_end_and_scalps_left_alone():
    broker = ManagedBroker()
    broker.positions = [_position(1, "Bot DAY BUY 40%"), _position(2, "Bot SCALP SELL 10%", side="SELL")]
    assert regime.manage_positions(broker, AT(19)) == []
    actions = regime.manage_positions(broker, AT(20, 30))
    assert [a.kind for a in actions] == ["day_close"] and broker.closed == [(1, False)]


def test_swing_trailing_moves_stop_in_profit():
    broker = ManagedBroker()
    broker.bars = _frame(_flat(), spread=0.0005)                            # ATR H1 ~ 0.0014
    broker.positions = [_position(7, "Bot SWING BUY 60%", current=1.1060)]
    [action] = regime.manage_positions(broker, AT(12), known=None)
    assert action.kind == "trailing" and broker.modified[0][0] == 7
    assert 1.0950 < broker.modified[0][1] < 1.1060


def test_position_mode_from_memory_or_comment():
    position = _position(3, "Bot DAY SELL 30%")
    assert regime.position_mode(position) == "day"
    assert regime.position_mode(position, {3: "swing"}) == "swing"
    assert regime.position_mode(_position(4, "manuel")) is None


# ----------------------------------------------------------------- kill switch journalier

def test_daily_stop_closes_everything_pauses_and_locks(monkeypatch):
    broker = FakeBroker()
    broker.account = AccountInfo(login=1, balance=100_000, equity=96_500, margin=0, margin_free=96_500,
                                 margin_level=0, profit=-3_500, currency="USD", leverage=100)
    broker.positions = [_position(11, "Bot SCALP BUY"), _position(12, "Bot DAY SELL", side="SELL")]
    evaluated = []
    monkeypatch.setattr(eng, "evaluate_market", lambda symbol, broker: evaluated.append(symbol) or {"action": "HOLD"})
    engine = TradingEngine(symbols=["EURUSD"], broker=broker)
    assert engine.step() == [] and evaluated == []
    assert broker.closed == [(11, True), (12, True)] and engine.is_paused
    record = engine.recent_decisions[-1]
    assert record["status"] == "kill_switch" and "3.50%" in record["verdict"]

    broker.positions = []
    engine.resume()
    assert engine.step() == [] and evaluated == []                           # journée verrouillée
    assert len(broker.closed) == 2


def test_no_kill_switch_below_limit(monkeypatch):
    broker = FakeBroker()
    broker.account = AccountInfo(login=1, balance=100_000, equity=99_000, margin=0, margin_free=99_000,
                                 margin_level=0, profit=-1_000, currency="USD", leverage=100)
    monkeypatch.setattr(eng, "is_high_impact_news_imminent", lambda symbol: False)
    monkeypatch.setattr(eng, "evaluate_market", lambda symbol, broker: {"action": "HOLD", "reason": "r"})
    engine = TradingEngine(symbols=["EURUSD"], broker=broker)
    assert [o.status for o in engine.step()] == ["hold"] and not engine.is_paused


def test_adaptive_engine_records_mode_and_manages(monkeypatch):
    adaptive = replace(eng.settings, strategy=replace(eng.settings.strategy, profile="adaptive"))
    monkeypatch.setattr(eng, "settings", adaptive)
    monkeypatch.setattr(eng, "is_high_impact_news_imminent", lambda symbol: False)
    monkeypatch.setattr(eng, "evaluate_adaptive", lambda symbol, broker: {
        "action": "HOLD", "mode": "day", "verdict": "[DAY] aucun croisement", "reason": "r"})
    managed = []
    monkeypatch.setattr(eng, "manage_positions", lambda broker, known: managed.append(True) or [])
    engine = TradingEngine(symbols=["EURUSD"], broker=FakeBroker())
    engine.step()
    assert managed == [True] and engine.recent_decisions[-1]["mode"] == "day"
