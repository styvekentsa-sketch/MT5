"""
Analyse de sentiment des actualités financières récupérées par `news_scraper`.

Moteurs disponibles (`settings.sentiment.engine`) :
  - "vader"    : VADER enrichi d'un lexique financier (rapide, sans GPU) — défaut
  - "keywords" : score par mots-clés financiers, sans aucune dépendance
  - "finbert"  : modèle ProsusAI/finbert (nécessite `transformers` + `torch`)
  - "llm"      : non implémenté pour l'instant, repli sur VADER

Si un moteur ne peut pas être chargé, on retombe sur le suivant
(finbert -> vader -> keywords) : le moteur "keywords" ne peut pas échouer.

Tous les scores sont normalisés dans [-1.0, +1.0] ; toute erreur donne 0.0 (neutre).

Orientation par symbole : pour une paire BASE/QUOTE, une nouvelle positive sur la
devise de cotation est baissière pour la paire (ex : « Dollar surges » => EURUSD
baissier). `analyze_news_items(items, symbol="EURUSD")` applique cette inversion.

Usage :
    from sentiment.news_scraper import get_latest_news
    from sentiment.sentiment_analyzer import analyze_text, analyze_news_items

    analyze_text("Gold soars to a record high")   # {'score': ..., 'label': 'bullish', 'engine': 'vader'}
    analyze_news_items(get_latest_news(), symbol="EURUSD")  # -> float dans [-1, 1]
"""

from __future__ import annotations

import math
import re
import threading
from datetime import datetime, timezone
from typing import Any, Iterable, Iterator, Protocol

from config.settings import settings
from utils.logger import get_logger

log = get_logger(__name__)


# =============================================================================
#  Constantes
# =============================================================================

BULLISH_THRESHOLD = 0.15             # score >= seuil  -> "bullish"
BEARISH_THRESHOLD = -0.15            # score <= seuil  -> "bearish"

TITLE_WEIGHT = 0.6                   # pondération titre / extrait dans un article
NEWS_HALF_LIFE_HOURS = 3.0           # un article de 3h pèse moitié moins qu'un article frais
UNDATED_ARTICLE_WEIGHT = 0.5
PAIR_MENTION_BOOST = 1.5             # l'article cite explicitement la paire (ex : "EUR/USD")
MAX_TEXT_LENGTH = 2000               # au-delà, le texte est tronqué avant analyse

NORMALIZATION_ALPHA = 15             # même normalisation que VADER : x / sqrt(x² + alpha)
NEGATION_WINDOW = 3                  # nombre de mots précédents inspectés pour une négation
NEGATION_SCALAR = -0.74              # facteur appliqué à un terme nié (valeur VADER)
NEGATIONS = {"not", "no", "never", "without", "hardly", "fails", "failed", "fail", "nor"}

# Lexique financier, échelle VADER (-4 .. +4).
# Verbes : déclinés automatiquement (surge -> surges, surged, surging).
FINANCIAL_VERBS: dict[str, float] = {
    "surge": 2.5, "soar": 2.8, "rally": 2.2, "rebound": 1.8, "gain": 1.5, "jump": 2.0,
    "climb": 1.5, "rise": 1.2, "advance": 1.2, "beat": 1.8, "outperform": 2.0,
    "upgrade": 2.0, "boost": 1.8, "strengthen": 1.6, "recover": 1.8, "accelerate": 1.2,
    "plunge": -2.8, "plummet": -3.0, "slump": -2.5, "tumble": -2.5, "crash": -3.2,
    "sink": -2.0, "drop": -1.5, "fall": -1.5, "decline": -1.5, "slide": -1.5, "slip": -1.0,
    "miss": -1.8, "underperform": -2.0, "downgrade": -2.0, "weaken": -1.6, "retreat": -1.2,
    "collapse": -3.0, "stall": -1.2, "shrink": -1.5,
}
# Noms, adjectifs et formes irrégulières (pris tels quels).
FINANCIAL_TERMS: dict[str, float] = {
    "rose": 1.2, "sank": -2.0, "fell": -1.5,
    "bullish": 2.5, "bearish": -2.5, "hawkish": 1.0, "dovish": -1.0,
    "strong": 1.5, "stronger": 1.5, "robust": 1.5, "resilient": 1.5, "solid": 1.2,
    "growth": 1.5, "expansion": 1.2, "recovery": 1.8, "optimism": 2.0, "upbeat": 1.8,
    "weak": -1.5, "weaker": -1.5, "sluggish": -1.5, "recession": -2.8, "contraction": -1.8,
    "slowdown": -1.8, "default": -2.5, "layoffs": -2.0, "crisis": -2.8, "turmoil": -2.5,
    "selloff": -2.5, "losses": -1.8, "fears": -1.8, "uncertainty": -1.2, "volatility": -0.8,
    "tariffs": -1.2, "war": -2.0, "bankruptcy": -3.0, "stagflation": -2.5,
    # Expressions (les tirets sont convertis en espaces avant l'analyse)
    "sell off": -2.5, "record high": 2.0, "all time high": 2.0, "multi year high": 1.5,
    "record low": -2.0, "multi year low": -1.5, "better than expected": 2.0,
    "worse than expected": -2.0, "above expectations": 1.8, "below expectations": -1.8,
    "beats expectations": 2.0, "misses expectations": -2.0, "risk on": 1.5, "risk off": -1.5,
    # Orientés devise : une hausse de taux soutient la monnaie concernée
    "safe haven demand": -0.5, "rate hike": 0.8, "rate hikes": 0.8, "rate cut": -0.8, "rate cuts": -0.8,
}

