"""
Stratégie C - Rejet de range (achat sur la borne basse ; la vente sur la borne haute est le
symétrique exact, calculé sur la série miroir par strategies/base.py).

Conditions (timeframe du signal, H1 par défaut, bougies clôturées) :
  1. Contexte, évalué AVANT le balayage : range validé par le détecteur causal (ADX <= 20,
     bornes B / H validées) et au moins 2 contacts sur chaque borne (pivots confirmés dans les
     20 % extrêmes du range)
  2. Balayage : une bougie dont le plus bas passe sous la borne basse B (contact avec les 20 %
     inférieurs du range, puis au-delà)
  3. Réintégration : une clôture revenue au-dessus de B au plus tard 3 bougies après le balayage
  4. Confirmation : la dernière bougie clôturée (la bougie de réintégration ou la suivante) est
     haussière et clôture dans le tiers supérieur de son amplitude
Plan :
  - entrée   : premier Ask après la confirmation, refusé s'il s'écarte de plus de 0.20 ATR de la
               clôture de confirmation
  - stop     : extrême du balayage - b, avec b = max(0.10 ATR, 2 spreads, 1 tick)
  - objectif : borne opposée H - b
  - rejet automatique (INVALIDÉ) si le ratio net estimé (gain net / perte prévue) est < 3
"""

from __future__ import annotations

import pandas as pd

from core.regime_detector import WARMUP_BARS, confirmed_pivots, detect_regime
from models.schemas import MarketRegime, SetupStatus, Side, TradingStyle
from strategies.base import (
    ENTRY_MAX_DRIFT_ATR,
    MIN_NET_REWARD_RISK,
    BaseStrategy,
    BuyPlan,
    Quote,
    net_reward_risk,
    protection_margin,
)

EDGE_ZONE = 0.20                     # 20 % extrêmes du range
MIN_CONTACTS = 2
MAX_REINTEGRATION_BARS = 3
CLOSE_IN_TOP = 2 / 3                 # clôture dans le tiers supérieur de la bougie


class RangeRejectionStrategy(BaseStrategy):
    name = "range_rejet"
    style = TradingStyle.DAY
    bars = 400
    validity_bars = 1

    def __init__(self, timeframe: str = "H1", style: TradingStyle = TradingStyle.DAY) -> None:
        self.timeframes = (timeframe,)
        self.style = style

    def plan_buy(self, data: dict[str, pd.DataFrame], quote: Quote | None) -> BuyPlan | None:
        df = data.get(self.timeframes[0], pd.DataFrame())
        n = len(df)
        if n < WARMUP_BARS + MAX_REINTEGRATION_BARS + 2:
            return None

        # 4. Confirmation : dernière bougie haussière, clôture dans son tiers supérieur
        last = df.iloc[-1]
        amplitude = float(last["high"] - last["low"])
        if not (last["close"] > last["open"] and amplitude > 0
                and float(last["close"] - last["low"]) >= CLOSE_IN_TOP * amplitude):
            return None
        confirm = n - 1

        # Balayage le plus récent compatible : réintégration <= 3 bougies, confirmation = réintégration ou suivante
        for sweep in range(confirm, max(confirm - MAX_REINTEGRATION_BARS - 1, WARMUP_BARS) - 1, -1):
            reading = detect_regime(df.iloc[:sweep])                 # contexte d'avant le balayage
            if reading.regime != MarketRegime.RANGE or reading.range_bounds is None or not reading.atr:
                continue
            low_bound, high_bound = reading.range_bounds
            width = high_bound - low_bound
            if float(df["low"].iloc[sweep]) >= low_bound:
                continue                                             # pas de balayage de la borne basse
            pivots = confirmed_pivots(df.iloc[:sweep])
            low_contacts = sum(p.kind == "low" and p.price <= low_bound + EDGE_ZONE * width for p in pivots)
            high_contacts = sum(p.kind == "high" and p.price >= high_bound - EDGE_ZONE * width for p in pivots)
            if low_contacts < MIN_CONTACTS or high_contacts < MIN_CONTACTS:
                continue
            closes = df["close"].iloc[sweep:min(sweep + MAX_REINTEGRATION_BARS, confirm) + 1]
            back = [i for i, close in enumerate(closes) if close > low_bound]
            if not back:
                continue
            reintegration = sweep + back[0]
            if not reintegration <= confirm <= reintegration + 1:
                continue
            return self._plan(df, sweep, confirm, reading, low_bound, high_bound, low_contacts, high_contacts, quote)
        return None

    def _plan(self, df: pd.DataFrame, sweep: int, confirm: int, reading, low_bound: float, high_bound: float,
              low_contacts: int, high_contacts: int, quote: Quote | None) -> BuyPlan | None:
        atr = reading.atr
        sweep_low = float(df["low"].iloc[sweep:confirm + 1].min())
        confirm_close = float(df["close"].iloc[confirm])
        spread = quote.spread if quote else 0.0
        margin = protection_margin(atr, spread, quote.tick_size if quote else 0.0)
        stop = sweep_low - margin
        take_profit = high_bound - margin
        entry = quote.entry(Side.BUY) if quote else confirm_close
        if not stop < entry < take_profit:
            return None
        ratio = net_reward_risk(Side.BUY, entry, stop, take_profit, spread)
        levels = (("borne_basse", low_bound), ("borne_haute", high_bound), ("extrême_balayage", sweep_low),
                  ("clôture_confirmation", confirm_close))
        indicators = (("atr", atr), ("marge_b", margin), ("adx", reading.adx or 0.0),
                      ("contacts_bas", float(low_contacts)), ("contacts_haut", float(high_contacts)))
        reason = (f"Range validé (ADX {reading.adx:.0f}, {low_contacts} contacts {{bas}} / {high_contacts} {{haut}}) ; "
                  f"balayage de la borne {{borne}}, réintégration en {confirm - sweep} bougie(s), "
                  "bougie {bougie} clôturant dans son tiers {tiers}")
        notes: list[str] = []
        status = SetupStatus.READY
        if quote is None:
            status = SetupStatus.WATCHING
            notes.append("prix exécutable indisponible : entrée estimée à la clôture de confirmation")
        elif abs(entry - confirm_close) > ENTRY_MAX_DRIFT_ATR * atr:
            status = SetupStatus.INVALIDATED
            notes.append(f"entrée refusée : prix à {abs(entry - confirm_close) / atr:.2f} ATR de la clôture de "
                         f"confirmation (maximum {ENTRY_MAX_DRIFT_ATR:g})")
        if ratio < MIN_NET_REWARD_RISK - 1e-6:                  # tolérance virgule flottante
            status = SetupStatus.INVALIDATED
            notes.append(f"rejet automatique : ratio net estimé {ratio:.2f} < {MIN_NET_REWARD_RISK:g}")
        return BuyPlan(status, entry, stop, take_profit, ratio, MarketRegime.RANGE, reason, tuple(notes), levels,
                       indicators)
