"""
Cerveau du bot : fusion de l'analyse technique et du sentiment des actualités.

Pipeline de `evaluate_market(symbol)` :
  1. bougies MT5 -> signal technique (score [-1, 1])            strategies.technical_analysis
  2. actualités récentes -> sentiment orienté pour le symbole    sentiment.sentiment_analyzer
  3. score hybride = poids_tech * technique + poids_news * sentiment   (settings.strategy)
  4. filtres : annonce à fort impact imminente, seuil de score,
     confirmation obligatoire par le signal technique
  5. SL (ATR ou dernier plus bas/haut) et TP (ratio risque/rendement de la config)

La fonction ne lève jamais d'exception : en cas de problème, elle retourne HOLD
avec la raison dans `reason`.

Usage :
    from strategies.strategy_manager import evaluate_market

    decision = evaluate_market("EURUSD")
    if decision["action"] != "HOLD":
        ...  # passer par risk_manager puis execute_order
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Any, Literal

from brokers.mt5_client import get_current_tick, get_historical_data
from config.settings import settings
from sentiment.news_scraper import NewsArticle, get_latest_news, is_high_impact_news_imminent
from sentiment.sentiment_analyzer import analyze_news_items, classify
from strategies.technical_analysis import TechnicalSignal, technical_signal
from utils.logger import get_logger

log = get_logger(__name__)

Action = Literal["BUY", "SELL", "HOLD"]


# =============================================================================
#  Constantes
# =============================================================================

SWING_BUFFER_ATR = 0.1               # marge sous le plus bas / au-dessus du plus haut, en ATR
MAX_SL_ATR = 3.0                     # au-delà, le SL structurel est jugé trop large -> SL ATR
PRICE_DECIMALS = 5                   # arrondi d'affichage ; l'ordre est arrondi aux `digits` du symbole

_news_lock = threading.Lock()
_news_cache: list[NewsArticle] = []
_news_fetched_at: float | None = None        # time.monotonic() du dernier téléchargement


# =============================================================================
#  Helpers internes
# =============================================================================

def get_recent_news() -> list[NewsArticle]:
    """
    Actualités récentes, partagées entre symboles et rafraîchies au plus toutes les
    `settings.news.refresh_interval_seconds`. Un échec conserve le cache précédent.
    """
    global _news_cache, _news_fetched_at
    with _news_lock:
        now = time.monotonic()
        if _news_fetched_at is not None and now - _news_fetched_at < settings.news.refresh_interval_seconds:
            return _news_cache
        articles = get_latest_news()
        if articles:
            _news_cache = articles
        _news_fetched_at = now
        return _news_cache


def _entry_price(symbol: str, action: Action, fallback: float | None) -> float | None:
    """Prix d'entrée estimé : Ask pour un achat, Bid pour une vente ; clôture à défaut."""
    try:
        tick = get_current_tick(symbol)
        if tick is not None:
            price = tick.ask if action == "BUY" else tick.bid
            if price > 0:
                return price
    except Exception as exc:  # noqa: BLE001
        log.warning("Tick %s indisponible (%s) : prix d'entrée estimé sur la dernière clôture", symbol, exc)
    return fallback


def compute_sl_tp(action: Action, entry: float | None, tech: TechnicalSignal) -> tuple[float | None, float | None]:
    """
    Stop Loss et Take Profit suggérés.

    SL : distance ATR (`settings.risk.sl_atr_multiplier`), élargie jusqu'au dernier
    plus bas (achat) / plus haut (vente) + marge si ce niveau reste à moins de
    MAX_SL_ATR ATR : on évite de placer le stop juste devant un niveau technique.

    TP : même distance multipliée par le ratio tp_atr_multiplier / sl_atr_multiplier,
    pour conserver le ratio risque/rendement de la configuration.

    Returns:
        (sl, tp), ou (None, None) si l'ATR ou le prix d'entrée manquent.
    """
    if action not in ("BUY", "SELL") or entry is None or not tech.atr or tech.atr <= 0:
        return None, None

    atr = tech.atr
    distance = atr * settings.risk.sl_atr_multiplier
    swing = tech.swing_low if action == "BUY" else tech.swing_high
    if swing is not None:
        structural = (entry - swing if action == "BUY" else swing - entry) + SWING_BUFFER_ATR * atr
        if distance < structural <= MAX_SL_ATR * atr:
            distance = structural

    reward_ratio = settings.risk.tp_atr_multiplier / settings.risk.sl_atr_multiplier
    sign = 1 if action == "BUY" else -1
    sl = entry - sign * distance
    tp = entry + sign * distance * reward_ratio
    return round(sl, PRICE_DECIMALS), round(tp, PRICE_DECIMALS)


