"""
Stratégie « analyse descendante » (achat ; la vente est le symétrique exact, calculé sur la série
miroir par strategies/base.py). Chaque unité de temps a un rôle, de la plus grande à la plus petite :

  1. W1 - direction : règle des 3 bougies sur les 3 dernières semaines CLÔTURÉES (la bougie en cours
     ne compte pas) : après un doji, la clôture au-dessus de son plus haut donne la direction haussière,
     la bougie suivante ne doit pas revenir sous le doji (market_structure.three_candle_direction)
  2. D1 - supports et résistances : sommets / creux regroupés, valides à partir de
     TOPDOWN_MIN_TOUCHES touches (3)
  3. H4 - offre et demande : zone de demande (Order Block haussier encore actif) MAJEURE, c'est-à-dire
     posée sur un support D1, touchée par le prix dans les TAP_BARS dernières bougies H4 et pas encore
     quittée de plus de MAX_DEPARTURE_ATR ATR H4
  4. H1 -> M30 -> M15 (-> M5 pour métaux, crypto et indices) - raffinement : sur l'unité de temps de
     raffinement, plus bas de la réaction depuis le contact avec la zone, puis cassure (clôture) du
     dernier sommet confirmé AVANT ce plus bas (changement de caractère : le marché reprend le dernier
     sommet de la baisse). Stop sous ce plus bas, moins la marge b. L'unité la plus grande dont le stop
     tient dans TOPDOWN_MAX_SL_ATR ATR H4 est retenue ; sinon on descend d'un cran.

Plan :
  - entrée   : premier Ask après confirmation, refusé s'il s'écarte de plus de 0.20 ATR (de l'unité de
               raffinement) de la clôture de confirmation, ou si la cassure date de plus de 3 bougies
  - stop     : plus bas de la réaction - b, avec b = max(0.10 ATR, 2 spreads, 1 tick)
  - objectif : ratio net estimé de 3 ; refusé si une résistance D1 se trouve avant l'objectif
  - note     : liquidité prise si le plus bas de la réaction est passé sous un support D1 aux creux égaux

Statuts : SURVEILLANCE (direction, support et zone valides, pas encore de cassure), PRÊT, INVALIDÉ
(stop trop large même raffiné, résistance avant l'objectif, entrée trop éloignée), EXPIRÉ.
"""

from __future__ import annotations

import time
from dataclasses import replace
from datetime import datetime

import numpy as np
import pandas as pd

from config.settings import settings
from core.regime_detector import atr as atr_series
from core.regime_detector import confirmed_pivots
from models.schemas import MarketRegime, SetupStatus, Side, TradingSetup, TradingStyle
from strategies.base import (
    ENTRY_MAX_DRIFT_ATR,
    MIN_NET_REWARD_RISK,
    TIMEFRAME_SECONDS,
    BaseStrategy,
    BuyPlan,
    Quote,
    closed_candles,
    net_reward_risk,
    protection_margin,
    target_for_ratio,
)
from strategies.market_structure import Level, Zone, key_levels, supply_demand_zones, three_candle_direction

REFINEMENT_TIMEFRAMES = ("H1", "M30", "M15", "M5")
LEVEL_TOLERANCE_ATR = 0.25           # regroupement des sommets / creux D1 en niveaux, en ATR D1
ZONE_TOLERANCE_ATR = 0.30            # zone H4 « posée » sur un niveau D1, en ATR D1
TAP_BARS = 6                         # contact avec la zone H4 dans les 6 dernières bougies H4 (24 h)
MAX_DEPARTURE_ATR = 1.5              # prix encore à moins de 1.5 ATR H4 au-dessus de la zone
TRIGGER_FRESH_BARS = 3
MIN_BARS = {"W1": 3, "D1": 60, "H4": 60}
CACHE_MAX_SECONDS = 600              # bougies W1 / D1 / H4 relues au plus toutes les 10 minutes en direct


def _last_atr(df: pd.DataFrame) -> float:
    values = atr_series(df).dropna()
    return float(values.iloc[-1]) if len(values) else 0.0


def is_fine_symbol(symbol: str, prefixes: tuple[str, ...]) -> bool:
    """Métaux, crypto, indices : raffinement jusqu'en M5 (stop le plus serré possible)."""
    name = symbol.upper()
    return any(name.startswith(p) for p in prefixes)