# Mots-clés (fragments regex, texte en minuscules) identifiant chaque actif dans un article.
ASSET_KEYWORDS: dict[str, tuple[str, ...]] = {
    "USD": (r"(?<!canadian )(?<!australian )(?<!zealand )(?<!kong )(?<!singapore )dollar",
            "greenback", "fed", "federal reserve", "fomc", "powell", r"u\.s\.", "us economy",
            "treasury", "treasuries", "nonfarm", "payrolls", "usd"),
    "EUR": ("euro", "euros", "eurozone", "euro zone", "euro area", "ecb", "lagarde", "eur"),
    "GBP": ("pound", "sterling", "boe", "bank of england", "gbp", "uk economy", "britain"),
    "JPY": ("yen", "boj", "bank of japan", "jpy", "japan"),
    "CHF": ("swiss franc", "franc", "snb", "chf"),
    "CAD": ("loonie", "canadian dollar", "bank of canada", "boc", "cad"),
    "AUD": ("aussie", "australian dollar", "rba", "aud"),
    "NZD": ("kiwi", "new zealand dollar", "rbnz", "nzd"),
    "XAU": ("gold", "bullion", "xau"),
    "XAG": ("silver", "xag"),
    "OIL": ("oil", "crude", "brent", "wti", "opec"),
    "US30": ("dow", "dow jones", "wall street", "us stocks"),
    "US500": (r"s&p", r"s&p 500", "wall street", "us stocks"),
    "NAS100": ("nasdaq", "tech stocks", "wall street"),
    "GER40": ("dax", "german stocks"),
    "UK100": ("ftse",),
    "JP225": ("nikkei",),
}

# Préfixes de symboles broker -> code d'actif (indices, métaux, énergie)
SYMBOL_ALIASES: dict[str, str] = {
    "XAU": "XAU", "GOLD": "XAU", "XAG": "XAG", "SILVER": "XAG",
    "WTI": "OIL", "BRENT": "OIL", "USOIL": "OIL", "UKOIL": "OIL",
    "US30": "US30", "DJ30": "US30", "DJI": "US30",
    "US500": "US500", "SPX": "US500", "SP500": "US500",
    "NAS100": "NAS100", "US100": "NAS100", "USTEC": "NAS100", "NDX": "NAS100",
    "GER40": "GER40", "GER30": "GER40", "DE40": "GER40", "DAX": "GER40",
    "UK100": "UK100", "FTSE": "UK100", "JP225": "JP225", "NIKKEI": "JP225",
}

_TOKEN_RE = re.compile(r"[a-z']+")


def _inflect(verb: str) -> set[str]:
    """Formes usuelles d'un verbe anglais (approximatives, les formes fausses sont inoffensives)."""
    if verb.endswith("e"):
        return {verb, verb + "s", verb + "d", verb[:-1] + "ing"}
    if verb.endswith("y") and verb[-2] not in "aeiou":
        return {verb, verb[:-1] + "ies", verb[:-1] + "ied", verb + "ing"}
    forms = {verb, verb + ("es" if verb.endswith(("s", "sh", "ch")) else "s"), verb + "ed", verb + "ing"}
    if len(verb) <= 4 and verb[-1] not in "aeiouwy" and verb[-2] in "aeiou":   # drop -> dropped
        forms |= {verb + verb[-1] + "ed", verb + verb[-1] + "ing"}
    return forms


