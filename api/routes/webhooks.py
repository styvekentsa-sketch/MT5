"""
Webhooks entrants (TradingView, autres plateformes) : réception et journal des 20 derniers.

Les signaux reçus sont JOURNALISÉS UNIQUEMENT, jamais exécutés : un ordre ne passe que
par la stratégie et le gestionnaire de risque du moteur.

Authentification : TradingView ne permet pas d'en-têtes personnalisés, d'où un secret
dédié (`WEBHOOK_SECRET`, distinct de l'API_KEY) transmis soit dans l'URL
(`?token=...`), soit dans le JSON (`"passphrase"`, `"secret"` ou `"token"`).
Les champs secrets sont masqués avant stockage ; les tentatives refusées sont aussi
journalisées (utile pour diagnostiquer une alerte mal configurée).

Stockage : table SQLite `webhooks_log` (persistante). Le journal en mémoire ne sert
que de secours si la base est indisponible.
"""

from __future__ import annotations

import itertools
import json
import secrets
import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Path, Request, status

from api.schemas import WebhookAckOut, WebhookEventOut, WebhookLogOut
from api.security import require_api_key
from config.settings import settings
from utils.database import get_database
from utils.logger import get_logger
from utils.notifier import Level, notify

log = get_logger(__name__)

LOG_CAPACITY = 20
MAX_BODY_BYTES = 16 * 1024
MAX_TEXT_CHARS = 4_000               # corps non JSON conservé au plus sur 4 000 caractères
SOURCE_PATTERN = r"^[a-z0-9_-]{1,32}$"
ENDPOINT = "/api/webhooks/{source}"
SECRET_FIELDS = {"passphrase", "secret", "token", "key", "api_key", "apikey", "password"}
SYMBOL_FIELDS = ("symbol", "ticker", "instrument")
ACTION_FIELDS = ("action", "side", "signal", "order")
MASK = "***"
PAYLOAD_TOO_LARGE = 413                # littéral : le nom de la constante Starlette varie selon les versions


# =============================================================================
#  Journal en mémoire
# =============================================================================

@dataclass(frozen=True)
class WebhookEvent:
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


class WebhookLog:
    """Derniers webhooks reçus (taille bornée, thread-safe)."""

    def __init__(self, capacity: int = LOG_CAPACITY) -> None:
        self._events: deque[WebhookEvent] = deque(maxlen=capacity)
        self._lock = threading.Lock()
        self._ids = itertools.count(1)
        self.capacity = capacity

    def add(self, **fields: Any) -> WebhookEvent:
        with self._lock:
            event = WebhookEvent(id=next(self._ids), received_at=datetime.now(timezone.utc), **fields)
            self._events.append(event)
            return event

    def events(self) -> list[WebhookEvent]:
        """Du plus récent au plus ancien."""
        with self._lock:
            return list(reversed(self._events))

    def clear(self) -> None:
        with self._lock:
            self._events.clear()


webhook_log = WebhookLog()     # secours si la base SQLite est indisponible


def _record(**fields: Any) -> WebhookEvent:
    """Enregistre le webhook en base ; en cas d'échec, dans le journal en mémoire."""
    received_at = datetime.now(timezone.utc)
    webhook_id = get_database().insert_webhook(received_at=received_at, **fields)
    if webhook_id is None:
        return webhook_log.add(**fields)
    return WebhookEvent(id=webhook_id, received_at=received_at, **fields)


def recent_events(limit: int = LOG_CAPACITY) -> list[WebhookEvent]:
    """Derniers webhooks, du plus récent au plus ancien (base, sinon mémoire)."""
    db = get_database()
    if not db.available:
        return webhook_log.events()[:limit]
    items, _ = db.list_webhooks(limit=limit)
    return [WebhookEvent(**{**item, "received_at": datetime.fromisoformat(item["received_at"])}) for item in items]


def webhook_stats() -> tuple[datetime | None, int]:
    """(date du dernier webhook reçu, nombre total en historique)."""
    db = get_database()
    if not db.available:
        events = webhook_log.events()
        return (events[0].received_at if events else None), len(events)
    items, total = db.list_webhooks(limit=1)
    return (datetime.fromisoformat(items[0]["received_at"]) if items else None), total


# =============================================================================
#  Helpers
# =============================================================================

def _parse_body(raw: bytes) -> Any:
    """JSON si possible, sinon texte brut tronqué (TradingView envoie du text/plain hors JSON)."""
    text = raw.decode("utf-8", errors="replace")
    try:
        return json.loads(text)
    except ValueError:
        return text[:MAX_TEXT_CHARS]


