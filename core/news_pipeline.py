"""
Pipeline des actualités et du calendrier économique, et fenêtre de sécurité associée.

1. Calendrier économique, sources interchangeables (`CalendarProvider`) :
     - ForexFactoryProvider   calendrier de la semaine (heure, devise, importance, consensus, précédent ;
                              le résultat réel n'y est pas publié)
     - SimulatedCalendarProvider  événements fournis en code ou dans un fichier JSON (NEWS_CALENDAR_FILE),
                              résultat réel compris : tests, backtests, démonstrations
   Les événements de toutes les sources sont fusionnés et dédoublonnés (même devise, même titre,
   même minute) ; une source qui échoue n'empêche pas les autres de répondre.

2. Fenêtre de sécurité : aucune nouvelle proposition de trading sur un actif de
   NEWS_BLOCK_MINUTES_BEFORE (30) minutes avant à NEWS_BLOCK_MINUTES_AFTER (15) minutes après une
   annonce à fort impact sur l'une de ses devises (EURUSD : EUR et USD ; XAUUSD : USD). Calendrier
   indisponible (toutes les sources en échec) : propositions suspendues par précaution.
   Une alerte par annonce, jamais répétée pendant la fenêtre.

3. Analyse structurée (`NewsAnalysis`, Pydantic) d'un événement ou d'un article : fait, source et
   fiabilité, actifs concernés, surprise (réel vs consensus), effet potentiel par actif et horizon,
   conséquence sur les setups en cours (validé / invalidé / neutre). Analyse déterministe par règles ;
   un analyseur par LLM pourrait implémenter la même interface (`analyze_event` / `analyze_article`).
   Chaque fiche porte aussi l'heure de réception, l'intensité (faible / moyenne / forte), une
   confiance (à calibrer sur l'historique), sa durée de validité et l'action proposée
   (autoriser, attendre, bloquer, réévaluer). Les mots-clés orientent l'analyse, ils ne déclenchent
   jamais un ordre : la décision reste au moteur et au module de risque.
"""

from __future__ import annotations

import json
import re
import threading
import time
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Iterable

from pydantic import BaseModel, ConfigDict, Field, field_validator

from config.settings import settings
from models.schemas import Side, TradingSetup
from utils.logger import get_logger

log = get_logger(__name__)

CACHE_SECONDS = 300


# =============================================================================
#  Modèles
# =============================================================================

class Importance(str, Enum):
    LOW = "faible"
    MEDIUM = "moyenne"
    HIGH = "forte"


IMPACT_LABELS = {"low": Importance.LOW, "medium": Importance.MEDIUM, "high": Importance.HIGH,
                 "faible": Importance.LOW, "moyenne": Importance.MEDIUM, "forte": Importance.HIGH}


def _normalize(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()


class CalendarEvent(BaseModel):
    """Annonce économique programmée."""
    model_config = ConfigDict(frozen=True)

    time: datetime = Field(description="Heure de publication (UTC)")
    source: str
    title: str
    currency: str = Field(min_length=3, max_length=3)
    importance: Importance
    previous: str | None = None
    consensus: str | None = None
    actual: str | None = None
    assets: tuple[str, ...] = Field(default=(), description="Symboles suivis exposés à cette devise")

    @field_validator("time")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)

    @field_validator("currency")
    @classmethod
    def _upper(cls, value: str) -> str:
        return value.upper()

    @property
    def key(self) -> str:
        """Identité d'une annonce, quelle que soit la source (déduplication)."""
        return f"{self.currency}|{_normalize(self.title)}|{self.time:%Y%m%d%H%M}"


class Reliability(str, Enum):
    HIGH = "élevée"
    MEDIUM = "moyenne"
    LOW = "faible"


class Horizon(str, Enum):
    MINUTES = "minutes"
    HOURS = "heures"
    DAYS = "jours"


class Intensity(str, Enum):
    LOW = "faible"
    MEDIUM = "moyenne"
    HIGH = "forte"


class Action(str, Enum):
    ALLOW = "autoriser"
    WAIT = "attendre"
    BLOCK = "bloquer"
    REASSESS = "réévaluer"


class Surprise(BaseModel):
    actual: float
    consensus: float
    difference: float
    relative_pct: float | None = Field(description="Écart en % du consensus")
    direction: str = Field(description="au-dessus, en dessous ou conforme au consensus")


