"""Dépendances partagées par les routes : accès au moteur, au broker et construction du statut."""

from __future__ import annotations

from fastapi import HTTPException, Request, status

from api.schemas import OutcomeOut, StatusOut
from brokers.base import BaseBroker, is_dry_run
from brokers.factory import get_broker
from config.settings import settings
from core.engine import TradingEngine
from utils.logger import get_logger

log = get_logger(__name__)


def get_engine(request: Request) -> TradingEngine:
    """Moteur attaché à l'application par `create_app(engine)` ; 503 s'il n'y en a pas."""
    engine = getattr(request.app.state, "engine", None)
    if engine is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Aucun moteur de trading attaché à l'API")
    return engine


def get_active_broker(request: Request) -> BaseBroker:
    """Broker du moteur attaché à l'API (sinon celui du processus, cf. BROKER_TYPE)."""
    engine = getattr(request.app.state, "engine", None)
    return engine.broker if engine is not None else get_broker()


def build_status(engine: TradingEngine) -> StatusOut:
    """Instantané de l'état du moteur et de la connexion au broker."""
    broker = engine.broker
    try:
        connected = broker.is_connected()
    except Exception as exc:  # noqa: BLE001 - le statut doit toujours répondre
        log.warning("État de la connexion %s indisponible : %s", broker.name, exc)
        connected = False

    if not engine.is_running:
        state = "stopped"
    elif engine.is_paused:
        state = "paused"
    else:
        state = "running"

    return StatusOut(
        state=state,
        mode="PAPER" if is_dry_run() else "LIVE",
        mt5_connected=connected,
        broker=broker.name,
        broker_description=broker.description,
        market=broker.market,
        symbols=list(engine.symbols),
        timeframe=settings.trading.timeframe,
        loop_interval_seconds=engine.interval_seconds,
        started_at=engine.started_at,
        cycle_count=engine.cycle_count,
        last_cycle_at=engine.last_cycle_at,
        last_cycle_duration_seconds=engine.last_cycle_duration,
        last_outcomes=[OutcomeOut(symbol=o.symbol, status=o.status, detail=o.detail) for o in engine.last_outcomes],
    )
