"""
Détecteur de régime causal : disponibilité des pivots (aucune information future), structure,
ATR / ADX de Wilder (valeurs de référence, symétrie, robustesse, causalité), classification.
"""

import numpy as np
import pandas as pd
import pytest

from core import regime_detector as rd
from core.regime_detector import Structure, confirmed_pivots, detect_regime, market_structure
from models.schemas import MarketRegime


def ohlc(closes, seed=0, wick=(0.0001, 0.0005), freq="1h") -> pd.DataFrame:
    closes = np.asarray(closes, dtype=float)
    opens = np.concatenate([[closes[0]], closes[:-1]])
    up, down = np.random.default_rng(seed).uniform(*wick, (2, len(closes)))
    return pd.DataFrame({"open": opens, "high": np.maximum(opens, closes) + up,
                         "low": np.minimum(opens, closes) - down, "close": closes},
                        index=pd.date_range("2026-01-01", periods=len(closes), freq=freq, tz="UTC"))


def bars(highs, lows=None) -> pd.DataFrame:
    """Bougies définies par leurs plus hauts (et plus bas) : tests de pivots exacts."""
    highs = np.asarray(highs, dtype=float)
    lows = highs - 1 if lows is None else np.asarray(lows, dtype=float)
    mid = (highs + lows) / 2
    return pd.DataFrame({"open": mid, "high": highs, "low": lows, "close": mid},
                        index=pd.date_range("2026-01-01", periods=len(highs), freq="1h", tz="UTC"))


def wave(n=260, slope=0.0006, amplitude=0.002, period=10, seed=0):
    i = np.arange(n)
    return ohlc(1.10 + slope * i + amplitude * np.sin(2 * np.pi * i / period), seed=seed)


def mirror(df: pd.DataFrame, pivot: float = 3.0) -> pd.DataFrame:
    """Série miroir (prix -> pivot - prix) : une hausse devient une baisse, sommets et creux s'échangent."""
    return pd.DataFrame({"open": pivot - df["open"], "high": pivot - df["low"], "low": pivot - df["high"],
                         "close": pivot - df["close"]}, index=df.index)


# ----------------------------------------------------------------- pivots confirmés : disponibilité temporelle

PEAK = bars([10, 11, 12, 15, 13, 12, 11, 10])                   # sommet strict à l'indice 3


def test_pivot_needs_two_closed_candles_after_it():
    assert [p for p in confirmed_pivots(PEAK, upto=3) if p.kind == "high"] == []      # bougie du pivot
    assert [p for p in confirmed_pivots(PEAK, upto=4) if p.kind == "high"] == []      # 1 bougie de confirmation
    [peak] = [p for p in confirmed_pivots(PEAK, upto=5) if p.kind == "high"]          # 2e bougie clôturée
    assert (peak.position, peak.price, peak.confirmed_position) == (3, 15.0, 5)
    assert peak.confirmed_time == PEAK.index[5]


def test_live_replay_never_sees_a_pivot_early_nor_repaints():
    """Rejeu bougie par bougie : à chaque instant, exactement les pivots confirmés à cet instant."""
    df = ohlc(1.1 + 0.003 * np.sin(np.arange(120) / 3) + np.random.default_rng(3).normal(0, 0.0004, 120), seed=3)
    final = confirmed_pivots(df)
    assert len(final) > 10
    for t in range(len(df)):
        live = confirmed_pivots(df.iloc[: t + 1])                            # ce que voit le bot à la clôture de t
        assert live == [p for p in final if p.confirmed_position <= t]       # ni avance, ni pivot redessiné
        assert all(p.position + 2 <= t for p in live)


def test_pivot_comparisons_are_strict():
    assert [p for p in confirmed_pivots(bars([10, 11, 15, 15, 12, 11, 10])) if p.kind == "high"] == []
    assert [p for p in confirmed_pivots(bars([10, 15, 11, 15, 10, 9, 8])) if p.kind == "high"] == []   # voisin égal à 2 bougies


def test_trough_is_the_mirror_rule():
    df = bars(highs=[20, 19, 18, 16, 18, 19, 20], lows=[15, 14, 13, 10, 13, 14, 15])
    [trough] = [p for p in confirmed_pivots(df) if p.kind == "low"]
    assert (trough.position, trough.price, trough.confirmed_position) == (3, 10.0, 5)


def test_pivots_of_mirror_series_swap_kinds():
    df = wave(n=120)
    original = {(p.kind, p.position) for p in confirmed_pivots(df)}
    swapped = {("low" if p.kind == "high" else "high", p.position) for p in confirmed_pivots(mirror(df))}
    assert original == swapped