def _build_lexicon() -> dict[tuple[str, ...], float]:
    lexicon: dict[tuple[str, ...], float] = {}
    for verb, value in FINANCIAL_VERBS.items():
        for form in _inflect(verb):
            lexicon[(form,)] = value
    for term, value in FINANCIAL_TERMS.items():
        lexicon[tuple(term.split())] = value
    return lexicon


LEXICON = _build_lexicon()
MAX_PHRASE_LENGTH = max(len(k) for k in LEXICON)

_ASSET_PATTERNS = {
    asset: re.compile(r"(?<![a-z0-9])(?:" + "|".join(keywords) + r")(?![a-z0-9])")
    for asset, keywords in ASSET_KEYWORDS.items()
}


# =============================================================================
#  Moteurs
# =============================================================================

class SentimentEngine(Protocol):
    name: str

    def score(self, text: str) -> float:
        """Score de sentiment dans [-1, 1]."""


def _normalize(raw: float) -> float:
    return raw / math.sqrt(raw * raw + NORMALIZATION_ALPHA) if raw else 0.0


def _tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower().replace("-", " "))


def _lexicon_matches(tokens: list[str]) -> Iterator[tuple[int, tuple[str, ...], float]]:
    """(position, termes, valeur) de chaque entrée du lexique, expression la plus longue d'abord."""
    i = 0
    while i < len(tokens):
        for length in range(min(MAX_PHRASE_LENGTH, len(tokens) - i), 0, -1):
            key = tuple(tokens[i:i + length])
            value = LEXICON.get(key)
            if value is not None:
                yield i, key, value
                i += length
                break
        else:
            i += 1


class KeywordEngine:
    """Somme des termes du lexique financier, avec gestion simple de la négation."""

    name = "keywords"

    def score(self, text: str) -> float:
        tokens = _tokenize(text)
        total = 0.0
        for i, _key, value in _lexicon_matches(tokens):
            window = tokens[max(0, i - NEGATION_WINDOW):i]
            if any(t in NEGATIONS or t.endswith("n't") for t in window):
                value *= NEGATION_SCALAR
            total += value
        return _normalize(total)


class VaderEngine:
    """VADER (lexique généraliste) enrichi des termes financiers à un mot."""

    name = "vader"

    def __init__(self) -> None:
        from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

        self._analyzer = SentimentIntensityAnalyzer()
        self._analyzer.lexicon.update({k[0]: v for k, v in LEXICON.items() if len(k) == 1})

    def score(self, text: str) -> float:
        return float(self._analyzer.polarity_scores(text)["compound"])


class FinBertEngine:
    """Modèle ProsusAI/finbert : score = P(positive) - P(negative)."""

    name = "finbert"
    MODEL = "ProsusAI/finbert"

    def __init__(self) -> None:
        from transformers import pipeline

        self._pipe = pipeline("text-classification", model=self.MODEL, top_k=None, truncation=True)

    def score(self, text: str) -> float:
        result = self._pipe(text)
        if result and isinstance(result[0], list):   # le format varie selon la version
            result = result[0]
        probs = {r["label"].lower(): float(r["score"]) for r in result}
        return probs.get("positive", 0.0) - probs.get("negative", 0.0)


_ENGINE_CHAIN: dict[str, tuple[type, ...]] = {
    "finbert": (FinBertEngine, VaderEngine, KeywordEngine),
    "llm": (VaderEngine, KeywordEngine),
    "vader": (VaderEngine, KeywordEngine),
    "keywords": (KeywordEngine,),
}

_engine_lock = threading.Lock()
_engine: SentimentEngine | None = None


def get_engine() -> SentimentEngine:
    """
    Moteur actif (chargé une seule fois). Parcourt la chaîne de repli du moteur
    configuré jusqu'au premier qui se charge ; "keywords" sert de filet de sécurité.
    """
    global _engine
    with _engine_lock:
        if _engine is not None:
            return _engine

        requested = settings.sentiment.engine
        if requested == "llm":
            log.warning("Moteur de sentiment 'llm' pas encore implémenté : repli sur VADER")
        for engine_cls in _ENGINE_CHAIN.get(requested, (VaderEngine, KeywordEngine)):
            try:
                _engine = engine_cls()
                break
            except Exception as exc:  # noqa: BLE001 - dépendance optionnelle absente ou cassée
                log.warning("Moteur de sentiment '%s' indisponible (%s) : repli", engine_cls.name, exc)
        else:
            _engine = KeywordEngine()

        log.info("Moteur de sentiment actif : %s (demandé : %s)", _engine.name, requested)
        return _engine


