"""
Détecteur de régime de marché, strictement causal (aucune information future).

Toutes les fonctions prennent des bougies CLÔTURÉES, de la plus ancienne à la plus récente
(la bougie en cours doit être retirée avant l'appel). Le résultat pour la bougie `t` ne
dépend que des bougies <= t : rejouer l'historique bougie par bougie donne exactement les
mêmes valeurs qu'en direct.

1. Pivots confirmés (fractales 2-2)
   - sommet : plus haut STRICTEMENT supérieur aux deux bougies précédentes et aux deux suivantes
   - creux  : plus bas STRICTEMENT inférieur aux deux précédentes et aux deux suivantes
   - un pivot à la bougie i n'est disponible qu'à la clôture de la bougie i + 2 ; jamais redessiné
2. Structure de marché (trois derniers pivots confirmés de chaque type)
   - haussière : sommets croissants ET creux croissants
   - baissière : sommets décroissants ET creux décroissants
   - indéterminée : sinon (ou moins de trois pivots de chaque type)
3. ATR(14) et ADX(14) de Wilder
   - TR = max(H - B, |H - C(t-1)|, |B - C(t-1)|) ; première valeur = moyenne des 14 premiers TR,
     puis ATR(t) = (ATR(t-1) x 13 + TR(t)) / 14
   - +DM / -DM, lissages de Wilder (somme initiale puis S(t) = S(t-1) - S(t-1)/14 + x(t)),
     +DI = 100 x S(+DM) / S(TR), DX = 100 x |+DI - -DI| / (+DI + -DI), ADX = moyenne de Wilder du DX
   - valeurs NaN pendant la chauffe ; WARMUP_BARS bougies au minimum pour une lecture fiable
   - pour juger une bougie t, l'ATR de référence est celui disponible AVANT elle : ATR(t-1)
4. Classification (par priorité)
   - CHOC              volatilité anormale (TR(t) >= 3 x ATR(t-1), ou ATR(t-1) >= 1.5 x sa médiane),
                       spread >= 2 x sa médiane récente, ou annonce majeure imminente
   - TENDANCE HAUSSIÈRE / BAISSIÈRE   structure haussière / baissière ET ADX >= 25
   - RANGE             ADX <= 20 ET bornes validées : deux derniers sommets et deux derniers creux
                       confirmés alignés à 1 ATR près, largeur >= 2 ATR, clôture entre les bornes
   - TRANSITION        tous les autres cas
   - INCONNU           historique trop court pour une lecture fiable
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

import numpy as np
import pandas as pd

from models.schemas import MarketRegime

PERIOD = 14
PIVOT_SPAN = 2                       # bougies de part et d'autre d'un pivot
WARMUP_BARS = 100                    # historique minimal : le lissage de Wilder converge lentement
ADX_TREND = 25.0
ADX_RANGE = 20.0
SHOCK_TR_ATR = 3.0                   # bougie dont l'amplitude vaut 3 ATR précédents
SHOCK_ATR_RATIO = 1.5                # ATR précédent >= 1.5 x sa médiane : volatilité anormale
SHOCK_SPREAD_RATIO = 2.0
ATR_MEDIAN_BARS = 100
SPREAD_MEDIAN_BARS = 60
RANGE_EDGE_ATR = 1.0                 # deux sommets (ou creux) « alignés » à 1 ATR près
RANGE_MIN_WIDTH_ATR = 2.0


class Structure(str, Enum):
    BULLISH = "haussière"
    BEARISH = "baissière"
    UNDETERMINED = "indéterminée"


# =============================================================================
#  ATR et ADX de Wilder
# =============================================================================

def _true_range_array(high: np.ndarray, low: np.ndarray, close: np.ndarray) -> np.ndarray:
    prev_close = np.concatenate([[np.nan], close[:-1]])
    tr = np.fmax(high - low, np.fmax(np.abs(high - prev_close), np.abs(low - prev_close)))
    if len(tr):
        tr[0] = high[0] - low[0]                              # première bougie : pas de clôture précédente
    return tr


def true_range(df: pd.DataFrame) -> pd.Series:
    return pd.Series(_true_range_array(*_ohlc_arrays(df)), index=df.index)


def _ohlc_arrays(df: pd.DataFrame) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    return (df["high"].to_numpy(dtype=float), df["low"].to_numpy(dtype=float), df["close"].to_numpy(dtype=float))


def _wilder_average(values: np.ndarray, period: int, start: int) -> np.ndarray:
    """Moyenne de Wilder : moyenne simple des `period` premières valeurs (à partir de `start`), puis récursive."""
    out = np.full(len(values), np.nan)
    first = start + period - 1
    if len(values) <= first:
        return out
    out[first] = values[start:first + 1].mean()
    for i in range(first + 1, len(values)):
        out[i] = (out[i - 1] * (period - 1) + values[i]) / period
    return out


def _wilder_sum(values: np.ndarray, period: int, start: int) -> np.ndarray:
    """Lissage de Wilder en somme : somme des `period` premières valeurs, puis S - S/period + x."""
    out = np.full(len(values), np.nan)
    first = start + period - 1
    if len(values) <= first:
        return out
    out[first] = values[start:first + 1].sum()
    for i in range(first + 1, len(values)):
        out[i] = out[i - 1] - out[i - 1] / period + values[i]
    return out


def atr(df: pd.DataFrame, period: int = PERIOD) -> pd.Series:
    """ATR de Wilder ; NaN tant que `period` bougies ne sont pas disponibles."""
    return pd.Series(_wilder_average(_true_range_array(*_ohlc_arrays(df)), period, start=0), index=df.index)


def _adx_arrays(high: np.ndarray, low: np.ndarray, tr: np.ndarray, period: int) -> tuple[np.ndarray, ...]:
    up = np.diff(high, prepend=np.nan)
    down = -np.diff(low, prepend=np.nan)
    with np.errstate(invalid="ignore"):
        plus_dm = np.where((up > down) & (up > 0), up, 0.0)
        minus_dm = np.where((down > up) & (down > 0), down, 0.0)
    # les mouvements directionnels commencent à la 2e bougie (diff) : lissage à partir de l'indice 1
    smoothed_tr = _wilder_sum(tr, period, start=1)
    with np.errstate(invalid="ignore", divide="ignore"):
        plus_di = 100 * _wilder_sum(plus_dm, period, start=1) / smoothed_tr
        minus_di = 100 * _wilder_sum(minus_dm, period, start=1) / smoothed_tr
        total = plus_di + minus_di
        dx = np.where(total > 0, 100 * np.abs(plus_di - minus_di) / np.where(total > 0, total, 1.0), 0.0)
    dx[np.isnan(plus_di)] = np.nan
    adx_values = _wilder_average(np.nan_to_num(dx, nan=0.0), period, start=period)   # premier DX valide : `period`
    adx_values[: 2 * period - 1] = np.nan
    return adx_values, plus_di, minus_di


def adx(df: pd.DataFrame, period: int = PERIOD) -> pd.DataFrame:
    """Colonnes adx, plus_di, minus_di (Wilder) ; +DI / -DI dès la bougie `period`, ADX dès `2 x period - 1`."""
    high, low, close = _ohlc_arrays(df)
    values, plus_di, minus_di = _adx_arrays(high, low, _true_range_array(high, low, close), period)
    return pd.DataFrame({"adx": values, "plus_di": plus_di, "minus_di": minus_di}, index=df.index)


# =============================================================================
#  Pivots confirmés et structure
# =============================================================================

@dataclass(frozen=True)
class SwingPivot:
    kind: str                         # "high" (sommet) ou "low" (creux)
    position: int                     # indice de la bougie du pivot
    time: pd.Timestamp
    price: float
    confirmed_position: int           # indice de la bougie dont la clôture confirme le pivot (position + 2)
    confirmed_time: pd.Timestamp


def confirmed_pivots(df: pd.DataFrame, upto: int | None = None, span: int = PIVOT_SPAN) -> list[SwingPivot]:
    """
    Pivots confirmés à la clôture de la bougie `upto` (dernière bougie par défaut) : seuls les
    pivots dont les `span` bougies suivantes sont clôturées (position + span <= upto) sont rendus.
    Comparaisons strictes : un plus haut égal à un voisin n'est pas un sommet.
    """
    n = len(df)
    upto = n - 1 if upto is None else min(upto, n - 1)
    highs, lows = df["high"].to_numpy(dtype=float), df["low"].to_numpy(dtype=float)
    candidates = np.arange(span, upto - span + 1)
    if candidates.size == 0:
        return []
    is_high = np.ones(candidates.size, dtype=bool)
    is_low = np.ones(candidates.size, dtype=bool)
    for k in range(1, span + 1):                  # strictement au-delà des k-ièmes voisins des deux côtés
        is_high &= (highs[candidates] > highs[candidates - k]) & (highs[candidates] > highs[candidates + k])
        is_low &= (lows[candidates] < lows[candidates - k]) & (lows[candidates] < lows[candidates + k])
    pivots: list[SwingPivot] = []
    for i in candidates[is_high | is_low]:
        i = int(i)
        confirm = i + span
        if is_high[i - span]:
            pivots.append(SwingPivot("high", i, df.index[i], float(highs[i]), confirm, df.index[confirm]))
        if is_low[i - span]:
            pivots.append(SwingPivot("low", i, df.index[i], float(lows[i]), confirm, df.index[confirm]))
    return pivots


def _monotonic(prices: list[float], increasing: bool) -> bool:
    pairs = zip(prices, prices[1:])
    return all(b > a for a, b in pairs) if increasing else all(b < a for a, b in pairs)


def market_structure(pivots: list[SwingPivot], count: int = 3) -> Structure:
    """Structure d'après les `count` derniers sommets et creux confirmés."""
    highs = [p.price for p in pivots if p.kind == "high"][-count:]
    lows = [p.price for p in pivots if p.kind == "low"][-count:]
    if len(highs) < count or len(lows) < count:
        return Structure.UNDETERMINED
    if _monotonic(highs, True) and _monotonic(lows, True):
        return Structure.BULLISH
    if _monotonic(highs, False) and _monotonic(lows, False):
        return Structure.BEARISH
    return Structure.UNDETERMINED


