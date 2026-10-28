"""Modèles Pydantic des requêtes et réponses de l'API (documentés dans Swagger / OpenAPI)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

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
