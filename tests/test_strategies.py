"""
Stratégies à setups : formules communes (marge b, ratio net, objectif), pullback de tendance et
rejet de range, symétrie achat / vente, statuts (prêt, surveillance, invalidé, expiré).
"""

import re
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from core.regime_detector import detect_regime
from models.schemas import MarketRegime, SetupStatus, Side
from strategies import base
from strategies.base import Quote, mirror_frame, mirror_quote, net_reward_risk, protection_margin, target_for_ratio
from strategies.pullback import PullbackStrategy
from strategies.range_rejection import RangeRejectionStrategy

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)


def ohlc(closes, start="2026-01-01", freq="1h", wick=(0.0001, 0.0005), seed=0) -> pd.DataFrame:
    closes = np.asarray(closes, dtype=float)
    opens = np.concatenate([[closes[0]], closes[:-1]])
    up, down = np.random.default_rng(seed).uniform(*wick, (2, len(closes)))
    return pd.DataFrame({"open": opens, "high": np.maximum(opens, closes) + up,
                         "low": np.minimum(opens, closes) - down, "close": closes},
                        index=pd.date_range(start, periods=len(closes), freq=freq, tz="UTC"))


# ----------------------------------------------------------------- formules communes

@pytest.mark.parametrize("atr, spread, tick, expected", [
    (0.0020, 0.00005, 0.00001, 0.00020),       # 0.10 ATR domine
    (0.0020, 0.00015, 0.00001, 0.00030),       # 2 spreads dominent
    (0.0001, 0.000001, 0.00005, 0.00005),      # 1 tick domine
])
def test_protection_margin(atr, spread, tick, expected):
    assert protection_margin(atr, spread, tick) == pytest.approx(expected)


def test_net_ratio_and_target_for_ratio_are_consistent():
    entry, stop, cost = 1.1000, 1.0980, 0.0001
    tp = target_for_ratio(Side.BUY, entry, stop, 3.0, cost)
    assert tp == pytest.approx(1.1000 + 3 * (0.0020 + 0.0001) + 0.0001)
    assert net_reward_risk(Side.BUY, entry, stop, tp, cost) == pytest.approx(3.0)
    sell_tp = target_for_ratio(Side.SELL, 1.1000, 1.1020, 3.0, cost)
    assert sell_tp == pytest.approx(1.1000 - 0.0064) and net_reward_risk(Side.SELL, 1.1, 1.102, sell_tp, cost) == pytest.approx(3)
    assert net_reward_risk(Side.BUY, 1.1, 1.098, 1.104, 0.0) == pytest.approx(2.0)   # sans coût : ratio brut


def test_mirroring_is_an_involution():
    df = ohlc(1.1 + np.cumsum(np.random.default_rng(1).normal(0, 0.001, 50)))
    k = base.mirror_constant([df])
    pd.testing.assert_frame_equal(mirror_frame(mirror_frame(df, k), k), df)
    quote = Quote(1.1000, 1.1002, 0.00001)
    assert mirror_quote(mirror_quote(quote, k), k) == quote and mirror_quote(quote, k).spread == pytest.approx(0.0002)


# ----------------------------------------------------------------- pullback de tendance

CUT = 229                                       # contexte arrêté pendant la correction (sommet d'impulsion : 224)


def _context() -> pd.DataFrame:
    i = np.arange(240)
    return ohlc(1.10 + 0.0006 * i + 0.004 * np.sin(2 * np.pi * i / 10)).iloc[:CUT]


def _trigger(ctx: pd.DataFrame, break_bars_ago: int = 0, broken: bool = True) -> pd.DataFrame:
    """M15 depuis le sommet de l'impulsion : repli sur le sommet cassé, sommet de correction, cassure."""
    top = float(ctx["high"].iloc[224])
    low = float(ctx["low"].iloc[225:].min())
    down = np.linspace(top - 0.0003, low + 0.0002, 12)
    bounce = [low + 0.0008, low + 0.0012, low + 0.0007, low + 0.0004]          # sommet de correction : low + 0.0012
    rise = [low + 0.0006, low + 0.0009] + ([low + 0.0016] if broken else [low + 0.0010])
    closes = np.concatenate([down, bounce, rise, [low + 0.0017] * break_bars_ago])
    end = ctx.index[-1] + pd.Timedelta(minutes=45)                             # dernière M15 clôturée du contexte
    start = end - pd.Timedelta(minutes=15 * (len(closes) - 1))
    return ohlc(closes, start=str(start), freq="15min", wick=(0.00002, 0.00005), seed=3)


def _pullback(ctx=None, trig=None, quote=None):
    ctx = _context() if ctx is None else ctx
    trig = _trigger(ctx) if trig is None else trig
    if quote is None:
        close = float(trig["close"].iloc[-1])
        quote = Quote(bid=close + 0.00003, ask=close + 0.00005, tick_size=0.00001)
    return PullbackStrategy().find_setup("EURUSD", {"H1": ctx, "M15": trig}, NOW, quote), ctx, trig, quote


def test_context_is_a_confirmed_uptrend_retesting_the_broken_high():
    reading = detect_regime(_context())
    assert reading.regime == MarketRegime.TREND_UP


def test_pullback_ready_setup_respects_formulas():
    setup, ctx, trig, quote = _pullback()
    assert setup is not None and setup.status == SetupStatus.READY, (setup and (setup.reason, setup.notes))
    assert setup.side == Side.BUY and setup.regime == MarketRegime.TREND_UP and setup.entry_price == quote.ask
    atr = detect_regime(ctx).atr
    b = max(0.10 * atr, 2 * quote.spread, quote.tick_size)
    assert setup.indicators["marge_b"] == pytest.approx(b, abs=1e-6)
    correction_low = min(float(ctx["low"].iloc[225:].min()), float(trig["low"].min()))
    assert setup.stop_loss == pytest.approx(correction_low - b, abs=1e-6)                   # SL = creux - b
    assert setup.net_reward_risk == pytest.approx(3.0, abs=1e-3)                            # ratio net = 3
    assert net_reward_risk(Side.BUY, setup.entry_price, setup.stop_loss, setup.take_profit, quote.spread) >= 3 - 1e-6
    assert abs(setup.indicators["creux_correction"] - setup.indicators["sommet_cassé"]) <= 0.20 * atr


def test_pullback_entry_refused_when_price_ran_away():
    setup, ctx, trig, _ = _pullback()
    atr = detect_regime(ctx).atr
    far = float(trig["close"].iloc[-1]) + 0.25 * atr
    setup, *_ = _pullback(quote=Quote(bid=far - 0.00002, ask=far, tick_size=0.00001))
    assert setup.status == SetupStatus.INVALIDATED and "entrée refusée" in setup.notes[0]


def test_pullback_waits_for_the_break_and_expires_after_three_bars():
    ctx = _context()
    waiting, *_ = _pullback(trig=_trigger(ctx, broken=False))
    assert waiting.status == SetupStatus.WATCHING and "attente de la cassure" in waiting.reason
    expired, *_ = _pullback(trig=_trigger(ctx, break_bars_ago=3))
    assert expired.status == SetupStatus.EXPIRED