# =============================================================================
#  Pivots classiques de séance (supports / résistances institutionnels)
# =============================================================================

@dataclass(frozen=True)
class Pivots:
    """Points pivots classiques (P = (H + B + C) / 3) calculés sur la séance précédente."""
    pivot: float
    r1: float
    r2: float
    s1: float
    s2: float

    @classmethod
    def from_bar(cls, high: float, low: float, close: float) -> Pivots:
        p = (high + low + close) / 3
        return cls(pivot=p, r1=2 * p - low, r2=p + (high - low), s1=2 * p - high, s2=p - (high - low))

    def levels(self) -> dict[str, float]:
        return {"S2": self.s2, "S1": self.s1, "P": self.pivot, "R1": self.r1, "R2": self.r2}

    def nearest(self, price: float, above: bool) -> tuple[str, float] | None:
        """Niveau le plus proche au-dessus (résistance) ou en dessous (support) du prix."""
        candidates = [(name, lvl) for name, lvl in self.levels().items() if (lvl > price if above else lvl < price)]
        if not candidates:
            return None
        return min(candidates, key=lambda item: abs(item[1] - price))


def previous_session_pivots(daily: pd.DataFrame | None) -> Pivots | None:
    """Pivots de la dernière séance journalière clôturée (`daily` : bougies D1 clôturées)."""
    if daily is None or daily.empty:
        return None
    last = daily.iloc[-1]
    return Pivots.from_bar(float(last["high"]), float(last["low"]), float(last["close"]))