# ----------------------------------------------------------------- structure de marché

def test_structure_from_three_confirmed_pivots():
    assert market_structure(confirmed_pivots(wave())) == Structure.BULLISH
    assert market_structure(confirmed_pivots(mirror(wave()))) == Structure.BEARISH
    flat = ohlc(1.1 + 0.002 * np.sin(np.arange(200) / 2) + np.random.default_rng(1).normal(0, 0.0006, 200), seed=1)
    assert market_structure(confirmed_pivots(flat)) == Structure.UNDETERMINED
    assert market_structure(confirmed_pivots(PEAK)) == Structure.UNDETERMINED      # moins de 3 pivots


# ----------------------------------------------------------------- ATR / ADX de Wilder

def _reference_atr(df, period=14):
    tr = [df["high"].iloc[0] - df["low"].iloc[0]]
    for i in range(1, len(df)):
        h, low, pc = df["high"].iloc[i], df["low"].iloc[i], df["close"].iloc[i - 1]
        tr.append(max(h - low, abs(h - pc), abs(low - pc)))
    out = [np.nan] * len(df)
    out[period - 1] = sum(tr[:period]) / period
    for i in range(period, len(df)):
        out[i] = (out[i - 1] * (period - 1) + tr[i]) / period
    return np.array(out)


def test_atr_matches_wilder_reference_and_warmup():
    df = ohlc(1.1 + np.cumsum(np.random.default_rng(5).normal(0, 0.0008, 150)), seed=5)
    atr = rd.atr(df).to_numpy()
    assert np.isnan(atr[:13]).all() and not np.isnan(atr[13:]).any()
    np.testing.assert_allclose(atr, _reference_atr(df), rtol=1e-12, equal_nan=True)


def test_constant_range_gives_constant_atr():
    df = bars(highs=np.full(60, 11.0), lows=np.full(60, 10.0))
    np.testing.assert_allclose(rd.atr(df).iloc[13:], 1.0)


def test_adx_warmup_bounds_and_strong_trend():
    df = wave(n=260)
    values = rd.adx(df)
    assert values["adx"].iloc[:27].isna().all() and values["adx"].iloc[27:].notna().all()
    assert values["plus_di"].iloc[:14].isna().all() and values["plus_di"].iloc[14:].notna().all()
    assert ((values["adx"].dropna() >= 0) & (values["adx"].dropna() <= 100)).all()
    assert values["adx"].iloc[-1] > 25 and values["plus_di"].iloc[-1] > values["minus_di"].iloc[-1]


def test_adx_is_symmetric_under_price_mirroring():
    df = ohlc(1.1 + np.cumsum(np.random.default_rng(9).normal(0, 0.0007, 200)), seed=9)
    a, b = rd.adx(df), rd.adx(mirror(df))
    np.testing.assert_allclose(a["adx"], b["adx"], rtol=1e-9, equal_nan=True)
    np.testing.assert_allclose(a["plus_di"], b["minus_di"], rtol=1e-9, equal_nan=True)
    np.testing.assert_allclose(rd.atr(df), rd.atr(mirror(df)), rtol=1e-9, equal_nan=True)


def test_adx_scale_invariance_and_flat_market():
    df = ohlc(1.1 + np.cumsum(np.random.default_rng(2).normal(0, 0.0007, 200)), seed=2)
    scaled = df * 1000
    np.testing.assert_allclose(rd.adx(df)["adx"], rd.adx(scaled)["adx"], rtol=1e-9, equal_nan=True)
    np.testing.assert_allclose(rd.atr(df) * 1000, rd.atr(scaled), rtol=1e-9, equal_nan=True)
    flat = bars(highs=np.full(80, 11.0), lows=np.full(80, 10.0))              # aucun mouvement directionnel
    values = rd.adx(flat)
    assert np.isfinite(values["adx"].iloc[27:]).all() and (values["adx"].iloc[27:] == 0).all()


def test_indicators_are_causal():
    df = ohlc(1.1 + np.cumsum(np.random.default_rng(4).normal(0, 0.0007, 220)), seed=4)
    full_atr, full_adx = rd.atr(df), rd.adx(df)
    for t in (40, 100, 180):
        past = df.iloc[: t + 1]
        assert rd.atr(past).iloc[-1] == pytest.approx(full_atr.iloc[t])
        assert rd.adx(past)["adx"].iloc[-1] == pytest.approx(full_adx["adx"].iloc[t])


# ----------------------------------------------------------------- classification

