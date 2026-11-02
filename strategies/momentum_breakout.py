"""
Stratégie C - Breakout Momentum (achat ; la vente est le symétrique exact, calculé sur la série
miroir par strategies/base.py).

Contexte (timeframe de contexte, H1 par défaut, bougies clôturées) :
  - détecteur de régime causal : ni choc de volatilité, ni tendance opposée à la cassure
Niveau clé et cassure (timeframe de déclenchement, M15 par défaut) :
  1. niveau clé = plus haut des 40 bougies précédant la bougie de cassure (sommet du range récent :
     tout pivot confirmé de la fenêtre est à ce niveau ou en dessous)
  2. bougie d'impulsion haussière clôturant au-dessus du niveau d'au moins 0.10 ATR, corps
     >= 1.2 x ATR(14) (ATR de Wilder disponible AVANT la bougie de cassure)
Micro-respiration :
  3. jamais d'entrée sur la bougie de cassure : on attend 1 à 3 bougies de correction ou de
     consolidation ; jusqu'à 2 bougies prolongeant l'impulsion avant la respiration en font partie
  4. aucune clôture sous le niveau clé (le niveau cassé devient support), sinon : invalidé
  5. relance = bougie haussière clôturant au-dessus du plus haut de la bougie précédente ;
     plus de 3 bougies de respiration sans relance : setup annulé
  6. relance PUISSANTE (facultatif) : corps >= r x ATR(5), ATR(5) = moyenne des vraies amplitudes
     des 5 bougies précédant la relance (r = STRATEGY_MOMENTUM_RESTART_BODY_ATR ; 0, le défaut,
     désactive le filtre : à 0.8 / 1.0, il écartait en backtest les relances moyennes des trades
     gagnants) ; une relance trop faible invalide le setup
Plan (relance sur la DERNIÈRE bougie clôturée, sinon le signal est trop ancien) :
  - entrée   : premier Ask après la relance, refusé s'il s'écarte de plus de 0.20 ATR de la
               clôture de relance
  - stop     : plus bas de la zone de respiration - m x b, avec b = max(0.10 ATR, 2 spreads, 1 tick)
               et m le multiplicateur de l'actif (STRATEGY_MARGIN_MULTIPLIERS : 1.0 pour le forex
               majeur, 1.5 pour l'or, dont les mèches M15 fauchent un stop trop serré)
  - objectif : TP assurant le ratio net STRATEGY_MOMENTUM_TARGET_RR (3 par défaut ; gain net / perte
               prévue, coût = spread), calculé sur le stop élargi : un stop plus large repousse le TP
  - gestion  : breakeven puis trailing stop facultatifs (risk/trailing.py, STRATEGY_MOMENTUM_BREAKEVEN_R,
               STRATEGY_MOMENTUM_TRAILING_*), appliqués par le backtest (backtesting/setup_engine.py)
Validité : une bougie de déclenchement (momentum : le signal se périme vite).
Sessions de liquidité (UTC réel) : Londres 07:00-10:00, New York 12:30-16:00. Un setup est rejeté
(« Hors session de liquidité ») si l'heure de décision (clôture de la dernière bougie de
déclenchement) OU l'ouverture de la bougie de cassure est hors de ces fenêtres (fin de séance
asiatique, heures creuses, entre deux sessions), AVANT toute taille de position : aucun ordre,
aucune alerte de setup prêt. Les heures sont lues sur les bougies et converties depuis l'heure du
serveur MT5 (utils/server_time.py) : même jugement en direct et en backtest.

Plusieurs cassures récentes (ex : la relance est elle-même une grande bougie au-dessus du range) :
chacune est analysée et le meilleur statut est retenu (prêt > surveillance > invalidé > expiré).
"""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone

import numpy as np
import pandas as pd

from config.settings import settings
from core.regime_detector import WARMUP_BARS, RegimeReading, detect_regime
from core.regime_detector import atr as atr_series
from models.schemas import MarketRegime, SetupStatus, Side, TradingSetup, TradingStyle
from strategies.base import (
    ENTRY_MAX_DRIFT_ATR,
    TIMEFRAME_SECONDS,
    BaseStrategy,
    BuyPlan,
    Quote,
    TrailingStop,
    net_reward_risk,
    protection_margin,
    target_for_ratio,
)
from utils.server_time import bar_time_to_utc

