"""
Routes de lecture : statut du bot, compte, positions ouvertes, sentiment des actualités,
données ingérées (flux RSS, calendrier économique, webhooks).

Les routes sont synchrones (`def`) : FastAPI les exécute dans un pool de threads,
les appels bloquants à MT5 ou au réseau ne gèlent donc pas le serveur.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Query, status

from api.dependencies import build_status, get_engine
from api.routes.webhooks import ENDPOINT as WEBHOOK_ENDPOINT
from api.routes.webhooks import webhook_stats
from api.schemas import (
    AccountOut,
    ArticleOut,
    CalendarEventOut,
    DataFeedsOut,
    FeedSourceOut,
    NewsItemOut,
    PositionOut,
    SentimentOut,
    StatusOut,
)
from api.security import require_api_key
from brokers.mt5_client import get_account_info, get_closed_deals, get_open_positions
from config.settings import settings
from core.engine import TradingEngine
from sentiment.news_scraper import (
    NEWS_BLOCK_MINUTES_AFTER,
    NEWS_BLOCK_MINUTES_BEFORE,
    EconomicEvent,
    NewsArticle,
    get_calendar_status,
    feed_names,
    get_feed_statuses,
    get_upcoming_events,
    symbol_currencies,
)
from sentiment.sentiment_analyzer import (
    analyze_article,
    analyze_news_items,
    article_insights,
    classify,
)
from sentiment.sentiment_analyzer import get_engine as get_sentiment_engine
from strategies.strategy_manager import get_news_fetched_at, get_recent_news

router = APIRouter(prefix="/api", tags=["Monitoring"], dependencies=[Depends(require_api_key)])

POSITION_TYPE_BUY = 0                # valeur de mt5.POSITION_TYPE_BUY
CALENDAR_HOURS_BACK = 2              # annonces passées conservées dans la timeline


@router.get("/status", response_model=StatusOut, summary="Statut du bot")
def get_status(engine: TradingEngine = Depends(get_engine)) -> StatusOut:
    """État de la boucle (running / paused / stopped), mode PAPER/LIVE, connexion MT5 et dernier tour."""
    return build_status(engine)


@router.get("/account", response_model=AccountOut, summary="Métriques du compte MT5")
def get_account() -> AccountOut:
    """Solde, fonds propres, marge et P/L du jour (réalisé depuis 00:00 UTC + latent)."""
    account = get_account_info()
    if account is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Informations du compte indisponibles")

    start_of_day = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    realized = sum(d.net_profit for d in get_closed_deals(start_of_day))
    daily_pl = realized + account.profit
    start_balance = account.balance - realized
    return AccountOut(
        login=account.login,
        currency=account.currency,
        balance=account.balance,
        equity=account.equity,
        margin=account.margin,
        margin_free=account.margin_free,
        margin_level=account.margin_level,
        leverage=account.leverage,
        floating_pl=round(account.profit, 2),
        realized_pl_today=round(realized, 2),
        daily_pl=round(daily_pl, 2),
        daily_pl_pct=round(daily_pl / start_balance * 100, 3) if start_balance > 0 else 0.0,
    )


@router.get("/positions", response_model=list[PositionOut], summary="Positions ouvertes")
def get_positions(
    bot_only: bool = Query(False, description="Ne retourner que les positions ouvertes par le bot"),
) -> list[PositionOut]:
    """Positions ouvertes sur le compte, avec prix, SL/TP et profit en cours."""
    return [
        PositionOut(
            ticket=p.ticket,
            symbol=p.symbol,
            type="BUY" if p.type == POSITION_TYPE_BUY else "SELL",
            volume=p.volume,
            price_open=p.price_open,
            price_current=p.price_current,
            sl=p.sl or None,                 # MT5 renvoie 0.0 quand aucun SL n'est défini
            tp=p.tp or None,
            profit=p.profit,
            swap=p.swap,
            opened_at=datetime.fromtimestamp(p.time, tz=timezone.utc),
            magic=p.magic,
            is_bot=p.magic == settings.trading.magic_number,
            comment=p.comment,
        )
        for p in get_open_positions(bot_only=bot_only)
    ]


@router.get("/sentiment", response_model=SentimentOut, summary="Actualités et sentiment")
def get_sentiment(
    limit: int = Query(50, ge=1, le=200, description="Nombre maximal d'articles retournés"),
) -> SentimentOut:
    """
    Dernières actualités (cache partagé avec la stratégie), score de chaque article,
    sentiment global et sentiment orienté pour chaque symbole tradé.
    """
    articles = get_recent_news()
    global_score = analyze_news_items(articles)
    return SentimentOut(
        engine=get_sentiment_engine().name,
        global_score=global_score,
        global_label=classify(global_score),
        by_symbol={s: analyze_news_items(articles, symbol=s) for s in settings.trading.symbols},
        article_count=len(articles),
        articles=[_article_out(a) for a in articles[:limit]],
    )


def _article_out(article: NewsArticle) -> ArticleOut:
    sentiment = analyze_article(article.title, article.snippet)
    return ArticleOut(
        title=article.title,
        source=article.source,
        link=article.link,
        published=article.published,
        snippet=article.snippet,
        score=sentiment["score"],
        label=sentiment["label"],
    )


# =============================================================================
#  Données ingérées (Data & Scraper Monitor)
# =============================================================================

@router.get("/data-feeds", response_model=DataFeedsOut, summary="Données lues par le bot")
def get_data_feeds(
    limit: int = Query(100, ge=1, le=300, description="Nombre maximal d'articles"),
    hours_ahead: int = Query(24, ge=1, le=168, description="Horizon du calendrier économique (heures)"),
) -> DataFeedsOut:
    """
    Traçabilité de tout ce que le bot lit : articles RSS (mots-clés, score, symboles concernés),
    annonces économiques à fort impact (statut vis-à-vis du blocage des trades) et état de
    chaque source (dernière et prochaine lecture).
    """
    now = datetime.now(timezone.utc)
    symbols = list(settings.trading.symbols)
    articles = get_recent_news()
    fetched_at = get_news_fetched_at()
    calendar = [_calendar_item(e, now, symbols)
                for e in get_upcoming_events(hours_ahead=hours_ahead, hours_back=CALENDAR_HOURS_BACK)]
    return DataFeedsOut(
        generated_at=now,
        tracked_symbols=symbols,
        block_window_minutes=(NEWS_BLOCK_MINUTES_BEFORE, NEWS_BLOCK_MINUTES_AFTER),
        sources=_sources(fetched_at),
        news=[_news_item(a, fetched_at, symbols) for a in articles[:limit]],
        calendar=calendar,
    )


def _news_item(article: NewsArticle, fetched_at: datetime | None, symbols: list[str]) -> NewsItemOut:
    return NewsItemOut(
        title=article.title,
        source=article.source,
        link=article.link,
        published=article.published,
        fetched_at=fetched_at,
        snippet=article.snippet,
        **article_insights(article.title, article.snippet, symbols),
    )


def _calendar_item(event: EconomicEvent, now: datetime, symbols: list[str]) -> CalendarEventOut:
    """Statut identique à la règle du moteur (`is_high_impact_news_imminent`)."""
    minutes = (event.time - now).total_seconds() / 60
    if -NEWS_BLOCK_MINUTES_AFTER <= minutes <= NEWS_BLOCK_MINUTES_BEFORE:
        event_status = "imminent"
    elif minutes > 0:
        event_status = "upcoming"
    else:
        event_status = "passed"
    exposed = [s for s in symbols if event.currency in symbol_currencies(s)]
    return CalendarEventOut(
        title=event.title,
        currency=event.currency,
        time=event.time,
        impact=event.impact,
        forecast=event.forecast,
        previous=event.previous,
        status=event_status,
        blocks_trading=event_status == "imminent" and event.impact == "High" and bool(exposed),
        symbols=exposed,
    )


def _sources(news_fetched_at: datetime | None) -> list[FeedSourceOut]:
    """État de chaque source : flux RSS configurés, calendrier économique, webhooks entrants."""
    sources: list[FeedSourceOut] = []
    next_news = (news_fetched_at + timedelta(seconds=settings.news.refresh_interval_seconds)
                 if news_fetched_at else None)
    names = feed_names()
    for url, feed in get_feed_statuses().items():
        name = names.get(url) or urlparse(url).netloc.removeprefix("www.") or url
        if feed is None:
            sources.append(FeedSourceOut(name=name, kind="rss", url=url, status="pending", last_fetch=None,
                                         next_fetch=next_news, items=0, detail="pas encore lu"))
        else:
            sources.append(FeedSourceOut(name=name, kind="rss", url=url, status="ok" if feed.ok else "error",
                                         last_fetch=feed.last_fetch, next_fetch=next_news,
                                         items=feed.article_count, detail=feed.error))

    calendar = get_calendar_status()
    last_calendar = calendar["last_fetch"]
    sources.append(FeedSourceOut(
        name="Calendrier économique (Forex Factory)",
        kind="calendar",
        url=calendar["url"],
        status="ok" if calendar["available"] else "error",
        last_fetch=last_calendar,
        next_fetch=last_calendar + timedelta(seconds=calendar["ttl_seconds"]) if last_calendar else None,
        items=calendar["event_count"],
        detail="" if calendar["available"] else "indisponible : nouveaux trades bloqués par précaution",
    ))

    last_received, total_webhooks = webhook_stats()
    sources.append(FeedSourceOut(
        name="Webhooks entrants",
        kind="webhook",
        url=None,
        status="ok" if settings.api.webhooks_enabled else "disabled",
        last_fetch=last_received,
        next_fetch=None,
        items=total_webhooks,
        detail=WEBHOOK_ENDPOINT if settings.api.webhooks_enabled else "WEBHOOK_SECRET non configuré",
    ))
    return sources
