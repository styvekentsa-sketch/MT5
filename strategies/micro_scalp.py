"""
Stratégie E - Petits trades (micro-scalping) : gains courts sur M5 (ou M1) en session active, avec un
objectif serré (STRATEGY_SCALP_TP_R, 1.2 R) et un stop serré (STRATEGY_SCALP_SL_ATR, 1 ATR).
Achat ; la vente est le symétrique exact (série miroir, strategies/base.py).

Micro-tendance (M15, bougies clôturées) : EMA 20 au-dessus de l'EMA 50 et dernière clôture au-dessus de
l'EMA 20 ; on ne scalpe que dans le sens du courant.
Déclencheurs (timeframe court, dernière bougie CLÔTURÉE ; ATR(14) d'avant cette bougie) :
  - micro-cassure : les STRATEGY_SCALP_RANGE_BARS (12) bougies précédentes forment une micro-consolidation
    (amplitude <= STRATEGY_SCALP_MAX_RANGE_ATR, 2 ATR) ; la dernière clôture au-dessus de son plus haut
  - petit rebond : repli récent (3 bougies) jusqu'à l'EMA 20 montante du timeframe court, puis clôture
    au-dessus du plus haut de la bougie précédente et de l'EMA 20
  Dans les deux cas : bougie haussière, corps >= 0.5 ATR, clôture dans le tiers haut.
Plan : entrée au premier Ask (refus à plus de 0.20 ATR de la clôture), SL = entrée - SL_ATR x ATR,
TP au ratio net STRATEGY_SCALP_TP_R. Le vérificateur indépendant applique CE ratio minimal à cette
stratégie (les autres restent à RISK_MIN_NET_REWARD_RISK) ; tout le reste est identique : 1 % de
risque, risque cumulé, positions max, perte journalière, spread, annonces, mémoire des erreurs.
Session : Londres 07:00-10:00 / New York 12:30-16:00 UTC. Validité : une bougie du timeframe court.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from config.settings import settings
from core.regime_detector import atr as atr_series
from models.schemas import MarketRegime, SetupStatus, Side, TradingSetup, TradingStyle
from strategies.base import (
    ENTRY_MAX_DRIFT_ATR,
    TIMEFRAME_SECONDS,
    BaseStrategy,
    BuyPlan,
    Quote,
    net_reward_risk,
    target_for_ratio,
)
from strategies.momentum_breakout import OFF_SESSION, _sessions_text, liquidity_session
from utils.server_time import bar_time_to_utc

ATR_PERIOD = 14
MIN_BODY_ATR = 0.5
CLOSE_STRENGTH = 2 / 3
PULLBACK_BARS = 3
EMA_TOUCH_ATR = 0.10                 # le repli « touche » l'EMA 20 à 0.10 ATR près


def _ema(values: pd.Series, span: int) -> pd.Series:
    return values.astype(float).ewm(span=span, adjust=False).mean()


class MicroScalpStrategy(BaseStrategy):
    name = "petits_trades"
    style = TradingStyle.SCALPING
    bars = 200
    validity_bars = 1                # en bougies du timeframe COURT (cf. expiry)
    label = "Petits trades"
    order_tag = "PETIT"              # commentaire « Bot PETIT BUY » (distinct des modes SCALP / DAY / SWING)
    anchor = "heure_signal"          # une seule entrée par bougie de signal (moteur)

    def __init__(self, context_tf: str = "M15", trigger_tf: str | None = None) -> None:
        self.timeframes = (context_tf, trigger_tf or settings.strategy.scalp_timeframe)

    @property
    def min_net_reward_risk(self) -> float:
        """Ratio minimal propre à ce mode pour le vérificateur indépendant (objectif court)."""
        return settings.strategy.scalp_tp_r

    @property
    def role(self) -> str:
        cfg = settings.strategy
        return (f"micro-structures {self.timeframes[1]} (micro-cassures, petits rebonds sur l'EMA 20), "
                f"SL {cfg.scalp_sl_atr:g} ATR, TP {cfg.scalp_tp_r:g} R")

    def expiry(self, now: datetime) -> datetime:
        return now + timedelta(seconds=TIMEFRAME_SECONDS.get(self.timeframes[1], 300) * self.validity_bars)

    def find_setup(self, symbol: str, data: dict[str, pd.DataFrame], now: datetime,
                   quote: Quote | None = None) -> TradingSetup | None:
        setup = super().find_setup(symbol, data, now, quote)
        if setup is None or setup.status != SetupStatus.READY:
            return setup
        trig = data[self.timeframes[1]]
        decision = bar_time_to_utc(trig.index[-1] + timedelta(seconds=TIMEFRAME_SECONDS[self.timeframes[1]]),
                                   self.bar_timezone)
        if liquidity_session(decision) is not None:
            return setup
        note = f"{OFF_SESSION} : petit trade à {decision:%H:%M} UTC ({_sessions_text()})"
        return setup.model_copy(update={"status": SetupStatus.INVALIDATED, "notes": [*setup.notes, note]})

    def plan_buy(self, data: dict[str, pd.DataFrame], quote: Quote | None) -> BuyPlan | None:
        cfg = settings.strategy
        ctx = data.get(self.timeframes[0], pd.DataFrame())
        trig = data.get(self.timeframes[1], pd.DataFrame())
        if len(ctx) < 60 or len(trig) < max(ATR_PERIOD, cfg.scalp_range_bars) + 25 or quote is None:
            return None
        ctx_close = ctx["close"]
        fast, slow = _ema(ctx_close, 20).iloc[-1], _ema(ctx_close, 50).iloc[-1]
        if not (fast > slow and ctx_close.iloc[-1] > fast):
            return None                                       # pas de micro-tendance haussière M15

        o, h, low, c = (trig[col].to_numpy(dtype=float) for col in ("open", "high", "low", "close"))
        atr = float(atr_series(trig, ATR_PERIOD).to_numpy()[-2])
        if not np.isfinite(atr) or atr <= 0:
            return None
        rng = h[-1] - low[-1]
        if c[-1] <= o[-1] or (c[-1] - o[-1]) < MIN_BODY_ATR * atr or rng <= 0 or (c[-1] - low[-1]) / rng < CLOSE_STRENGTH:
            return None                                       # bougie de signal sans conviction

        window = slice(-cfg.scalp_range_bars - 1, -1)
        box_high, box_low = h[window].max(), low[window].min()
        ema_fast = _ema(trig["close"], 20).to_numpy()
        if box_high - box_low <= cfg.scalp_max_range_atr * atr and c[-1] > box_high:
            trigger = (f"micro-cassure {{haut}} d'une consolidation de {cfg.scalp_range_bars} bougies "
                       f"({(box_high - box_low) / atr:.1f} ATR)")
        elif (ema_fast[-1] > ema_fast[-6] and low[-PULLBACK_BARS - 1:-1].min() <= ema_fast[-2] + EMA_TOUCH_ATR * atr
              and c[-1] > h[-2] and c[-1] > ema_fast[-1]):
            trigger = "petit rebond sur l'EMA 20 montante"
        else:
            return None

        entry = quote.entry(Side.BUY)
        if abs(entry - c[-1]) > ENTRY_MAX_DRIFT_ATR * atr:
            return None                                       # trop tard pour un petit trade
        stop = entry - cfg.scalp_sl_atr * atr
        take_profit = target_for_ratio(Side.BUY, entry, stop, cfg.scalp_tp_r, quote.spread)
        reason = (f"Petit trade {{bougie}} {self.timeframes[1]} : {trigger}, micro-tendance {self.timeframes[0]} "
                  f"(EMA 20 {{au_dessus}} EMA 50) ; objectif court {cfg.scalp_tp_r:g} R")
        return BuyPlan(SetupStatus.READY, entry, stop, take_profit,
                       net_reward_risk(Side.BUY, entry, stop, take_profit, quote.spread), MarketRegime.TRANSITION,
                       reason, (), (("clôture_signal", float(c[-1])),),
                       (("atr", atr), ("heure_signal", float(trig.index[-1].timestamp()))))
