"""
Stratégie D - Impulsion (rattrapage de tendance) : entrer dans un mouvement directionnel violent
et propre qui se déroule maintenant, sans attendre un setup « de manuel ». Achat ; la vente est
le symétrique exact, calculée sur la série miroir par strategies/base.py.

Détection (timeframe de déclenchement, M15 par défaut ; bougies CLÔTURÉES) :
  1. les IMPULSE_BARS (3) dernières bougies sont haussières, chacune avec un corps >= IMPULSE_MIN_BODY_ATR
     (0.4) ATR et une mèche haute <= IMPULSE_MAX_WICK_PCT (35 %) de son amplitude : pas de rejet majeur
  2. accélération : mouvement total (clôture finale - ouverture de la 1re bougie) >= IMPULSE_MIN_MOVE_ATR
     (1.5) ATR, la dernière bougie clôturant dans le tiers haut de son amplitude
  3. pas d'épuisement : mouvement <= IMPULSE_MAX_MOVE_ATR (4) ATR (au-delà, on achèterait le sommet)
  L'ATR de référence est l'ATR(14) de Wilder AVANT l'impulsion : l'accélération se mesure par
  rapport à la volatilité normale du marché.
Contexte : H1 pas en tendance baissière confirmée (on ne rattrape pas un contre-mouvement) ;
session de liquidité Londres 07:00-10:00 / New York 12:30-16:00 UTC (IMPULSE_SESSION_ONLY).
Plan (impulsion terminée sur la DERNIÈRE bougie clôturée) :
  - entrée : premier Ask, refusé s'il s'écarte de plus de 0.30 ATR de la dernière clôture
  - stop   : entrée - IMPULSE_SL_ATR (1.5) x ATR récent (ATR(14) à la dernière bougie, impulsion comprise)
  - TP     : ratio net RISK_MIN_NET_REWARD_RISK (3) : le vérificateur indépendant l'accepte
Le risque reste celui de toutes les stratégies : 1 % par trade, risque cumulé, perte journalière,
vérificateur indépendant, fenêtre d'annonces, mémoire des erreurs (strategies/base.py).
"""

from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from config.settings import settings
from core.regime_detector import WARMUP_BARS, detect_regime
from core.regime_detector import atr as atr_series
from models.schemas import MarketRegime, SetupStatus, Side, TradingSetup, TradingStyle
from strategies.base import TIMEFRAME_SECONDS, BaseStrategy, BuyPlan, Quote, net_reward_risk, target_for_ratio
from strategies.momentum_breakout import OFF_SESSION, _sessions_text, liquidity_session
from utils.server_time import bar_time_to_utc

ATR_PERIOD = 14
ENTRY_MAX_DRIFT_ATR = 0.30           # l'impulsion continue : un peu plus de latitude que les setups
CLOSE_STRENGTH = 2 / 3               # dernière clôture dans le tiers haut de sa bougie


