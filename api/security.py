"""
Authentification par clé API (en-tête `X-API-Key`), exigée sur toutes les routes `/api`.

La comparaison est faite en temps constant (`secrets.compare_digest`) et chaque
tentative refusée est loguée avec l'adresse du client.
"""

from __future__ import annotations

import secrets

from fastapi import HTTPException, Request, Security, status
from fastapi.security import APIKeyHeader

from config.settings import settings
from utils.logger import get_logger

log = get_logger(__name__)

API_KEY_HEADER = "X-API-Key"

api_key_header = APIKeyHeader(
    name=API_KEY_HEADER,
    auto_error=False,
    description="Clé définie par API_KEY dans le fichier .env",
)


def require_api_key(request: Request, api_key: str | None = Security(api_key_header)) -> None:
    """Dépendance FastAPI : 401 si la clé est absente ou invalide, 503 si aucune clé n'est configurée."""
    expected = settings.api.api_key
    if not expected:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "API désactivée : API_KEY non configurée")
    if not api_key or not secrets.compare_digest(api_key.encode(), expected.encode()):
        client = request.client.host if request.client else "inconnu"
        log.warning("Accès API refusé (%s %s) depuis %s : clé invalide ou absente",
                    request.method, request.url.path, client)
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Clé API invalide ou absente",
            headers={"WWW-Authenticate": "ApiKey"},
        )
