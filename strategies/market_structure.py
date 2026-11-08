"""
Structure de marché, avec des définitions programmables (paramètres de `StructureParams`) :

  - sommets et creux significatifs : pivots confirmés du détecteur de régime (`swing_strength`
    bougies de chaque côté), connus seulement `swing_strength` bougies après coup
  - cassures de structure : clôture au-delà du dernier sommet (creux) confirmé ; BOS si la cassure
    prolonge la tendance en cours, CHoCH (changement de caractère) si elle la retourne
  - Fair Value Gaps : déséquilibre entre la mèche de la bougie i-2 et celle de la bougie i
    (au moins `fvg_min_atr` ATR), comblé quand le prix retraverse toute la zone
  - Order Blocks : dernière bougie de sens opposé avant l'impulsion qui a cassé la structure,
    invalidé par une clôture au-delà de la zone
  - supports et résistances : sommets / creux regroupés à `level_tolerance_atr` ATR près
  - zones de liquidité : sommets (creux) égaux, où des stops sont SUSCEPTIBLES de se concentrer.
    Ce n'est qu'une estimation déduite du graphique : le carnet d'ordres réel n'est pas visible.
  - bandes de Bollinger et régime de volatilité (compression / expansion de leur largeur)
  - contexte multi-unités de temps : direction D1, H4 et H1 (EMA 20/50 + dernière cassure)
  - règle des 3 bougies (hebdomadaire) : direction donnée par la clôture qui suit un doji
  - zones d'offre et de demande : Order Blocks encore actifs, MAJEURS s'ils reposent sur un support
    (demande) ou une résistance (offre) d'une unité de temps supérieure, mineurs sinon

Toutes les fonctions travaillent sur les bougies CLÔTURÉES et n'utilisent jamais une bougie future
(chaque élément porte l'heure à laquelle il est connu).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from typing import Any, Literal

import numpy as np
import pandas as pd
from ta.trend import EMAIndicator
from ta.volatility import BollingerBands

from core.regime_detector import SwingPivot, atr, confirmed_pivots
from strategies.base import closed_candles

Direction = Literal["haussier", "baissier", "range"]

CONTEXT_TIMEFRAMES = ("D1", "H4", "H1")
CONTEXT_BARS = 300
CONTEXT_MIN_BARS = 60
MAX_ITEMS = 5                       # zones / niveaux renvoyés au plus, les plus proches du prix d'abord


@dataclass(frozen=True)
class StructureParams:
    swing_strength: int = 2             # bougies de chaque côté d'un pivot
    fvg_min_atr: float = 0.1            # taille minimale d'un FVG, en ATR
    ob_max_lookback: int = 10           # bougies remontées pour trouver la bougie d'un Order Block
    level_tolerance_atr: float = 0.25   # regroupement des sommets / creux en niveaux
    min_touches: int = 2                # touches minimales pour un support / une résistance
    atr_window: int = 14
    bollinger_window: int = 20
    bollinger_std: float = 2.0
    squeeze_percentile: float = 20.0    # largeur des bandes sous ce centile : compression
    expansion_percentile: float = 80.0


@dataclass(frozen=True)
class Swing:
    kind: Literal["sommet", "creux"]
    price: float
    time: str
    confirmed_at: str                   # bougie à la clôture de laquelle le pivot est connu


@dataclass(frozen=True)
class StructureBreak:
    kind: Literal["BOS", "CHoCH"]
    direction: Literal["haussier", "baissier"]
    level: float                        # sommet / creux cassé
    time: str                           # bougie de cassure (clôture)


@dataclass(frozen=True)
class Zone:
    kind: str                           # fvg / order_block
    direction: Literal["haussier", "baissier"]
    low: float
    high: float
    time: str
    active: bool = True                 # FVG non comblé / Order Block non invalidé
    major: bool = False                 # zone d'offre / de demande collée à un niveau de l'unité supérieure


@dataclass(frozen=True)
class Level:
    kind: Literal["support", "résistance"]
    price: float
    touches: int
    liquidity: bool                     # sommets / creux égaux : stops probablement regroupés au-delà


@dataclass
class MarketStructure:
    trend: Direction
    last_break: StructureBreak | None
    swings: list[Swing] = field(default_factory=list)
    breaks: list[StructureBreak] = field(default_factory=list)
    fair_value_gaps: list[Zone] = field(default_factory=list)
    order_blocks: list[Zone] = field(default_factory=list)
    levels: list[Level] = field(default_factory=list)
    bollinger: dict[str, Any] = field(default_factory=dict)
    atr: float | None = None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def summary(self) -> str:
        parts = [f"structure {self.trend}"]
        if self.last_break:
            parts.append(f"dernière cassure {self.last_break.kind} {self.last_break.direction} "
                         f"({self.last_break.level:g})")
        if self.bollinger.get("regime"):
            parts.append(f"volatilité {self.bollinger['regime']}")
        return ", ".join(parts)


# =============================================================================
#  Briques
# =============================================================================

def _stamp(index: pd.Index, i: int) -> str:
    value = index[i]
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def structure_breaks(df: pd.DataFrame,
                     pivots: list[SwingPivot]) -> tuple[list[tuple[int, str, str, float]], Direction]:
    """
    Cassures (position, BOS|CHoCH, direction, niveau) dans l'ordre chronologique et tendance finale.
    Un sommet n'est cassable qu'une fois confirmé et qu'une seule fois.
    """
    closes = df["close"].to_numpy()
    by_confirmation: dict[int, list[tuple[str, float]]] = {}
    for pivot in pivots:
        by_confirmation.setdefault(pivot.confirmed_position, []).append((pivot.kind, pivot.price))
    last_high = last_low = None
    trend: Direction = "range"
    breaks = []
    for j in range(len(df)):
        if last_high is not None and closes[j] > last_high:
            breaks.append((j, "CHoCH" if trend == "baissier" else "BOS", "haussier", last_high))
            trend, last_high = "haussier", None
        elif last_low is not None and closes[j] < last_low:
            breaks.append((j, "CHoCH" if trend == "haussier" else "BOS", "baissier", last_low))
            trend, last_low = "baissier", None
        for kind, price in by_confirmation.get(j, ()):        # connu à la clôture de j : cassable dès j + 1
            if kind == "high":
                last_high = price
            else:
                last_low = price
    return breaks, trend


def fair_value_gaps(df: pd.DataFrame, volatility: pd.Series, min_atr: float = 0.1) -> list[Zone]:
    highs, lows = df["high"].to_numpy(), df["low"].to_numpy()
    zones = []
    for i in range(2, len(df)):
        size_min = min_atr * (volatility.iloc[i] if not np.isnan(volatility.iloc[i]) else 0.0)
        if lows[i] > highs[i - 2] and lows[i] - highs[i - 2] > size_min:
            low, high, direction = highs[i - 2], lows[i], "haussier"
            filled = bool((lows[i + 1:] <= low).any())
        elif highs[i] < lows[i - 2] and lows[i - 2] - highs[i] > size_min:
            low, high, direction = highs[i], lows[i - 2], "baissier"
            filled = bool((highs[i + 1:] >= high).any())
        else:
            continue
        zones.append(Zone("fvg", direction, float(low), float(high), _stamp(df.index, i), active=not filled))
    return zones


def order_blocks(df: pd.DataFrame, breaks: list[tuple[int, str, str, float]], lookback: int = 10) -> list[Zone]:
    """Dernière bougie de sens opposé avant chaque cassure ; invalidé par une clôture au-delà de la zone."""
    opens, closes = df["open"].to_numpy(), df["close"].to_numpy()
    highs, lows = df["high"].to_numpy(), df["low"].to_numpy()
    zones, seen = [], set()
    for j, _kind, direction, _level in breaks:
        bullish = direction == "haussier"
        k = next((k for k in range(j - 1, max(-1, j - 1 - lookback), -1)
                  if ((closes[k] < opens[k]) if bullish else (closes[k] > opens[k]))), None)
        if k is None or k in seen:
            continue
        seen.add(k)
        after = closes[j + 1:]
        broken = bool((after < lows[k]).any()) if bullish else bool((after > highs[k]).any())
        zones.append(Zone("order_block", direction, float(lows[k]), float(highs[k]), _stamp(df.index, k),
                          active=not broken))
    return zones


def key_levels(pivots: list[SwingPivot], atr_value: float, tolerance_atr: float = 0.25,
               min_touches: int = 2, price: float | None = None) -> list[Level]:
    """Sommets (résistances) et creux (supports) regroupés ; les sommets / creux égaux marquent la liquidité."""
    tolerance = tolerance_atr * atr_value
    levels = []
    for kind, label in (("high", "résistance"), ("low", "support")):
        prices = sorted(p.price for p in pivots if p.kind == kind)
        clusters: list[list[float]] = []
        for p in prices:
            if clusters and p - clusters[-1][-1] <= tolerance:
                clusters[-1].append(p)
            else:
                clusters.append([p])
        for cluster in clusters:
            if len(cluster) >= min_touches:
                levels.append(Level(label, round(float(np.mean(cluster)), 6), len(cluster),
                                    liquidity=max(cluster) - min(cluster) <= tolerance / 2))
    if price is not None:
        levels.sort(key=lambda lv: abs(lv.price - price))
    return levels


def is_doji(open_: float, high: float, low: float, close: float, body_ratio: float = 0.25) -> bool:
    """Corps <= `body_ratio` de l'amplitude : acheteurs et vendeurs à égalité."""
    span = high - low
    return span > 0 and abs(close - open_) <= body_ratio * span


