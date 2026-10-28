"""Indicateurs et signal technique sur données synthétiques."""

import numpy as np
import pandas as pd
import pytest

from strategies.technical_analysis import MIN_BARS, swing_levels, technical_signal


def _candles(closes: np.ndarray) -> pd.DataFrame:
    index = pd.date_range("2026-01-01", periods=len(closes), freq="15min", tz="UTC")
    opens = np.concatenate([[closes[0]], closes[:-1]])
    return pd.DataFrame({
        "open": opens,
        "high": np.maximum(opens, closes) + 0.0005,
        "low": np.minimum(opens, closes) - 0.0005,
        "close": closes,
    }, index=index)


def _accelerating(direction: int, bars: int = 300) -> pd.DataFrame:
    t = np.arange(bars)
    return _candles(1.10 + direction * (0.0002 * t + 0.000002 * t ** 2))


def test_accelerating_uptrend_gives_buy():
    signal = technical_signal(_accelerating(+1))
    assert signal.is_valid and signal.signal == "BUY" and signal.score > 0
    assert signal.atr and signal.atr > 0
    assert signal.swing_low < signal.close <= signal.swing_high


def test_accelerating_downtrend_gives_sell():
    signal = technical_signal(_accelerating(-1))
    assert signal.signal == "SELL" and signal.score < 0


def test_unfinished_last_candle_is_ignored():
    df = _accelerating(+1)
    reference = technical_signal(df)
    df.iloc[-1, df.columns.get_loc("close")] = 0.5          # bougie en cours aberrante
    assert technical_signal(df) == reference


def test_invalid_or_short_data_is_neutral():
    for df in (None, pd.DataFrame(), pd.DataFrame({"close": [1.0] * 200}), _accelerating(+1, bars=MIN_BARS)):
        signal = technical_signal(df)
        assert signal.signal == "NEUTRAL" and not signal.is_valid and signal.reasons


def test_non_numeric_rows_are_dropped():
    df = _accelerating(+1).astype(object)
    df.iloc[10, 0] = "corrompu"
    assert technical_signal(df).is_valid


def test_swing_levels():
    df = _candles(np.array([1.0, 1.2, 0.9, 1.1]))
    high, low = swing_levels(df, lookback=4)
    assert high == pytest.approx(1.2005) and low == pytest.approx(0.8995)
    assert swing_levels(df.iloc[0:0]) == (None, None)
