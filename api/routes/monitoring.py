"""
Routes de lecture : statut du bot, compte, positions ouvertes, sentiment des actualités.

Les routes sont synchrones (`def`) : FastAPI les exécute dans un pool de threads,
les appels bloquants à MT5 ou au réseau ne gèlent donc pas le serveur.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status

from api.dependencies import build_status, get_engine
from api.schemas import AccountOut, ArticleOut, PositionOut, SentimentOut, StatusOut
from api.security import require_api_key
from brokers.mt5_client import get_account_info, get_closed_deals, get_open_positions
from config.settings import settings
from core.engine import TradingEngine
from sentiment.news_scraper import NewsArticle
from sentiment.sentiment_analyzer import analyze_article, analyze_news_items, classify
from sentiment.sentiment_analyzer import get_engine as get_sentiment_engine
from strategies.strategy_manager import get_recent_news

router = APIRouter(prefix="/api", tags=["Monitoring"], dependencies=[Depends(require_api_key)])

POSITION_TYPE_BUY = 0                # valeur de mt5.POSITION_TYPE_BUY


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
