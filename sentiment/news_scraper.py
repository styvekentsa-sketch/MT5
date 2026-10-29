"""
Récupération des données fondamentales : calendrier économique et actualités.

Responsabilités :
  - calendrier économique de la semaine (export public Forex Factory), filtré
    sur les événements à fort impact des devises de nos paires
  - garde-fou `is_high_impact_news_imminent()` : interdit les entrées autour
    des annonces majeures (NFP, CPI, décisions de taux...)
  - agrégation des flux RSS financiers configurés dans `settings.news.rss_feeds`

Robustesse : chaque appel HTTP est réessayé avec backoff exponentiel (tenacity)
sur les erreurs transitoires (connexion, timeout, HTTP 5xx). Aucune fonction
publique ne lève d'exception : en cas d'échec, elles retournent une liste vide
(ou un statut sécurisé pour le garde-fou) et l'erreur est loguée.

Usage :
    from sentiment.news_scraper import get_economic_calendar, is_high_impact_news_imminent, get_latest_news

    if is_high_impact_news_imminent(minutes_buffer=15):
        ...  # pas de nouvelle position
    events = get_economic_calendar()
    articles = get_latest_news()
"""

from __future__ import annotations

import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from concurrent.futures import TimeoutError as FuturesTimeout
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable
from urllib.parse import quote, urlparse

import feedparser
import requests
from bs4 import BeautifulSoup
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from config import settings
from utils.logger import get_logger

log = get_logger(__name__)


class TransientHTTPError(requests.RequestException):
    """Réponse HTTP 5xx : erreur côté serveur, susceptible de disparaître au réessai."""


# =============================================================================
#  Constantes
# =============================================================================

# Export public du calendrier Forex Factory (semaine en cours, mis à jour ~1x/heure).
# La source limite fortement la fréquence d'appel : on met le résultat en cache.
CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
CALENDAR_CACHE_TTL_SECONDS = 3600

HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept": "application/json, application/rss+xml, application/xml, text/xml, */*",
}

MAX_HTTP_ATTEMPTS = 3
RETRY_WAIT_MIN_SECONDS = 1           # 1s, 2s, 4s... plafonné à RETRY_WAIT_MAX_SECONDS
RETRY_WAIT_MAX_SECONDS = 10

SNIPPET_MAX_LENGTH = 300

IMPACT_LEVELS = {"Low": 1, "Medium": 2, "High": 3}

# Préfixes d'indices / matières premières -> devise dont les annonces les font bouger
SYMBOL_CURRENCY_HINTS: dict[str, tuple[str, ...]] = {
    "XAU": ("USD",), "XAG": ("USD",), "XPT": ("USD",), "WTI": ("USD",), "BRENT": ("USD",),
    "US30": ("USD",), "US500": ("USD",), "SPX": ("USD",), "NAS": ("USD",), "US100": ("USD",),
    "USTEC": ("USD",), "DJ": ("USD",), "BTC": ("USD",), "ETH": ("USD",),
    "GER": ("EUR",), "DE40": ("EUR",), "DAX": ("EUR",), "FRA40": ("EUR",), "EU50": ("EUR",),
    "STOXX": ("EUR",), "UK100": ("GBP",), "FTSE": ("GBP",), "JP225": ("JPY",), "NIK": ("JPY",),
    "AUS200": ("AUD",),
}

MAJOR_CURRENCIES = {"USD", "EUR", "GBP", "JPY", "CHF", "CAD", "AUD", "NZD", "CNY"}

# -----------------------------------------------------------------------------
#  Catalogue de flux d'actualités (vérifiés joignables ; complété par NEWS_RSS_FEEDS)
#  Reuters ne publie plus de flux RSS public depuis 2020 : ses titres passent par
#  Google News, qui attribue chaque article à son éditeur d'origine.
# -----------------------------------------------------------------------------
GOOGLE_NEWS_SEARCH = "https://news.google.com/rss/search?hl=en-US&gl=US&ceid=US:en&q="
YAHOO_SYMBOL_FEED = "https://feeds.finance.yahoo.com/rss/2.0/headline?s={ticker}&region=US&lang=en-US"
YAHOO_TICKERS = {"XAU": "GC=F", "XAG": "SI=F", "WTI": "CL=F", "USOIL": "CL=F", "BRENT": "BZ=F", "UKOIL": "BZ=F"}

FEED_WORKERS = 8                     # flux lus en parallèle
FEED_DEADLINE_FACTOR = 3             # délai global de collecte = 3 x NEWS_REQUEST_TIMEOUT_SECONDS
FEED_FAILURE_THRESHOLD = 3           # échecs consécutifs avant mise en pause d'un flux
FEED_PAUSE_BASE_SECONDS = 300        # pause : 5 min, doublée à chaque échec supplémentaire...
FEED_PAUSE_MAX_SECONDS = 3600        # ... dans la limite d'une heure
FUTURE_TOLERANCE = timedelta(minutes=10)   # date de publication dans le futur : horloge du flux erronée

# Fenêtre de blocage autour d'une annonce à fort impact (partagée par le moteur et le dashboard)
NEWS_BLOCK_MINUTES_BEFORE = 15
NEWS_BLOCK_MINUTES_AFTER = 5




_calendar_lock = threading.Lock()
_calendar_cache: list[EconomicEvent] | None = None
_calendar_fetched_at = 0.0           # time.monotonic() du dernier téléchargement réussi
_calendar_fetched_wall: datetime | None = None   # même instant, en heure UTC (affichage)

_feed_lock = threading.Lock()
_feed_status: dict[str, FeedStatus] = {}         # dernier résultat de téléchargement par flux RSS


# =============================================================================
#  Structures de retour
# =============================================================================

@dataclass(frozen=True)
class EconomicEvent:
    title: str
    currency: str
    time: datetime                   # UTC
    impact: str                      # "High" | "Medium" | "Low" | "Holiday"
    forecast: str = ""
    previous: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class FeedSpec:
    name: str
    url: str
    category: str                    # forex, macro, commodities, central_bank, symbol, custom


@dataclass(frozen=True)
class FeedStatus:
    url: str
    ok: bool
    last_fetch: datetime             # UTC
    article_count: int
    error: str = ""
    consecutive_failures: int = 0


@dataclass(frozen=True)
class NewsArticle:
    title: str
    source: str
    link: str
    published: datetime | None       # UTC, None si le flux ne fournit pas de date
    snippet: str                     # contenu : résumé texte, sans HTML, tronqué
    feed: str = ""                   # flux d'origine (ex : « Bloomberg Markets »)
    category: str = ""               # catégorie du flux (forex, macro, central_bank...)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


DEFAULT_FEEDS: tuple[FeedSpec, ...] = (
    FeedSpec("FXStreet", "https://www.fxstreet.com/rss/news", "forex"),
    FeedSpec("investingLive (ex-ForexLive)", "https://investinglive.com/feed/news", "forex"),
    FeedSpec("FXEmpire", "https://www.fxempire.com/api/v1/en/articles/rss/news", "forex"),
    FeedSpec("Investing.com Forex", "https://www.investing.com/rss/news_1.rss", "forex"),
    FeedSpec("Nasdaq Devises", "https://www.nasdaq.com/feed/rssoutbound?category=Currencies", "forex"),
    FeedSpec("Google News Forex",
             GOOGLE_NEWS_SEARCH + quote('(forex OR "EUR/USD" OR "GBP/USD" OR "gold price") when:1d'), "forex"),
    FeedSpec("Reuters (via Google News)",
             GOOGLE_NEWS_SEARCH + quote("site:reuters.com (forex OR dollar OR euro OR gold OR inflation) when:1d"), "macro"),
    FeedSpec("Bloomberg Markets", "https://feeds.bloomberg.com/markets/news.rss", "macro"),
    FeedSpec("Bloomberg Economics", "https://feeds.bloomberg.com/economics/news.rss", "macro"),
    FeedSpec("WSJ Markets", "https://feeds.content.dowjones.io/public/rss/RSSMarketsMain", "macro"),
    FeedSpec("MarketWatch", "https://feeds.content.dowjones.io/public/rss/mw_topstories", "macro"),
    FeedSpec("CNBC Economy", "https://www.cnbc.com/id/20910258/device/rss/rss.html", "macro"),
    FeedSpec("Investing.com Économie", "https://www.investing.com/rss/news_14.rss", "macro"),
    FeedSpec("Investing.com Matières premières", "https://www.investing.com/rss/news_11.rss", "commodities"),
    FeedSpec("Réserve fédérale (Fed)", "https://www.federalreserve.gov/feeds/press_all.xml", "central_bank"),
    FeedSpec("BCE", "https://www.ecb.europa.eu/rss/press.html", "central_bank"),
    FeedSpec("Banque d'Angleterre", "https://www.bankofengland.co.uk/rss/news", "central_bank"),
)


# =============================================================================
#  Helpers internes
# =============================================================================

@retry(
    retry=retry_if_exception_type((requests.ConnectionError, requests.Timeout, TransientHTTPError)),
    stop=stop_after_attempt(MAX_HTTP_ATTEMPTS),
    wait=wait_exponential(multiplier=1, min=RETRY_WAIT_MIN_SECONDS, max=RETRY_WAIT_MAX_SECONDS),
    before_sleep=before_sleep_log(log, logging.WARNING),
    reraise=True,
)
def _http_get(url: str) -> requests.Response:
    """
    GET avec réessais automatiques sur les erreurs transitoires.
    Les erreurs 4xx (dont 429 Too Many Requests) ne sont PAS réessayées :
    insister aggraverait le blocage par la source.
    """
    response = requests.get(url, headers=HTTP_HEADERS, timeout=settings.news.request_timeout_seconds)
    if response.status_code >= 500:
        raise TransientHTTPError(f"HTTP {response.status_code} sur {url}", response=response)
    response.raise_for_status()
    return response


def symbol_currencies(symbol: str) -> set[str]:
    """Devises dont les annonces impactent un symbole (EURUSD -> {EUR, USD}, XAUUSD -> {USD})."""
    sym = symbol.upper()
    for prefix, currencies in SYMBOL_CURRENCY_HINTS.items():
        if sym.startswith(prefix):
            return set(currencies)
    base, quote = sym[:3], sym[3:6]
    return {c for c in (base, quote) if c in MAJOR_CURRENCIES}


def get_watched_currencies(symbols: Iterable[str] | None = None) -> set[str]:
    """Devises surveillées, déduites des symboles tradés (par défaut `settings.trading.symbols`)."""
    currencies: set[str] = set()
    for symbol in symbols if symbols is not None else settings.trading.symbols:
        currencies |= symbol_currencies(symbol)
    return currencies


def _parse_event(raw: dict[str, Any]) -> EconomicEvent | None:
    """Convertit une entrée JSON Forex Factory ; None si l'entrée est inexploitable."""
    try:
        when = datetime.fromisoformat(raw["date"]).astimezone(timezone.utc)
    except (KeyError, TypeError, ValueError):
        log.debug("Événement de calendrier ignoré (date invalide) : %s", raw)
        return None
    return EconomicEvent(
        title=str(raw.get("title", "")).strip(),
        currency=str(raw.get("country", "")).strip().upper(),
        time=when,
        impact=str(raw.get("impact", "")).strip(),
        forecast=str(raw.get("forecast") or "").strip(),
        previous=str(raw.get("previous") or "").strip(),
    )


def _load_calendar(force_refresh: bool = False) -> list[EconomicEvent] | None:
    """
    Calendrier complet de la semaine, avec cache de CALENDAR_CACHE_TTL_SECONDS.
    En cas d'échec réseau, retourne le dernier cache connu (même périmé) ;
    None uniquement si aucune donnée n'a jamais pu être récupérée.
    """
    global _calendar_cache, _calendar_fetched_at, _calendar_fetched_wall

    with _calendar_lock:
        fresh = time.monotonic() - _calendar_fetched_at < CALENDAR_CACHE_TTL_SECONDS
        if _calendar_cache is not None and fresh and not force_refresh:
            return _calendar_cache

        try:
            payload = _http_get(CALENDAR_URL).json()
            if not isinstance(payload, list):
                raise ValueError(f"format inattendu ({type(payload).__name__})")
        except Exception as exc:  # noqa: BLE001 - le bot ne doit jamais planter ici
            if _calendar_cache is not None:
                log.warning("Calendrier économique indisponible (%s) : utilisation du cache précédent", exc)
            else:
                log.error("Calendrier économique indisponible et aucun cache : %s", exc)
            return _calendar_cache

        events = [e for e in (_parse_event(item) for item in payload if isinstance(item, dict)) if e]
        events.sort(key=lambda e: e.time)
        _calendar_cache = events
        _calendar_fetched_at = time.monotonic()
        _calendar_fetched_wall = datetime.now(timezone.utc)
        log.info("Calendrier économique mis à jour : %d événements cette semaine", len(events))
        return events


def _clean_html(text: str) -> str:
    """Supprime les balises HTML et normalise les espaces."""
    if not text:
        return ""
    plain = BeautifulSoup(text, "lxml").get_text(" ", strip=True)
    return " ".join(plain.split())


def _truncate(text: str, max_length: int = SNIPPET_MAX_LENGTH) -> str:
    if len(text) <= max_length:
        return text
    return text[:max_length].rsplit(" ", 1)[0].rstrip(" ,.;:") + "…"


def _entry_datetime(entry: Any) -> datetime | None:
    """Date de publication d'une entrée feedparser, convertie en UTC."""
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        parsed = entry.get(key)
        if parsed:
            # feedparser normalise déjà ces struct_time en UTC
            return datetime(*parsed[:6], tzinfo=timezone.utc)
    return None


def _domain(url: str) -> str:
    return urlparse(url).netloc.removeprefix("www.") or url


def _entry_source(entry: Any, feed: Any, url: str, name: str = "") -> str:
    """Éditeur : celui de l'article (agrégateurs type Google News), sinon nom du flux, titre du flux, domaine."""
    source = entry.get("source")
    if isinstance(source, dict) and source.get("title"):
        return str(source["title"]).strip()
    if name:
        return name
    if feed.get("title"):
        return str(feed["title"]).strip()
    return _domain(url)


def _clean_title(title: str, source: str) -> str:
    """Retire le suffixe « - Éditeur » ajouté par les agrégateurs (« Dollar rises - Reuters »)."""
    suffix = f" - {source}"
    return title[: -len(suffix)].rstrip() if source and title.endswith(suffix) else title


def title_key(title: str) -> str:
    """Clé de dédoublonnage : minuscules, ponctuation et espaces normalisés."""
    return " ".join(re.sub(r"[^\w]+", " ", title.casefold()).split())


def _record_feed(url: str, ok: bool, count: int = 0, error: str = "") -> None:
    with _feed_lock:
        previous = _feed_status.get(url)
        failures = 0 if ok else (previous.consecutive_failures + 1 if previous else 1)
        _feed_status[url] = FeedStatus(url, ok, datetime.now(timezone.utc), count, error, failures)


def _paused(url: str) -> bool:
    """Flux en échec répété : mis en pause (5 min, puis 10, 20... jusqu'à 1 h) pour ne pas ralentir la collecte."""
    with _feed_lock:
        status = _feed_status.get(url)
    if status is None or status.ok or status.consecutive_failures < FEED_FAILURE_THRESHOLD:
        return False
    pause = min(FEED_PAUSE_BASE_SECONDS * 2 ** (status.consecutive_failures - FEED_FAILURE_THRESHOLD),
                FEED_PAUSE_MAX_SECONDS)
    return datetime.now(timezone.utc) - status.last_fetch < timedelta(seconds=pause)


def _fetch_feed(url: str, name: str = "", category: str = "") -> list[NewsArticle]:
    """
    Télécharge un flux RSS/Atom et normalise ses articles (titre, contenu, source, lien,
    date UTC). Ne lève jamais d'exception : liste vide en cas d'échec, tracé par flux.
    """
    try:
        response = _http_get(url)
        parsed = feedparser.parse(response.content)
    except Exception as exc:  # noqa: BLE001 - une source en panne ne bloque pas les autres
        log.warning("Flux RSS inaccessible (%s) : %s", name or url, exc)
        _record_feed(url, ok=False, error=str(exc))
        return []

    if parsed.bozo and not parsed.entries:
        log.warning("Flux RSS illisible (%s) : %s", name or url, parsed.get("bozo_exception"))
        _record_feed(url, ok=False, error=f"flux illisible : {parsed.get('bozo_exception')}")
        return []

    now = datetime.now(timezone.utc)
    articles: list[NewsArticle] = []
    for entry in parsed.entries:
        try:
            source = _entry_source(entry, parsed.feed, url, name)
            title = _clean_title(_clean_html(entry.get("title", "")), source)
            link = str(entry.get("link", "")).strip()
            if not title or not link:
                continue
            published = _entry_datetime(entry)
            if published is not None and published > now + FUTURE_TOLERANCE:
                published = now                          # date future : horloge du flux erronée
            summary = entry.get("summary") or entry.get("description") or ""
            articles.append(NewsArticle(
                title=title,
                source=source,
                link=link,
                published=published,
                snippet=_truncate(_clean_html(summary)),
                feed=name or _domain(url),
                category=category,
            ))
        except Exception as exc:  # noqa: BLE001 - un article malformé n'invalide pas tout le flux
            log.debug("Article ignoré dans %s : %s", name or url, exc)
    log.debug("Flux %s : %d articles", name or url, len(articles))
    _record_feed(url, ok=True, count=len(articles))
    return articles


def _yahoo_ticker(symbol: str) -> str | None:
    """Ticker Yahoo Finance d'un symbole broker (EURUSD -> EURUSD=X, XAUUSD -> GC=F)."""
    sym = re.split(r"[^A-Z0-9]", symbol.upper(), maxsplit=1)[0]
    for prefix, ticker in YAHOO_TICKERS.items():
        if sym.startswith(prefix):
            return ticker
    if sym[:3] in MAJOR_CURRENCIES and sym[3:6] in MAJOR_CURRENCIES:
        return f"{sym[:6]}=X"
    return None


def configured_feeds() -> list[FeedSpec]:
    """
    Flux lus par le bot : catalogue intégré (si NEWS_DEFAULT_FEEDS=true), un flux Yahoo
    Finance par symbole tradé, puis les flux personnels de NEWS_RSS_FEEDS. Sans doublon d'URL.
    """
    specs: list[FeedSpec] = []
    if settings.news.use_default_feeds:
        specs.extend(DEFAULT_FEEDS)
        for symbol in settings.trading.symbols:
            ticker = _yahoo_ticker(symbol)
            if ticker:
                specs.append(FeedSpec(f"Yahoo Finance {symbol}",
                                      YAHOO_SYMBOL_FEED.format(ticker=quote(ticker, safe="=")), "symbol"))
    specs.extend(FeedSpec(_domain(url), url, "custom") for url in settings.news.rss_feeds)
    seen: set[str] = set()
    return [s for s in specs if not (s.url in seen or seen.add(s.url))]


def get_feed_statuses(feeds: Iterable[str] | None = None) -> dict[str, FeedStatus | None]:
    """Dernier résultat de téléchargement de chaque flux lu par le bot (None = jamais téléchargé)."""
    urls = list(feeds) if feeds is not None else [s.url for s in configured_feeds()]
    with _feed_lock:
        return {url: _feed_status.get(url) for url in urls}


def feed_names() -> dict[str, str]:
    """URL -> nom lisible de chaque flux lu par le bot."""
    return {s.url: s.name for s in configured_feeds()}

# =============================================================================
#  Calendrier économique
# =============================================================================

def get_economic_calendar(
    min_impact: str = "High",
    currencies: Iterable[str] | None = None,
    day: datetime | None = None,
    force_refresh: bool = False,
) -> list[EconomicEvent]:
    """
    Événements économiques d'une journée (UTC), triés chronologiquement.

    Args:
        min_impact: impact minimal retenu ("High" par défaut, "Medium" ou "Low"
            pour élargir). Les jours fériés ("Holiday") sont exclus.
        currencies: devises à conserver ; par défaut celles des paires tradées
            (`settings.trading.symbols`). Passer un itérable vide pour tout garder.
        day: journée ciblée (par défaut aujourd'hui, UTC).
        force_refresh: ignore le cache et retélécharge le calendrier.

    Returns:
        Liste d'`EconomicEvent` ; liste vide si le calendrier est indisponible.
    """
    try:
        events = _load_calendar(force_refresh=force_refresh)
        if not events:
            return []

        threshold = IMPACT_LEVELS.get(min_impact.capitalize(), IMPACT_LEVELS["High"])
        watched = {c.upper() for c in currencies} if currencies is not None else get_watched_currencies()
        target_day = (day or datetime.now(timezone.utc)).astimezone(timezone.utc).date()

        return [
            e for e in events
            if e.time.date() == target_day
            and IMPACT_LEVELS.get(e.impact, 0) >= threshold
            and (not watched or e.currency in watched)
        ]
    except Exception:  # noqa: BLE001
        log.exception("Erreur inattendue lors du filtrage du calendrier économique")
        return []


def get_calendar_status() -> dict[str, Any]:
    """État du calendrier : disponible, dernière mise à jour (UTC), nombre d'événements, URL source."""
    with _calendar_lock:
        return {
            "available": _calendar_cache is not None,
            "last_fetch": _calendar_fetched_wall,
            "event_count": len(_calendar_cache or []),
            "url": CALENDAR_URL,
            "ttl_seconds": CALENDAR_CACHE_TTL_SECONDS,
        }


def get_upcoming_events(
    hours_ahead: float = 24,
    hours_back: float = 2,
    min_impact: str = "High",
    currencies: Iterable[str] | None = None,
) -> list[EconomicEvent]:
    """
    Événements de [maintenant - hours_back, maintenant + hours_ahead], triés chronologiquement,
    filtrés par impact minimal et devises (par défaut celles des paires tradées).
    Liste vide si le calendrier est indisponible ; ne lève jamais d'exception.
    """
    try:
        events = _load_calendar()
        if not events:
            return []
        threshold = IMPACT_LEVELS.get(min_impact.capitalize(), IMPACT_LEVELS["High"])
        watched = {c.upper() for c in currencies} if currencies is not None else get_watched_currencies()
        now = datetime.now(timezone.utc)
        start, end = now - timedelta(hours=hours_back), now + timedelta(hours=hours_ahead)
        return [
            e for e in events
            if start <= e.time <= end
            and IMPACT_LEVELS.get(e.impact, 0) >= threshold
            and (not watched or e.currency in watched)
        ]
    except Exception:  # noqa: BLE001
        log.exception("Erreur inattendue lors de la lecture du calendrier économique")
        return []


def get_upcoming_high_impact_events(
    minutes_buffer: int = NEWS_BLOCK_MINUTES_BEFORE,
    minutes_after: int = NEWS_BLOCK_MINUTES_AFTER,
    symbol: str | None = None,
) -> list[EconomicEvent] | None:
    """
    Événements à fort impact dans la fenêtre [maintenant - minutes_after, maintenant + minutes_buffer].

    Returns:
        La liste des événements concernés (éventuellement vide), ou None si le
        calendrier est indisponible (situation à traiter comme un risque).
    """
    events = _load_calendar()
    if events is None:
        return None

    currencies = symbol_currencies(symbol) if symbol else get_watched_currencies()
    now = datetime.now(timezone.utc)
    window_start = now - timedelta(minutes=minutes_after)
    window_end = now + timedelta(minutes=minutes_buffer)
    return [
        e for e in events
        if e.impact == "High"
        and e.currency in currencies
        and window_start <= e.time <= window_end
    ]


def is_high_impact_news_imminent(
    minutes_buffer: int = NEWS_BLOCK_MINUTES_BEFORE,
    minutes_after: int = NEWS_BLOCK_MINUTES_AFTER,
    symbol: str | None = None,
    block_if_unavailable: bool = True,
) -> bool:
    """
    Indique si une annonce à fort impact tombe dans les `minutes_buffer` minutes
    (ou est tombée il y a moins de `minutes_after` minutes, la volatilité post-annonce
    étant elle aussi dangereuse). À appeler avant toute ouverture de position.

    Args:
        minutes_buffer: horizon de garde avant l'annonce.
        minutes_after: horizon de garde après l'annonce (0 pour désactiver).
        symbol: limite la vérification aux devises de ce symbole ; par défaut
            toutes les devises des paires tradées.
        block_if_unavailable: statut retourné si le calendrier n'a jamais pu être
            chargé. True (défaut) = on bloque par prudence.

    Returns:
        True si le bot doit s'abstenir d'ouvrir des positions.
    """
    try:
        upcoming = get_upcoming_high_impact_events(minutes_buffer, minutes_after, symbol)
    except Exception:  # noqa: BLE001
        log.exception("Erreur inattendue lors de la vérification des annonces imminentes")
        upcoming = None

    if upcoming is None:
        log.warning(
            "Calendrier économique indisponible : %s",
            "nouvelles positions bloquées par précaution" if block_if_unavailable else "vérification ignorée",
        )
        return block_if_unavailable

    for event in upcoming:
        delta_min = (event.time - datetime.now(timezone.utc)).total_seconds() / 60
        when = f"dans {delta_min:.0f} min" if delta_min >= 0 else f"il y a {-delta_min:.0f} min"
        log.warning("Annonce à fort impact %s : [%s] %s (%s UTC)",
                    when, event.currency, event.title, event.time.strftime("%H:%M"))
    return bool(upcoming)


# =============================================================================
#  Actualités en temps réel
# =============================================================================

def _as_spec(feed: str | FeedSpec) -> FeedSpec:
    return feed if isinstance(feed, FeedSpec) else FeedSpec(_domain(feed), feed, "custom")


def get_latest_news(
    feeds: Iterable[str | FeedSpec] | None = None,
    lookback_hours: int | None = None,
    limit: int | None = None,
    max_per_feed: int | None = None,
) -> list[NewsArticle]:
    """
    Agrège les actualités récentes de plusieurs sources, de façon équilibrée.

    - Les flux sont lus en parallèle, chacun isolé : une source injoignable, lente ou
      malformée n'empêche jamais la lecture des autres (délai global de collecte borné).
    - Un flux en échec répété est mis en pause quelques minutes (voir `_paused`).
    - Équilibrage : au plus `max_per_feed` articles par flux (les plus récents), pour
      qu'une source très prolifique ne domine pas l'analyse de sentiment.
    - Dédoublonnage entre sources : même lien ou même titre normalisé.

    Args:
        feeds: URLs ou FeedSpec ; par défaut `configured_feeds()`.
        lookback_hours: ancienneté maximale (défaut NEWS_LOOKBACK_HOURS) ; articles sans date conservés.
        limit: nombre maximal d'articles retournés (les plus récents d'abord).
        max_per_feed: plafond par flux (défaut NEWS_MAX_PER_FEED).

    Returns:
        Articles normalisés, du plus récent au plus ancien ; liste vide si aucune source n'est disponible.
    """
    try:
        specs = [_as_spec(f) for f in feeds] if feeds is not None else configured_feeds()
        if not specs:
            log.warning("Aucun flux d'actualités configuré (NEWS_DEFAULT_FEEDS / NEWS_RSS_FEEDS)")
            return []
        active = [s for s in specs if not _paused(s.url)]
        hours = lookback_hours if lookback_hours is not None else settings.news.lookback_hours
        per_feed = max_per_feed or settings.news.max_per_feed
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        results = _collect(active)

        oldest = datetime.min.replace(tzinfo=timezone.utc)
        recency = lambda a: a.published or oldest   # noqa: E731
        balanced: list[NewsArticle] = []
        for spec in active:
            recent = [a for a in results.get(spec.url, []) if a.published is None or a.published >= cutoff]
            balanced.extend(sorted(recent, key=recency, reverse=True)[:per_feed])

        # Dédoublonnage : on garde la PREMIÈRE publication (souvent la source qui a sorti l'information,
        # les suivantes étant des reprises) ; les articles sans date passent après les articles datés.
        latest = datetime.max.replace(tzinfo=timezone.utc)
        seen_links: set[str] = set()
        seen_titles: set[str] = set()
        articles: list[NewsArticle] = []
        for article in sorted(balanced, key=lambda a: a.published or latest):
            key = title_key(article.title)
            if article.link in seen_links or key in seen_titles:
                continue
            seen_links.add(article.link)
            seen_titles.add(key)
            articles.append(article)
        articles.sort(key=recency, reverse=True)
        if limit is not None:
            articles = articles[:limit]

        sources_ok = sum(1 for s in active if results.get(s.url))
        log.info("%d articles depuis %d/%d sources (fenêtre %dh, %d en pause)",
                 len(articles), sources_ok, len(specs), hours, len(specs) - len(active))
        return articles
    except Exception:  # noqa: BLE001
        log.exception("Erreur inattendue lors de la récupération des actualités")
        return []


def _collect(specs: list[FeedSpec]) -> dict[str, list[NewsArticle]]:
    """Lit les flux en parallèle avec un délai global ; les flux trop lents sont abandonnés pour ce tour."""
    if not specs:
        return {}
    results: dict[str, list[NewsArticle]] = {}
    pool = ThreadPoolExecutor(max_workers=min(FEED_WORKERS, len(specs)), thread_name_prefix="news")
    futures = {pool.submit(_fetch_feed, s.url, s.name, s.category): s for s in specs}
    try:
        for future in as_completed(futures, timeout=settings.news.request_timeout_seconds * FEED_DEADLINE_FACTOR):
            spec = futures[future]
            try:
                results[spec.url] = future.result()
            except Exception as exc:  # noqa: BLE001 - déjà isolé dans _fetch_feed : filet de sécurité
                log.warning("Source %s ignorée : %s", spec.name, exc)
                results[spec.url] = []
    except FuturesTimeout:
        for future, spec in futures.items():
            if not future.done():
                log.warning("Source %s trop lente : ignorée pour cette collecte", spec.name)
                _record_feed(spec.url, ok=False, error="délai global de collecte dépassé")
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return results

if __name__ == "__main__":
    # `python -m sentiment.news_scraper` : test rapide des sources
    log.info("Devises surveillées : %s", ", ".join(sorted(get_watched_currencies())))
    for ev in get_economic_calendar():
        log.info("  %s UTC | %s | %-6s | %s", ev.time.strftime("%H:%M"), ev.currency, ev.impact, ev.title)
    log.info("Annonce majeure imminente : %s", is_high_impact_news_imminent())
    for art in get_latest_news(limit=10):
        log.info("  [%s] %s", art.source, art.title)
