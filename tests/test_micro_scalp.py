"""
Mode « Petits trades » (micro-scalping) : micro-cassure et petit rebond M5, objectif court (1.2 R)
accepté par le vérificateur pour ce seul mode, sessions ; cohabitation avec le moteur (priorité à
l'impulsion, limites de risque globales, journal identifiant la stratégie).
"""

from dataclasses import replace
from datetime import datetime

import numpy as np
import pandas as pd
import pytest

from brokers.models import AccountInfo, ClosedDeal, Position, POSITION_TYPE_BUY, Tick
from config.settings import settings
from core import engine as eng
from core.engine import TradingEngine
from learning.trade_memory import TradeMemory
from models.schemas import SetupStatus, Side, TradingStyle
from risk.verifier import verify_order
from strategies import micro_scalp as scalp_module
from strategies.base import Quote, mirror_frame, net_reward_risk
from strategies.impulse import ImpulseStrategy
from strategies.micro_scalp import MicroScalpStrategy
from tests.fakes import FakeBroker
from tests.test_strategies import _h1_context, ohlc

SIGNAL_AT = pd.Timestamp("2026-11-03 08:30", tz="UTC")           # ouverture de la bougie de signal (Londres)
BREAKOUT = (1.0996, 1.1006, 1.0995, 1.1005)


def _m5(rows_after=(BREAKOUT,), end=SIGNAL_AT) -> pd.DataFrame:
    """M5 : micro-consolidation serrée (sommet ~1.0998), puis la bougie de signal."""
    rows = []
    for i in range(40):
        o, c = (1.0993, 1.0997) if i % 2 == 0 else (1.0997, 1.0993)
        rows.append((o, max(o, c) + 0.0001, min(o, c) - 0.0001, c))
    rows += list(rows_after)
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"],
                        index=pd.date_range(end=end, periods=len(rows), freq="5min"))


def _rebound() -> pd.DataFrame:
    """M5 : montée régulière, repli de 3 bougies jusqu'à l'EMA 20, puis bougie haussière franche."""
    closes = list(1.0950 + 0.0002 * np.arange(40))
    top = closes[-1]
    closes += [top - 0.0007, top - 0.0014, top - 0.0019]
    frame = ohlc(closes, start="2026-11-03 05:00", freq="5min", wick=(0.00002, 0.00005), seed=4)
    last = frame["close"].iloc[-1]
    signal = pd.DataFrame([(last, last + 0.0011, last - 0.00002, last + 0.0010)], columns=frame.columns,
                          index=[frame.index[-1] + pd.Timedelta(minutes=5)])
    frame = pd.concat([frame, signal])
    return frame.set_axis(pd.date_range(end=SIGNAL_AT, periods=len(frame), freq="5min"))


def _quote(trig) -> Quote:
    close = float(trig["close"].iloc[-1])
    return Quote(bid=close, ask=close + 0.00002, tick_size=0.00001)


def _decision(trig) -> datetime:
    return (trig.index[-1] + pd.Timedelta(minutes=5)).to_pydatetime()


def _find(trig=None, ctx=None):
    trig = _m5() if trig is None else trig
    ctx = _h1_context() if ctx is None else ctx                     # tendance haussière : EMA 20 > EMA 50
    return MicroScalpStrategy().find_setup("EURUSD", {"M15": ctx, "M5": trig}, _decision(trig), _quote(trig)), trig


# ----------------------------------------------------------------- détection et plan

def test_micro_breakout_gives_a_short_target_trade():
    setup, trig = _find()
    assert setup.status == SetupStatus.READY, setup.notes
    assert setup.strategy == "petits_trades" and setup.style == TradingStyle.SCALPING
    atr = setup.indicators["atr"]
    quote = _quote(trig)
    assert setup.stop_loss == pytest.approx(quote.ask - settings.strategy.scalp_sl_atr * atr, abs=1e-9)
    assert net_reward_risk(Side.BUY, setup.entry_price, setup.stop_loss, setup.take_profit, quote.spread) == \
        pytest.approx(settings.strategy.scalp_tp_r, abs=1e-6)                       # 1.2 R, pas 3 R
    assert "micro-cassure" in setup.reason


def test_small_rebound_on_the_rising_ema():
    setup, _ = _find(_rebound())
    assert setup is not None and setup.status == SetupStatus.READY, setup and setup.notes
    assert "petit rebond" in setup.reason


@pytest.mark.parametrize("signal", [
    (1.0996, 1.0999, 1.0995, 1.0998),                              # corps minuscule
    (1.0996, 1.1012, 1.0995, 1.0999),                              # rejet : clôture loin du plus haut
])
def test_weak_signal_candles_are_ignored(signal):
    setup, _ = _find(_m5((signal,)))
    assert setup is None or setup.side == Side.SELL


def test_only_with_the_m15_micro_trend():
    down = mirror_frame(_h1_context(), 10.0)                        # EMA 20 sous EMA 50
    setup, _ = _find(ctx=down)
    assert setup is None or setup.side == Side.SELL


@pytest.mark.parametrize("end, ok", [("2026-11-03 08:30", True), ("2026-11-03 13:00", True),
                                     ("2026-11-03 03:00", False), ("2026-11-03 18:00", False)])
