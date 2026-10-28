"""
Analyse technique : indicateurs (EMA, RSI, MACD, ATR), niveaux de swing et signal court terme.

Le signal est calculé sur les bougies CLÔTURÉES uniquement : la dernière bougie
renvoyée par MT5 est en cours de formation et ses valeurs changent à chaque tick.

Score technique dans [-1, 1], somme pondérée de trois composantes :
  - tendance (EMA rapide vs EMA lente, position du prix)   TREND_WEIGHT
  - momentum (histogramme MACD et son évolution)            MOMENTUM_WEIGHT
  - RSI (zone haussière/baissière, surachat/survente)        RSI_WEIGHT

Usage :
    from strategies.technical_analysis import technical_signal

    tech = technical_signal(df)          # df = get_historical_data("EURUSD", 500)
    print(tech.signal, tech.score, tech.reasons)
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

import numpy as np
import pandas as pd
from ta.momentum import RSIIndicator
from ta.trend import MACD, EMAIndicator
from ta.volatility import AverageTrueRange

from utils.logger import get_logger

log = get_logger(__name__)

TechnicalAction = Literal["BUY", "SELL", "NEUTRAL"]


# =============================================================================
#  Constantes
# =============================================================================

EMA_FAST = 20
EMA_SLOW = 50
RSI_WINDOW = 14
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
ATR_WINDOW = 14
SWING_LOOKBACK = 20                  # bougies utilisées pour le dernier plus haut / plus bas

MIN_BARS = EMA_SLOW + 10             # bougies clôturées minimales pour un signal fiable

TREND_WEIGHT = 0.40
MOMENTUM_WEIGHT = 0.35
RSI_WEIGHT = 0.25
SIGNAL_THRESHOLD = 0.30              # |score| minimal pour un signal BUY / SELL

RSI_OVERBOUGHT = 70
RSI_OVERSOLD = 30

REQUIRED_COLUMNS = ("open", "high", "low", "close")


# =============================================================================
#  Structures de retour
# =============================================================================

@dataclass(frozen=True)
class TechnicalSignal:
    signal: TechnicalAction
    score: float                     # [-1, 1]
    close: float | None = None       # clôture de la dernière bougie terminée
    ema_fast: float | None = None
    ema_slow: float | None = None
    rsi: float | None = None
    macd_hist: float | None = None
    atr: float | None = None
    swing_high: float | None = None
    swing_low: float | None = None
    reasons: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_valid(self) -> bool:
        """False si les données étaient insuffisantes pour calculer les indicateurs."""
        return self.close is not None and self.atr is not None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _neutral(reason: str) -> TechnicalSignal:
    return TechnicalSignal(signal="NEUTRAL", score=0.0, reasons=(reason,))


# =============================================================================
#  Indicateurs
# =============================================================================

def _closed_candles(df: pd.DataFrame | None) -> pd.DataFrame | None:
    """Nettoie le DataFrame OHLC et retire la bougie en cours ; None si inutilisable."""
    if df is None or df.empty:
        return None
    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        log.error("Colonnes OHLC manquantes : %s", ", ".join(missing))
        return None
    clean = df[list(REQUIRED_COLUMNS)].apply(pd.to_numeric, errors="coerce").dropna()
    return clean.iloc[:-1]


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    Ajoute au DataFrame (copie) les colonnes : ema_fast, ema_slow, rsi, macd,
    macd_signal, macd_hist, atr. Les premières lignes valent NaN (période de chauffe).
    """
    out = df.copy()
    close, high, low = out["close"], out["high"], out["low"]

    out["ema_fast"] = EMAIndicator(close, window=EMA_FAST).ema_indicator()
    out["ema_slow"] = EMAIndicator(close, window=EMA_SLOW).ema_indicator()
    out["rsi"] = RSIIndicator(close, window=RSI_WINDOW).rsi()

    macd = MACD(close, window_slow=MACD_SLOW, window_fast=MACD_FAST, window_sign=MACD_SIGNAL)
    out["macd"] = macd.macd()
    out["macd_signal"] = macd.macd_signal()
    out["macd_hist"] = macd.macd_diff()

    # `ta` remplit la période de chauffe de l'ATR avec des 0 : on les remplace par NaN
    atr = AverageTrueRange(high, low, close, window=ATR_WINDOW).average_true_range()
    out["atr"] = atr.replace(0.0, np.nan)
    return out


def swing_levels(df: pd.DataFrame, lookback: int = SWING_LOOKBACK) -> tuple[float | None, float | None]:
    """(plus haut, plus bas) des `lookback` dernières bougies ; (None, None) si vide."""
    window = df.tail(lookback)
    if window.empty:
        return None, None
    return float(window["high"].max()), float(window["low"].min())


# =============================================================================
#  Composantes du score
# =============================================================================

def _trend_component(close: float, ema_fast: float, ema_slow: float) -> tuple[float, str]:
    """+1/-1 si EMA rapide au-dessus/en dessous de l'EMA lente, 0.5 si le prix contredit."""
    label = f"EMA{EMA_FAST}/EMA{EMA_SLOW}"
    if ema_fast > ema_slow:
        if close > ema_fast:
            return 1.0, f"tendance haussière ({label}, prix au-dessus)"
        return 0.5, f"tendance haussière ({label}) mais prix sous l'EMA{EMA_FAST}"
    if ema_fast < ema_slow:
        if close < ema_fast:
            return -1.0, f"tendance baissière ({label}, prix en dessous)"
        return -0.5, f"tendance baissière ({label}) mais prix au-dessus de l'EMA{EMA_FAST}"
    return 0.0, f"{label} à plat"


