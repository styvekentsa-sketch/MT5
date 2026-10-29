"""
Passerelle multi-comptes : point d'entrée unique du dashboard (port API_PORT, 8000 par défaut).

Chaque compte tourne dans son propre processus, avec son API interne sur 127.0.0.1:810x.
La passerelle :
  - GET  /api/accounts                    : synthèse de tous les comptes (état, mode, solde, P/L, positions)
  - *    /api/accounts/{id}/api/...       : relaie n'importe quelle route de l'API du compte
                                            (statut, positions, graphique, kill-switch, mode...)
  - POST /api/webhooks/{source}           : signal TradingView diffusé à tous les comptes (journalisé)

Les routes /api exigent la clé API (en-tête X-API-Key), comme l'API d'un compte unique.
Un compte arrêté ou en démarrage n'empêche jamais l'accès aux autres (503 pour lui seul).
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any

import requests
from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from starlette.concurrency import run_in_threadpool

from api.schemas import AccountsOut, AccountSummaryOut
from api.security import require_api_key
from config.accounts import AccountSpec
from config.settings import settings
from utils.logger import get_logger

log = get_logger(__name__)

SUMMARY_TIMEOUT_SECONDS = 5
PROXY_TIMEOUT_SECONDS = 90           # /api/data-feeds peut télécharger toutes les sources au premier appel

_session = requests.Session()


def _worker_request(account: AccountSpec, method: str, path: str, *, params: Any = None, body: bytes | None = None,
                    content_type: str | None = None, timeout: float = PROXY_TIMEOUT_SECONDS) -> requests.Response:
    """Requête vers l'API interne d'un compte (même clé API que la passerelle)."""
    headers = {"X-API-Key": settings.api.api_key}
    if content_type:
        headers["Content-Type"] = content_type
    return _session.request(method, f"{account.api_url}{path}", params=params, data=body, headers=headers,
                            timeout=timeout)


def _reason(exc: requests.RequestException) -> str:
    if isinstance(exc, requests.ConnectionError):
        return "processus arrêté ou en cours de démarrage"
    if isinstance(exc, requests.Timeout):
        return "délai de réponse dépassé"
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        return f"HTTP {exc.response.status_code}"
    return type(exc).__name__


def _get_json(account: AccountSpec, path: str) -> Any:
    response = _worker_request(account, "GET", path, timeout=SUMMARY_TIMEOUT_SECONDS)
    response.raise_for_status()
    return response.json()


def account_summary(account: AccountSpec) -> AccountSummaryOut:
    """Synthèse d'un compte ; `reachable=False` (sans exception) si son processus ne répond pas."""
    base = {"id": account.id, "label": account.label, "login": account.login, "server": account.server}
    try:
        status_body = _get_json(account, "/api/status")
    except (requests.RequestException, ValueError) as exc:
        reason = _reason(exc) if isinstance(exc, requests.RequestException) else "réponse invalide"
        return AccountSummaryOut(**base, reachable=False, error=reason)

    summary: dict[str, Any] = {**base, "reachable": True, "state": status_body.get("state"),
                               "mode": status_body.get("mode"), "mt5_connected": status_body.get("mt5_connected")}
    try:
        account_body = _get_json(account, "/api/account")
        summary.update({key: account_body.get(key) for key in
                        ("currency", "balance", "equity", "daily_pl", "daily_pl_pct", "floating_pl")})
    except (requests.RequestException, ValueError) as exc:        # MT5 indisponible : synthèse partielle
        summary["error"] = f"compte MT5 indisponible ({_reason(exc) if isinstance(exc, requests.RequestException) else 'réponse invalide'})"
    try:
        positions = _get_json(account, "/api/positions?bot_only=false")
        summary["open_positions"] = len(positions)
    except (requests.RequestException, ValueError):
        pass
    return AccountSummaryOut(**summary)


def create_gateway_app(accounts: tuple[AccountSpec, ...]) -> FastAPI:
    """Application FastAPI de la passerelle pour les comptes donnés."""
    by_id = {a.id: a for a in accounts}
    app = FastAPI(
        title="MT5 Trading Bot — passerelle multi-comptes",
        version="1.0.0",
        description=("Synthèse de tous les comptes et accès aux routes de chaque compte via "
                     "`/api/accounts/{id}/api/...`. En-tête `X-API-Key` requis (bouton **Authorize**)."),
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.api.cors_origins),
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["X-API-Key", "Content-Type"],
    )

    @app.get("/health", tags=["Santé"], summary="Disponibilité de la passerelle")
    def health() -> dict[str, Any]:
        return {"status": "ok", "accounts": len(accounts)}

    @app.get("/api/accounts", response_model=AccountsOut, dependencies=[Depends(require_api_key)],
             tags=["Comptes"], summary="Synthèse de tous les comptes")
    def list_accounts() -> AccountsOut:
        """État, mode, solde, P/L du jour et positions de chaque compte (interrogés en parallèle)."""
        with ThreadPoolExecutor(max_workers=max(1, len(accounts))) as pool:
            return AccountsOut(accounts=list(pool.map(account_summary, accounts)))

    @app.api_route("/api/accounts/{account_id}/{path:path}", methods=["GET", "POST"],
                   dependencies=[Depends(require_api_key)], tags=["Comptes"], summary="Route d'un compte")
    async def proxy(account_id: str, path: str, request: Request) -> Response:
        """Relaie la requête vers l'API du compte (ex : /api/accounts/main/api/positions)."""
        account = by_id.get(account_id)
        if account is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"Compte inconnu : {account_id}")
        if not path.startswith("api/") or path.startswith("api/accounts"):
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"Route inconnue : /{path}")
        body = await request.body() if request.method == "POST" else None
        try:
            response = await run_in_threadpool(
                _worker_request, account, request.method, f"/{path}",
                params=list(request.query_params.multi_items()), body=body,
                content_type=request.headers.get("content-type"),
            )
        except requests.RequestException as exc:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                                f"Compte {account.label} injoignable : {_reason(exc)}") from exc
        return Response(content=response.content, status_code=response.status_code,
                        media_type=response.headers.get("content-type", "application/json"))

    @app.post("/api/webhooks/{source}", tags=["Webhooks"], summary="Signal entrant, diffusé à tous les comptes")
    async def broadcast_webhook(source: str, request: Request) -> Response:
        """
        Transmet le signal (et son secret) au journal de chaque compte. Accepté si au moins
        un compte l'a accepté ; jamais exécuté automatiquement.
        """
        body = await request.body()
        params = list(request.query_params.multi_items())
        content_type = request.headers.get("content-type")
        responses: list[requests.Response] = []
        for account in accounts:
            try:
                responses.append(await run_in_threadpool(
                    _worker_request, account, "POST", f"/api/webhooks/{source}",
                    params=params, body=body, content_type=content_type, timeout=SUMMARY_TIMEOUT_SECONDS))
            except requests.RequestException as exc:
                log.warning("Webhook non transmis au compte %s : %s", account.label, _reason(exc))
        accepted = [r for r in responses if r.status_code == status.HTTP_202_ACCEPTED]
        chosen = accepted[0] if accepted else (responses[0] if responses else None)
        if chosen is None:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Aucun compte disponible")
        return Response(content=chosen.content, status_code=chosen.status_code,
                        media_type=chosen.headers.get("content-type", "application/json"))

    return app