class AssetEffect(BaseModel):
    direction: str = Field(description="haussier, baissier ou neutre")
    strength: float = Field(ge=0, le=1)


class SetupImpact(BaseModel):
    setup_id: str
    symbol: str
    side: Side
    verdict: str = Field(description="validé, invalidé ou neutre")
    reason: str


class NewsAnalysis(BaseModel):
    """Lecture structurée d'une annonce ou d'un article."""
    kind: str = Field(description="calendrier ou article")
    fact: str
    published_at: datetime | None
    source: str
    reliability: Reliability
    assets: list[str]
    surprise: Surprise | None = None
    effects: dict[str, AssetEffect] = Field(default_factory=dict)
    horizon: Horizon
    setup_impacts: list[SetupImpact] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    received_at: datetime | None = Field(None, description="Heure de réception par le bot (UTC)")
    intensity: Intensity = Intensity.LOW
    confidence: float = Field(0.0, ge=0, le=1, description="Confiance de l'analyse, à calibrer")
    valid_until: datetime | None = Field(None, description="Fin de la période où le signal reste exploitable")
    action: Action = Action.ALLOW
    action_reason: str = ""


# =============================================================================
#  Sources du calendrier
# =============================================================================

class CalendarProvider(ABC):
    name = "source"

    @abstractmethod
    def fetch(self) -> list[CalendarEvent]:
        """Événements connus de la source ; lève une exception si elle est indisponible."""


class ForexFactoryProvider(CalendarProvider):
    """Calendrier de la semaine (nfs.faireconomy.media), avec cache et nouvelles tentatives du scraper."""
    name = "Forex Factory"

    def fetch(self) -> list[CalendarEvent]:
        from sentiment.news_scraper import _load_calendar

        raw = _load_calendar()
        if raw is None:
            raise ConnectionError("calendrier Forex Factory indisponible")
        return [CalendarEvent(time=e.time, source=self.name, title=e.title, currency=e.currency,
                              importance=IMPACT_LABELS[e.impact.lower()], previous=e.previous or None,
                              consensus=e.forecast or None)
                for e in raw if e.impact.lower() in IMPACT_LABELS and len(e.currency) == 3]


class SimulatedCalendarProvider(CalendarProvider):
    """Événements fournis en code ou par un fichier JSON (liste d'objets au format CalendarEvent)."""
    name = "simulé"

    def __init__(self, events: Iterable[CalendarEvent | dict[str, Any]] = (), path: Path | None = None) -> None:
        self._events: list[CalendarEvent] = [self._parse(e) for e in events]
        self.path = path
        self.fail = False                                  # tests : simuler une panne de la source

    def _parse(self, item: CalendarEvent | dict[str, Any]) -> CalendarEvent:
        if isinstance(item, CalendarEvent):
            return item
        data = {"source": self.name, **item}
        data["importance"] = IMPACT_LABELS.get(str(data.get("importance", "")).lower(), data.get("importance"))
        return CalendarEvent(**data)

    def add(self, event: CalendarEvent | dict[str, Any]) -> None:
        self._events.append(self._parse(event))

    def fetch(self) -> list[CalendarEvent]:
        if self.fail:
            raise ConnectionError("source simulée en panne")
        from_file = []
        if self.path is not None:
            from_file = [self._parse(item) for item in json.loads(self.path.read_text(encoding="utf-8"))]
        return [*self._events, *from_file]


# =============================================================================
#  Analyse structurée (règles)
# =============================================================================

# Indicateurs dont une hausse est une mauvaise nouvelle pour la devise
INVERSE_INDICATORS = ("unemployment", "jobless", "claimant", "claims", "chômage")
HIGH_RELIABILITY = ("reuters", "bloomberg", "wall street journal", "wsj", "financial times", "federal reserve", "fed",
                    "ecb", "bce", "bank of england", "boe", "banque", "forex factory", "simulé")
MEDIUM_RELIABILITY = ("fxstreet", "investing", "marketwatch", "cnbc", "nasdaq", "yahoo", "fxempire", "investinglive")
LONG_HORIZON = ("rate decision", "interest rate", "monetary policy", "cash rate", "fomc", "taux directeur", "policy")
SURPRISE_FULL_STRENGTH = 0.20         # écart de 20 % du consensus = effet maximal
RELIABILITY_CONFIDENCE = {Reliability.HIGH: 0.9, Reliability.MEDIUM: 0.7, Reliability.LOW: 0.4}
ARTICLE_MODEL_CONFIDENCE = 0.6        # sentiment par lexique : moins sûr qu'un chiffre publié
HORIZON_VALIDITY = {Horizon.MINUTES: timedelta(minutes=30), Horizon.HOURS: timedelta(hours=4),
                    Horizon.DAYS: timedelta(days=2)}