def _momentum_component(hist: float, prev_hist: float) -> tuple[float, str]:
    """+1/-1 selon le signe de l'histogramme MACD, réduit de moitié s'il s'essouffle."""
    if hist > 0:
        if hist >= prev_hist:
            return 1.0, "MACD haussier en accélération"
        return 0.5, "MACD haussier en essoufflement"
    if hist < 0:
        if hist <= prev_hist:
            return -1.0, "MACD baissier en accélération"
        return -0.5, "MACD baissier en essoufflement"
    return 0.0, "MACD neutre"


def _rsi_component(rsi: float) -> tuple[float, str]:
    """
    Zone 30-70 : linéaire de -1 (30) à +1 (70) autour de 50.
    Surachat/survente : contre-signal modéré (on évite d'acheter un sommet / vendre un creux).
    """
    if rsi >= RSI_OVERBOUGHT:
        return -0.5, f"RSI {rsi:.0f} en surachat"
    if rsi <= RSI_OVERSOLD:
        return 0.5, f"RSI {rsi:.0f} en survente"
    value = (rsi - 50) / (RSI_OVERBOUGHT - 50)
    return value, f"RSI {rsi:.0f} {'favorable à la hausse' if value > 0 else 'favorable à la baisse' if value < 0 else 'neutre'}"


# =============================================================================
#  Signal technique
# =============================================================================

def signal_from_indicators(ind: pd.DataFrame, pos: int = -1) -> TechnicalSignal:
    """
    Signal technique à la bougie clôturée `pos` d'un DataFrame déjà enrichi par
    `compute_indicators`. Ne lit que les lignes <= `pos` (aucune donnée future) :
    le backtest calcule les indicateurs une seule fois puis appelle cette fonction
    bougie par bougie, avec exactement la même notation que le bot en direct.
    """
    try:
        pos = pos if pos >= 0 else len(ind) + pos
        if pos < 1 or pos >= len(ind):
            return _neutral("historique insuffisant")
        last, prev = ind.iloc[pos], ind.iloc[pos - 1]
        needed = ("close", "ema_fast", "ema_slow", "rsi", "macd_hist", "atr")
        if any(pd.isna(last[c]) for c in needed) or pd.isna(prev["macd_hist"]):
            return _neutral("indicateurs incomplets (NaN sur la dernière bougie)")

        trend, trend_reason = _trend_component(last["close"], last["ema_fast"], last["ema_slow"])
        momentum, momentum_reason = _momentum_component(last["macd_hist"], prev["macd_hist"])
        rsi_value, rsi_reason = _rsi_component(last["rsi"])

        score = TREND_WEIGHT * trend + MOMENTUM_WEIGHT * momentum + RSI_WEIGHT * rsi_value
        score = max(-1.0, min(1.0, score))
        if math.isnan(score):
            return _neutral("score technique invalide")

        signal: TechnicalAction = (
            "BUY" if score >= SIGNAL_THRESHOLD else "SELL" if score <= -SIGNAL_THRESHOLD else "NEUTRAL"
        )
        swing_high, swing_low = swing_levels(ind.iloc[max(0, pos - SWING_LOOKBACK + 1):pos + 1])

        return TechnicalSignal(
            signal=signal,
            score=round(score, 4),
            close=float(last["close"]),
            ema_fast=float(last["ema_fast"]),
            ema_slow=float(last["ema_slow"]),
            rsi=round(float(last["rsi"]), 2),
            macd_hist=float(last["macd_hist"]),
            atr=float(last["atr"]),
            swing_high=swing_high,
            swing_low=swing_low,
            reasons=(trend_reason, momentum_reason, rsi_reason),
        )
    except Exception as exc:  # noqa: BLE001 - un bug d'indicateur ne doit pas arrêter le bot
        log.exception("Erreur lors du calcul du signal technique")
        return _neutral(f"erreur de calcul : {exc}")


def technical_signal(df: pd.DataFrame | None) -> TechnicalSignal:
    """
    Calcule le signal technique court terme à partir des bougies OHLC.

    Args:
        df: DataFrame de `get_historical_data()` (la dernière bougie, en cours, est ignorée).

    Returns:
        `TechnicalSignal` ; signal "NEUTRAL" avec `is_valid == False` si les données
        sont absentes, incomplètes ou insuffisantes. Ne lève jamais d'exception.
    """
    try:
        candles = _closed_candles(df)
        if candles is None:
            return _neutral("aucune bougie exploitable")
        if len(candles) < MIN_BARS:
            log.warning("Historique insuffisant : %d bougies clôturées (minimum %d)", len(candles), MIN_BARS)
            return _neutral(f"historique insuffisant ({len(candles)}/{MIN_BARS} bougies)")

        result = signal_from_indicators(compute_indicators(candles))
        if result.is_valid:
            log.debug("Signal technique %s (%+.2f) : %s", result.signal, result.score, "; ".join(result.reasons))
        return result
    except Exception as exc:  # noqa: BLE001 - un bug d'indicateur ne doit pas arrêter le bot
        log.exception("Erreur lors du calcul du signal technique")
        return _neutral(f"erreur de calcul : {exc}")