def three_candle_direction(df: pd.DataFrame | None, doji_body_ratio: float = 0.25) -> dict[str, Any]:
    """
    Règle des 3 bougies sur les 3 dernières bougies CLÔTURÉES de `df` (la bougie en cours ne compte pas) :
      - doji : hésitation ; la bougie suivante qui CLÔTURE au-dessus de son plus haut (sous son plus bas)
        donne la direction ; la bougie d'après ne doit pas la contredire (clôture de l'autre côté du doji)
      - doji en dernière position : confirmation attendue (range)
      - sans doji : direction de la majorité des bougies si la dernière clôture dépasse l'ouverture
        de la première
    """
    bars = df.tail(3) if df is not None else pd.DataFrame()
    if len(bars) < 3:
        return {"direction": "range", "reason": "moins de 3 bougies clôturées", "doji": None, "candles": []}
    o, h, l, c = (bars[col].to_numpy(dtype=float) for col in ("open", "high", "low", "close"))
    dojis = [i for i in range(3) if is_doji(o[i], h[i], l[i], c[i], doji_body_ratio)]
    candles = [{"time": _stamp(bars.index, i), "open": o[i], "high": h[i], "low": l[i], "close": c[i],
                "doji": i in dojis} for i in range(3)]
    direction: Direction = "range"
    if dojis:
        d = dojis[-1]
        if d == 2:
            reason = "doji sur la dernière bougie : confirmation attendue"
        else:
            nxt = d + 1
            if c[nxt] > h[d]:
                direction, reason = "haussier", "clôture au-dessus du doji"
            elif c[nxt] < l[d]:
                direction, reason = "baissier", "clôture sous le doji"
            else:
                reason = "la bougie qui suit le doji clôture dans son amplitude"
            if direction != "range" and nxt < 2:
                back = c[2] < l[d] if direction == "haussier" else c[2] > h[d]
                if back:
                    direction, reason = "range", "la dernière bougie est revenue de l'autre côté du doji"
                else:
                    reason += ", confirmée par la bougie suivante"
        return {"direction": direction, "reason": reason, "doji": d, "candles": candles}
    bullish, bearish = int((c > o).sum()), int((c < o).sum())
    if bullish >= 2 and c[2] > o[0]:
        direction, reason = "haussier", f"{bullish} bougies haussières sur 3, clôture au-dessus de l'ouverture"
    elif bearish >= 2 and c[2] < o[0]:
        direction, reason = "baissier", f"{bearish} bougies baissières sur 3, clôture sous l'ouverture"
    else:
        reason = "bougies sans direction commune"
    return {"direction": direction, "reason": reason, "doji": None, "candles": candles}