_NUMBER = re.compile(r"(-?\d+(?:[.,]\d+)?)\s*([kKmMbBtT%]?)")
_MULTIPLIERS = {"k": 1e3, "m": 1e6, "b": 1e9, "t": 1e12}


def parse_value(text: str | None) -> float | None:
    """« 3.2% » -> 3.2 ; « 250K » -> 250 000 ; « -0.1% » -> -0.1 ; vide ou illisible -> None."""
    if not text:
        return None
    match = _NUMBER.search(text.replace("−", "-"))
    if not match:
        return None
    value = float(match.group(1).replace(",", "."))
    return value * _MULTIPLIERS.get(match.group(2).lower(), 1.0)


def source_reliability(source: str) -> Reliability:
    name = source.lower()
    if any(s in name for s in HIGH_RELIABILITY):
        return Reliability.HIGH
    if any(s in name for s in MEDIUM_RELIABILITY):
        return Reliability.MEDIUM
    return Reliability.LOW


def _currency_sign_on(symbol: str, currency: str) -> int:
    """+1 si la devise est la base du symbole (EURUSD, EUR), -1 si c'est la cotation (USD), 0 sinon."""
    from sentiment.news_scraper import symbol_currencies

    sym = symbol.upper()
    if currency not in symbol_currencies(sym):
        return 0
    if sym.startswith(currency):
        return 1
    return -1                                              # devise de cotation (EURUSD : USD, XAUUSD : USD)


def _intensity(strength: float) -> Intensity:
    return Intensity.HIGH if strength >= 0.66 else Intensity.MEDIUM if strength >= 0.33 else Intensity.LOW


def classify(analysis: NewsAnalysis, now: datetime | None = None, importance: Importance | None = None,
             received_at: datetime | None = None) -> NewsAnalysis:
    """
    Intensité, validité et action proposée :
      - annonce pas encore publiée (ou sans résultat) : attendre si l'importance est forte ou moyenne
      - signal périmé (au-delà de sa validité) : autoriser, il n'influence plus rien
      - un setup en cours va à contre-sens d'un effet moyen ou fort : réévaluer
      - effet fort encore frais : bloquer les nouvelles entrées le temps que le marché digère
    """
    now = now or datetime.now(timezone.utc)
    analysis.received_at = received_at or analysis.received_at or now
    strength = max((e.strength for e in analysis.effects.values()), default=0.0)
    published = analysis.published_at
    pending = analysis.kind == "calendrier" and analysis.surprise is None
    if pending and importance is not None:
        analysis.intensity = {Importance.HIGH: Intensity.HIGH, Importance.MEDIUM: Intensity.MEDIUM}.get(
            importance, Intensity.LOW)
    else:
        analysis.intensity = _intensity(strength)
    base = RELIABILITY_CONFIDENCE[analysis.reliability]
    analysis.confidence = round(base * (0.5 if pending else 1.0 if analysis.kind == "calendrier"
                                        else ARTICLE_MODEL_CONFIDENCE), 2)
    start = published or analysis.received_at
    analysis.valid_until = start + HORIZON_VALIDITY[analysis.horizon]

    contrary = [i for i in analysis.setup_impacts if i.verdict == "invalidé"]
    if now > analysis.valid_until:
        analysis.action, analysis.action_reason = Action.ALLOW, "signal périmé : plus d'influence attendue"
    elif pending and analysis.intensity != Intensity.LOW:
        analysis.action, analysis.action_reason = Action.WAIT, "résultat pas encore publié"
    elif contrary and analysis.intensity != Intensity.LOW:
        analysis.action = Action.REASSESS
        analysis.action_reason = f"{len(contrary)} setup(s) à contre-sens de l'effet attendu"
    elif analysis.intensity == Intensity.HIGH and published and now - published <= timedelta(minutes=15):
        analysis.action, analysis.action_reason = Action.BLOCK, "effet fort encore frais : volatilité attendue"
    else:
        analysis.action, analysis.action_reason = Action.ALLOW, "effet faible ou compatible avec les setups"
    return analysis