def test_pullback_invalidated_when_structural_low_is_broken():
    ctx = _context().copy()
    ctx.iloc[-1, ctx.columns.get_loc("close")] = 1.0                # clôture sous le creux structurel
    ctx.iloc[-1, ctx.columns.get_loc("low")] = 0.99
    setup, *_ = _pullback(ctx=ctx, trig=_trigger(_context()))
    assert setup is None or setup.side == Side.SELL


def test_pullback_sell_is_the_exact_mirror_of_the_buy():
    buy, ctx, trig, quote = _pullback()
    k = 10.0
    sell = PullbackStrategy().find_setup("EURUSD", {"H1": mirror_frame(ctx, k), "M15": mirror_frame(trig, k)}, NOW,
                                         mirror_quote(quote, k))
    assert sell.side == Side.SELL and sell.status == SetupStatus.READY and sell.regime == MarketRegime.TREND_DOWN
    assert sell.entry_price == pytest.approx(k - buy.entry_price) and sell.stop_loss == pytest.approx(k - buy.stop_loss)
    assert sell.take_profit == pytest.approx(k - buy.take_profit) and sell.net_reward_risk == pytest.approx(buy.net_reward_risk)
    assert "baissière" in sell.reason and "creux cassé" in sell.reason


def test_no_pullback_without_trend():
    flat = ohlc(1.10 + 0.004 * np.sin(2 * np.pi * np.arange(260) / 16), seed=7)
    assert PullbackStrategy().find_setup("EURUSD", {"H1": flat, "M15": flat}, NOW, Quote(1.1, 1.1001, 0.00001)) is None


# ----------------------------------------------------------------- rejet de range

def _range_base() -> pd.DataFrame:
    i = np.arange(260)
    return ohlc(1.10 + 0.004 * np.sin(2 * np.pi * i / 16) + np.random.default_rng(7).normal(0, 0.0002, 260), seed=7)


def _range_scenario(sweep_low=1.0952, confirm=(1.0955, 1.0966, 1.0953, 1.0964)):
    df = _range_base().iloc[:252]                                    # range validé jusqu'ici (B ~ 1.09558)
    t = df.index[-1]
    extra = pd.DataFrame([[1.0962, 1.0963, sweep_low, 1.0954],       # balayage : plus bas sous B, clôture sous B
                          list(confirm)],                            # réintégration + bougie haussière (tiers haut)
                         columns=["open", "high", "low", "close"],
                         index=[t + pd.Timedelta(hours=1), t + pd.Timedelta(hours=2)])
    return pd.concat([df, extra])


def _range_setup(df, quote=None):
    close = float(df["close"].iloc[-1])
    quote = quote or Quote(bid=close, ask=close + 0.00002, tick_size=0.00001)
    return RangeRejectionStrategy().find_setup("EURUSD", {"H1": df}, NOW, quote), quote


def test_range_context_before_the_sweep():
    reading = detect_regime(_range_base().iloc[:252])
    assert reading.regime == MarketRegime.RANGE and reading.range_bounds[0] == pytest.approx(1.09558, abs=1e-5)


def test_range_rejection_ready_setup_respects_formulas():
    df = _range_scenario()
    setup, quote = _range_setup(df)
    assert setup is not None and setup.status == SetupStatus.READY, (setup and (setup.reason, setup.notes))
    reading = detect_regime(df.iloc[:252])
    low, high = reading.range_bounds
    b = max(0.10 * reading.atr, 2 * quote.spread, quote.tick_size)
    assert setup.side == Side.BUY and setup.regime == MarketRegime.RANGE and setup.entry_price == quote.ask
    assert setup.stop_loss == pytest.approx(1.0952 - b, abs=1e-6)                         # extrême du balayage - b
    assert setup.take_profit == pytest.approx(high - b, abs=1e-6)                         # borne opposée - b
    expected = ((high - b) - quote.ask - quote.spread) / (quote.ask - (1.0952 - b) + quote.spread)
    assert setup.net_reward_risk == pytest.approx(expected, abs=1e-3) and setup.net_reward_risk >= 3
    assert setup.indicators["contacts_bas"] >= 2 and setup.indicators["contacts_haut"] >= 2
    assert "tiers supérieur" in setup.reason and "borne basse" in setup.reason


def test_range_rejection_automatically_rejected_below_net_ratio_3():
    setup, _ = _range_setup(_range_scenario(sweep_low=1.0910))       # balayage profond : stop trop loin
    assert setup.status == SetupStatus.INVALIDATED and setup.net_reward_risk < 3
    assert any("rejet automatique" in note for note in setup.notes)


@pytest.mark.parametrize("confirm", [
    (1.0964, 1.0966, 1.0953, 1.0955),                                # bougie baissière
    (1.0955, 1.0975, 1.0953, 1.0960),                                # clôture hors du tiers supérieur
    (1.0950, 1.0955, 1.0948, 1.0954),                                # pas de réintégration (clôture sous B)
])
def test_range_rejection_requires_a_valid_confirmation(confirm):
    setup, _ = _range_setup(_range_scenario(confirm=confirm))
    assert setup is None


def test_range_sell_is_the_exact_mirror():
    df = _range_scenario()
    buy, quote = _range_setup(df)
    k = 10.0
    sell = RangeRejectionStrategy().find_setup("EURUSD", {"H1": mirror_frame(df, k)}, NOW, mirror_quote(quote, k))
    assert sell.side == Side.SELL and sell.status == SetupStatus.READY
    assert sell.stop_loss == pytest.approx(k - buy.stop_loss) and sell.take_profit == pytest.approx(k - buy.take_profit)
    assert "borne haute" in sell.reason and "tiers inférieur" in sell.reason


def test_range_entry_refused_when_price_ran_away():
    df = _range_scenario()
    atr = detect_regime(df.iloc[:252]).atr
    far = float(df["close"].iloc[-1]) + 0.3 * atr
    setup, _ = _range_setup(df, Quote(bid=far - 0.00002, ask=far, tick_size=0.00001))
    assert setup.status == SetupStatus.INVALIDATED and any("entrée refusée" in n for n in setup.notes)


# ----------------------------------------------------------------- breakout momentum

from strategies.momentum_breakout import BREAK_BODY_ATR, MAX_RESPIRATION, MomentumBreakoutStrategy  # noqa: E402

RANGE_BARS = 70
BREAKOUT = (1.1004, 1.1022, 1.1003, 1.1020)                      # corps 0.0016 : ~1.7 ATR du range
RESPIRATION = [(1.1020, 1.1021, 1.1012, 1.1014), (1.1014, 1.1016, 1.1011, 1.1013)]
RESTART = (1.1012, 1.1026, 1.1011, 1.1025)                       # haussière, clôture > plus haut précédent,
                                                                 # corps 0.0013 >= ATR(5) ~0.0012
LONDON_BREAKOUT = pd.Timestamp("2026-11-02 07:00", tz="UTC")      # ouverture de la bougie de cassure


def _h1_context(kind: str = "trend") -> pd.DataFrame:
    """
    H1 cohérent avec les M15 (prix ~1.10) : `trend` = tendance haussière (ADX ~26, EMA 50 > EMA 200,
    sommet des 20 dernières bougies déjà sous l'entrée) ; `rebound` = rebond haussier fort dans une
    tendance baissière de fond (ADX élevé mais EMA 50 < EMA 200) ; `flat` = range amorphe (ADX < 20).
    """
    i = np.arange(300)
    if kind == "flat":
        closes = 1.0995 + 0.0015 * np.sin(2 * np.pi * i / 16)
    elif kind == "rebound":
        closes = np.where(i < 230, 1.16 - 0.0004 * i, 1.16 - 0.0004 * 229 + 0.0005 * (i - 229))
        closes = closes + 0.001 * np.sin(2 * np.pi * i / 10)
        closes = closes - closes[-1] + 1.0995
    else:
        closes = 1.10 - 0.00012 * 299 + 0.00012 * i + 0.001 * np.sin(2 * np.pi * i / 10)
    return ohlc(closes, start="2026-10-20", seed=11)