# =============================================================================
#  Classification
# =============================================================================

@dataclass(frozen=True)
class RegimeReading:
    regime: MarketRegime
    structure: Structure
    adx: float | None
    plus_di: float | None
    minus_di: float | None
    atr: float | None                 # ATR(t-1) : référence pour juger la bougie t
    atr_ratio: float | None           # ATR(t-1) / médiane des ATR_MEDIAN_BARS ATR précédents
    true_range: float | None          # amplitude de la dernière bougie
    pivots: tuple[SwingPivot, ...] = ()
    range_bounds: tuple[float, float] | None = None
    session_pivots: Pivots | None = None
    reasons: tuple[str, ...] = field(default_factory=tuple)

    @property
    def direction(self) -> int:
        return {MarketRegime.TREND_UP: 1, MarketRegime.TREND_DOWN: -1}.get(self.regime, 0)

    @property
    def is_trending(self) -> bool:
        return self.direction != 0


def _finite(value) -> float | None:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if np.isfinite(value) else None


def _range_bounds(pivots: list[SwingPivot], atr_value: float, close: float) -> tuple[tuple[float, float] | None, str]:
    highs = [p.price for p in pivots if p.kind == "high"][-2:]
    lows = [p.price for p in pivots if p.kind == "low"][-2:]
    if len(highs) < 2 or len(lows) < 2:
        return None, "bornes non validées (moins de deux sommets / creux confirmés)"
    upper, lower = max(highs), min(lows)
    if abs(highs[0] - highs[1]) > RANGE_EDGE_ATR * atr_value or abs(lows[0] - lows[1]) > RANGE_EDGE_ATR * atr_value:
        return None, "bornes non validées (sommets ou creux non alignés)"
    if upper - lower < RANGE_MIN_WIDTH_ATR * atr_value:
        return None, f"bornes non validées (largeur < {RANGE_MIN_WIDTH_ATR:g} ATR)"
    if not lower <= close <= upper:
        return None, "bornes non validées (clôture hors du range)"
    return (lower, upper), f"range {lower:.5g} - {upper:.5g}"