def supply_demand_zones(df: pd.DataFrame, levels: list[Level], tolerance: float,
                        params: StructureParams | None = None) -> list[Zone]:
    """
    Zones de demande (Order Blocks haussiers) et d'offre (baissiers) encore actives de `df` (bougies
    clôturées). MAJEURE : la zone repose sur un support (demande) / une résistance (offre) de `levels`
    (unité de temps supérieure), à `tolerance` près en prix ; mineure sinon.
    """
    p = params or StructureParams()
    pivots = confirmed_pivots(df, span=p.swing_strength)
    raw_breaks, _ = structure_breaks(df, pivots)
    zones = []
    for zone in order_blocks(df, raw_breaks, p.ob_max_lookback):
        if not zone.active:
            continue
        kind = "demande" if zone.direction == "haussier" else "offre"
        wanted = "support" if kind == "demande" else "résistance"
        major = any(lv.kind == wanted and zone.low - tolerance <= lv.price <= zone.high + tolerance for lv in levels)
        zones.append(replace(zone, kind=kind, major=major))
    return zones


def bollinger(df: pd.DataFrame, window: int = 20, std: float = 2.0, squeeze: float = 20.0,
              expansion: float = 80.0) -> dict[str, Any]:
    if len(df) < window + 1:
        return {}
    bands = BollingerBands(df["close"], window=window, window_dev=std)
    upper, middle, lower = bands.bollinger_hband(), bands.bollinger_mavg(), bands.bollinger_lband()
    width = ((upper - lower) / middle).dropna()
    if width.empty:
        return {}
    rank = float((width < width.iloc[-1]).mean() * 100)
    regime = "compression" if rank <= squeeze else "expansion" if rank >= expansion else "normale"
    close = float(df["close"].iloc[-1])
    return {"upper": round(float(upper.iloc[-1]), 6), "middle": round(float(middle.iloc[-1]), 6),
            "lower": round(float(lower.iloc[-1]), 6), "width": round(float(width.iloc[-1]), 6),
            "width_percentile": round(rank, 1), "regime": regime,
            "position": "au-dessus" if close > upper.iloc[-1] else "en dessous" if close < lower.iloc[-1] else "dans"}


