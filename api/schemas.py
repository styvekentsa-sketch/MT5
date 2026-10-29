"""Modèles Pydantic des requêtes et réponses de l'API (documentés dans Swagger / OpenAPI)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

Mode = Literal["PAPER", "LIVE"]


# =============================================================================
#  Monitoring
# =============================================================================

class OutcomeOut(BaseModel):
    symbol: str
    status: str = Field(description="traded, hold, news_blocked, cooldown, rejected, failed ou error")
    detail: str = ""


class StatusOut(BaseModel):
    state: Literal["running", "paused", "stopped"] = Field(
        description="running = boucle active, paused = boucle active sans nouveaux trades, stopped = boucle arrêtée")
    mode: Mode
    mt5_connected: bool
    symbols: list[str]
    timeframe: str
    loop_interval_seconds: int
    started_at: datetime | None
    cycle_count: int
    last_cycle_at: datetime | None
    last_cycle_duration_seconds: float | None
    last_outcomes: list[OutcomeOut]


class AccountOut(BaseModel):
    login: int
    currency: str
    balance: float
    equity: float
    margin: float
    margin_free: float
    margin_level: float = Field(description="Niveau de marge en % (0 si aucune position)")
    leverage: int
    floating_pl: float = Field(description="P/L latent des positions ouvertes")
    realized_pl_today: float = Field(description="P/L réalisé depuis 00:00 UTC (commissions et swaps inclus)")
    daily_pl: float = Field(description="P/L du jour : réalisé + latent")
    daily_pl_pct: float = Field(description="P/L du jour en % du solde de début de journée")


class PositionOut(BaseModel):
    ticket: int
    symbol: str
    type: Literal["BUY", "SELL"]
    volume: float
    price_open: float
    price_current: float
    sl: float | None
    tp: float | None
    profit: float
    swap: float
    opened_at: datetime
    magic: int
    is_bot: bool = Field(description="True si la position a été ouverte par ce bot (magic number)")
    comment: str


class ArticleOut(BaseModel):
    title: str
    source: str
    link: str
    published: datetime | None
    snippet: str
    score: float = Field(ge=-1, le=1)
    label: Literal["bullish", "bearish", "neutral"]


class SentimentOut(BaseModel):
    engine: str
    global_score: float = Field(ge=-1, le=1, description="Ton moyen pondéré des actualités sur les actifs suivis")
    global_label: Literal["bullish", "bearish", "neutral"]
    by_symbol: dict[str, float] = Field(description="Sentiment orienté par symbole (> 0 = haussier pour la paire)")
    article_count: int
    articles: list[ArticleOut]


# =============================================================================
#  Contrôle
# =============================================================================

class ControlOut(BaseModel):
    success: bool
    message: str
    status: StatusOut


class ModeIn(BaseModel):
    dry_run: bool = Field(description="True = PAPER (ordres simulés), False = LIVE (ordres réels)")
    confirm: str | None = Field(
        default=None, description="Obligatoire et égal à 'LIVE' pour passer en mode LIVE")


class ModeOut(BaseModel):
    mode: Mode
    message: str


class KillSwitchIn(BaseModel):
    include_manual: bool = Field(
        default=False, description="Fermer aussi les positions qui n'ont pas été ouvertes par le bot")
    pause_engine: bool = Field(default=True, description="Mettre le moteur en pause avant les clôtures")


class ClosedPositionOut(BaseModel):
    ticket: int
    symbol: str
    success: bool
    message: str


class KillSwitchOut(BaseModel):
    closed: int
    failed: int
    engine_paused: bool
    results: list[ClosedPositionOut]


# =============================================================================
#  Données ingérées (Data Feeds)
# =============================================================================

class FeedSourceOut(BaseModel):
    name: str
    kind: Literal["rss", "calendar", "webhook"]
    url: str | None = Field(description="Adresse lue par le bot (None pour les webhooks entrants)")
    status: Literal["ok", "error", "pending", "disabled"]
    last_fetch: datetime | None
    next_fetch: datetime | None = Field(description="Prochaine lecture prévue (cache côté bot)")
    items: int
    detail: str = ""


class NewsItemOut(BaseModel):
    title: str
    source: str
    link: str
    published: datetime | None
    fetched_at: datetime | None = Field(description="Heure d'extraction par le bot")
    snippet: str
    score: float = Field(ge=-1, le=1)
    label: Literal["bullish", "bearish", "neutral"]
    keywords: list[str] = Field(description="Termes du lexique financier détectés")
    symbols: dict[str, float] = Field(
        description="Symboles suivis concernés et sens de l'impact (+1 base, -1 cotation, 1.5 paire citée)")


class CalendarEventOut(BaseModel):
    title: str
    currency: str
    time: datetime
    impact: str
    forecast: str
    previous: str
    status: Literal["imminent", "upcoming", "passed"] = Field(
        description="imminent = dans la fenêtre de blocage des nouveaux trades")
    blocks_trading: bool
    symbols: list[str] = Field(description="Symboles tradés exposés à cette annonce")


class DataFeedsOut(BaseModel):
    generated_at: datetime
    tracked_symbols: list[str]
    block_window_minutes: tuple[int, int] = Field(description="(avant, après) l'annonce")
    sources: list[FeedSourceOut]
    news: list[NewsItemOut]
    calendar: list[CalendarEventOut]


# =============================================================================
#  Webhooks entrants
# =============================================================================

class WebhookEventOut(BaseModel):
    id: int
    received_at: datetime
    source: str
    symbol: str | None
    action: str | None
    accepted: bool
    reason: str
    client: str
    size_bytes: int
    payload: Any = Field(description="Corps reçu (JSON ou texte brut), secrets masqués")


class WebhookLogOut(BaseModel):
    enabled: bool = Field(description="False si WEBHOOK_SECRET n'est pas configuré")
    endpoint: str = Field(description="Chemin de réception, ex : /api/webhooks/{source}")
    capacity: int
    events: list[WebhookEventOut]


class WebhookAckOut(BaseModel):
    accepted: bool
    id: int
    message: str


# =============================================================================
#  Historique (SQLite)
# =============================================================================

class WebhookHistoryItem(BaseModel):
    id: int
    received_at: datetime
    source: str
    symbol: str | None
    action: str | None
    accepted: bool
    reason: str
    client: str
    size_bytes: int
    payload: Any


class DecisionHistoryItem(BaseModel):
    id: int
    decided_at: datetime
    cycle: int | None
    symbol: str
    status: str = Field(description="traded, hold, news_blocked, cooldown, rejected, failed ou error")
    action: str | None
    confidence: int | None
    technical_score: float | None
    sentiment_score: float | None
    hybrid_score: float | None
    entry_price: float | None
    sl: float | None
    tp: float | None
    volume: float | None
    ticket: int | None
    dry_run: bool | None
    detail: str


class ArticleHistoryItem(BaseModel):
    id: int
    link: str
    title: str
    source: str
    published_at: datetime | None
    first_seen_at: datetime
    last_seen_at: datetime
    snippet: str
    score: float
    label: str
    keywords: list[str]
    symbols: dict[str, float]


class HistoryPage(BaseModel):
    total: int = Field(description="Nombre total d'éléments correspondant aux filtres")
    limit: int
    offset: int


class WebhookHistoryOut(HistoryPage):
    items: list[WebhookHistoryItem]


class DecisionHistoryOut(HistoryPage):
    items: list[DecisionHistoryItem]


class ArticleHistoryOut(HistoryPage):
    items: list[ArticleHistoryItem]


# =============================================================================
#  Marché (graphique)
# =============================================================================

class CandleOut(BaseModel):
    time: int = Field(description="Ouverture de la bougie, secondes Unix (heure serveur du broker)")
    open: float
    high: float
    low: float
    close: float
    volume: float


class CandlesOut(BaseModel):
    symbol: str
    timeframe: str
    digits: int = Field(description="Nombre de décimales du prix")
    bid: float | None
    ask: float | None
    candles: list[CandleOut] = Field(description="Ordre chronologique ; la dernière bougie est en cours")


# =============================================================================
#  Multi-comptes (passerelle)
# =============================================================================

class AccountSummaryOut(BaseModel):
    id: str
    label: str
    login: int
    server: str
    reachable: bool = Field(description="False si le processus du compte ne répond pas")
    error: str = ""
    state: Literal["running", "paused", "stopped"] | None = None
    mode: Mode | None = None
    mt5_connected: bool | None = None
    currency: str | None = None
    balance: float | None = None
    equity: float | None = None
    daily_pl: float | None = None
    daily_pl_pct: float | None = None
    floating_pl: float | None = None
    open_positions: int | None = None


class AccountsOut(BaseModel):
    accounts: list[AccountSummaryOut]