def setup_consequence(analysis: NewsAnalysis, setup: TradingSetup) -> SetupImpact:
    """Validation (effet dans le sens du setup), invalidation (effet contraire) ou neutre."""
    effect = analysis.effects.get(setup.symbol)
    if effect is None or effect.direction == "neutre":
        return SetupImpact(setup_id=setup.id, symbol=setup.symbol, side=setup.side, verdict="neutre",
                           reason="aucun effet attendu sur cet actif")
    favourable = (effect.direction == "haussier") == (setup.side == Side.BUY)
    return SetupImpact(setup_id=setup.id, symbol=setup.symbol, side=setup.side,
                       verdict="validé" if favourable else "invalidé",
                       reason=f"effet {effect.direction} (force {effect.strength:.2f}) "
                              f"{'dans le sens' if favourable else 'à contre-sens'} du setup {setup.side.value}")


def analyze_event(event: CalendarEvent, setups: Iterable[TradingSetup] = (), now: datetime | None = None,
                  received_at: datetime | None = None) -> NewsAnalysis:
    """Annonce du calendrier : surprise réel vs consensus, effet sur chaque actif exposé, horizon."""
    actual, consensus = parse_value(event.actual), parse_value(event.consensus)
    notes, surprise, effects = [], None, {}
    if actual is None:
        notes.append("résultat réel pas encore publié : effet indéterminé, prudence autour de l'heure de publication")
    elif consensus is None:
        notes.append("pas de consensus publié : surprise incalculable")
    else:
        diff = actual - consensus
        relative = diff / abs(consensus) * 100 if consensus else None
        surprise = Surprise(actual=actual, consensus=consensus, difference=round(diff, 6),
                            relative_pct=round(relative, 2) if relative is not None else None,
                            direction="conforme" if diff == 0 else "au-dessus" if diff > 0 else "en dessous")
        inverse = any(word in event.title.lower() for word in INVERSE_INDICATORS)
        currency_sign = 0 if diff == 0 else (1 if diff > 0 else -1) * (-1 if inverse else 1)
        strength = min(1.0, abs(relative) / 100 / SURPRISE_FULL_STRENGTH) if relative is not None else 0.5
        for asset in event.assets:
            sign = currency_sign * _currency_sign_on(asset, event.currency)
            effects[asset] = AssetEffect(direction="haussier" if sign > 0 else "baissier" if sign < 0 else "neutre",
                                         strength=round(strength if sign else 0.0, 3))
        if inverse:
            notes.append("indicateur inversé : un chiffre plus élevé pèse sur la devise")
    title = event.title.lower()
    horizon = Horizon.DAYS if any(w in title for w in LONG_HORIZON) else Horizon.HOURS
    fact = f"{event.title} ({event.currency}, importance {event.importance.value})"
    if event.actual is not None:
        fact += f" : {event.actual} (consensus {event.consensus or '—'}, précédent {event.previous or '—'})"
    analysis = NewsAnalysis(kind="calendrier", fact=fact, published_at=event.time, source=event.source,
                            reliability=source_reliability(event.source), assets=list(event.assets),
                            surprise=surprise, effects=effects, horizon=horizon, notes=notes)
    analysis.setup_impacts = [setup_consequence(analysis, s) for s in setups]
    return classify(analysis, now, event.importance, received_at)


def analyze_article(article: Any, symbols: Iterable[str] | None = None,
                    setups: Iterable[TradingSetup] = (), now: datetime | None = None) -> NewsAnalysis:
    """Article d'actualité : sentiment orienté par actif (règles du moteur de sentiment), fiabilité de la source."""
    from sentiment.sentiment_analyzer import article_insights

    symbols = list(symbols if symbols is not None else settings.trading.symbols)
    title, snippet = getattr(article, "title", ""), getattr(article, "snippet", "")
    insights = article_insights(title, snippet, symbols)
    effects = {}
    for symbol, orientation in insights["symbols"].items():
        signed = insights["score"] * orientation
        effects[symbol] = AssetEffect(direction="haussier" if signed > 0.05 else "baissier" if signed < -0.05 else "neutre",
                                      strength=round(min(1.0, abs(signed)), 3))
    text = f"{title} {snippet}".lower()
    analysis = NewsAnalysis(kind="article", fact=title, published_at=getattr(article, "published", None),
                            source=getattr(article, "source", ""), reliability=source_reliability(getattr(article, "source", "")),
                            assets=list(insights["symbols"]), effects=effects,
                            horizon=Horizon.DAYS if any(w in text for w in LONG_HORIZON) else Horizon.HOURS,
                            notes=[f"mots-clés : {', '.join(insights['keywords'])}"] if insights["keywords"] else [])
    analysis.setup_impacts = [setup_consequence(analysis, s) for s in setups]
    return classify(analysis, now, received_at=getattr(article, "received_at", None))


