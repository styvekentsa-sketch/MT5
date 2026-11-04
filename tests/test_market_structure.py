"""
Structure de marché (strategies/market_structure.py) : BOS / CHoCH, Fair Value Gaps, Order Blocks,
supports / résistances et liquidité, Bollinger, contexte D1/H4/H1 et route /api/analysis/structure.
"""

import numpy as np
import pandas as pd

from core.regime_detector import atr, confirmed_pivots
from strategies.market_structure import (
    analyze_structure,
    bollinger,
    fair_value_gaps,
    key_levels,
    multi_timeframe_context,
    structure_breaks,
)
from tests.fakes import FakeBroker
from tests.test_analysis import AUTH, client, broker  # noqa: F401 - fixtures réutilisées


def frame(closes, wick=0.0002, freq="15min") -> pd.DataFrame:
    closes = np.asarray(closes, dtype=float)
    opens = np.concatenate([[closes[0]], (closes[:-1] + closes[1:]) / 2])   # extrêmes uniques : pivots stricts
    return pd.DataFrame({"open": opens, "high": np.maximum(opens, closes) + wick,
                         "low": np.minimum(opens, closes) - wick, "close": closes},
                        index=pd.date_range("2026-11-02", periods=len(closes), freq=freq, tz="UTC"))


def leg(start, end, bars):
    return list(np.linspace(start, end, bars + 1)[1:])


def _reversal() -> pd.DataFrame:
    """Descente en escalier (creux de plus en plus bas), puis rebond au-dessus du dernier sommet."""
    closes = leg(1.1100, 1.1000, 30)                                       # chauffe sans pivot
    for low, high in ((1.0960, 1.0985), (1.0940, 1.0965), (1.0920, 1.0945)):
        closes += leg(closes[-1], low, 4) + leg(low, high, 4)
    closes += leg(closes[-1], 1.0900, 4) + leg(1.0900, 1.0990, 6) + [1.0995]   # cassure du sommet 1.0945
    return frame(closes)


def test_bearish_bos_then_bullish_choch():
    df = _reversal()
    breaks, trend = structure_breaks(df, confirmed_pivots(df))
    kinds = [(kind, direction) for _, kind, direction, _ in breaks]
    assert ("BOS", "baissier") in kinds
    assert kinds[-1] == ("CHoCH", "haussier") and trend == "haussier"


def test_breaks_never_use_unconfirmed_pivots():
    df = _reversal()
    pivots = confirmed_pivots(df)
    for j, _kind, _direction, level in structure_breaks(df, pivots)[0]:
        source = next(p for p in pivots if p.price == level)
        assert source.confirmed_position < j                              # pivot connu AVANT la cassure


def test_order_block_is_the_last_opposite_candle_before_the_choch():
    reading = analyze_structure(_reversal(), closed=False)
    bullish = [z for z in reading.order_blocks if z.direction == "haussier"]
    assert bullish and bullish[0].low < 1.0905                            # dernière bougie baissière : le creux 1.0900
    assert reading.last_break.kind == "CHoCH" and "structure haussier" in reading.summary()


def test_fair_value_gap_detected_then_filled():
    closes = leg(1.1000, 1.1010, 20) + [1.1012, 1.1060, 1.1070, 1.1072]
    df = frame(closes, wick=0.0001)
    gaps = [z for z in fair_value_gaps(df, atr(df)) if z.direction == "haussier"]
    assert gaps and gaps[-1].active and gaps[-1].high > gaps[-1].low
    filled = frame(closes + [1.1040, 1.1000], wick=0.0001)               # retour sous le bas du déséquilibre
    assert not [z for z in fair_value_gaps(filled, atr(filled)) if z.time == gaps[-1].time][0].active


def test_equal_highs_mark_a_liquidity_resistance():
    closes = leg(1.1000, 1.0990, 20)
    for _ in range(3):                                                    # trois sommets à 1.1030
        closes += leg(closes[-1], 1.1030, 4) + leg(1.1030, 1.1000, 4)
    df = frame(closes)
    levels = key_levels(confirmed_pivots(df), float(atr(df).iloc[-1]), price=float(df["close"].iloc[-1]))
    resistance = next(lv for lv in levels if lv.kind == "résistance")
    assert resistance.touches == 3 and resistance.liquidity and abs(resistance.price - 1.1032) < 1e-4


def test_bollinger_detects_compression_after_volatility():
    rng = np.random.default_rng(1)
    closes = list(1.10 + np.cumsum(rng.normal(0, 0.002, 120))) + [1.10] * 0
    closes += list(closes[-1] + rng.normal(0, 0.00005, 40))
    reading = bollinger(frame(closes))
    assert reading["regime"] == "compression" and reading["lower"] < reading["middle"] < reading["upper"]


def test_short_history_is_reported_not_guessed():
    reading = analyze_structure(frame(leg(1.1, 1.2, 10)))
    assert reading.trend == "range" and reading.notes == ["historique insuffisant"]


def test_multi_timeframe_context_alignment():
    up = FakeBroker()
    up.bars = frame(leg(1.0500, 1.0600, 40) + sum((leg(1.06 + i * 0.004, 1.064 + i * 0.004, 5)
                                                  + leg(1.064 + i * 0.004, 1.062 + i * 0.004, 3)
                                                  for i in range(12)), []))
    context = multi_timeframe_context(up, "EURUSD")
    assert set(context["timeframes"]) == {"D1", "H4", "H1"}
    assert context["dominant"] == "haussier" and context["aligned"]

    empty = FakeBroker()
    empty.bars = frame(leg(1.1, 1.2, 10))
    assert multi_timeframe_context(empty, "EURUSD")["aligned"] is False


def test_structure_endpoint(client):  # noqa: F811
    body = client.get("/api/analysis/structure?symbol=eurusd&timeframe=M15", headers=AUTH).json()
    assert body["symbol"] == "EURUSD" and body["trend"] in ("haussier", "baissier", "range")
    assert {"fair_value_gaps", "order_blocks", "levels", "bollinger", "breaks", "context"} <= set(body)
    assert set(body["context"]["timeframes"]) == {"D1", "H4", "H1"}
    assert client.get("/api/analysis/structure?symbol=eurusd&timeframe=X9", headers=AUTH).status_code == 422
