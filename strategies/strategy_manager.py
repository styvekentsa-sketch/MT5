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
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

from brokers.base import BaseBroker
from brokers.factory import get_broker
from brokers.models import SymbolSpecs
from config.settings import settings
from sentiment.news_scraper import NewsArticle, get_latest_news, is_high_impact_news_imminent
from sentiment.sentiment_analyzer import analyze_news_items, article_insights, classify
from strategies.technical_analysis import TechnicalSignal, technical_signal
from utils.database import get_database
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
_news_fetched_wall: datetime | None = None   # même instant, en heure UTC (affichage)


# =============================================================================
#  Helpers internes
# =============================================================================

def get_recent_news() -> list[NewsArticle]:
    """
    Actualités récentes, partagées entre symboles et rafraîchies au plus toutes les
    `settings.news.refresh_interval_seconds`. Un échec conserve le cache précédent.
    """
    global _news_cache, _news_fetched_at, _news_fetched_wall
    with _news_lock:
        now = time.monotonic()
        if _news_fetched_at is not None and now - _news_fetched_at < settings.news.refresh_interval_seconds:
            return _news_cache
        articles = get_latest_news()
        if articles:
            _news_cache = articles
            _persist_articles(articles)
        _news_fetched_at = now
        _news_fetched_wall = datetime.now(timezone.utc)
        return _news_cache


def _persist_articles(articles: list[NewsArticle]) -> None:
    """Historise les articles téléchargés avec leur analyse (table market_sentiment)."""
    try:
        symbols = settings.trading.symbols
        get_database().upsert_articles(
            {"link": a.link, "title": a.title, "source": a.source, "published_at": a.published,
             "snippet": a.snippet, **article_insights(a.title, a.snippet, symbols)}
            for a in articles
        )
    except Exception:  # noqa: BLE001 - l'historique ne doit jamais bloquer la stratégie
        log.exception("Historisation des articles impossible")


def get_news_fetched_at() -> datetime | None:
    """Heure UTC du dernier téléchargement des flux (None si jamais téléchargés)."""
    return _news_fetched_wall


def _entry_price(broker: BaseBroker, symbol: str, action: Action,
                 fallback: float | None) -> tuple[float | None, float]:
    """(prix d'entrée estimé, spread) : Ask pour un achat, Bid pour une vente ; clôture et 0 à défaut."""
    try:
        tick = broker.get_current_tick(symbol)
        if tick is not None:
            price = tick.ask if action == "BUY" else tick.bid
            if price > 0:
                return price, max(0.0, tick.ask - tick.bid)
    except Exception as exc:  # noqa: BLE001
        log.warning("Tick %s indisponible (%s) : prix d'entrée estimé sur la dernière clôture", symbol, exc)
    return fallback, 0.0


def pip_size(specs: SymbolSpecs | None) -> float:
    """
    Taille d'un pip : 10 points pour les cotations à 2, 3 ou 5 décimales (EURUSD 0.0001,
    USDJPY 0.01, XAUUSD 0.1), 1 point sinon ; pas de prix pour un contrat crypto.
    """
    if specs is None:
        return 0.0
    if specs.market == "crypto":
        return specs.tick_size
    return specs.point * 10 if specs.digits in (2, 3, 5) else specs.point