class ImpulseStrategy(BaseStrategy):
    name = "impulsion"
    style = TradingStyle.DAY
    bars = 300
    validity_bars = 1                # en bougies du timeframe de DÉCLENCHEMENT (cf. expiry)
    label = "Impulsion"
    order_tag = "IMPULSE"
    anchor = "départ_impulsion"      # une seule entrée par impulsion (moteur)

    def __init__(self, context_tf: str = "H1", trigger_tf: str | None = None,
                 style: TradingStyle = TradingStyle.DAY) -> None:
        self.timeframes = (context_tf, trigger_tf or settings.strategy.impulse_timeframe)
        self.style = style

    @property
    def role(self) -> str:
        cfg = settings.strategy
        return (f"grandes impulsions {self.timeframes[1]} ({cfg.impulse_bars} bougies fortes, {cfg.impulse_min_move_atr:g}-"
                f"{cfg.impulse_max_move_atr:g} ATR), SL {cfg.impulse_sl_atr:g} ATR, TP {settings.risk.min_net_reward_risk:g} R")

    def expiry(self, now: datetime) -> datetime:
        return now + timedelta(seconds=TIMEFRAME_SECONDS.get(self.timeframes[1], 900) * self.validity_bars)

    def find_setup(self, symbol: str, data: dict[str, pd.DataFrame], now: datetime,
                   quote: Quote | None = None) -> TradingSetup | None:
        setup = super().find_setup(symbol, data, now, quote)
        if setup is None or setup.status != SetupStatus.READY or not settings.strategy.impulse_session_only:
            return setup
        trig = data[self.timeframes[1]]
        decision = bar_time_to_utc(trig.index[-1] + timedelta(seconds=TIMEFRAME_SECONDS[self.timeframes[1]]),
                                   self.bar_timezone)
        if liquidity_session(decision) is not None:
            return setup
        note = f"{OFF_SESSION} : impulsion à {decision:%H:%M} UTC ({_sessions_text()})"
        return setup.model_copy(update={"status": SetupStatus.INVALIDATED, "notes": [*setup.notes, note]})

    def plan_buy(self, data: dict[str, pd.DataFrame], quote: Quote | None) -> BuyPlan | None:
        cfg = settings.strategy
        ctx = data.get(self.timeframes[0], pd.DataFrame())
        trig = data.get(self.timeframes[1], pd.DataFrame())
        k = cfg.impulse_bars
        if len(ctx) < WARMUP_BARS or len(trig) < ATR_PERIOD + k + 2:
            return None
        reading = detect_regime(ctx)
        if reading.regime == MarketRegime.TREND_DOWN:
            return None                                       # pas de rattrapage contre la tendance H1
        measure = impulse(trig, k)
        if measure is None:
            return None
        move_atr, body_min_atr, wick_max, base_atr, recent_atr = measure
        if move_atr < cfg.impulse_min_move_atr or move_atr > cfg.impulse_max_move_atr:
            return None
        if body_min_atr < cfg.impulse_min_body_atr or wick_max > cfg.impulse_max_wick_pct / 100:
            return None
        last_close = float(trig["close"].iloc[-1])
        if quote is None:
            return None
        entry = quote.entry(Side.BUY)
        if abs(entry - last_close) > ENTRY_MAX_DRIFT_ATR * base_atr:
            return None                                       # le prix est déjà parti : trop tard
        stop = entry - cfg.impulse_sl_atr * recent_atr
        take_profit = target_for_ratio(Side.BUY, entry, stop, settings.risk.min_net_reward_risk, quote.spread)
        reason = (f"Impulsion {{bougie}} : {k} bougies fortes sans rejet (corps >= {body_min_atr:.2f} ATR, mèche "
                  f"opposée <= {wick_max:.0%}), mouvement {move_atr:.1f} ATR ; contexte H1 {reading.regime.value}")
        return BuyPlan(SetupStatus.READY, entry, stop, take_profit,
                       net_reward_risk(Side.BUY, entry, stop, take_profit, quote.spread),
                       reading.regime if reading.regime != MarketRegime.UNKNOWN else MarketRegime.TRANSITION,
                       reason, (), (("départ_impulsion", float(trig["open"].iloc[-k])), ("clôture_impulsion", last_close)),
                       (("atr", base_atr), ("atr_récent", recent_atr), ("mouvement_atr", move_atr),
                        ("corps_min_atr", body_min_atr), ("mèche_max", wick_max)))


def impulse(trig: pd.DataFrame, bars: int) -> tuple[float, float, float, float, float] | None:
    """
    Mesure d'une impulsion HAUSSIÈRE sur les `bars` dernières bougies clôturées : (mouvement en ATR,
    plus petit corps en ATR, plus grande mèche haute en part d'amplitude, ATR avant l'impulsion,
    ATR récent) ; None si une bougie n'est pas haussière, ne clôture pas dans le tiers haut, ou si
    l'ATR est indisponible.
    """
    o, h, low, c = (trig[col].to_numpy(dtype=float)[-bars:] for col in ("open", "high", "low", "close"))
    atrs = atr_series(trig, ATR_PERIOD).to_numpy()
    base_atr, recent_atr = atrs[-bars - 1], atrs[-1]
    if not (np.isfinite(base_atr) and np.isfinite(recent_atr)) or base_atr <= 0:
        return None
    ranges = h - low
    if np.any(c <= o) or np.any(ranges <= 0):
        return None
    if (c[-1] - low[-1]) / ranges[-1] < CLOSE_STRENGTH:
        return None                                           # dernière bougie rejetée
    bodies = (c - o) / base_atr
    wicks = (h - c) / ranges
    return float((c[-1] - o[0]) / base_atr), float(bodies.min()), float(wicks.max()), float(base_atr), float(recent_atr)