_SOURCE_SUFFIX = re.compile(r"\s+[-–—|]\s+[^-–—|]{2,40}$")   # « Titre - Reuters » (agrégateurs)


def dedupe_articles(articles: Iterable[Any]) -> list[Any]:
    """
    Un communiqué repris par plusieurs sources n'est gardé qu'une fois (le premier publié) ;
    le suffixe de source ajouté par les agrégateurs (« - Reuters ») est ignoré dans la comparaison.
    """
    from sentiment.news_scraper import title_key

    kept: dict[str, Any] = {}
    for article in sorted(articles, key=lambda a: getattr(a, "published", None) or datetime.max.replace(tzinfo=timezone.utc)):
        kept.setdefault(title_key(_SOURCE_SUFFIX.sub("", getattr(article, "title", ""))), article)
    return list(kept.values())


# =============================================================================
#  Pipeline : fusion des sources et fenêtre de sécurité
# =============================================================================

class SafetyBlock(BaseModel):
    symbol: str
    reason: str
    events: list[CalendarEvent] = Field(default_factory=list)
    until: datetime | None = Field(None, description="Fin de la fenêtre de blocage (UTC)")

    @property
    def keys(self) -> tuple[str, ...]:
        return tuple(e.key for e in self.events) or ("calendar-unavailable",)