def test_small_trades_only_in_active_sessions(end, ok):
    setup, _ = _find(_m5(end=pd.Timestamp(end, tz="UTC")))
    assert (setup.status == SetupStatus.READY) == ok
    if not ok:
        assert "Hors session de liquidité" in setup.notes[-1]


# ----------------------------------------------------------------- vérificateur : ratio propre au mode

@pytest.fixture
def broker(eurusd_specs):
    trig, ctx = _m5(), _h1_context()
    quote = _quote(trig)
    fake = FakeBroker()
    fake.account = AccountInfo(login=1, balance=10_000, equity=10_000, margin=0, margin_free=10_000, margin_level=0,
                               profit=0, currency="USD", leverage=100, account_type="demo")
    fake.specs = {"EURUSD": eurusd_specs}
    fake.ticks = {"EURUSD": Tick("EURUSD", quote.bid, quote.ask, 0.0, 2, _decision(trig))}
    fake.profit_fn = lambda side, symbol, volume, o, c: round((c - o if side == "BUY" else o - c) / 0.00001 * volume, 2)
    fake.margin_fn = lambda *a: 100.0

    def history(symbol, count=100, timeframe=None):
        frame, step = (trig, pd.Timedelta(minutes=5)) if timeframe in ("M1", "M5") else (ctx, pd.Timedelta(minutes=15))
        return pd.concat([frame, frame.iloc[-1:].set_axis([frame.index[-1] + step])])
    fake.get_historical_data = history
    return fake


def test_verifier_accepts_1_2r_only_for_small_trades(broker):
    setup, _ = _find()
    args = ("EURUSD", "BUY", setup.entry_price, setup.stop_loss, setup.take_profit, broker)
    assert verify_order(*args, min_ratio=MicroScalpStrategy().min_net_reward_risk).approved
    refused = verify_order(*args)                                    # règle globale : 3 R
    assert not refused.approved and any("ratio net" in f for f in refused.failures)


@pytest.mark.parametrize("field, value", [("scalp_tp_r", 0.5), ("scalp_sl_atr", 0), ("scalp_timeframe", "M15")])
def test_invalid_scalp_settings(field, value):
    from config.env import ConfigError
    with pytest.raises(ConfigError):
        replace(settings.strategy, **{field: value}).validate()


# ----------------------------------------------------------------- moteur : cohabitation

@pytest.fixture
def engine(broker, monkeypatch, tmp_path):
    monkeypatch.setattr(eng, "evaluate_market", lambda symbol, broker: {
        "symbol": symbol, "action": "HOLD", "confidence": 0, "reason": "range", "verdict": "Aucun signal"})
    monkeypatch.setattr(eng, "is_high_impact_news_imminent", lambda symbol=None: False)
    monkeypatch.setattr(scalp_module, "liquidity_session", lambda at: "Londres")
    engine = TradingEngine(symbols=["EURUSD"], broker=broker)
    engine.scalp = MicroScalpStrategy()
    engine.memory = TradeMemory(tmp_path)
    return engine


def test_engine_opens_a_small_trade_tagged_and_journaled(engine, broker):
    [outcome] = engine.step()
    assert outcome.status == "traded", outcome.verdict
    assert outcome.verdict.startswith("[Petits trades] BUY")
    [order] = broker.orders
    assert order["sl"] < order["tp"]
    [entry] = engine.memory.trades.load()
    assert entry["strategy"] == "petits_trades" and entry["mode"] == "scalping"     # distinct du momentum
    risk = (broker.ticks["EURUSD"].ask - order["sl"]) / 0.00001 * order["volume"]
    assert risk <= 0.01 * 10_000 + 1e-6                                          # 1 % maximum


def test_order_comment_identifies_the_mode(engine, broker, monkeypatch):
    comments = []
    original = broker.place_order
    monkeypatch.setattr(broker, "place_order",
                        lambda *a, comment="", **k: comments.append(comment) or original(*a, comment=comment, **k))
    engine.step()
    assert comments == ["Bot PETIT BUY"]


def _open_position(ticket):
    return Position(ticket=ticket, symbol="GBPUSD", type=POSITION_TYPE_BUY, volume=0.01, price_open=1.3,
                    price_current=1.3, sl=1.29, tp=1.33, profit=0.0, swap=0.0, time=0,
                    magic=settings.trading.magic_number)


def test_small_trades_respect_the_maximum_number_of_positions(engine, broker):
    broker.positions = [_open_position(1), _open_position(2)]       # RISK_MAX_OPEN_POSITIONS = 2
    [outcome] = engine.step()
    assert outcome.status == "hold" and broker.orders == []
    assert "petits trades : signal écarté" in outcome.verdict and "positions" in outcome.verdict


def test_small_trades_stop_at_the_daily_loss_limit(engine, broker):
    broker.deals = [ClosedDeal(ticket=9, symbol="EURUSD", time=datetime.now(), net_profit=-400.0,
                               magic=settings.trading.magic_number)]          # -3.8 % aujourd'hui
    outcomes = engine.step()
    assert broker.orders == []
    assert engine.recent_decisions[-1]["status"] == "kill_switch" or any(
        "perte journalière" in (o.verdict or "") for o in outcomes)


def test_impulse_has_priority_over_small_trades(engine):
    engine.impulse = ImpulseStrategy()
    assert [s.name for s in engine.opportunities] == ["impulsion", "petits_trades"]
    engine.impulse = None
    assert [s.name for s in engine.opportunities] == ["petits_trades"]