def _decision_time(trig: pd.DataFrame) -> datetime:
    """Clôture de la dernière bougie M15 : instant de la décision."""
    return (trig.index[-1] + pd.Timedelta(minutes=15)).to_pydatetime()


def _momentum_frame(after=(), breakout=BREAKOUT, breakout_at=LONDON_BREAKOUT) -> pd.DataFrame:
    """M15 : range serré (sommet ~1.1007), bougie d'impulsion, puis les bougies `after`."""
    rows = []
    for i in range(RANGE_BARS):
        o, c = (1.0997, 1.1003) if i % 2 == 0 else (1.1003, 1.0997)
        rows.append((o, max(o, c) + 0.0002 + 0.00002 * (i % 3), min(o, c) - 0.0002, c))
    rows += [breakout, *after]
    index = pd.date_range(start=breakout_at - pd.Timedelta(minutes=15 * RANGE_BARS), periods=len(rows), freq="15min")
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=index)


def _momentum(after=(), breakout=BREAKOUT, ctx=None, quote=None, breakout_at=LONDON_BREAKOUT, now=None):
    trig = _momentum_frame(after, breakout, breakout_at)
    ctx = _h1_context() if ctx is None else ctx
    if quote is None:
        close = float(trig["close"].iloc[-1])
        quote = Quote(bid=close, ask=close + 0.00002, tick_size=0.00001)
    now = _decision_time(trig) if now is None else now
    return MomentumBreakoutStrategy().find_setup("EURUSD", {"H1": ctx, "M15": trig}, now, quote), trig, quote


def test_momentum_ready_after_micro_respiration_and_restart():
    setup, trig, quote = _momentum([*RESPIRATION, RESTART])
    assert setup is not None and setup.status == SetupStatus.READY, (setup and (setup.reason, setup.notes))
    assert setup.side == Side.BUY and setup.entry_price == quote.ask and setup.strategy == "momentum_breakout"
    level = float(trig["high"].iloc[RANGE_BARS - 40:RANGE_BARS].max())
    assert setup.indicators["niveau_cassé"] == pytest.approx(level)
    b = setup.indicators["marge_b"]
    assert b == pytest.approx(max(0.10 * setup.indicators["atr"], 2 * quote.spread, quote.tick_size), abs=1e-7)
    assert setup.stop_loss == pytest.approx(min(r[2] for r in RESPIRATION) - b, abs=1e-7)     # sous la respiration
    assert net_reward_risk(Side.BUY, setup.entry_price, setup.stop_loss, setup.take_profit, quote.spread) >= 3 - 1e-6
    assert (BREAKOUT[3] - BREAKOUT[0]) >= BREAK_BODY_ATR * setup.indicators["atr"]
    assert setup.expires_at - setup.created_at == pd.Timedelta(minutes=15)                   # une bougie M15


def test_momentum_never_enters_on_the_breakout_candle():
    setup, *_ = _momentum()
    assert setup.status == SetupStatus.WATCHING and "attente de la micro-respiration" in setup.reason


def test_momentum_waits_during_respiration():
    setup, *_ = _momentum(RESPIRATION)
    assert setup.status == SetupStatus.WATCHING and "respiration en cours (2/3" in setup.reason


def test_momentum_cancelled_after_three_respiration_bars_without_restart():
    flat = [(1.1013, 1.1015, 1.1011, 1.1012)] * 2
    setup, *_ = _momentum([*RESPIRATION, *flat])
    assert len(RESPIRATION) + len(flat) > MAX_RESPIRATION
    assert setup.status == SetupStatus.EXPIRED and "plus de 3 bougies" in setup.reason


def test_momentum_invalidated_when_price_closes_back_under_the_level():
    setup, *_ = _momentum([RESPIRATION[0], (1.1013, 1.1014, 1.1001, 1.1004)])
    assert setup.status == SetupStatus.INVALIDATED and "sous le niveau" in setup.notes[0]


def test_momentum_requires_a_powerful_candle():
    weak = (1.1004, 1.1012, 1.1003, 1.1010)                       # corps 0.0006 < 1.2 ATR
    setup, *_ = _momentum([*RESPIRATION, RESTART], breakout=weak)
    assert setup is None or setup.status != SetupStatus.READY


def test_momentum_stale_restart_is_not_executed():
    setup, *_ = _momentum([*RESPIRATION, RESTART, (1.1021, 1.1022, 1.1018, 1.1019)])
    assert setup is None or setup.status != SetupStatus.READY


def test_momentum_entry_refused_when_price_ran_away():
    far = RESTART[3] + 0.0010
    setup, *_ = _momentum([*RESPIRATION, RESTART], quote=Quote(bid=far - 0.00002, ask=far, tick_size=0.00001))
    assert setup.status == SetupStatus.INVALIDATED and "entrée refusée" in setup.notes[0]


def test_momentum_blocked_against_the_higher_timeframe_trend():
    down = mirror_frame(_context(), 10.0)                          # H1 en tendance baissière
    assert detect_regime(down).regime == MarketRegime.TREND_DOWN
    setup, *_ = _momentum([*RESPIRATION, RESTART], ctx=down)
    assert setup is None or setup.side == Side.SELL


def test_momentum_sell_is_the_exact_mirror_of_the_buy():
    buy, trig, quote = _momentum([*RESPIRATION, RESTART])
    k = 10.0
    ctx = _h1_context()
    sell = MomentumBreakoutStrategy().find_setup("EURUSD", {"H1": mirror_frame(ctx, k), "M15": mirror_frame(trig, k)},
                                                 _decision_time(trig), mirror_quote(quote, k))
    assert sell.side == Side.SELL and sell.status == SetupStatus.READY
    assert sell.entry_price == pytest.approx(k - buy.entry_price) and sell.stop_loss == pytest.approx(k - buy.stop_loss)
    assert sell.take_profit == pytest.approx(k - buy.take_profit) and "baissière" in sell.reason