class NewsPipeline:
    """Calendrier fusionné, fenêtre de sécurité par actif, alertes dédoublonnées, analyses structurées."""

    def __init__(self, providers: Iterable[CalendarProvider], minutes_before: int | None = None,
                 minutes_after: int | None = None, symbols: Iterable[str] | None = None,
                 cache_seconds: int = CACHE_SECONDS) -> None:
        self.providers = list(providers)
        self.before = timedelta(minutes=settings.news.block_minutes_before if minutes_before is None else minutes_before)
        self.after = timedelta(minutes=settings.news.block_minutes_after if minutes_after is None else minutes_after)
        self.symbols = tuple(symbols if symbols is not None else settings.trading.symbols)
        self.cache_seconds = cache_seconds
        self._lock = threading.Lock()
        self._cache: tuple[float, list[CalendarEvent]] | None = None
        self.available = True
        self.errors: dict[str, str] = {}
        self._alerted: set[str] = set()
        self.received: dict[str, datetime] = {}          # clé de l'annonce -> première réception (UTC)

    # ------------------------------------------------------------------ calendrier fusionné

    def events(self, force: bool = False) -> list[CalendarEvent]:
        """Événements de toutes les sources, dédoublonnés et triés ; les actifs suivis exposés sont renseignés."""
        from sentiment.news_scraper import symbol_currencies

        with self._lock:
            if not force and self._cache and time.monotonic() - self._cache[0] < self.cache_seconds:
                return self._cache[1]
            merged: dict[str, CalendarEvent] = {}
            ok = 0
            for index, provider in enumerate(self.providers):
                label = f"{index + 1}. {provider.name}"            # deux sources peuvent porter le même nom
                try:
                    events = provider.fetch()
                except Exception as exc:  # noqa: BLE001 - une source en panne n'empêche pas les autres
                    self.errors[label] = str(exc)
                    log.warning("Calendrier %s indisponible : %s", provider.name, exc)
                    continue
                ok += 1
                self.errors.pop(label, None)
                for event in events:
                    current = merged.get(event.key)
                    if current is None:
                        merged[event.key] = event
                    else:                           # doublon : on complète les champs manquants
                        merged[event.key] = current.model_copy(update={
                            field: getattr(event, field) for field in ("actual", "consensus", "previous")
                            if getattr(current, field) is None and getattr(event, field) is not None})
            self.available = ok > 0 or not self.providers
            received = datetime.now(timezone.utc)
            for key, event in merged.items():                # le résultat publié est une nouvelle réception
                self.received.setdefault(f"{key}|{event.actual}", received)
            result = sorted((e.model_copy(update={"assets": tuple(s for s in self.symbols
                                                                  if e.currency in symbol_currencies(s))})
                             for e in merged.values()), key=lambda e: e.time)
            self._cache = (time.monotonic(), result)
            return result

    # ------------------------------------------------------------------ fenêtre de sécurité

    def safety_check(self, symbol: str, now: datetime | None = None) -> SafetyBlock | None:
        """Blocage si une annonce forte sur une devise de l'actif est dans [-30 min, +15 min], ou calendrier indisponible."""
        from sentiment.news_scraper import symbol_currencies

        now = now or datetime.now(timezone.utc)
        events = self.events()
        if not self.available:
            return SafetyBlock(symbol=symbol, reason="calendrier économique indisponible : nouvelles propositions "
                                                     "suspendues par précaution")
        currencies = symbol_currencies(symbol)
        hits = [e for e in events if e.importance == Importance.HIGH and e.currency in currencies
                and e.time - self.before <= now <= e.time + self.after]
        if not hits:
            return None
        until = max(e.time for e in hits) + self.after
        names = ", ".join(f"{e.title} ({e.currency}, {e.time:%H:%M} UTC)" for e in hits)
        return SafetyBlock(symbol=symbol, events=hits, until=until,
                           reason=f"annonce majeure {names} : propositions suspendues jusqu'à {until:%H:%M} UTC "
                                  f"(fenêtre -{int(self.before.total_seconds() // 60)} / "
                                  f"+{int(self.after.total_seconds() // 60)} min)")

    def first_alert(self, block: SafetyBlock) -> bool:
        """True une seule fois par annonce (ou par panne du calendrier) : pas d'alerte répétée pendant la fenêtre."""
        with self._lock:
            fresh = [k for k in block.keys if k not in self._alerted]
            self._alerted.update(block.keys)
            return bool(fresh)

    def received_at(self, event: CalendarEvent) -> datetime | None:
        return self.received.get(f"{event.key}|{event.actual}")

    def cards(self, setups: Iterable[TradingSetup] = (), now: datetime | None = None,
              hours_before: float = 6, hours_after: float = 12) -> list[NewsAnalysis]:
        """Fiches structurées des annonces suivies proches de `now` (actifs suivis exposés uniquement)."""
        now = now or datetime.now(timezone.utc)
        setups = list(setups)
        window = [e for e in self.events() if e.assets and e.importance != Importance.LOW
                  and now - timedelta(hours=hours_before) <= e.time <= now + timedelta(hours=hours_after)]
        return [analyze_event(e, [s for s in setups if s.symbol in e.assets], now, self.received_at(e))
                for e in window]

    def upcoming(self, symbol: str, now: datetime | None = None) -> list[CalendarEvent]:
        block = self.safety_check(symbol, now)
        return block.events if block else []


# =============================================================================
#  Pipeline du processus
# =============================================================================

_pipeline: NewsPipeline | None = None
_pipeline_lock = threading.Lock()


def build_providers() -> list[CalendarProvider]:
    cfg = settings.news
    if cfg.calendar_provider == "simulated":
        return [SimulatedCalendarProvider(path=cfg.calendar_file)]
    providers: list[CalendarProvider] = [ForexFactoryProvider()]
    if cfg.calendar_file is not None:                     # annonces ajoutées à la main (résultats réels...)
        providers.append(SimulatedCalendarProvider(path=cfg.calendar_file))
    return providers


def get_pipeline() -> NewsPipeline:
    global _pipeline
    with _pipeline_lock:
        if _pipeline is None:
            _pipeline = NewsPipeline(build_providers())
        return _pipeline


def set_pipeline(pipeline: NewsPipeline | None) -> None:
    global _pipeline
    with _pipeline_lock:
        _pipeline = pipeline


# Noms utilisés par le moteur (mêmes signatures que l'ancien filtre du scraper)
def is_high_impact_news_imminent(symbol: str | None = None, now: datetime | None = None) -> bool:
    pipeline = get_pipeline()
    symbols = [symbol] if symbol else list(pipeline.symbols)
    return any(pipeline.safety_check(s, now) is not None for s in symbols)


def get_upcoming_high_impact_events(symbol: str | None = None, now: datetime | None = None) -> list[CalendarEvent]:
    pipeline = get_pipeline()
    symbols = [symbol] if symbol else list(pipeline.symbols)
    return [e for s in symbols for e in pipeline.upcoming(s, now)]