class TopDownStrategy(BaseStrategy):
    name = "descendante"
    style = TradingStyle.DAY
    timeframes = ("H1", "W1", "D1", "H4", "M30", "M15", "M5")    # le premier fixe la durée de validité
    bars = 300
    validity_bars = 2
    label = "Analyse descendante"
    role = ("direction W1 (3 bougies), support / résistance D1, offre / demande H4 majeure, "
            "entrée raffinée H1 → M15 (M5 : métaux, crypto, indices)")
    order_tag = "TOPDOWN"
    anchor = "declencheur"

    def __init__(self) -> None:
        cfg = settings.strategy
        self.max_sl_atr = cfg.topdown_max_sl_atr
        self.min_touches = cfg.topdown_min_touches
        self.fine_symbols = cfg.topdown_fine_symbols
        self._fine = False
        self._cache: dict[tuple[str, str], tuple[float, pd.DataFrame]] = {}

    # ------------------------------------------------------------------ données

    def refinement_timeframes(self) -> tuple[str, ...]:
        return REFINEMENT_TIMEFRAMES if self._fine else REFINEMENT_TIMEFRAMES[:3]

    def load(self, broker, symbol: str) -> dict[str, pd.DataFrame]:
        """Bougies clôturées ; W1, D1, H4 gardées en cache (une bougie ne s'y clôture qu'au bout d'heures)."""
        data, now = {}, time.monotonic()
        for tf in dict.fromkeys(self.timeframes):
            ttl = min(TIMEFRAME_SECONDS[tf] / 60, CACHE_MAX_SECONDS)
            cached = self._cache.get((symbol, tf))
            if cached is not None and now - cached[0] < ttl:
                data[tf] = cached[1]
                continue
            data[tf] = closed_candles(broker.get_historical_data(symbol, count=self.bars, timeframe=tf))
            self._cache[(symbol, tf)] = (now, data[tf])
        return data

    def find_setup(self, symbol: str, data: dict[str, pd.DataFrame], now: datetime,
                   quote: Quote | None = None) -> TradingSetup | None:
        self._fine = is_fine_symbol(symbol, self.fine_symbols)
        return super().find_setup(symbol, data, now, quote)

    # ------------------------------------------------------------------ étapes 1 à 3

    @staticmethod
    def _tapped_zone(h4: pd.DataFrame, zones: list[Zone], atr_h4: float) -> tuple[Zone, pd.Timestamp] | None:
        """Zone de demande majeure touchée récemment et que le prix n'a pas encore quittée."""
        close = float(h4["close"].iloc[-1])
        recent = h4.iloc[-TAP_BARS:]
        best = None
        for zone in zones:
            touched = recent[(recent["low"] <= zone.high) & (recent.index > pd.Timestamp(zone.time))]
            if touched.empty or close > zone.high + MAX_DEPARTURE_ATR * atr_h4 or close < zone.low:
                continue
            first = touched.index[0]
            if best is None or zone.high > best[0].high:                 # la plus haute : la plus proche du prix
                best = (zone, first)
        return best

    def plan_buy(self, data: dict[str, pd.DataFrame], quote: Quote | None) -> BuyPlan | None:
        weekly, daily, h4 = (data.get(tf, pd.DataFrame()) for tf in ("W1", "D1", "H4"))
        if any(len(data.get(tf, ())) < n for tf, n in MIN_BARS.items()):
            return None

        # 1. Direction hebdomadaire
        weekly_dir = three_candle_direction(weekly)
        if weekly_dir["direction"] != "haussier":
            return None

        # 2. Supports / résistances D1 (TOPDOWN_MIN_TOUCHES touches)
        atr_d1, atr_h4 = _last_atr(daily), _last_atr(h4)
        if not atr_d1 or not atr_h4:
            return None
        levels = key_levels(confirmed_pivots(daily), atr_d1, LEVEL_TOLERANCE_ATR, self.min_touches)
        supports = [lv for lv in levels if lv.kind == "support"]
        if not supports:
            return None

        # 3. Demande H4 majeure (posée sur un support D1) en contact
        zones = [z for z in supply_demand_zones(h4, levels, ZONE_TOLERANCE_ATR * atr_d1)
                 if z.kind == "demande" and z.major]
        tapped = self._tapped_zone(h4, zones, atr_h4)
        if tapped is None:
            return None
        zone, tap_time = tapped
        support = min(supports, key=lambda lv: abs(lv.price - (zone.low + zone.high) / 2))
        resistances = sorted(lv.price for lv in levels if lv.kind == "résistance" and lv.price > zone.high)
        # aucun prix dans les textes : pour une vente ils seraient ceux de la série miroir (niveaux : `levels`)
        rule = ("majorité des 3 bougies" if weekly_dir["doji"] is None else "doji puis clôture au-delà"
                + (", bougie suivante dans le même sens" if "confirmée" in weekly_dir["reason"] else ""))
        context = (f"Direction {{direction}} en W1 (règle des 3 bougies : {rule}) ; {{support}} D1 validé "
                   f"({support.touches} touches) ; {{demande}} H4 majeure posée dessus et touchée")
        base_levels = [("support_d1", support.price), ("zone_basse", zone.low), ("zone_haute", zone.high)]

        # 4. Raffinement : la plus grande unité dont le stop tient dans le budget
        budget = self.max_sl_atr * atr_h4
        oversized, finest = [], None
        for tf in self.refinement_timeframes():
            plan = self._refine(tf, data.get(tf, pd.DataFrame()), tap_time, quote, support, resistances, context,
                                base_levels, atr_h4)
            if plan is None:
                continue
            if plan.entry - plan.stop_loss <= budget:
                return plan
            oversized.append(f"{tf} {(plan.entry - plan.stop_loss) / atr_h4:.2f}")
            finest = plan
        if finest is None:
            return None
        return replace(finest, status=SetupStatus.INVALIDATED,
                       notes=finest.notes + (f"stop trop large même raffiné, en ATR H4 : {', '.join(oversized)} "
                                             f"(maximum {self.max_sl_atr:g})",))

    # ------------------------------------------------------------------ étape 4

    def _refine(self, tf: str, bars: pd.DataFrame, tap_time: pd.Timestamp, quote: Quote | None, support: Level,
                resistances: list[float], context: str, base_levels: list[tuple[str, float]],
                atr_h4: float) -> BuyPlan | None:
        after = bars[bars.index >= tap_time] if len(bars) else bars
        if len(bars) < 30 or len(after) < 3:
            return None
        atr_tf = _last_atr(bars)
        if not atr_tf:
            return None
        low_time = after["low"].idxmin()
        reaction_low = float(after["low"].min())
        spread = quote.spread if quote else 0.0
        tick = quote.tick_size if quote else 0.0
        margin = protection_margin(atr_tf, spread, tick)
        stop = reaction_low - margin
        notes = []
        if support.liquidity and reaction_low < support.price:
            notes.append("liquidité prise sous le {support} D1 (creux égaux)")
        levels = [*base_levels, ("plus_bas_reaction", reaction_low)]
        indicators = [("atr_h4", atr_h4), (f"atr_{tf.lower()}", atr_tf), ("marge_b", margin)]
        prefix = f"{context} ; raffinement {tf}"

        def plan(status: SetupStatus, entry: float, reason: str, extra: tuple[tuple[str, float], ...] = (),
                 more: tuple[str, ...] = ()) -> BuyPlan | None:
            if entry <= stop:
                return None
            take_profit = target_for_ratio(Side.BUY, entry, stop, MIN_NET_REWARD_RISK, spread)
            blocking = [r for r in resistances if r < take_profit]
            all_notes = tuple(notes) + more
            if blocking and status == SetupStatus.READY:
                status = SetupStatus.INVALIDATED
                all_notes += (f"{{resistance}} D1 avant l'objectif à {MIN_NET_REWARD_RISK:g} R",)
                extra = extra + (("obstacle_d1", blocking[0]),)
            return BuyPlan(status, entry, stop, take_profit, net_reward_risk(Side.BUY, entry, stop, take_profit, spread),
                           MarketRegime.TREND_UP, f"{prefix} : {reason}", all_notes, tuple(levels) + extra,
                           tuple(indicators))

        # dernier sommet confirmé AVANT le plus bas de la réaction : sa cassure marque le changement de caractère
        highs = [p for p in confirmed_pivots(bars) if p.kind == "high" and p.time < low_time]
        if not highs:
            return plan(SetupStatus.WATCHING, float(bars["close"].iloc[-1]),
                        "attente d'un {sommet} de référence pour la cassure")
        trigger = highs[-1]
        closes = bars["close"].to_numpy(dtype=float)
        low_pos = int(bars.index.get_loc(low_time))
        above = np.nonzero(closes[low_pos + 1:] > trigger.price)[0]
        trigger_level = (("declencheur", trigger.price),)
        if not above.size:
            return plan(SetupStatus.WATCHING, trigger.price + max(tick, 0.0),
                        "attente de la clôture {au_dessus} du dernier {sommet} de la baisse", trigger_level)
        confirm_pos = low_pos + 1 + int(above[0])
        confirm_close = float(closes[confirm_pos])
        extra = trigger_level + (("clôture_confirmation", confirm_close),)
        if len(bars) - 1 - confirm_pos >= TRIGGER_FRESH_BARS:
            return plan(SetupStatus.EXPIRED, confirm_close,
                        f"cassure trop ancienne (> {TRIGGER_FRESH_BARS} bougies {tf})", extra)
        if quote is None:
            return plan(SetupStatus.WATCHING, confirm_close, "cassure confirmée, prix exécutable indisponible", extra)
        entry = quote.entry(Side.BUY)
        if abs(entry - confirm_close) > ENTRY_MAX_DRIFT_ATR * atr_tf:
            return plan(SetupStatus.INVALIDATED, entry, "cassure confirmée", extra,
                        (f"entrée refusée : prix à {abs(entry - confirm_close) / atr_tf:.2f} ATR {tf} de la clôture "
                         f"de confirmation (maximum {ENTRY_MAX_DRIFT_ATR:g})",))
        return plan(SetupStatus.READY, entry, "cassure confirmée du dernier {sommet} de la baisse (changement de "
                    "caractère)", extra)