def _decision(
    symbol: str,
    action: Action,
    reason: str,
    confidence: int = 0,
    sl: float | None = None,
    tp: float | None = None,
    entry: float | None = None,
    tech: TechnicalSignal | None = None,
    sentiment: float | None = None,
    hybrid: float | None = None,
    news_blocked: bool = False,
) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "action": action,
        "confidence": confidence,
        "suggested_sl": sl,
        "suggested_tp": tp,
        "reason": reason,
        # Détails pour les logs, la base de données et le futur dashboard
        "entry_price": entry,
        "technical_signal": tech.signal if tech else None,
        "technical_score": tech.score if tech else None,
        "sentiment_score": sentiment,
        "hybrid_score": hybrid,
        "news_blocked": news_blocked,
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


# =============================================================================
#  Évaluation du marché
# =============================================================================

def _evaluate(symbol: str) -> dict[str, Any]:
    # 1. Analyse technique
    try:
        df = get_historical_data(symbol, count=settings.trading.bars_history)
    except Exception as exc:  # noqa: BLE001 - MT5ConnectionError, symbole inconnu...
        log.error("[%s] Bougies indisponibles : %s", symbol, exc)
        return _decision(symbol, "HOLD", f"Données de marché indisponibles : {exc}")

    tech = technical_signal(df)
    if not tech.is_valid:
        return _decision(symbol, "HOLD", f"Analyse technique impossible : {'; '.join(tech.reasons)}", tech=tech)

    # 2. Fondamental : annonces imminentes + sentiment des actualités
    news_blocked = is_high_impact_news_imminent(symbol=symbol)
    articles = get_recent_news()
    sentiment = analyze_news_items(articles, symbol=symbol) if articles else 0.0

    # 3. Score hybride
    tech_weight = settings.strategy.technical_weight
    news_weight = settings.strategy.sentiment_weight
    threshold = settings.strategy.signal_threshold
    hybrid = round(max(-1.0, min(1.0, tech_weight * tech.score + news_weight * sentiment)), 4)
    confidence = int(round(abs(hybrid) * 100))
    direction: Action = "BUY" if hybrid > 0 else "SELL" if hybrid < 0 else "HOLD"

    reasons = [
        f"Technique {tech.signal} ({tech.score:+.2f}) : {'; '.join(tech.reasons)}",
        f"Sentiment {classify(sentiment)} ({sentiment:+.2f}"
        f"{', aucune actualité disponible' if not articles else ''})",
        f"Score hybride {hybrid:+.2f} ({tech_weight:.0%} technique / {news_weight:.0%} news, seuil {threshold:.2f})",
    ]

    # 4. Filtres
    action: Action = "HOLD"
    if news_blocked:
        reasons.append("Annonce à fort impact imminente : trade bloqué")
    elif abs(hybrid) < threshold:
        reasons.append("Score insuffisant : pas de trade")
    elif tech.signal != direction:
        reasons.append("Signal technique non confirmé : pas de trade sur le seul sentiment")
    else:
        action = direction

    # 5. Niveaux de sortie
    sl = tp = entry = None
    if action != "HOLD":
        entry = _entry_price(symbol, action, tech.close)
        sl, tp = compute_sl_tp(action, entry, tech)
        if sl is None:
            action = "HOLD"
            reasons.append("SL impossible à calculer (ATR ou prix manquant) : trade annulé")
        else:
            reasons.append(f"{action} @ ~{entry} | SL {sl} | TP {tp}")

    reason = " | ".join(reasons)
    log.info("[%s] %s (confiance %d%%) - %s", symbol, action, confidence, reason)
    return _decision(symbol, action, reason, confidence, sl, tp, entry, tech, sentiment, hybrid, news_blocked)


def evaluate_market(symbol: str) -> dict[str, Any]:
    """
    Évalue le marché pour un symbole et retourne la décision de trading.

    Returns:
        dict avec :
          - action        : "BUY", "SELL" ou "HOLD"
          - confidence    : 0-100, force du score hybride (|score| x 100)
          - suggested_sl  : Stop Loss suggéré (None si HOLD)
          - suggested_tp  : Take Profit suggéré (None si HOLD)
          - reason        : explication lisible de la décision
        ainsi que des détails : entry_price, technical_signal, technical_score,
        sentiment_score, hybrid_score, news_blocked, timestamp.
        Toute erreur produit un HOLD ; aucune exception n'est levée.
    """
    try:
        return _evaluate(symbol)
    except Exception as exc:  # noqa: BLE001
        log.exception("[%s] Erreur inattendue lors de l'évaluation du marché", symbol)
        return _decision(symbol, "HOLD", f"Erreur interne : {exc}")


if __name__ == "__main__":
    # `python -m strategies.strategy_manager` : évalue chaque symbole configuré (aucun ordre envoyé)
    from brokers.mt5_client import connect_mt5, shutdown_mt5

    if connect_mt5():
        try:
            for sym in settings.trading.symbols:
                evaluate_market(sym)
        finally:
            shutdown_mt5()
