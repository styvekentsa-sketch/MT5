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
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable
from urllib.parse import urlparse

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

_calendar_lock = threading.Lock()
_calendar_cache: list[EconomicEvent] | None = None
_calendar_fetched_at = 0.0           # time.monotonic() du dernier téléchargement réussi


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
class NewsArticle:
    title: str
    source: str
    link: str
    published: datetime | None       # UTC, None si le flux ne fournit pas de date
    snippet: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


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


def _symbol_currencies(symbol: str) -> set[str]:
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
        currencies |= _symbol_currencies(symbol)
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
    global _calendar_cache, _calendar_fetched_at

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


def _entry_source(entry: Any, feed: Any, url: str) -> str:
    """Nom de la source : éditeur d'origine (Google News), titre du flux, ou domaine."""
    source = entry.get("source")
    if isinstance(source, dict) and source.get("title"):
        return str(source["title"]).strip()
    if feed.get("title"):
        return str(feed["title"]).strip()
    return urlparse(url).netloc.removeprefix("www.")


def _fetch_feed(url: str) -> list[NewsArticle]:
    """Télécharge et parse un flux RSS/Atom ; liste vide en cas d'échec."""
    try:
        response = _http_get(url)
    except Exception as exc:  # noqa: BLE001
        log.warning("Flux RSS inaccessible (%s) : %s", url, exc)
        return []

    parsed = feedparser.parse(response.content)
    if parsed.bozo and not parsed.entries:
        log.warning("Flux RSS illisible (%s) : %s", url, parsed.get("bozo_exception"))
        return []

    articles: list[NewsArticle] = []
    for entry in parsed.entries:
        title = _clean_html(entry.get("title", ""))
        link = str(entry.get("link", "")).strip()
        if not title or not link:
            continue
        summary = entry.get("summary") or entry.get("description") or ""
        articles.append(NewsArticle(
            title=title,
            source=_entry_source(entry, parsed.feed, url),
            link=link,
            published=_entry_datetime(entry),
            snippet=_truncate(_clean_html(summary)),
        ))
    log.debug("Flux %s : %d articles", url, len(articles))
    return articles


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


def get_upcoming_high_impact_events(
    minutes_buffer: int = 15,
    minutes_after: int = 5,
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

    currencies = _symbol_currencies(symbol) if symbol else get_watched_currencies()
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
    minutes_buffer: int = 15,
    minutes_after: int = 5,
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

def get_latest_news(
    feeds: Iterable[str] | None = None,
    lookback_hours: int | None = None,
    limit: int | None = None,
) -> list[NewsArticle]:
    """
    Agrège les articles récents des flux RSS financiers.

    Args:
        feeds: URLs des flux ; par défaut `settings.news.rss_feeds`.
        lookback_hours: ancienneté maximale des articles ; par défaut
            `settings.news.lookback_hours`. Les articles sans date sont conservés.
        limit: nombre maximal d'articles retournés (les plus récents d'abord).

    Returns:
        Articles dédoublonnés (lien et titre), triés du plus récent au plus ancien ;
        liste vide si aucun flux n'est configuré ou accessible.
    """
    try:
        feed_urls = list(feeds) if feeds is not None else list(settings.news.rss_feeds)
        if not feed_urls:
            log.warning("Aucun flux RSS configuré (NEWS_RSS_FEEDS)")
            return []

        hours = lookback_hours if lookback_hours is not None else settings.news.lookback_hours
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)

        seen_links: set[str] = set()
        seen_titles: set[str] = set()
        articles: list[NewsArticle] = []
        for url in feed_urls:
            for article in _fetch_feed(url):
                if article.published is not None and article.published < cutoff:
                    continue
                title_key = article.title.casefold()
                if article.link in seen_links or title_key in seen_titles:
                    continue
                seen_links.add(article.link)
                seen_titles.add(title_key)
                articles.append(article)

        oldest = datetime.min.replace(tzinfo=timezone.utc)
        articles.sort(key=lambda a: a.published or oldest, reverse=True)
        if limit is not None:
            articles = articles[:limit]

        log.info("%d articles récupérés depuis %d flux RSS (fenêtre %dh)", len(articles), len(feed_urls), hours)
        return articles
    except Exception:  # noqa: BLE001
        log.exception("Erreur inattendue lors de la récupération des actualités")
        return []


if __name__ == "__main__":
    # `python -m sentiment.news_scraper` : test rapide des sources
    log.info("Devises surveillées : %s", ", ".join(sorted(get_watched_currencies())))
    for ev in get_economic_calendar():
        log.info("  %s UTC | %s | %-6s | %s", ev.time.strftime("%H:%M"), ev.currency, ev.impact, ev.title)
    log.info("Annonce majeure imminente : %s", is_high_impact_news_imminent())
    for art in get_latest_news(limit=10):
        log.info("  [%s] %s", art.source, art.title)