# =============================================================================
#  Analyse complète
# =============================================================================

def analyze_structure(df: pd.DataFrame | None, params: StructureParams | None = None,
                      closed: bool = True) -> MarketStructure:
    """Structure du graphique `df` (OHLC) ; `closed=True` retire la bougie en cours de formation."""
    p = params or StructureParams()
    bars = closed_candles(df) if closed else df
    if bars is None or len(bars) < max(p.bollinger_window, p.atr_window) + 2 * p.swing_strength + 2:
        return MarketStructure(trend="range", last_break=None, notes=["historique insuffisant"])
    volatility = atr(bars, p.atr_window)
    last_atr = float(volatility.dropna().iloc[-1]) if volatility.notna().any() else 0.0
    pivots = confirmed_pivots(bars, span=p.swing_strength)
    raw_breaks, trend = structure_breaks(bars, pivots)
    breaks = [StructureBreak(kind, direction, level, _stamp(bars.index, j)) for j, kind, direction, level in raw_breaks]
    price = float(bars["close"].iloc[-1])

    def nearest(zones: list[Zone]) -> list[Zone]:
        active = [z for z in zones if z.active]
        return sorted(active, key=lambda z: min(abs(price - z.low), abs(price - z.high)))[:MAX_ITEMS]

    return MarketStructure(
        trend=trend,
        last_break=breaks[-1] if breaks else None,
        swings=[Swing("sommet" if pv.kind == "high" else "creux", pv.price, _stamp(bars.index, pv.position),
                      _stamp(bars.index, pv.confirmed_position)) for pv in pivots[-2 * MAX_ITEMS:]],
        breaks=breaks[-MAX_ITEMS:],
        fair_value_gaps=nearest(fair_value_gaps(bars, volatility, p.fvg_min_atr)),
        order_blocks=nearest(order_blocks(bars, raw_breaks, p.ob_max_lookback)),
        levels=key_levels(pivots, last_atr, p.level_tolerance_atr, p.min_touches, price)[:MAX_ITEMS] if last_atr else [],
        bollinger=bollinger(bars, p.bollinger_window, p.bollinger_std, p.squeeze_percentile, p.expansion_percentile),
        atr=round(last_atr, 6) if last_atr else None,
        notes=["zones de liquidité estimées d'après le graphique : le carnet d'ordres réel n'est pas visible"],
    )


def timeframe_direction(df: pd.DataFrame | None) -> dict[str, Any]:
    """Direction d'une unité de temps : EMA 20/50 et dernière cassure doivent concorder, sinon range."""
    bars = closed_candles(df)
    if len(bars) < CONTEXT_MIN_BARS:
        return {"direction": "range", "reason": "historique insuffisant"}
    close = bars["close"]
    fast = float(EMAIndicator(close, window=20).ema_indicator().iloc[-1])
    slow = float(EMAIndicator(close, window=50).ema_indicator().iloc[-1])
    structure = analyze_structure(bars, closed=False)
    ema = "haussier" if fast > slow else "baissier" if fast < slow else "range"
    direction: Direction = ema if ema == structure.trend else "range"
    return {"direction": direction, "ema": ema, "structure": structure.trend,
            "reason": f"EMA 20 {'>' if fast > slow else '<'} EMA 50, structure {structure.trend}"}


def multi_timeframe_context(broker, symbol: str, timeframes: tuple[str, ...] = CONTEXT_TIMEFRAMES) -> dict[str, Any]:
    """Direction dominante (D1, H4) et structure intermédiaire (H1) ; « aligné » si toutes concordent."""
    directions = {}
    for tf in timeframes:
        try:
            directions[tf] = timeframe_direction(broker.get_historical_data(symbol, CONTEXT_BARS, tf))
        except Exception as exc:  # noqa: BLE001 - une unité de temps indisponible n'empêche pas les autres
            directions[tf] = {"direction": "range", "reason": f"données indisponibles : {exc}"}
    values = {d["direction"] for d in directions.values()}
    dominant = values.pop() if len(values) == 1 else "mixte"
    return {"symbol": symbol, "timeframes": directions, "dominant": dominant,
            "aligned": dominant in ("haussier", "baissier")}