def compute_sl_tp(action: Action, entry: float | None, tech: TechnicalSignal,
                  spread: float = 0.0, pip: float = 0.0) -> tuple[float | None, float | None]:
    """
    Stop Loss et Take Profit suggérés.

    SL : distance ATR (`settings.risk.sl_atr_multiplier`), élargie jusqu'au dernier
    plus bas (achat) / plus haut (vente) + marge si ce niveau reste à moins de
    MAX_SL_ATR ATR : on évite de placer le stop juste devant un niveau technique.

    TP : même distance multipliée par le ratio tp_atr_multiplier / sl_atr_multiplier,
    pour conserver le ratio risque/rendement de la configuration.

    Scalping (`settings.strategy`) : pas d'élargissement structurel, et SL d'au moins
    `min_sl_spreads` fois le `spread` actuel (sinon le coût d'entrée absorbe le gain visé).

    SL / TP fixes (`sl_pips` / `tp_pips` > 0, profil micro) : distances en pips (`pip` = taille
    d'un pip du symbole), à la place de l'ATR ; la distance minimale en spreads s'applique encore.

    Returns:
        (sl, tp), ou (None, None) si l'ATR ou le prix d'entrée manquent.
    """
    if action not in ("BUY", "SELL") or entry is None:
        return None, None
    strategy = settings.strategy
    if strategy.sl_pips > 0 and pip > 0:
        distance = max(strategy.sl_pips * pip, spread * strategy.min_sl_spreads)
        sign = 1 if action == "BUY" else -1
        tp_distance = distance * strategy.tp_pips / strategy.sl_pips
        return round(entry - sign * distance, PRICE_DECIMALS), round(entry + sign * tp_distance, PRICE_DECIMALS)
    if not tech.atr or tech.atr <= 0:
        return None, None

    atr = tech.atr
    distance = atr * settings.risk.sl_atr_multiplier
    swing = tech.swing_low if action == "BUY" else tech.swing_high
    if swing is not None and settings.strategy.structural_sl:
        structural = (entry - swing if action == "BUY" else swing - entry) + SWING_BUFFER_ATR * atr
        if distance < structural <= MAX_SL_ATR * atr:
            distance = structural

    distance = max(distance, spread * settings.strategy.min_sl_spreads)

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
    verdict: str = "",
    direction: Action | None = None,
) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "action": action,
        # Direction envisagée par le score hybride, même si le trade est refusé (journal des décisions)
        "direction": direction if direction in ("BUY", "SELL") else (action if action != "HOLD" else None),
        # Motif principal, court et chiffré (journal des décisions) ; `reason` garde l'explication complète
        "verdict": verdict or reason,
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

@dataclass(frozen=True)
class HybridDecision:
    action: Action                   # BUY / SELL si toutes les conditions sont réunies, sinon HOLD
    hybrid: float                    # score combiné dans [-1, 1]
    confidence: int                  # |hybrid| x 100
    reasons: tuple[str, ...]
    direction: Action = "HOLD"       # sens suggéré par le score hybride, avant les filtres
    verdict: str = ""                # motif principal de la décision, chiffré


def decide(tech: TechnicalSignal, sentiment: float, news_blocked: bool, has_news: bool = True) -> HybridDecision:
    """
    Cœur pur de la décision (sans accès au marché), partagé par le moteur et le backtest :
    score hybride = poids technique x technique + poids news x sentiment, puis filtres
    (annonce imminente, seuil, confirmation obligatoire par le signal technique).
    """
    tech_weight = settings.strategy.technical_weight
    news_weight = settings.strategy.sentiment_weight
    threshold = settings.strategy.signal_threshold
    hybrid = round(max(-1.0, min(1.0, tech_weight * tech.score + news_weight * sentiment)), 4)
    confidence = int(round(abs(hybrid) * 100))
    direction: Action = "BUY" if hybrid > 0 else "SELL" if hybrid < 0 else "HOLD"

    reasons = [
        f"Technique {tech.signal} ({tech.score:+.2f}) : {'; '.join(tech.reasons)}",
        f"Sentiment {classify(sentiment)} ({sentiment:+.2f}"
        f"{', aucune actualité disponible' if not has_news else ''})",
        f"Score hybride {hybrid:+.2f} ({tech_weight:.0%} technique / {news_weight:.0%} news, seuil {threshold:.2f})",
    ]

    action: Action = "HOLD"
    scores = f"technique {tech.score:+.2f}, sentiment {sentiment:+.2f}"
    if news_blocked:
        reasons.append("Annonce à fort impact imminente : trade bloqué")
        verdict = "Annonce économique à fort impact imminente : trade bloqué"
    elif hybrid == 0 or abs(hybrid) < threshold:
        reasons.append("Score insuffisant : pas de trade")
        verdict = f"Score hybride {hybrid:+.2f} sous le seuil de ±{threshold:.2f} ({scores})"
    elif tech.signal != direction:
        reasons.append("Signal technique non confirmé : pas de trade sur le seul sentiment")
        verdict = (f"Signal technique {tech.signal} ({tech.score:+.2f}) : ne confirme pas le "
                   f"{'BUY' if direction == 'BUY' else 'SELL'} suggéré par le score hybride {hybrid:+.2f}")
    else:
        action = direction
        verdict = f"Signal {direction} confirmé : score hybride {hybrid:+.2f} au-delà du seuil de ±{threshold:.2f} ({scores})"
    return HybridDecision(action, hybrid, confidence, tuple(reasons), direction, verdict)