def test_momentum_setups_go_through_sizing_verifier_news_and_journal(monkeypatch, eurusd_specs):
    """Séquence commune : taille à 1 %, vérificateur indépendant, fenêtre d'annonces, journal."""
    from brokers.models import AccountInfo, Tick
    from core.news_pipeline import NewsPipeline, SimulatedCalendarProvider
    from tests.fakes import FakeBroker

    trig = _momentum_frame([*RESPIRATION, RESTART])
    ctx = _h1_context()
    close = float(trig["close"].iloc[-1])
    quote = Quote(bid=close, ask=close + 0.00002, tick_size=0.00001)
    broker = FakeBroker()
    broker.account = AccountInfo(login=1, balance=10_000, equity=10_000, margin=0, margin_free=10_000, margin_level=0,
                                 profit=0, currency="USD", leverage=100)
    broker.specs = {"EURUSD": eurusd_specs}
    now = _decision_time(trig)
    broker.ticks = {"EURUSD": Tick("EURUSD", quote.bid, quote.ask, 0.0, 2, now)}
    broker.profit_fn = lambda side, symbol, volume, o, c: round((c - o if side == "BUY" else o - c) / 0.00001 * volume, 2)
    broker.margin_fn = lambda *a: 100.0
    verified = []
    real_verify = base.verify_order
    monkeypatch.setattr(base, "verify_order", lambda *a, **k: verified.append(a) or real_verify(*a, **k))
    journal = []
    memory = type("Memory", (), {"check": lambda self, *a: None,
                                 "record_setup": lambda self, setup, features: journal.append(setup)})()
    quiet = NewsPipeline([SimulatedCalendarProvider([])], minutes_before=30, minutes_after=15, cache_seconds=0)

    strategy = MomentumBreakoutStrategy()
    setup = strategy.evaluate("EURUSD", {"H1": ctx, "M15": trig}, now, quote, broker, memory=memory, news=quiet)
    assert verified, "le vérificateur indépendant doit être consulté"
    assert setup.volume > 0 and setup.risk_pct <= 1.0 + 1e-6, (setup.status, setup.notes)
    if setup.status == SetupStatus.READY:
        assert journal == [setup]                                    # journalisé (Feedback Logger)
    else:
        assert any("vérificateur" in n or "risque" in n for n in setup.notes)

    nfp = {"time": (now + pd.Timedelta(minutes=10)).isoformat(), "title": "Non-Farm Employment Change",
           "currency": "USD", "importance": "high"}
    news = NewsPipeline([SimulatedCalendarProvider([nfp])], minutes_before=30, minutes_after=15, cache_seconds=0)
    blocked = strategy.evaluate("EURUSD", {"H1": ctx, "M15": trig}, now, quote, broker, news=news)
    assert blocked.status == SetupStatus.INVALIDATED and "fenêtre de sécurité" in blocked.notes[-1]


def test_registry_selects_strategies():
    from strategies.registry import SETUP_STRATEGIES, setup_strategies
    assert [s.name for s in setup_strategies()] == list(SETUP_STRATEGIES)
    assert [s.name for s in setup_strategies(["momentum_breakout"])] == ["momentum_breakout"]
    with pytest.raises(ValueError, match="inconnue"):
        setup_strategies(["martingale"])


# ----------------------------------------------------------------- marge b par actif (breakout momentum)

def _momentum_for(symbol: str, frames_k: float | None = None):
    """Même marché (mêmes bougies, même cotation) analysé sous deux noms d'actif."""
    trig = _momentum_frame([*RESPIRATION, RESTART])
    ctx = _h1_context()
    close = float(trig["close"].iloc[-1])
    quote = Quote(bid=close, ask=close + 0.00002, tick_size=0.00001)
    if frames_k is not None:
        trig, ctx, quote = mirror_frame(trig, frames_k), mirror_frame(ctx, frames_k), mirror_quote(quote, frames_k)
    return MomentumBreakoutStrategy().find_setup(symbol, {"H1": ctx, "M15": trig}, _decision_time(trig), quote), quote


def test_gold_stop_is_wider_than_eurusd_for_the_same_volatility():
    from config.settings import settings
    eur, quote = _momentum_for("EURUSD")
    gold, _ = _momentum_for("XAUUSD")
    factor = settings.strategy.margin_multiplier("XAUUSD")
    assert settings.strategy.margin_multiplier("EURUSD") == 1.0 and factor == 1.5
    assert eur.status == gold.status == SetupStatus.READY
    assert eur.entry_price == gold.entry_price and eur.indicators["atr"] == gold.indicators["atr"]
    b = eur.indicators["marge_b"]
    assert gold.indicators["marge_b"] == pytest.approx(factor * b, abs=1e-7)
    assert gold.stop_loss < eur.stop_loss                                               # stop plus large
    assert eur.stop_loss - gold.stop_loss == pytest.approx((factor - 1) * b, abs=1e-7)
    # le TP est repoussé pour garder le ratio net >= 3 malgré le stop élargi
    assert gold.take_profit > eur.take_profit
    for setup in (eur, gold):
        assert net_reward_risk(Side.BUY, setup.entry_price, setup.stop_loss, setup.take_profit, quote.spread) >= 3 - 1e-6
        assert setup.net_reward_risk >= 3 - 1e-3


def test_gold_sell_stop_is_widened_above_the_entry():
    eur, _ = _momentum_for("EURUSD", frames_k=10.0)
    gold, quote = _momentum_for("XAUUSD", frames_k=10.0)
    assert eur.side == gold.side == Side.SELL and gold.stop_loss > eur.stop_loss and gold.take_profit < eur.take_profit
    assert net_reward_risk(Side.SELL, gold.entry_price, gold.stop_loss, gold.take_profit, quote.spread) >= 3 - 1e-6


def test_margin_multipliers_configuration(monkeypatch):
    from dataclasses import replace

    import importlib

    config = importlib.import_module("config.settings")
    from config.env import ConfigError
    assert config.parse_multipliers("xauusd:2, XAGUSD:1.5") == (("XAUUSD", 2.0), ("XAGUSD", 1.5))
    with pytest.raises(ConfigError):
        config.parse_multipliers("XAUUSD=2")
    with pytest.raises(ConfigError):
        replace(config.settings.strategy, margin_multipliers=(("XAUUSD", 0.5),)).validate()      # jamais plus serré
    wider = replace(config.settings.strategy, margin_multipliers=(("XAUUSD", 2.0),))
    from types import SimpleNamespace

    from strategies import momentum_breakout
    monkeypatch.setattr(momentum_breakout, "settings", SimpleNamespace(strategy=wider))
    gold, _ = _momentum_for("XAUUSD")
    eur, _ = _momentum_for("EURUSD")
    assert gold.indicators["multiplicateur_b"] == 2.0
    assert gold.indicators["marge_b"] == pytest.approx(2 * eur.indicators["marge_b"], abs=1e-7)


# ----------------------------------------------------------------- sessions de liquidité (breakout momentum)

from strategies.momentum_breakout import OFF_SESSION, liquidity_session  # noqa: E402


@pytest.mark.parametrize("clock, session", [
    ("06:59", None), ("07:00", "Londres"), ("09:59", "Londres"), ("10:00", None),          # fin [07:00, 10:00)
    ("12:29", None), ("12:30", "New York"), ("15:59", "New York"), ("16:00", None),
    ("02:15", None), ("23:45", None),                                                     # Asie, nuit
])
def test_liquidity_session_boundaries(clock, session):
    assert liquidity_session(pd.Timestamp(f"2026-11-02 {clock}", tz="UTC").to_pydatetime()) == session


def test_liquidity_session_converts_to_utc():
    paris = pd.Timestamp("2026-11-02 08:30", tz="Europe/Paris").to_pydatetime()           # 07:30 UTC
    server = pd.Timestamp("2026-11-02 09:30", tz="Etc/GMT-2").to_pydatetime()             # MT5 UTC+2 : 07:30 UTC
    assert liquidity_session(paris) == liquidity_session(server) == "Londres"