# =============================================================================
#  Analyse d'un texte
# =============================================================================

def classify(score: float) -> str:
    """Classe un score : "bullish", "bearish" ou "neutral"."""
    if score >= BULLISH_THRESHOLD:
        return "bullish"
    if score <= BEARISH_THRESHOLD:
        return "bearish"
    return "neutral"


def _neutral(engine_name: str = "none") -> dict[str, Any]:
    return {"score": 0.0, "label": "neutral", "engine": engine_name}


def analyze_text(text: str) -> dict[str, Any]:
    """
    Analyse le sentiment d'un texte (typiquement titre + extrait).

    Returns:
        {"score": float dans [-1, 1], "label": "bullish"|"bearish"|"neutral", "engine": str}.
        Texte vide/invalide ou erreur du moteur -> score 0.0, label "neutral".
    """
    if not isinstance(text, str) or not text.strip():
        return _neutral()

    engine = get_engine()
    try:
        score = engine.score(text[:MAX_TEXT_LENGTH])
        score = max(-1.0, min(1.0, float(score)))
        if math.isnan(score):
            raise ValueError("score NaN")
    except Exception as exc:  # noqa: BLE001
        log.warning("Échec de l'analyse de sentiment (%s) : %s", engine.name, exc)
        return _neutral(engine.name)

    result = {"score": round(score, 4), "label": classify(score), "engine": engine.name}
    log.debug("Sentiment %+.3f (%s) : %.80s", score, result["label"], text)
    return result


# =============================================================================
#  Analyse d'une liste d'articles
# =============================================================================

def split_symbol(symbol: str) -> tuple[str | None, str | None]:
    """
    Décompose un symbole broker en (actif de base, actif de cotation).
    EURUSD -> ("EUR", "USD"), XAUUSD -> ("XAU", "USD"), US30.cash -> ("US30", None).
    Symbole inconnu -> (None, None).
    """
    sym = re.split(r"[^A-Z0-9]", symbol.upper(), maxsplit=1)[0]
    for prefix in sorted(SYMBOL_ALIASES, key=len, reverse=True):
        if sym.startswith(prefix):
            rest = sym[len(prefix):len(prefix) + 3]
            return SYMBOL_ALIASES[prefix], rest if rest in ASSET_KEYWORDS else None
    base, quote = sym[:3], sym[3:6]
    if base in ASSET_KEYWORDS and quote in ASSET_KEYWORDS:
        return base, quote
    return None, None


def _mentions(asset: str | None, text: str) -> bool:
    return asset is not None and asset in _ASSET_PATTERNS and bool(_ASSET_PATTERNS[asset].search(text))


def _direction(text: str, base: str | None, quote: str | None) -> float:
    """
    Sens de l'impact d'un article sur une paire : +1 (base concernée ou paire citée),
    -1 (seule la devise de cotation est concernée), 0 (non concerné ou ambigu).
    """
    if base and quote and re.search(rf"{base.lower()}\s*/?\s*{quote.lower()}", text):
        return PAIR_MENTION_BOOST
    on_base, on_quote = _mentions(base, text), _mentions(quote, text)
    if on_base and not on_quote:
        return 1.0
    if on_quote and not on_base:
        return -1.0
    return 0.0       # les deux devises citées sans la paire : sens indéterminable


def detect_keywords(text: str, limit: int = 8) -> list[str]:
    """Termes du lexique financier présents dans le texte (expressions incluses), sans doublon."""
    if not isinstance(text, str) or not text.strip():
        return []
    found: list[str] = []
    for _i, key, _value in _lexicon_matches(_tokenize(text)):
        term = " ".join(key)
        if term not in found:
            found.append(term)
            if len(found) >= limit:
                break
    return found


def relevant_symbols(text: str, symbols: Iterable[str]) -> dict[str, float]:
    """
    Symboles concernés par un texte et sens de l'impact pour chacun :
    +1 (actif de base), -1 (devise de cotation), PAIR_MENTION_BOOST (paire citée).
    Les symboles non concernés ou ambigus sont absents du résultat.
    """
    if not isinstance(text, str) or not text.strip():
        return {}
    lowered = text.lower()
    result: dict[str, float] = {}
    for symbol in symbols:
        base, quote = split_symbol(symbol)
        if base is None:
            continue
        direction = _direction(lowered, base, quote)
        if direction:
            result[symbol] = direction
    return result