def _evaluate(symbol: str, broker: BaseBroker) -> dict[str, Any]:
    # 1. Analyse technique
    try:
        df = broker.get_historical_data(symbol, count=settings.trading.bars_history)
    except Exception as exc:  # noqa: BLE001 - broker déconnecté, symbole inconnu...
        log.error("[%s] Bougies indisponibles : %s", symbol, exc)
        return _decision(symbol, "HOLD", f"Données de marché indisponibles : {exc}")

    tech = technical_signal(df)
    if not tech.is_valid:
        return _decision(symbol, "HOLD", f"Analyse technique impossible : {'; '.join(tech.reasons)}", tech=tech)

    # 2. Fondamental : annonces imminentes + sentiment des actualités
    news_blocked = is_high_impact_news_imminent(symbol=symbol)
    articles = get_recent_news()
    sentiment = analyze_news_items(articles, symbol=symbol) if articles else 0.0

    # 3-4. Score hybride et filtres (logique partagée avec le backtest)
    hybrid_decision = decide(tech, sentiment, news_blocked, has_news=bool(articles))
    action, hybrid, confidence = hybrid_decision.action, hybrid_decision.hybrid, hybrid_decision.confidence
    reasons = list(hybrid_decision.reasons)
    verdict = hybrid_decision.verdict

    # 5. Niveaux de sortie
    sl = tp = entry = None
    if action != "HOLD":
        entry, spread = _entry_price(broker, symbol, action, tech.close)
        pip = pip_size(broker.get_symbol_specs(symbol)) if settings.strategy.sl_pips > 0 else 0.0
        sl, tp = compute_sl_tp(action, entry, tech, spread, pip)
        if sl is None:
            action = "HOLD"
            reasons.append("SL impossible à calculer (ATR ou prix manquant) : trade annulé")
            verdict = "Stop Loss impossible à calculer (ATR ou prix manquant) : trade annulé"
        else:
            reasons.append(f"{action} @ ~{entry} | SL {sl} | TP {tp}")

    reason = " | ".join(reasons)
    log.info("[%s] %s (confiance %d%%) - %s", symbol, action, confidence, reason)
    return _decision(symbol, action, reason, confidence, sl, tp, entry, tech, sentiment, hybrid, news_blocked,
                     verdict=verdict, direction=hybrid_decision.direction)


def evaluate_market(symbol: str, broker: BaseBroker | None = None) -> dict[str, Any]:
    """
    Évalue le marché pour un symbole et retourne la décision de trading.
    `broker` : source des bougies et des prix (par défaut le broker du processus).

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
        return _evaluate(symbol, broker or get_broker())
    except Exception as exc:  # noqa: BLE001
        log.exception("[%s] Erreur inattendue lors de l'évaluation du marché", symbol)
        return _decision(symbol, "HOLD", f"Erreur interne : {exc}")


if __name__ == "__main__":
    # `python -m strategies.strategy_manager` : évalue chaque symbole configuré (aucun ordre envoyé)
    current = get_broker()
    if current.connect():
        try:
            for sym in settings.trading.symbols:
                evaluate_market(sym, current)
        finally:
            current.disconnect()