LEVEL_LOOKBACK = 40                  # bougies définissant le sommet du range avant la cassure
BREAK_BODY_ATR = 1.2                 # corps minimal de la bougie d'impulsion
BREAK_CLOSE_ATR = 0.10               # clôture « nettement » au-dessus du niveau
MAX_RESPIRATION = 3                  # bougies de respiration au-delà desquelles le setup est annulé
MAX_EXTENSION = 2                    # bougies prolongeant l'impulsion avant la respiration
SEARCH_BARS = 1 + MAX_EXTENSION + MAX_RESPIRATION + 1      # cassure, extension, respiration, relance
ATR_PERIOD = 14
RESTART_ATR_BARS = 5                 # ATR(5) de référence de la bougie de relance
BLOCKING_REGIMES = (MarketRegime.SHOCK, MarketRegime.TREND_DOWN, MarketRegime.UNKNOWN)
_RANK = {SetupStatus.READY: 0, SetupStatus.WATCHING: 1, SetupStatus.INVALIDATED: 2, SetupStatus.EXPIRED: 3}
# Fenêtres de forte liquidité institutionnelle, en UTC : [début, fin)
LIQUIDITY_SESSIONS = (("Londres", time(7, 0), time(10, 0)), ("New York", time(12, 30), time(16, 0)))
OFF_SESSION = "Hors session de liquidité"


def liquidity_session(at: datetime) -> str | None:
    """Nom de la session de liquidité contenant `at` (converti en UTC), sinon None."""
    if at.tzinfo is not None:
        at = at.astimezone(timezone.utc)
    clock = at.time()
    return next((name for name, start, end in LIQUIDITY_SESSIONS if start <= clock < end), None)


def _sessions_text() -> str:
    return ", ".join(f"{name} {start:%H:%M}-{end:%H:%M}" for name, start, end in LIQUIDITY_SESSIONS) + " UTC"