def detect_regime(df: pd.DataFrame, daily: pd.DataFrame | None = None, spread_points: float | None = None,
                  news_imminent: bool = False) -> RegimeReading:
    """
    Régime à la clôture de la dernière bougie de `df` (bougies clôturées uniquement).
    `spread_points` : spread actuel (comparé à la médiane de la colonne « spread » si présente) ;
    `news_imminent` : annonce majeure imminente (calendrier économique) -> CHOC.
    """
    session = previous_session_pivots(daily)
    if df is None or len(df) < WARMUP_BARS:
        size = 0 if df is None else len(df)
        return RegimeReading(MarketRegime.UNKNOWN, Structure.UNDETERMINED, None, None, None, None, None, None,
                             session_pivots=session,
                             reasons=(f"préchauffage insuffisant ({size}/{WARMUP_BARS} bougies)",))

    high, low, close = _ohlc_arrays(df)
    tr = _true_range_array(high, low, close)                                # calculé une fois : ATR et ADX
    atr_values = _wilder_average(tr, PERIOD, start=0)
    adx_values, plus_di, minus_di = _adx_arrays(high, low, tr, PERIOD)
    tr_last = _finite(tr[-1])
    atr_prev = _finite(atr_values[-2])                                      # disponible AVANT la dernière bougie
    history = atr_values[:-1][~np.isnan(atr_values[:-1])][-ATR_MEDIAN_BARS:]
    median_atr = _finite(np.median(history)) if len(history) else None
    ratio = atr_prev / median_atr if atr_prev and median_atr else None
    value_adx, plus, minus = _finite(adx_values[-1]), _finite(plus_di[-1]), _finite(minus_di[-1])
    pivots = confirmed_pivots(df)
    structure = market_structure(pivots)
    close = float(df["close"].iloc[-1])

    def reading(regime: MarketRegime, *reasons: str, bounds=None) -> RegimeReading:
        return RegimeReading(regime, structure, value_adx, plus, minus, atr_prev, ratio, tr_last, tuple(pivots[-6:]),
                             bounds, session, reasons)

    # 1. Choc : prioritaire sur tout le reste
    shocks = []
    if news_imminent:
        shocks.append("annonce économique majeure imminente")
    if tr_last is not None and atr_prev and tr_last >= SHOCK_TR_ATR * atr_prev:
        shocks.append(f"bougie de {tr_last / atr_prev:.1f} ATR (>= {SHOCK_TR_ATR:g})")
    if ratio is not None and ratio >= SHOCK_ATR_RATIO:
        shocks.append(f"ATR {ratio:.2f} x sa médiane (>= {SHOCK_ATR_RATIO:g})")
    if "spread" in df.columns:
        spreads = df["spread"].iloc[:-1].tail(SPREAD_MEDIAN_BARS)
        median_spread = _finite(spreads[spreads > 0].median()) if (spreads > 0).any() else None
        current = spread_points if spread_points is not None else _finite(df["spread"].iloc[-1])
        if median_spread and current is not None and current >= SHOCK_SPREAD_RATIO * median_spread:
            shocks.append(f"spread {current:g} pts >= {SHOCK_SPREAD_RATIO:g} x sa médiane ({median_spread:g})")
    if shocks:
        return reading(MarketRegime.SHOCK, *shocks)

    if value_adx is None or atr_prev is None:
        return reading(MarketRegime.UNKNOWN, "ADX / ATR incalculables")

    # 2. Tendance : structure + ADX
    if value_adx >= ADX_TREND and structure == Structure.BULLISH:
        return reading(MarketRegime.TREND_UP, f"structure haussière (3 sommets et 3 creux croissants), ADX {value_adx:.0f}")
    if value_adx >= ADX_TREND and structure == Structure.BEARISH:
        return reading(MarketRegime.TREND_DOWN, f"structure baissière (3 sommets et 3 creux décroissants), ADX {value_adx:.0f}")

    # 3. Range : ADX faible + bornes validées
    if value_adx <= ADX_RANGE:
        bounds, why = _range_bounds(pivots, atr_prev, close)
        if bounds is not None:
            return reading(MarketRegime.RANGE, f"ADX {value_adx:.0f} <= {ADX_RANGE:g}, {why}", bounds=bounds)
        return reading(MarketRegime.TRANSITION, f"ADX {value_adx:.0f} <= {ADX_RANGE:g} mais {why}")

    # 4. Transition
    return reading(MarketRegime.TRANSITION, f"ADX {value_adx:.0f}, structure {structure.value}")