@pytest.mark.parametrize("breakout_at", [
    "2026-11-02 02:00",              # nuit / séance asiatique
    "2026-11-02 10:30",              # entre Londres et New York
    "2026-11-02 17:00",              # après New York
    "2026-11-02 21:00",              # heures creuses
])
def test_momentum_rejected_outside_liquidity_sessions(breakout_at):
    setup, *_ = _momentum([*RESPIRATION, RESTART], breakout_at=pd.Timestamp(breakout_at, tz="UTC"))
    assert setup.status == SetupStatus.INVALIDATED and setup.notes[-1].startswith(OFF_SESSION)


def test_momentum_breakout_formed_before_the_session_is_rejected():
    """Cassure à 06:45 (fin de séance asiatique), relance à 07:30 : décision en session, cassure hors session."""
    setup, trig, _ = _momentum([*RESPIRATION, RESTART], breakout_at=pd.Timestamp("2026-11-02 06:45", tz="UTC"))
    assert liquidity_session(_decision_time(trig)) == "Londres"
    assert setup.status == SetupStatus.INVALIDATED and "cassure formée à 06:45 UTC" in setup.notes[-1]


def test_momentum_watching_setups_are_also_rejected_off_session():
    setup, *_ = _momentum(RESPIRATION, breakout_at=pd.Timestamp("2026-11-02 03:00", tz="UTC"))
    assert setup.status == SetupStatus.INVALIDATED and OFF_SESSION in setup.notes[-1]


def test_new_york_session_accepts_setups():
    setup, *_ = _momentum([*RESPIRATION, RESTART], breakout_at=pd.Timestamp("2026-11-02 13:00", tz="UTC"))
    assert setup.status == SetupStatus.READY


def test_off_session_setups_place_no_order_and_send_no_ready_alert(monkeypatch, eurusd_specs):
    """Hors session : invalidé avant la taille de position, donc ni vérificateur, ni journal, ni alerte « prêt »."""
    from brokers.models import AccountInfo, Tick
    from tests.fakes import FakeBroker
    from utils import alerts

    trig = _momentum_frame([*RESPIRATION, RESTART], breakout_at=pd.Timestamp("2026-11-02 01:00", tz="UTC"))
    ctx = _h1_context()
    close = float(trig["close"].iloc[-1])
    broker = FakeBroker()
    broker.account = AccountInfo(login=1, balance=10_000, equity=10_000, margin=0, margin_free=10_000, margin_level=0,
                                 profit=0, currency="USD", leverage=100)
    broker.specs = {"EURUSD": eurusd_specs}
    broker.ticks = {"EURUSD": Tick("EURUSD", close, close + 0.00002, 0.0, 2, _decision_time(trig))}
    broker.get_historical_data = lambda symbol, count=100, timeframe=None: (
        pd.concat([trig, trig.iloc[-1:].set_axis([trig.index[-1] + pd.Timedelta(minutes=15)])]) if timeframe == "M15"
        else pd.concat([ctx, ctx.iloc[-1:].set_axis([ctx.index[-1] + pd.Timedelta(hours=1)])]))
    sized, ready = [], []
    monkeypatch.setattr(base, "calculate_position_size", lambda *a, **k: sized.append(a) or 0.1)
    monkeypatch.setattr(alerts, "setup_validated", lambda setup: ready.append(setup))
    setup = MomentumBreakoutStrategy().analyze(broker, "EURUSD", _decision_time(trig))
    assert setup.status == SetupStatus.INVALIDATED and OFF_SESSION in setup.notes[-1]
    assert sized == [] and ready == [] and broker.orders == []


# ----------------------------------------------------------------- heure du serveur MT5 -> UTC réel

from utils.server_time import bar_time_to_utc, utc_to_bar_time  # noqa: E402

ATHENS = "Europe/Athens"                                          # MetaQuotes-Demo : UTC+2 hiver, UTC+3 été


@pytest.mark.parametrize("server, utc", [
    ("2026-11-02 11:45", "2026-11-02 09:45"),                     # hiver : +2 (observé sur le terminal)
    ("2026-10-30 10:45", "2026-10-30 08:45"),                     # après le changement européen : +2 (observé)
    ("2026-08-15 10:00", "2026-08-15 07:00"),                     # été : +3
])
def test_server_bar_labels_convert_to_real_utc(server, utc):
    label = pd.Timestamp(server, tz="UTC")
    assert bar_time_to_utc(label, ATHENS) == pd.Timestamp(utc, tz="UTC")
    assert utc_to_bar_time(pd.Timestamp(utc, tz="UTC"), ATHENS) == label
    assert bar_time_to_utc(label, None) == label                  # ccxt, simulation : déjà en UTC


@pytest.mark.parametrize("server_breakout, ready", [
    ("2026-11-02 09:00", True),                                   # = 07:00 UTC : ouverture de Londres
    ("2026-11-02 07:00", False),                                  # = 05:00 UTC : séance asiatique
    ("2026-08-14 10:00", True),                                   # été : = 07:00 UTC
    ("2026-08-14 09:30", False),                                  # été : = 06:30 UTC
    ("2026-11-02 14:45", True),                                   # = 12:45 UTC : New York
])
def test_momentum_sessions_use_the_server_clock(server_breakout, ready):
    trig = _momentum_frame([*RESPIRATION, RESTART], breakout_at=pd.Timestamp(server_breakout, tz="UTC"))
    ctx = _h1_context()
    close = float(trig["close"].iloc[-1])
    strategy = MomentumBreakoutStrategy()
    strategy.bar_timezone = ATHENS
    setup = strategy.find_setup("EURUSD", {"H1": ctx, "M15": trig}, _decision_time(trig),
                                Quote(bid=close, ask=close + 0.00002, tick_size=0.00001))
    assert (setup.status == SetupStatus.READY) == ready, setup.notes
    if not ready:
        assert OFF_SESSION in setup.notes[-1]


def test_live_analysis_takes_the_clock_from_the_broker():
    from tests.fakes import FakeBroker
    broker = FakeBroker()
    broker.bar_timezone = ATHENS
    strategy = MomentumBreakoutStrategy()
    strategy.analyze(broker, "EURUSD", NOW)                       # aucune donnée : pas de setup
    assert strategy.bar_timezone == ATHENS


