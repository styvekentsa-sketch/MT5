"""
Application FastAPI du dashboard et serveur uvicorn en thread d'arrière-plan.

Le moteur de trading tourne dans le thread principal (il gère les signaux Ctrl+C) ;
l'API tourne dans un thread démon et partage l'instance `TradingEngine`.

Documentation interactive : http://127.0.0.1:8000/docs (Swagger) et /redoc.

Usage :
    from api.main import start_api_server

    engine = TradingEngine()
    api = start_api_server(engine)      # None si API_ENABLED=false
    engine.run()
    if api:
        api.stop()
"""

from __future__ import annotations

import threading

import uvicorn
from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from api.routes import control, monitoring
from brokers.mt5_client import MT5ConnectionError
from config.settings import settings
from core.engine import TradingEngine
from utils.logger import get_logger

log = get_logger(__name__)

API_VERSION = "1.0.0"
SHUTDOWN_TIMEOUT_SECONDS = 5

OPENAPI_TAGS = [
    {"name": "Monitoring", "description": "Lecture de l'état du bot, du compte, des positions et du sentiment."},
    {"name": "Contrôle", "description": "Actions sur le bot : pause, reprise, kill-switch, mode PAPER/LIVE."},
    {"name": "Santé", "description": "Vérification de disponibilité, sans authentification."},
]


def create_app(engine: TradingEngine | None = None) -> FastAPI:
    """Construit l'application FastAPI, attachée au moteur `engine`."""
    app = FastAPI(
        title="MT5 Trading Bot API",
        version=API_VERSION,
        description=(
            "Monitoring et contrôle du bot de trading MetaTrader 5.\n\n"
            "Toutes les routes `/api` exigent l'en-tête `X-API-Key` (bouton **Authorize**)."
        ),
        openapi_tags=OPENAPI_TAGS,
    )
    app.state.engine = engine

    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.api.cors_origins),
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["X-API-Key", "Content-Type"],
    )
    app.include_router(monitoring.router)
    app.include_router(control.router)

    @app.exception_handler(MT5ConnectionError)
    async def mt5_unavailable(_request: Request, exc: MT5ConnectionError) -> JSONResponse:
        log.error("Requête API impossible : %s", exc)
        return JSONResponse(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            content={"detail": f"MT5 indisponible : {exc}"})

    @app.get("/health", tags=["Santé"], summary="Disponibilité de l'API")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


class _BackgroundServer(uvicorn.Server):
    """Serveur uvicorn qui laisse la gestion des signaux au moteur (thread principal)."""

    def install_signal_handlers(self) -> None:  # uvicorn < 0.29 ; ignoré hors thread principal ensuite
        pass


class ApiServer:
    """Serveur API exécuté dans un thread démon."""

    def __init__(self, engine: TradingEngine) -> None:
        config = uvicorn.Config(
            create_app(engine),
            host=settings.api.host,
            port=settings.api.port,
            log_config=None,             # les logs uvicorn passent par utils.logger
            access_log=False,
        )
        self._server = _BackgroundServer(config)
        self._thread = threading.Thread(target=self._server.run, name="api-server", daemon=True)

    def start(self) -> None:
        self._thread.start()
        log.info("API démarrée sur http://%s:%d (documentation : /docs)", settings.api.host, settings.api.port)

    def stop(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=SHUTDOWN_TIMEOUT_SECONDS)
        log.info("API arrêtée")


def start_api_server(engine: TradingEngine) -> ApiServer | None:
    """Démarre l'API si `API_ENABLED=true` ; retourne None sinon ou en cas d'échec."""
    if not settings.api.enabled:
        log.info("API désactivée (API_ENABLED=false)")
        return None
    if not settings.api.is_local_only:
        log.warning("API exposée sur %s sans HTTPS : la clé API circule en clair sur le réseau. "
                    "Utiliser 127.0.0.1 ou un reverse proxy HTTPS.", settings.api.host)
    try:
        server = ApiServer(engine)
        server.start()
        return server
    except Exception:  # noqa: BLE001 - l'API ne doit pas empêcher le bot de trader
        log.exception("Impossible de démarrer l'API : le bot continue sans dashboard")
        return None