def _redact(payload: Any) -> Any:
    if isinstance(payload, dict):
        return {k: MASK if str(k).lower() in SECRET_FIELDS else _redact(v) for k, v in payload.items()}
    if isinstance(payload, list):
        return [_redact(v) for v in payload]
    return payload


def _first_field(payload: Any, fields: tuple[str, ...]) -> str | None:
    if not isinstance(payload, dict):
        return None
    lowered = {str(k).lower(): v for k, v in payload.items()}
    for name in fields:
        value = lowered.get(name)
        if isinstance(value, (str, int, float)) and not isinstance(value, bool) and str(value).strip():
            return str(value).strip().upper()[:32]
    return None


def _provided_secret(request: Request, payload: Any) -> str | None:
    token = request.query_params.get("token")
    if token:
        return token
    if isinstance(payload, dict):
        for name in ("passphrase", "secret", "token"):
            value = payload.get(name)
            if isinstance(value, str) and value:
                return value
    return None


async def _read_limited_body(request: Request) -> bytes:
    """Lit le corps en refusant tout dépassement de MAX_BODY_BYTES (413)."""
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        raise HTTPException(PAYLOAD_TOO_LARGE, f"Corps limité à {MAX_BODY_BYTES} octets")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_BODY_BYTES:
            raise HTTPException(PAYLOAD_TOO_LARGE, f"Corps limité à {MAX_BODY_BYTES} octets")
    return bytes(body)


# =============================================================================
#  Routes
# =============================================================================

receiver = APIRouter(prefix="/api/webhooks", tags=["Webhooks"])
journal = APIRouter(prefix="/api/webhooks", tags=["Webhooks"], dependencies=[Depends(require_api_key)])


@receiver.post(
    "/{source}",
    response_model=WebhookAckOut,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Recevoir un signal (TradingView...)",
    responses={401: {"description": "Secret absent ou invalide"}, 413: {"description": "Corps trop volumineux"},
               503: {"description": "Réception désactivée (WEBHOOK_SECRET vide)"}},
)
async def receive_webhook(
    request: Request,
    source: str = Path(pattern=SOURCE_PATTERN, description="Nom de la source, ex : tradingview"),
) -> WebhookAckOut:
    """
    Journalise un signal entrant. Authentification par `?token=<WEBHOOK_SECRET>` ou champ
    `passphrase` / `secret` / `token` du JSON. **Le signal n'est jamais exécuté.**
    """
    raw = await _read_limited_body(request)
    payload = _parse_body(raw)
    fields = {
        "source": source,
        "symbol": _first_field(payload, SYMBOL_FIELDS),
        "action": _first_field(payload, ACTION_FIELDS),
        "client": request.client.host if request.client else "inconnu",
        "size_bytes": len(raw),
        "payload": _redact(payload),
    }

    expected = settings.api.webhook_secret
    if not expected:
        _record(accepted=False, reason="réception désactivée (WEBHOOK_SECRET vide)", **fields)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Réception de webhooks désactivée")

    provided = _provided_secret(request, payload)
    if not provided or not secrets.compare_digest(provided.encode(), expected.encode()):
        _record(accepted=False, reason="secret absent ou invalide", **fields)
        log.warning("Webhook %s refusé depuis %s : secret absent ou invalide", source, fields["client"])
        notify(f"Webhook {source} rejeté", f"Secret absent ou invalide · origine {fields['client']}",
               Level.WARNING, dedup_key=f"webhook-rejected:{source}:{fields['client']}")
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Secret de webhook absent ou invalide")

    event = _record(accepted=True, reason="journalisé (non exécuté)", **fields)
    log.info("Webhook %s #%d reçu : %s %s", source, event.id, event.action or "?", event.symbol or "?")
    notify(f"Webhook {source} reçu", f"Signal #{event.id} : {event.action or '?'} {event.symbol or '?'} "
                                     "(journalisé, non exécuté)")
    return WebhookAckOut(accepted=True, id=event.id,
                         message="Signal journalisé : aucun ordre n'est passé automatiquement")


@journal.get("/events", response_model=WebhookLogOut, summary="Derniers webhooks reçus")
def list_webhook_events() -> WebhookLogOut:
    """Les 20 derniers webhooks (acceptés et refusés), du plus récent au plus ancien, secrets masqués."""
    return WebhookLogOut(
        enabled=settings.api.webhooks_enabled,
        endpoint=ENDPOINT,
        capacity=webhook_log.capacity,
        events=[WebhookEventOut(**e.__dict__) for e in recent_events(webhook_log.capacity)],
    )
