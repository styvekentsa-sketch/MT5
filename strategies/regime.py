"""
Moteur adaptatif (STRATEGY_PROFILE=adaptive) : à chaque tour et pour chaque symbole, choix
du style de trading selon le contexte de marché, puis signal et objectifs propres à ce style.

Sélection du mode (par priorité, sur bougies clôturées) :
  1. SWING    tendance de fond : EMA20/EMA50 D1 et H1 dans le même sens, écartées d'au moins
              0.5 ATR (pas de « tendance » dans un range) et prix du bon côté,
              sentiment des actualités pas franchement opposé
  2. DAY      ouverture des sessions majeures (ADAPTIVE_DAY_WINDOWS, Londres / New York, UTC)
  3. SCALPING heures liquides (ADAPTIVE_LIQUID_HOURS) et range serré : ATR M5 <= 1.2 x sa médiane
  sinon     aucun mode : pas de trade (marché en expansion ou hors session)

Signaux et objectifs :
  SCALPING  M1, EMA 5/13, RSI 7, MACD 5/13/4 ; tout score technique non nul ; SL / TP fixes
            (STRATEGY_SL_PIPS / STRATEGY_TP_PIPS, 5 pips) ; jamais coupé manuellement
  DAY       croisement EMA9/EMA21 M15 récent dans le sens de la tendance H1, résistance /
            support (plus haut / bas de la veille et des 48 dernières heures) au-delà du TP ;
            SL 1.5 ATR M15 borné 10-20 pips, TP 2 x SL borné 15-30 pips ; fermé à ADAPTIVE_DAY_CLOSE
  SWING     signal technique H1 confirmant la tendance D1, RSI D1 non épuisé ; SL 2 ATR H1,
            TP 4 ATR H1 ; trailing stop à 2 ATR H1 dès 1 ATR de gain

Le mode est inscrit dans le commentaire de l'ordre (« Bot SWING BUY 64% ») pour que la gestion
des positions (trailing, fermeture de fin de session) le retrouve, y compris après un redémarrage.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Literal

import numpy as np
import pandas as pd
from ta.momentum import RSIIndicator
from ta.trend import EMAIndicator
from ta.volatility import AverageTrueRange

from brokers.base import BaseBroker
from brokers.models import Position, SymbolSpecs
from config.settings import settings
from sentiment.sentiment_analyzer import analyze_news_items
from strategies.strategy_manager import get_recent_news, pip_size
from strategies.technical_analysis import INDICATOR_PROFILES, compute_indicators, signal_from_indicators
from utils.logger import get_logger

log = get_logger(__name__)

Mode = Literal["scalping", "day", "swing"]
MODE_TAGS: dict[Mode, str] = {"scalping": "SCALP", "day": "DAY", "swing": "SWING"}
_TAG_PATTERN = re.compile(r"\b(SCALP|DAY|SWING)\b")

# Historique demandé par timeframe (bougie en cours comprise)
BARS = {"M1": 200, "M5": 150, "M15": 200, "H1": 200, "D1": 80}

SCALP_MAX_ATR_RATIO = 1.2           # ATR M5 / médiane 100 bougies : au-delà, marché en expansion
SWING_SENTIMENT_TOLERANCE = 0.2     # sentiment opposé à la tendance toléré jusqu'à -0.2
TREND_MIN_SEPARATION_ATR = 0.5      # écart EMA rapide / lente minimal (en ATR) pour parler de tendance
SWING_TECH_THRESHOLD = 0.3          # signal H1 confirmé (même seuil que le profil standard)
SWING_RSI_BUY = (40.0, 70.0)        # RSI D1 : tendance vivante mais pas épuisée
SWING_RSI_SELL = (30.0, 60.0)
SWING_SL_ATR, SWING_TP_ATR = 2.0, 4.0
TRAIL_ATR, TRAIL_TRIGGER_ATR = 2.0, 1.0
TRAIL_MIN_STEP_ATR = 0.1            # on ne déplace le SL que d'au moins 0.1 ATR (pas de micro-modifications)
DAY_CROSS_LOOKBACK = 3              # croisement EMA9/21 dans les 3 dernières bougies M15
DAY_SL_PIPS, DAY_TP_PIPS = (10.0, 20.0), (15.0, 30.0)
DAY_SR_LOOKBACK_H1 = 48
DAY_LAST_ENTRY_MARGIN = 60          # plus d'entrée day trading dans l'heure qui précède la fermeture


# =============================================================================
#  Données multi-timeframes
# =============================================================================

@dataclass(frozen=True)
class MarketContext:
    symbol: str
    now: datetime
    frames: dict[str, pd.DataFrame]   # bougies clôturées par timeframe
    sentiment: float
    specs: SymbolSpecs | None

    def frame(self, timeframe: str) -> pd.DataFrame:
        return self.frames.get(timeframe, pd.DataFrame())


def _closed(df: pd.DataFrame | None) -> pd.DataFrame:
    """Bougies clôturées (la dernière, en cours, est retirée), colonnes OHLC numériques."""
    if df is None or df.empty or not {"open", "high", "low", "close"} <= set(df.columns):
        return pd.DataFrame()
    return df[["open", "high", "low", "close"]].apply(pd.to_numeric, errors="coerce").dropna().iloc[:-1]


def load_context(broker: BaseBroker, symbol: str, now: datetime | None = None) -> MarketContext:
    """Bougies M1, M5, M15, H1, D1 du symbole, sentiment des actualités et spécifications."""
    frames = {tf: _closed(broker.get_historical_data(symbol, count=count, timeframe=tf)) for tf, count in BARS.items()}
    articles = get_recent_news()
    sentiment = analyze_news_items(articles, symbol=symbol) if articles else 0.0
    return MarketContext(symbol, now or datetime.now(timezone.utc), frames, sentiment, broker.get_symbol_specs(symbol))


# =============================================================================
#  Indicateurs de contexte
# =============================================================================

def _ema(close: pd.Series, window: int) -> pd.Series:
    return EMAIndicator(close, window=window).ema_indicator()


def _atr(df: pd.DataFrame, window: int = 14) -> pd.Series:
    return AverageTrueRange(df["high"], df["low"], df["close"], window=window).average_true_range().replace(0.0, np.nan)


def trend(df: pd.DataFrame, fast: int = 20, slow: int = 50) -> int:
    """
    +1 tendance haussière (EMA rapide > lente d'au moins TREND_MIN_SEPARATION_ATR ATR, prix
    au-dessus), -1 baissière, 0 sinon (range, EMA collées) ou données insuffisantes.
    """
    if len(df) < slow + 2:
        return 0
    close = df["close"]
    f, s, c = _ema(close, fast).iloc[-1], _ema(close, slow).iloc[-1], close.iloc[-1]
    atr = _atr(df).iloc[-1]
    if not np.isfinite(atr) or abs(f - s) < TREND_MIN_SEPARATION_ATR * atr:
        return 0
    if f > s and c > f:
        return 1
    if f < s and c < f:
        return -1
    return 0


def _minute(now: datetime) -> int:
    return now.hour * 60 + now.minute


def _hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _in_window(minute: int, window: tuple[int, int]) -> bool:
    return window[0] <= minute < window[1]


# =============================================================================
#  Sélection du mode
# =============================================================================

def select_mode(ctx: MarketContext) -> tuple[Mode | None, str]:
    """Mode de trading pour ce symbole maintenant, et la raison du choix."""
    cfg = settings.adaptive
    d1_trend, h1_trend = trend(ctx.frame("D1")), trend(ctx.frame("H1"))
    if d1_trend and h1_trend == d1_trend and ctx.sentiment * d1_trend > -SWING_SENTIMENT_TOLERANCE:
        direction = "haussière" if d1_trend > 0 else "baissière"
        return "swing", f"tendance de fond {direction} D1 + H1 alignées (sentiment {ctx.sentiment:+.2f})"

    minute = _minute(ctx.now)
    for window in cfg.day_windows:
        if _in_window(minute, window) and minute < cfg.day_close - DAY_LAST_ENTRY_MARGIN:
            return "day", f"ouverture de session {_hhmm(window[0])}-{_hhmm(window[1])} UTC"

    if not _in_window(minute, cfg.liquid_hours):
        hours = f"{_hhmm(cfg.liquid_hours[0])}-{_hhmm(cfg.liquid_hours[1])} UTC"
        return None, f"hors des heures liquides ({hours}) et sans tendance de fond D1/H1 : aucun mode actif"
    m5 = ctx.frame("M5")
    if len(m5) < 40:
        return None, "historique M5 insuffisant pour mesurer la volatilité"
    atr = _atr(m5).dropna()
    ratio = float(atr.iloc[-1] / atr.tail(100).median()) if len(atr) else float("nan")
    if not np.isfinite(ratio):
        return None, "volatilité M5 incalculable"
    if ratio <= SCALP_MAX_ATR_RATIO:
        return "scalping", f"session liquide, range serré (ATR M5 = {ratio:.2f} x sa médiane)"
    return None, f"marché en expansion (ATR M5 = {ratio:.2f} x sa médiane > {SCALP_MAX_ATR_RATIO}) : scalping suspendu"


# =============================================================================
#  Signaux et niveaux par mode
# =============================================================================

@dataclass(frozen=True)
class ModeSignal:
    action: Literal["BUY", "SELL", "HOLD"]
    verdict: str
    sl_distance: float = 0.0          # en prix
    tp_distance: float = 0.0
    technical_score: float | None = None


def _hold(verdict: str, score: float | None = None) -> ModeSignal:
    return ModeSignal("HOLD", verdict, technical_score=score)


def _scalping_signal(ctx: MarketContext, pip: float) -> ModeSignal:
    m1 = ctx.frame("M1")
    if len(m1) < 30 or pip <= 0:
        return _hold("scalping : historique M1 ou taille de pip indisponible")
    params = INDICATOR_PROFILES["scalping"]
    tech = signal_from_indicators(compute_indicators(m1, params), params=params, threshold=0.0)
    if tech.signal not in ("BUY", "SELL"):
        return _hold(f"scalping : aucun micro-signal M1 (score {tech.score:+.2f})", tech.score)
    sl = settings.strategy.sl_pips or 5.0
    tp = settings.strategy.tp_pips or sl
    return ModeSignal(tech.signal, f"scalping : micro-signal {tech.signal} M1 ({tech.score:+.2f}, "
                                   f"{'; '.join(tech.reasons[:2])}) · SL {sl:g} / TP {tp:g} pips",
                      sl * pip, tp * pip, tech.score)


def _day_signal(ctx: MarketContext, pip: float) -> ModeSignal:
    m15, h1, d1 = ctx.frame("M15"), ctx.frame("H1"), ctx.frame("D1")
    if len(m15) < 40 or len(h1) < 60 or pip <= 0:
        return _hold("day trading : historique M15 / H1 insuffisant")
    fast, slow = _ema(m15["close"], 9), _ema(m15["close"], 21)
    diff = (fast - slow).dropna()
    direction = 0
    for i in range(1, DAY_CROSS_LOOKBACK + 1):
        if len(diff) > i and np.sign(diff.iloc[-i]) != np.sign(diff.iloc[-i - 1]) and diff.iloc[-i] != 0:
            direction = int(np.sign(diff.iloc[-1]))
            break
    if direction == 0:
        return _hold("day trading : aucun croisement EMA9/EMA21 M15 récent")
    h1_trend = trend(h1)
    if h1_trend != direction:
        label = "haussier" if direction > 0 else "baissier"
        return _hold(f"day trading : croisement M15 {label} contre la tendance H1 ({h1_trend:+d})")

    atr = float(_atr(m15).iloc[-1])
    sl_pips = float(np.clip(1.5 * atr / pip, *DAY_SL_PIPS))
    tp_pips = float(np.clip(2 * sl_pips, *DAY_TP_PIPS))
    close = float(m15["close"].iloc[-1])
    recent = h1.tail(DAY_SR_LOOKBACK_H1)
    previous_day = d1.iloc[-1] if len(d1) else None
    if direction > 0:
        levels = [lvl for lvl in (float(recent["high"].max()), previous_day["high"] if previous_day is not None else None)
                  if lvl is not None and lvl > close]
        obstacle = min(levels) if levels else None
    else:
        levels = [lvl for lvl in (float(recent["low"].min()), previous_day["low"] if previous_day is not None else None)
                  if lvl is not None and lvl < close]
        obstacle = max(levels) if levels else None
    side = "BUY" if direction > 0 else "SELL"
    if obstacle is not None and abs(obstacle - close) < tp_pips * pip:
        kind = "résistance" if direction > 0 else "support"
        return _hold(f"day trading : {kind} {obstacle:.5g} à {abs(obstacle - close) / pip:.0f} pips, "
                     f"en deçà du TP de {tp_pips:.0f} pips")
    return ModeSignal(side, f"day trading : croisement EMA9/EMA21 M15 {side} dans la tendance H1 · "
                            f"SL {sl_pips:.0f} / TP {tp_pips:.0f} pips", sl_pips * pip, tp_pips * pip)


def _swing_signal(ctx: MarketContext) -> ModeSignal:
    h1, d1 = ctx.frame("H1"), ctx.frame("D1")
    direction = trend(d1)
    if not direction or len(h1) < 60:
        return _hold("swing : tendance D1 ou historique H1 insuffisant")
    standard = INDICATOR_PROFILES["standard"]
    tech = signal_from_indicators(compute_indicators(h1, standard), params=standard, threshold=SWING_TECH_THRESHOLD)
    side = "BUY" if direction > 0 else "SELL"
    if tech.signal != side:
        return _hold(f"swing : tendance D1 {side} mais signal H1 {tech.signal} ({tech.score:+.2f}) : "
                     "attente d'une reprise", tech.score)
    rsi = float(RSIIndicator(d1["close"], window=14).rsi().iloc[-1])
    low, high = SWING_RSI_BUY if side == "BUY" else SWING_RSI_SELL
    if not low <= rsi <= high:
        return _hold(f"swing : RSI D1 {rsi:.0f} hors zone {low:.0f}-{high:.0f} (tendance épuisée)", tech.score)
    atr = float(_atr(h1).iloc[-1])
    return ModeSignal(side, f"swing : tendance D1 {side} confirmée en H1 ({tech.score:+.2f}), RSI D1 {rsi:.0f}, "
                            f"sentiment {ctx.sentiment:+.2f} · SL {SWING_SL_ATR:g} / TP {SWING_TP_ATR:g} ATR H1, trailing",
                      SWING_SL_ATR * atr, SWING_TP_ATR * atr, tech.score)


def evaluate_adaptive(symbol: str, broker: BaseBroker, now: datetime | None = None) -> dict[str, Any]:
    """
    Décision du moteur adaptatif, au même format que `evaluate_market` (+ `mode`, `order_comment`).
    Toute erreur produit un HOLD ; aucune exception n'est levée.
    """
    try:
        ctx = load_context(broker, symbol, now)
        mode, why = select_mode(ctx)
        if mode is None:
            return _result(symbol, None, _hold(f"Aucun mode actif : {why}"), ctx, broker, why)
        pip = pip_size(ctx.specs)
        signal = {"scalping": lambda: _scalping_signal(ctx, pip), "day": lambda: _day_signal(ctx, pip),
                  "swing": lambda: _swing_signal(ctx)}[mode]()
        return _result(symbol, mode, signal, ctx, broker, why)
    except Exception as exc:  # noqa: BLE001 - une erreur de données ne doit jamais arrêter le moteur
        log.exception("[%s] Erreur du moteur adaptatif", symbol)
        return {"symbol": symbol, "action": "HOLD", "confidence": 0, "reason": f"Erreur interne : {exc}",
                "verdict": f"Erreur interne : {exc}", "mode": None}


def _result(symbol: str, mode: Mode | None, signal: ModeSignal, ctx: MarketContext, broker: BaseBroker,
            why: str) -> dict[str, Any]:
    tag = f"[{MODE_TAGS[mode]}] " if mode else ""
    signal = replace(signal, verdict=re.sub(r"^(scalping|day trading|swing) : ", "", signal.verdict))
    base = {
        "symbol": symbol, "action": signal.action, "mode": mode,
        "direction": signal.action if signal.action != "HOLD" else None,
        "confidence": int(round(abs(signal.technical_score or 0) * 100)),
        "technical_score": signal.technical_score, "sentiment_score": round(ctx.sentiment, 4),
        "hybrid_score": signal.technical_score, "suggested_sl": None, "suggested_tp": None, "entry_price": None,
        "verdict": f"{tag}{signal.verdict}", "reason": f"Mode {mode or 'aucun'} ({why}) | {signal.verdict}",
        "timestamp": ctx.now.isoformat(timespec="seconds"),
    }
    if signal.action == "HOLD":
        log.info("[%s] %sHOLD - %s", symbol, tag, signal.verdict)
        return base
    tick = broker.get_current_tick(symbol)
    if tick is None:
        return {**base, "action": "HOLD", "direction": None, "verdict": f"{tag}prix indisponible : trade annulé"}
    entry = tick.ask if signal.action == "BUY" else tick.bid
    spread = max(0.0, tick.ask - tick.bid)
    sl_distance = max(signal.sl_distance, spread * settings.strategy.min_sl_spreads)
    tp_distance = signal.tp_distance * sl_distance / signal.sl_distance if signal.sl_distance else signal.tp_distance
    sign = 1 if signal.action == "BUY" else -1
    digits = ctx.specs.digits if ctx.specs else 5
    log.info("[%s] %s%s @ %s - %s", symbol, tag, signal.action, entry, signal.verdict)
    return {**base, "entry_price": entry,
            "suggested_sl": round(entry - sign * sl_distance, digits),
            "suggested_tp": round(entry + sign * tp_distance, digits),
            "order_comment": f"Bot {MODE_TAGS[mode]} {signal.action} {base['confidence']}%"}


# =============================================================================
#  Gestion des positions ouvertes
# =============================================================================

@dataclass(frozen=True)
class ManagementAction:
    symbol: str
    ticket: int
    mode: Mode
    kind: Literal["trailing", "day_close"]
    description: str


def position_mode(position: Position, known: dict[int, Mode] | None = None) -> Mode | None:
    """Mode d'une position : mémoire du moteur, sinon le commentaire de l'ordre (« Bot SWING BUY »)."""
    if known and position.ticket in known:
        return known[position.ticket]
    match = _TAG_PATTERN.search(position.comment or "")
    return {tag: mode for mode, tag in MODE_TAGS.items()}.get(match.group(1)) if match else None


def manage_positions(broker: BaseBroker, now: datetime | None = None,
                     known: dict[int, Mode] | None = None) -> list[ManagementAction]:
    """
    Gestion active selon le mode de chaque position du bot :
      - SCALPING : rien (le SL / TP fait le travail, pas de coupure sur le bruit de marché)
      - DAY      : fermeture à ADAPTIVE_DAY_CLOSE (UTC), aucun portage de nuit
      - SWING    : trailing stop à TRAIL_ATR ATR H1 dès TRAIL_TRIGGER_ATR ATR de gain
    """
    now = now or datetime.now(timezone.utc)
    actions: list[ManagementAction] = []
    for position in broker.get_positions(bot_only=True):
        mode = position_mode(position, known)
        try:
            if mode == "day" and _minute(now) >= settings.adaptive.day_close:
                result = broker.close_order(position.ticket, comment="Bot DAY close")
                state = "fermée" if result.success else f"échec ({result.message})"
                actions.append(ManagementAction(position.symbol, position.ticket, mode, "day_close",
                                                f"[DAY] Fin de session {_hhmm(settings.adaptive.day_close)} UTC : "
                                                f"position #{position.ticket} {state} (P/L {position.profit:+.2f})"))
            elif mode == "swing":
                action = _trail(broker, position)
                if action:
                    actions.append(action)
        except Exception:  # noqa: BLE001 - une position en erreur n'empêche pas de gérer les autres
            log.exception("[%s] Gestion de la position #%d impossible", position.symbol, position.ticket)
    return actions


def trailing_stop(side: str, price: float, entry: float, current_sl: float, atr: float) -> float | None:
    """Nouveau SL du trailing stop, ou None s'il ne doit pas bouger (gain insuffisant, SL déjà plus serré)."""
    if atr <= 0:
        return None
    if side == "BUY":
        if price - entry < TRAIL_TRIGGER_ATR * atr:
            return None
        candidate = price - TRAIL_ATR * atr
        return candidate if candidate > (current_sl or 0.0) + TRAIL_MIN_STEP_ATR * atr else None
    if entry - price < TRAIL_TRIGGER_ATR * atr:
        return None
    candidate = price + TRAIL_ATR * atr
    return candidate if (not current_sl or candidate < current_sl - TRAIL_MIN_STEP_ATR * atr) else None


def _trail(broker: BaseBroker, position: Position) -> ManagementAction | None:
    h1 = _closed(broker.get_historical_data(position.symbol, count=BARS["H1"], timeframe="H1"))
    if len(h1) < 20:
        return None
    atr = float(_atr(h1).iloc[-1])
    new_sl = trailing_stop(position.side, position.price_current, position.price_open, position.sl, atr)
    if new_sl is None:
        return None
    result = broker.modify_stops(position.ticket, new_sl, position.tp or None)
    state = "déplacé" if result.success else f"non déplacé ({result.message})"
    return ManagementAction(position.symbol, position.ticket, "swing", "trailing",
                            f"[SWING] Trailing stop #{position.ticket} {state} : SL {position.sl:.5g} -> {new_sl:.5g} "
                            f"(prix {position.price_current:.5g}, {TRAIL_ATR:g} ATR H1)")