def _field(item: Any, name: str) -> Any:
    return item.get(name) if isinstance(item, dict) else getattr(item, name, None)


def _recency_weight(published: Any, now: datetime) -> float:
    if not isinstance(published, datetime):
        return UNDATED_ARTICLE_WEIGHT
    if published.tzinfo is None:
        published = published.replace(tzinfo=timezone.utc)
    age_hours = max(0.0, (now - published).total_seconds() / 3600)
    return 0.5 ** (age_hours / NEWS_HALF_LIFE_HOURS)


def _article_score(title: str, snippet: str) -> float:
    title_score = analyze_text(title)["score"]
    if not snippet:
        return title_score
    return TITLE_WEIGHT * title_score + (1 - TITLE_WEIGHT) * analyze_text(snippet)["score"]


def analyze_article(title: str, snippet: str = "") -> dict[str, Any]:
    """Sentiment d'un article (titre pondéré à TITLE_WEIGHT, extrait au reste), même format qu'`analyze_text`."""
    score = round(_article_score(title or "", snippet or ""), 4)
    return {"score": score, "label": classify(score), "engine": get_engine().name}


def analyze_news_items(news_items: Iterable[Any], symbol: str | None = None) -> float:
    """
    Sentiment global pondéré d'une liste d'actualités, pour la stratégie.

    Seuls les articles concernant les actifs suivis sont retenus. Chaque article
    est pondéré par sa fraîcheur (demi-vie NEWS_HALF_LIFE_HOURS) et, si la paire
    est citée explicitement, par PAIR_MENTION_BOOST.

    Args:
        news_items: `NewsArticle`, dicts ({"title", "snippet", "published"}) ou chaînes.
        symbol: si fourni (ex : "EURUSD"), le score est orienté pour ce symbole :
            > 0 = haussier pour la paire, < 0 = baissier (une bonne nouvelle sur USD
            pèse négativement sur EURUSD). Sans symbole : ton moyen des actualités
            concernant l'ensemble des symboles tradés, sans orientation.

    Returns:
        Score dans [-1, 1] ; 0.0 si aucun article pertinent ou en cas d'erreur.
    """
    try:
        if symbol:
            base, quote = split_symbol(symbol)
            if base is None:
                log.warning("Symbole %s non reconnu : sentiment neutre", symbol)
                return 0.0
            watched: set[str] = set()
        else:
            base = quote = None
            watched = {a for s in settings.trading.symbols for a in split_symbol(s) if a}

        now = datetime.now(timezone.utc)
        weighted_sum = total_weight = 0.0
        analyzed = 0
        for item in news_items or []:
            title = item if isinstance(item, str) else str(_field(item, "title") or "")
            snippet = "" if isinstance(item, str) else str(_field(item, "snippet") or "")
            text = f"{title} {snippet}".lower()
            if not text.strip():
                continue

            if symbol:
                direction = _direction(text, base, quote)
            else:
                direction = 1.0 if any(_mentions(a, text) for a in watched) else 0.0
            if direction == 0.0:
                continue

            score = _article_score(title, snippet)
            weight = abs(direction) * _recency_weight(_field(item, "published"), now)
            weighted_sum += math.copysign(1.0, direction) * score * weight
            total_weight += weight
            analyzed += 1

        if total_weight == 0:
            log.info("Sentiment %s : aucun article pertinent, score neutre", symbol or "global")
            return 0.0

        result = round(max(-1.0, min(1.0, weighted_sum / total_weight)), 4)
        log.info("Sentiment %s : %+.3f (%s) sur %d article(s) pertinent(s)",
                 symbol or "global", result, classify(result), analyzed)
        return result
    except Exception:  # noqa: BLE001
        log.exception("Erreur inattendue lors de l'analyse de sentiment des actualités")
        return 0.0


if __name__ == "__main__":
    # `python -m sentiment.sentiment_analyzer` : test rapide sur les actualités réelles
    from sentiment.news_scraper import get_latest_news

    for sample in ("Gold soars to record high as dollar slumps",
                   "ECB signals rate cut as eurozone growth stalls",
                   "US payrolls beat expectations, dollar rallies"):
        log.info("%s -> %s", sample, analyze_text(sample))
    articles = get_latest_news()
    for sym in settings.trading.symbols:
        analyze_news_items(articles, symbol=sym)
    analyze_news_items(articles)