def test_trends_up_and_down():
    up = detect_regime(wave())
    assert up.regime == MarketRegime.TREND_UP and up.direction == 1 and up.structure == Structure.BULLISH
    down = detect_regime(mirror(wave()))
    assert down.regime == MarketRegime.TREND_DOWN and down.direction == -1


def test_range_with_validated_bounds():
    i = np.arange(260)
    df = ohlc(1.10 + 0.004 * np.sin(2 * np.pi * i / 16) + np.random.default_rng(7).normal(0, 0.0002, 260), seed=7)
    reading = detect_regime(df)
    assert reading.adx <= 20, reading.reasons
    assert reading.regime == MarketRegime.RANGE and reading.range_bounds is not None
    lower, upper = reading.range_bounds
    assert lower < df["close"].iloc[-1] < upper and upper - lower >= 2 * reading.atr


def _pivot(kind, position, price):
    stamp = pd.Timestamp("2026-01-01", tz="UTC")
    return rd.SwingPivot(kind, position, stamp, price, position + 2, stamp)


@pytest.mark.parametrize("highs, lows, close, expected", [
    ((1.110, 1.1102), (1.100, 1.1001), 1.105, "range"),                    # bornes alignées, largeur 10 ATR
    ((1.110, 1.1130), (1.100, 1.1001), 1.105, "non alignés"),             # sommets écartés de 3 ATR
    ((1.1015, 1.1016), (1.100, 1.1001), 1.1008, "largeur"),               # range de 1.5 ATR : trop étroit
    ((1.110, 1.1102), (1.100, 1.1001), 1.112, "hors du range"),           # cassure en cours
    ((1.110,), (1.100, 1.1001), 1.105, "moins de deux"),
])
def test_range_bounds_validation(highs, lows, close, expected):
    pivots = [_pivot("high", i, h) for i, h in enumerate(highs)] + [_pivot("low", 10 + i, low) for i, low in enumerate(lows)]
    bounds, why = rd._range_bounds(pivots, atr_value=0.001, close=close)
    assert expected in why
    assert (bounds is not None) == (expected == "range")


def test_low_adx_without_valid_bounds_is_transition(monkeypatch):
    i = np.arange(260)
    df = ohlc(1.10 + 0.004 * np.sin(2 * np.pi * i / 16) + np.random.default_rng(7).normal(0, 0.0002, 260), seed=7)
    monkeypatch.setattr(rd, "_range_bounds", lambda pivots, atr_value, close: (None, "bornes non validées (test)"))
    reading = detect_regime(df)
    assert reading.adx <= 20 and reading.regime == MarketRegime.TRANSITION and "bornes non validées" in reading.reasons[0]


def test_strong_adx_without_structure_is_transition():
    i = np.arange(260)
    closes = 1.10 + 0.0008 * i                                                # hausse parfaitement lisse : aucun pivot
    df = bars(highs=closes + 0.0003, lows=closes - 0.0003)
    df["close"], df["open"] = closes, np.concatenate([[closes[0]], closes[:-1]])
    reading = detect_regime(df)
    assert reading.adx >= 25 and reading.structure == Structure.UNDETERMINED
    assert reading.regime == MarketRegime.TRANSITION


@pytest.mark.parametrize("make_shock, needle", [
    (lambda df: df.assign(high=np.where(np.arange(len(df)) == len(df) - 1, df["high"] + 0.02, df["high"])), "ATR"),
    (lambda df: df.assign(spread=[10] * (len(df) - 1) + [35]), "spread"),
])
def test_shock_detection(make_shock, needle):
    reading = detect_regime(make_shock(wave()))
    assert reading.regime == MarketRegime.SHOCK and any(needle in r for r in reading.reasons)


def test_news_and_live_spread_trigger_shock():
    assert detect_regime(wave(), news_imminent=True).regime == MarketRegime.SHOCK
    df = wave().assign(spread=10)
    assert detect_regime(df, spread_points=25).regime == MarketRegime.SHOCK
    assert detect_regime(df, spread_points=12).regime == MarketRegime.TREND_UP


def test_reference_atr_is_the_one_available_before_the_bar():
    df = wave()
    shocked = df.copy()
    shocked.iloc[-1, shocked.columns.get_loc("high")] += 0.02
    reading = detect_regime(shocked)
    assert reading.atr == pytest.approx(rd.atr(df).iloc[-2])                 # la bougie anormale n'en fait pas partie
    assert reading.true_range > 3 * reading.atr


def test_insufficient_warmup_is_unknown():
    reading = detect_regime(wave(n=rd.WARMUP_BARS - 1))
    assert reading.regime == MarketRegime.UNKNOWN and "préchauffage" in reading.reasons[0]