def test_backtest_replays_the_calendar_on_the_bar_clock():
    from backtesting.setup_engine import SetupBacktester
    from core.news_pipeline import CalendarEvent, Importance
    from tests.test_backtest import EURUSD
    trig = _momentum_frame()
    h1 = trig.resample("1h").agg({"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()
    nfp = CalendarEvent(time=pd.Timestamp("2026-11-06 13:30", tz="UTC").to_pydatetime(), source="test",
                        title="Non-Farm Employment Change", currency="USD", importance=Importance.HIGH)
    strategy = MomentumBreakoutStrategy()
    tester = SetupBacktester([strategy], "EURUSD", {"H1": h1, "M15": trig}, EURUSD, calendar=[nfp],
                             bar_timezone=ATHENS)
    assert strategy.bar_timezone == ATHENS
    [event] = tester.news.events()
    assert event.time == pd.Timestamp("2026-11-06 15:30", tz="UTC") and event.actual is None   # heure serveur


# ----------------------------------------------------------------- force de la bougie de relance

from strategies.momentum_breakout import RESTART_ATR_BARS, mean_true_range  # noqa: E402


def _momentum_settings(monkeypatch, **changes):
    """Réglages de la stratégie momentum pour un test (Settings est figé : on remplace la référence du module)."""
    from dataclasses import replace
    from types import SimpleNamespace

    from config.settings import settings
    from strategies import momentum_breakout
    strategy = replace(settings.strategy, **changes)
    monkeypatch.setattr(momentum_breakout, "settings", SimpleNamespace(strategy=strategy))
    return strategy


@pytest.fixture
def strict_restart(monkeypatch):
    return _momentum_settings(monkeypatch, momentum_restart_body_atr=1.0)


def _atr5_before_restart(after) -> float:
    trig = _momentum_frame(after)
    bars = {c: trig[c].to_numpy(dtype=float) for c in ("open", "high", "low", "close")}
    return mean_true_range(bars, len(trig) - 1, RESTART_ATR_BARS)


def test_mean_true_range_uses_only_the_five_previous_candles():
    trig = _momentum_frame([*RESPIRATION, RESTART])
    bars = {c: trig[c].to_numpy(dtype=float) for c in ("open", "high", "low", "close")}
    n = len(trig)
    expected = np.mean([max(bars["high"][i] - bars["low"][i], abs(bars["high"][i] - bars["close"][i - 1]),
                            abs(bars["low"][i] - bars["close"][i - 1])) for i in range(n - 6, n - 1)])
    assert mean_true_range(bars, n - 1, 5) == pytest.approx(expected)
    bars["high"][n - 1] += 1.0                                    # la relance elle-même n'entre pas dans la mesure
    assert mean_true_range(bars, n - 1, 5) == pytest.approx(expected)
    assert mean_true_range(bars, 3, 5) == 0.0                     # historique insuffisant


def test_powerful_restart_is_accepted_and_measured(strict_restart):
    setup, *_ = _momentum([*RESPIRATION, RESTART])
    atr5 = _atr5_before_restart([*RESPIRATION, RESTART])
    assert setup.status == SetupStatus.READY
    assert setup.indicators["atr5"] == pytest.approx(atr5, abs=1e-7)
    assert setup.indicators["corps_relance_atr5"] == pytest.approx((RESTART[3] - RESTART[0]) / atr5, abs=1e-6)
    assert setup.indicators["corps_relance_atr5"] >= 1.0
    assert "relance haussière puissante" in setup.reason


@pytest.mark.parametrize("restart", [
    (1.1013, 1.1018, 1.1012, 1.1017),                             # « chiquenaude » : corps 0.0004, clôture > 1.1016
    (1.1013, 1.1023, 1.1012, 1.1021),                             # corps 0.0008 ~ 0.7 ATR(5) : sans conviction
])
def test_weak_restart_invalidates_the_setup(restart, strict_restart):
    atr5 = _atr5_before_restart([*RESPIRATION, restart])
    assert restart[3] > RESPIRATION[-1][1] and restart[3] > restart[0]          # c'est bien une relance
    assert restart[3] - restart[0] < atr5                                        # mais trop faible
    setup, *_ = _momentum([*RESPIRATION, restart])
    assert setup.status == SetupStatus.INVALIDATED and "relance trop faible" in setup.reason
    assert "relance sans conviction" in setup.notes[-1] and setup.indicators["corps_relance_atr5"] < 1.0


def test_weak_sell_restart_is_rejected_too(strict_restart):
    weak = (1.1013, 1.1023, 1.1012, 1.1021)
    buy, trig, quote = _momentum([*RESPIRATION, weak])
    k = 10.0
    sell = MomentumBreakoutStrategy().find_setup(
        "EURUSD", {"H1": mirror_frame(_h1_context(), k), "M15": mirror_frame(trig, k)},
        _decision_time(trig), mirror_quote(quote, k))
    assert sell.side == Side.SELL and sell.status == SetupStatus.INVALIDATED and "relance trop faible" in sell.reason


def test_restart_body_ratio_is_configurable(monkeypatch):
    from dataclasses import replace

    from config.settings import settings
    almost = (1.1013, 1.1023, 1.1012, 1.1021)                     # ~0.7 ATR(5)
    ratio = (almost[3] - almost[0]) / _atr5_before_restart([*RESPIRATION, almost])
    assert 0.6 < ratio < 0.8
    _momentum_settings(monkeypatch, momentum_restart_body_atr=0.6)
    setup, *_ = _momentum([*RESPIRATION, almost])
    assert setup.status == SetupStatus.READY
    with pytest.raises(Exception, match="STRATEGY_MOMENTUM_RESTART_BODY_ATR"):
        replace(settings.strategy, momentum_restart_body_atr=-0.5).validate()


def test_powerful_restart_is_not_mistaken_for_a_new_breakout():
    """La relance (corps >= 1.2 ATR, clôture au-dessus du range) appartient à la cassure qu'elle relance."""
    far = RESTART[3] + 0.0010
    setup, *_ = _momentum([*RESPIRATION, RESTART], quote=Quote(bid=far - 0.00002, ask=far, tick_size=0.00001))
    assert setup.status == SetupStatus.INVALIDATED and "entrée refusée" in setup.notes[0]   # pas « attente »


# ----------------------------------------------------------------- filtre désactivable, ratio cible, trailing stop

def test_restart_filter_is_disabled_by_default():
    from config.settings import settings
    assert settings.strategy.momentum_restart_body_atr == 0
    moderate = (1.1013, 1.1023, 1.1012, 1.1021)                   # ~0.7 ATR(5) : refusée par le filtre strict
    setup, *_ = _momentum([*RESPIRATION, moderate])
    assert setup.status == SetupStatus.READY and "relance haussière confirmée" in setup.reason
    assert setup.indicators["corps_relance_atr5"] < 1.0           # mesurée, mais pas filtrée


@pytest.mark.parametrize("target", [2.5, 3.0, 4.0])
def test_target_ratio_is_configurable(monkeypatch, target):
    _momentum_settings(monkeypatch, momentum_target_rr=target)
    setup, _, quote = _momentum([*RESPIRATION, RESTART])
    assert setup.status == SetupStatus.READY
    assert net_reward_risk(Side.BUY, setup.entry_price, setup.stop_loss, setup.take_profit,
                           quote.spread) == pytest.approx(target, abs=1e-6)


def test_target_below_the_verifier_minimum_is_refused_at_startup():
    from dataclasses import replace

    from config.env import ConfigError
    from config.settings import settings
    lower_target = replace(settings, strategy=replace(settings.strategy, momentum_target_rr=2.5))
    with pytest.raises(ConfigError, match="RISK_MIN_NET_REWARD_RISK"):
        lower_target.validate()
    both = replace(lower_target, risk=replace(settings.risk, min_net_reward_risk=2.5))
    both.strategy.validate()                                      # cohérent : objectif et vérificateur à 2.5
    assert both.strategy.momentum_target_rr >= both.risk.min_net_reward_risk


@pytest.mark.parametrize("field, value", [
    ("momentum_target_rr", 0.5), ("momentum_trailing_activation_r", 0.2), ("momentum_trailing_distance_r", 0),
])
def test_invalid_optimisation_settings(field, value):
    from dataclasses import replace

    from config.env import ConfigError
    from config.settings import settings
    with pytest.raises(ConfigError):
        replace(settings.strategy, **{field: value}).validate()


def test_trailing_is_off_by_default_and_built_from_settings(monkeypatch):
    from risk.trailing import TrailingStop
    assert MomentumBreakoutStrategy().trailing is None
    _momentum_settings(monkeypatch, momentum_breakeven_r=1.5, momentum_trailing_activation_r=2.0,
                       momentum_trailing_mode="atr", momentum_trailing_atr=1.2)
    assert MomentumBreakoutStrategy().trailing == TrailingStop(breakeven_r=1.5, activation_r=2.0, mode="atr",
                                                               atr_multiple=1.2, bars=3, distance_r=1.0)


def test_moderate_restart_is_accepted_when_the_body_filter_is_off_or_lenient(monkeypatch):
    moderate = (1.1013, 1.1023, 1.1012, 1.1021)                   # ~0.7 ATR(5)
    for ratio in (0.0, 0.3):                                      # désactivé, ou très permissif
        _momentum_settings(monkeypatch, momentum_restart_body_atr=ratio)
        setup, *_ = _momentum([*RESPIRATION, moderate])
        assert setup.status == SetupStatus.READY, (ratio, setup.notes)
        assert not any("sans conviction" in n for n in setup.notes)


def _position(side, entry, sl):
    from backtesting.broker import SimPosition
    return SimPosition(ticket=1, symbol="EURUSD", side=side, volume=1.0, entry_price=entry, sl=sl, tp=None,
                       opened_at=NOW, magic=0)


def _state(side, entry, sl, atr=None, **config):
    from backtesting.setup_engine import TrailingState
    from risk.trailing import TrailingStop
    p = _position(side, entry, sl)
    return p, TrailingState(TrailingStop(**config), p, atr)


def test_breakeven_moves_the_stop_to_entry_at_1_5r():
    p, state = _state("BUY", 1.1000, 1.0990, breakeven_r=1.5)    # R = 0.0010
    state.update(high=1.1014, low=1.0998, spread=0.0)             # +1.4 R : rien
    assert p.sl == 1.0990 and state.stage == "initial"
    state.update(high=1.1015, low=1.1005, spread=0.0)             # +1.5 R : zéro risque
    assert p.sl == pytest.approx(1.1000) and state.stage == "breakeven"
    state.update(high=1.1040, low=1.1002, spread=0.0)             # sans trailing : le stop reste à l'entrée
    assert p.sl == pytest.approx(1.1000)


def test_trailing_follows_the_lows_of_the_last_m15_candles():
    p, state = _state("BUY", 1.1000, 1.0990, breakeven_r=1.5, activation_r=1.5, mode="bars", bars=3)
    for high, low in ((1.1006, 1.0995), (1.1012, 1.1001), (1.1016, 1.1008)):   # 3e bougie : +1.6 R
        state.update(high=high, low=low, spread=0.0)
    assert p.sl == pytest.approx(1.1000) and state.stage == "breakeven"         # plus bas des 3 : 1.0995 < entrée
    state.update(high=1.1022, low=1.1013, spread=0.0)             # plus bas des 3 dernières : 1.1001
    assert p.sl == pytest.approx(1.1001) and state.stage == "trailing"
    state.update(high=1.1027, low=1.1019, spread=0.0)             # 1.1008
    assert p.sl == pytest.approx(1.1008)
    state.update(high=1.1020, low=1.1004, spread=0.0)             # hésitation : le stop ne recule jamais
    assert p.sl == pytest.approx(1.1008)                          # plus bas des 3 : 1.1004 < stop actuel


def test_trailing_with_an_atr_safety_distance():
    p, state = _state("BUY", 1.1000, 1.0990, atr=0.0004, activation_r=2.0, mode="atr", atr_multiple=1.5)
    state.update(high=1.1019, low=1.1005, spread=0.0)             # +1.9 R : pas encore
    assert p.sl == 1.0990
    state.update(high=1.1025, low=1.1015, spread=0.0)             # +2.5 R : 1.1025 - 1.5 x 0.0004
    assert p.sl == pytest.approx(1.1019) and state.stage == "trailing"


def test_trailing_with_a_distance_in_r():
    p, state = _state("BUY", 1.1000, 1.0990, activation_r=1.5, mode="r", distance_r=1.0)
    state.update(high=1.1015, low=1.1005, spread=0.0)
    assert p.sl == pytest.approx(1.1005)


def test_sell_breakeven_and_trailing_follow_the_ask():
    p, state = _state("SELL", 1.1000, 1.1010, breakeven_r=1.5, activation_r=2.0, mode="bars", bars=2)
    state.update(high=1.0995, low=1.0984, spread=0.0002)          # Ask bas = 1.0986 : +1.4 R
    assert p.sl == 1.1010
    state.update(high=1.0990, low=1.0983, spread=0.0002)          # Ask bas = 1.0985 : +1.5 R -> entrée
    assert p.sl == pytest.approx(1.1000) and state.stage == "breakeven"
    state.update(high=1.0985, low=1.0975, spread=0.0002)          # +2.3 R ; plus hauts Ask : 1.0992, 1.0987
    assert p.sl == pytest.approx(1.0992) and state.stage == "trailing"


def test_next_stop_is_symmetric():
    from risk.trailing import TrailingStop, next_stop
    cfg = TrailingStop(breakeven_r=1.0, activation_r=1.5, mode="bars", bars=2)
    k = 10.0
    buy = next_stop("BUY", 1.1, 1.099, 1.099, 1.1016, [1.1004, 1.1008], None, cfg)
    sell = next_stop("SELL", k - 1.1, k - 1.099, k - 1.099, k - 1.1016, [k - 1.1004, k - 1.1008], None, cfg)
    assert sell.stop == pytest.approx(k - buy.stop) and sell.stage == buy.stage == "trailing"


@pytest.mark.parametrize("field, value", [
    ("momentum_breakeven_r", 0.2), ("momentum_trailing_mode", "parabolic"), ("momentum_trailing_bars", 0),
    ("momentum_trailing_atr", 0),
])
def test_invalid_trailing_settings(field, value):
    from dataclasses import replace

    from config.env import ConfigError
    from config.settings import settings
    with pytest.raises(ConfigError):
        replace(settings.strategy, **{field: value}).validate()


def test_backtest_applies_breakeven_and_trailing_and_labels_exits(monkeypatch):
    """Même historique : la gestion ne change pas les entrées ; sorties marquées breakeven / trailing."""
    from backtesting.setup_engine import CostModel, SetupBacktester
    from tests.test_backtest import EURUSD, _h1, _m15
    m15 = _m15(n=900, seed=5, drift=0.00002, vol=0.001)            # 7 trades, dont des gagnants

    def run():
        from strategies.momentum_breakout import MomentumBreakoutStrategy as Strategy
        return SetupBacktester([Strategy()], "EURUSD", {"M15": m15, "H1": _h1(m15)}, EURUSD,
                               costs=CostModel(slippage_points=0)).run()

    monkeypatch.setattr(MomentumBreakoutStrategy, "off_session_reason", staticmethod(lambda *a: None))
    no_h1_filter = dict(momentum_min_h1_adx=0, momentum_h1_obstacle_atr=0, momentum_h1_ema_alignment=False)
    _momentum_settings(monkeypatch, **no_h1_filter)              # marche aléatoire : on teste la sortie
    fixed = run()
    _momentum_settings(monkeypatch, momentum_breakeven_r=1.0, momentum_trailing_activation_r=1.5,
                       momentum_trailing_mode="bars", momentum_trailing_bars=3, **no_h1_filter)
    managed = run()
    assert fixed.trades and [t.entry_time for t in managed.trades] == [t.entry_time for t in fixed.trades]
    assert all(t.exit_reason in {"sl", "tp", "breakeven", "trailing", "end"} for t in managed.trades)
    assert any(t.exit_reason in {"breakeven", "trailing"} for t in managed.trades)
    for trade in managed.trades:
        if trade.exit_reason == "trailing":
            assert trade.profit > 0 or trade.ambiguous                # stop au-dessus de l'entrée
        if trade.exit_reason == "breakeven":
            assert abs(trade.profit) <= 0.05 * abs(trade.planned_risk)   # ~zéro (spread, arrondis)


# ----------------------------------------------------------------- filtre de contexte H1 (breakout momentum)

from strategies.momentum_breakout import H1_INVALID  # noqa: E402


def _h1_readings(ctx):
    closes = ctx["close"]
    reading = detect_regime(ctx)
    fast = closes.ewm(span=50, adjust=False).mean().iloc[-1]
    slow = closes.ewm(span=200, adjust=False).mean().iloc[-1]
    return reading, fast, slow


def test_valid_h1_context_lets_the_setup_through():
    reading, fast, slow = _h1_readings(_h1_context())
    assert reading.adx >= 22 and fast > slow
    setup, *_ = _momentum([*RESPIRATION, RESTART])
    assert setup.status == SetupStatus.READY and not any(H1_INVALID in n for n in setup.notes)


def test_flat_h1_market_is_rejected_as_amorphous():
    flat = _h1_context("flat")
    reading, *_ = _h1_readings(flat)
    assert reading.adx < 22 and reading.regime not in (MarketRegime.TREND_DOWN, MarketRegime.SHOCK)
    setup, *_ = _momentum([*RESPIRATION, RESTART], ctx=flat)
    assert setup.status == SetupStatus.INVALIDATED
    assert setup.notes[-1].startswith("Contexte H1 invalide : Marché amorphe (ADX H1")
    assert f"< {22:g})" in setup.notes[-1]


def test_m15_buy_against_the_h1_background_trend_is_rejected():
    rebound = _h1_context("rebound")
    reading, fast, slow = _h1_readings(rebound)
    assert reading.adx >= 22 and fast < slow                      # rebond fort, mais fond baissier
    setup, *_ = _momentum([*RESPIRATION, RESTART], ctx=rebound)
    assert setup.status == SetupStatus.INVALIDATED
    assert "tendance de fond contraire (EMA 50 sous EMA 200 en H1)" in setup.notes[-1]


def test_sell_is_rejected_when_h1_emas_point_up():
    """Vente M15 (miroir) dans un H1 dont l'EMA 50 est au-dessus de l'EMA 200 : contexte contraire."""
    k = 10.0
    buy_ctx = _h1_context("rebound")                              # miroir : EMA 50 au-dessus de l'EMA 200
    trig = _momentum_frame([*RESPIRATION, RESTART])
    close = float(trig["close"].iloc[-1])
    quote = Quote(bid=close, ask=close + 0.00002, tick_size=0.00001)
    sell = MomentumBreakoutStrategy().find_setup(
        "EURUSD", {"H1": mirror_frame(buy_ctx, k), "M15": mirror_frame(trig, k)}, _decision_time(trig),
        mirror_quote(quote, k))
    assert sell.side == Side.SELL and sell.status == SetupStatus.INVALIDATED
    assert "EMA 50 au-dessus EMA 200" in sell.notes[-1]


def test_immediate_h1_resistance_blocks_the_buy():
    ctx = _h1_context().copy()
    entry = RESTART[3] + 0.00002
    atr_h1 = detect_regime(ctx).atr
    ctx.iloc[-6, ctx.columns.get_loc("high")] = entry + 0.5 * atr_h1             # sommet H1 non franchi, tout proche
    setup, *_ = _momentum([*RESPIRATION, RESTART], ctx=ctx)
    assert setup.status == SetupStatus.INVALIDATED and "obstacle H1 immédiat" in setup.notes[-1]
    assert re.search(r"sommet des 20 dernières bougies H1 à 0\.\d\d ATR", setup.notes[-1])    # < 1 ATR (le pic gonfle l'ATR)


def test_distant_or_broken_h1_resistance_is_accepted():
    entry = RESTART[3] + 0.00002
    for offset_atr in (1.5, -0.2):                                # assez loin, ou déjà franchi par le prix
        ctx = _h1_context().copy()
        ctx.iloc[-6, ctx.columns.get_loc("high")] = entry + offset_atr * detect_regime(ctx).atr
        setup, *_ = _momentum([*RESPIRATION, RESTART], ctx=ctx)
        assert setup.status == SetupStatus.READY, (offset_atr, setup.notes)


def test_h1_filters_can_be_disabled(monkeypatch):
    _momentum_settings(monkeypatch, momentum_min_h1_adx=0, momentum_h1_obstacle_atr=0, momentum_h1_ema_alignment=False)
    for kind in ("flat", "rebound"):
        setup, *_ = _momentum([*RESPIRATION, RESTART], ctx=_h1_context(kind))
        assert setup.status == SetupStatus.READY, (kind, setup.notes)


def test_h1_rejection_happens_before_sizing_and_alerts(monkeypatch, eurusd_specs):
    from brokers.models import AccountInfo, Tick
    from tests.fakes import FakeBroker
    from utils import alerts

    trig = _momentum_frame([*RESPIRATION, RESTART])
    ctx = _h1_context("flat")
    close = float(trig["close"].iloc[-1])
    broker = FakeBroker()
    broker.account = AccountInfo(login=1, balance=10_000, equity=10_000, margin=0, margin_free=10_000, margin_level=0,
                                 profit=0, currency="USD", leverage=100)
    broker.specs = {"EURUSD": eurusd_specs}
    broker.ticks = {"EURUSD": Tick("EURUSD", close, close + 0.00002, 0.0, 2, _decision_time(trig))}
    broker.get_historical_data = lambda symbol, count=100, timeframe=None: (
        pd.concat([trig, trig.iloc[-1:].set_axis([trig.index[-1] + pd.Timedelta(minutes=15)])]) if timeframe == "M15"
        else pd.concat([ctx, ctx.iloc[-1:].set_axis([ctx.index[-1] + pd.Timedelta(hours=1)])]))
    sized, ready = [], []
    monkeypatch.setattr(base, "calculate_position_size", lambda *a, **k: sized.append(a) or 0.1)
    monkeypatch.setattr(alerts, "setup_validated", lambda setup: ready.append(setup))
    setup = MomentumBreakoutStrategy().analyze(broker, "EURUSD", _decision_time(trig))
    assert setup.status == SetupStatus.INVALIDATED and H1_INVALID in setup.notes[-1]
    assert sized == [] and ready == [] and broker.orders == []


@pytest.mark.parametrize("field, value", [
    ("momentum_min_h1_adx", -1), ("momentum_h1_lookback", 2), ("momentum_h1_obstacle_atr", 11),
    ("momentum_h1_ema_fast", 200),                                # rapide >= lente
])
def test_invalid_h1_settings(field, value):
    from dataclasses import replace

    from config.env import ConfigError
    from config.settings import settings
    with pytest.raises(ConfigError):
        replace(settings.strategy, **{field: value}).validate()
