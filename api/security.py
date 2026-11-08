"""
Authentification par clé API (en-tête `X-API-Key`), exigée sur toutes les routes `/api`.

Deux niveaux d'accès :
  - API_KEY        : accès complet (lecture, contrôle du moteur, ordres, clôtures)
  - API_VIEWER_KEY : lecture seule (GET / HEAD) ; toute action est refusée (403). À donner à une
                     personne qui suit le bot sans devoir pouvoir trader.

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
READ_METHODS = ("GET", "HEAD", "OPTIONS")

api_key_header = APIKeyHeader(
    name=API_KEY_HEADER,
    auto_error=False,
    description="Clé définie par API_KEY dans le fichier .env",
)


def _matches(given: str, expected: str) -> bool:
    return bool(expected) and secrets.compare_digest(given.encode(), expected.encode())


def access_level(request: Request) -> str:
    """« complet » ou « lecture » pour la requête authentifiée."""
    return getattr(request.state, "access", "complet")


def require_api_key(request: Request, api_key: str | None = Security(api_key_header)) -> None:
    """
    Dépendance FastAPI : 401 si la clé est absente ou invalide, 403 pour une action avec la clé en
    lecture seule, 503 si aucune clé n'est configurée.
    """
    expected = settings.api.api_key
    if not expected:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "API désactivée : API_KEY non configurée")
    if api_key and _matches(api_key, expected):
        request.state.access = "complet"
        return
    if api_key and _matches(api_key, settings.api.viewer_key):
        request.state.access = "lecture"
        if request.method not in READ_METHODS:
            log.warning("Action refusée (%s %s) : clé en lecture seule", request.method, request.url.path)
            raise HTTPException(status.HTTP_403_FORBIDDEN,
                                "Accès en lecture seule : cette action demande la clé API complète")
        return
    client = request.client.host if request.client else "inconnu"
    log.warning("Accès API refusé (%s %s) depuis %s : clé invalide ou absente",
                request.method, request.url.path, client)
    raise HTTPException(
        status.HTTP_401_UNAUTHORIZED,
        "Clé API invalide ou absente",
        headers={"WWW-Authenticate": "ApiKey"},
    )