class MomentumBreakoutStrategy(BaseStrategy):
    name = "momentum_breakout"
    style = TradingStyle.DAY
    bars = 400
    validity_bars = 1                # en bougies du timeframe de DÉCLENCHEMENT (cf. expiry)

    def __init__(self, context_tf: str = "H1", trigger_tf: str = "M15", style: TradingStyle = TradingStyle.DAY) -> None:
        self.timeframes = (context_tf, trigger_tf)
        self.style = style
        self.margin_factor = 1.0                     # multiplicateur de b de l'actif en cours d'analyse

    @property
    def trailing(self) -> TrailingStop | None:
        cfg = settings.strategy
        trailing = TrailingStop(breakeven_r=cfg.momentum_breakeven_r, activation_r=cfg.momentum_trailing_activation_r,
                                mode=cfg.momentum_trailing_mode, distance_r=cfg.momentum_trailing_distance_r,
                                atr_multiple=cfg.momentum_trailing_atr, bars=cfg.momentum_trailing_bars)
        return trailing if trailing.enabled else None

    def find_setup(self, symbol: str, data: dict[str, pd.DataFrame], now: datetime,
                   quote: Quote | None = None) -> TradingSetup | None:
        self.margin_factor = settings.strategy.margin_multiplier(symbol)
        setup = super().find_setup(symbol, data, now, quote)
        if setup is None or setup.status not in (SetupStatus.READY, SetupStatus.WATCHING):
            return setup
        trig = data[self.timeframes[1]]
        decision = trig.index[-1] + timedelta(seconds=TIMEFRAME_SECONDS.get(self.timeframes[1], 900))
        breakout = setup.indicators.get("heure_cassure")
        reason = self.off_session_reason(
            bar_time_to_utc(decision, self.bar_timezone),
            None if breakout is None else bar_time_to_utc(datetime.fromtimestamp(breakout, tz=timezone.utc),
                                                          self.bar_timezone))
        if reason is None:
            return setup
        return setup.model_copy(update={"status": SetupStatus.INVALIDATED, "notes": [*setup.notes, reason]})

    @staticmethod
    def off_session_reason(decision: datetime, breakout: datetime | None) -> str | None:
        """Motif de rejet si la décision ou la bougie de cassure (heures UTC réelles) est hors session."""
        if liquidity_session(decision) is None:
            return f"{OFF_SESSION} : décision à {decision:%H:%M} UTC ({_sessions_text()})"
        if breakout is not None and liquidity_session(breakout) is None:
            return f"{OFF_SESSION} : cassure formée à {breakout:%H:%M} UTC ({_sessions_text()})"
        return None

    def expiry(self, now: datetime) -> datetime:
        return now + timedelta(seconds=TIMEFRAME_SECONDS.get(self.timeframes[1], 900) * self.validity_bars)

    def plan_buy(self, data: dict[str, pd.DataFrame], quote: Quote | None) -> BuyPlan | None:
        ctx = data.get(self.timeframes[0], pd.DataFrame())
        trig = data.get(self.timeframes[1], pd.DataFrame())
        if len(ctx) < WARMUP_BARS or len(trig) < LEVEL_LOOKBACK + ATR_PERIOD + SEARCH_BARS:
            return None
        reading = detect_regime(ctx)
        if reading.regime in BLOCKING_REGIMES:
            return None
        bars = {c: trig[c].to_numpy(dtype=float) for c in ("open", "high", "low", "close")}
        bars["time"] = (trig.index.asi8 // 10**9).astype(float)          # ouverture des bougies, s UTC
        atrs = atr_series(trig, ATR_PERIOD).to_numpy()
        plans, restarts = [], set()
        for j, level, atr in breakouts(bars, atrs):
            if j in restarts:
                continue                    # relance puissante d'une cassure précédente : pas une nouvelle cassure
            plan = self._follow(bars, j, level, atr, quote, reading, restarts)
            if plan is not None:
                plans.append(plan)
        # meilleur statut ; à statut égal, la cassure la plus récente (dernière de la liste)
        return min(reversed(plans), key=lambda p: _RANK[p.status], default=None)

    def _follow(self, bars: dict[str, np.ndarray], j: int, level: float, atr: float, quote: Quote | None,
                reading: RegimeReading, restarts: set[int]) -> BuyPlan | None:
        """
        Suit la cassure `j` : extension, respiration, relance ; plan correspondant (ou None).
        La position de la bougie de relance est ajoutée à `restarts`.
        """
        opens, highs, lows, closes = bars["open"], bars["high"], bars["low"], bars["close"]
        n = len(closes)

        def plan(status: SetupStatus, entry: float, zone_low: float, reason: str, notes: tuple[str, ...] = (),
                 extra: tuple[tuple[str, float], ...] = (),
                 measures: tuple[tuple[str, float], ...] = ()) -> BuyPlan | None:
            spread = quote.spread if quote else 0.0
            margin = protection_margin(atr, spread, quote.tick_size if quote else 0.0) * self.margin_factor
            stop = zone_low - margin
            if entry <= stop:
                return None                                      # le prix est déjà sous le stop
            take_profit = target_for_ratio(Side.BUY, entry, stop, settings.strategy.momentum_target_rr, spread)
            context = (f"Cassure {{bougie}} du {{sommet}} des {LEVEL_LOOKBACK} bougies (clôture {{au_dessus}} de "
                       f"{BREAK_CLOSE_ATR:g} ATR, corps {(closes[j] - opens[j]) / atr:.1f} ATR) ; contexte "
                       f"{self.timeframes[0]} compatible ({reading.regime.value}, ADX {reading.adx or 0:.0f})")
            return BuyPlan(status, entry, stop, take_profit,
                           net_reward_risk(Side.BUY, entry, stop, take_profit, spread), reading.regime,
                           f"{context} ; {reason}", notes, (("niveau_cassé", level), *extra),
                           (("atr", atr), ("marge_b", margin), ("multiplicateur_b", self.margin_factor),
                            ("heure_cassure", bars["time"][j]), ("adx", reading.adx or 0.0),
                            ("bougies_depuis_cassure", float(n - 1 - j)), *measures))

        impulse_end, respiration, trigger = j, [], None
        for i in range(j + 1, n):
            if closes[i] < level:
                return plan(SetupStatus.INVALIDATED, closes[i], float(lows[j]), "retour sous le niveau cassé",
                            notes=("clôture {sous} le niveau cassé pendant la respiration : cassure annulée",))
            if respiration and closes[i] > opens[i] and closes[i] > highs[i - 1]:
                trigger = i
                break
            if not respiration and closes[i] > highs[impulse_end]:
                if i - j > MAX_EXTENSION:
                    return plan(SetupStatus.EXPIRED, closes[i], float(lows[impulse_end]),
                                "impulsion prolongée sans respiration : on ne chasse pas le prix")
                impulse_end = i                                  # l'impulsion se prolonge
                continue
            respiration.append(i)
            if len(respiration) > MAX_RESPIRATION:
                return plan(SetupStatus.EXPIRED, closes[i], float(lows[respiration].min()),
                            f"micro-respiration de plus de {MAX_RESPIRATION} bougies sans relance : setup annulé")

        zone_low = float(lows[respiration].min()) if respiration else float(lows[impulse_end])
        extra = (("sommet_impulsion", float(highs[impulse_end])), ("creux_respiration", zone_low))
        if not respiration:
            return plan(SetupStatus.WATCHING, float(highs[impulse_end]), zone_low,
                        "attente de la micro-respiration (pas d'entrée sur la bougie d'impulsion)", extra=extra)
        if trigger is None:
            return plan(SetupStatus.WATCHING, float(highs[n - 1]), zone_low,
                        f"micro-respiration en cours ({len(respiration)}/{MAX_RESPIRATION} bougies), "
                        "attente de la relance", extra=extra)
        restarts.add(trigger)
        restart_close = float(closes[trigger])
        extra += (("clôture_relance", restart_close),)
        body, atr5 = closes[trigger] - opens[trigger], mean_true_range(bars, trigger, RESTART_ATR_BARS)
        minimum = settings.strategy.momentum_restart_body_atr          # 0 : filtre désactivé
        strength = (("corps_relance_atr5", body / atr5 if atr5 > 0 else 0.0), ("atr5", atr5))
        if minimum > 0 and (atr5 <= 0 or body < minimum * atr5):
            return plan(SetupStatus.INVALIDATED, restart_close, zone_low, "relance trop faible", extra=extra,
                        measures=strength,
                        notes=(f"relance sans conviction : corps {body / atr5 if atr5 > 0 else 0:.2f} x ATR("
                               f"{RESTART_ATR_BARS}) < {minimum:g} (faux breakout mou probable)",))
        if trigger < n - 1:
            return plan(SetupStatus.EXPIRED, restart_close, zone_low,
                        "relance trop ancienne (avant la dernière bougie clôturée)", extra=extra)
        if quote is None:
            return plan(SetupStatus.WATCHING, restart_close, zone_low, "relance confirmée, prix exécutable indisponible",
                        extra=extra)
        entry = quote.entry(Side.BUY)
        if abs(entry - restart_close) > ENTRY_MAX_DRIFT_ATR * atr:
            return plan(SetupStatus.INVALIDATED, entry, zone_low, "relance confirmée", extra=extra,
                        notes=(f"entrée refusée : prix à {abs(entry - restart_close) / atr:.2f} ATR de la "
                               f"clôture de relance (maximum {ENTRY_MAX_DRIFT_ATR:g})",))
        force = "puissante" if minimum > 0 else "confirmée"
        return plan(SetupStatus.READY, entry, zone_low,
                    f"relance {{bougie}} {force} (corps {strength[0][1]:.2f} x ATR({RESTART_ATR_BARS})) après "
                    f"{len(respiration)} bougie(s) de respiration, niveau cassé tenu comme support", extra=extra,
                    measures=strength)


def mean_true_range(bars: dict[str, np.ndarray], position: int, count: int) -> float:
    """Moyenne simple des vraies amplitudes des `count` bougies AVANT `position` (connue avant la bougie)."""
    start = max(1, position - count)
    if position - start < count:
        return 0.0
    highs, lows = bars["high"][start:position], bars["low"][start:position]
    prev_closes = bars["close"][start - 1:position - 1]
    true_range = np.maximum(highs - lows, np.maximum(np.abs(highs - prev_closes), np.abs(lows - prev_closes)))
    return float(true_range.mean())


def breakouts(bars: dict[str, np.ndarray], atrs: np.ndarray) -> list[tuple[int, float, float]]:
    """Cassures des SEARCH_BARS dernières bougies : (position, niveau clé, ATR avant la bougie), de la plus ancienne."""
    opens, highs, closes = bars["open"], bars["high"], bars["close"]
    n = len(closes)
    found = []
    for j in range(max(LEVEL_LOOKBACK + 1, n - SEARCH_BARS), n):
        atr = atrs[j - 1]
        if not np.isfinite(atr) or atr <= 0:
            continue
        level = float(highs[j - LEVEL_LOOKBACK:j].max())
        if closes[j] - opens[j] >= BREAK_BODY_ATR * atr and closes[j] >= level + BREAK_CLOSE_ATR * atr:
            found.append((j, level, float(atr)))
    return found
