"""
Erreurs des brokers et journal central des incidents.

Toute erreur d'un broker (MT5 ou exchange ccxt) passe par `record_error()` :
  - classée (réseau, authentification, limite de requêtes, fonds insuffisants, ordre invalide...)
  - écrite dans `logs/broker_errors.log` (en plus des logs habituels), avec le broker et l'opération
  - conservée en mémoire (derniers incidents) pour l'API de monitoring

Les pertes et retours de connexion sont signalés une seule fois par transition (log + alerte),
et jamais propagés hors du broker concerné : chaque compte tourne dans son propre processus,
une panne d'exchange n'affecte donc pas les autres comptes.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from config import settings
from utils.logger import _rotating_file, get_logger

ErrorKind = Literal["network", "auth", "rate_limit", "insufficient_funds", "invalid_order", "exchange", "unknown"]

MAX_RECORDS = 200
ERRORS_LOG_FILE = "broker_errors.log"


class BrokerError(RuntimeError):
    """Erreur d'un broker (données indisponibles, ordre refusé par l'exchange...)."""


class BrokerConnectionError(BrokerError):
    """La connexion au broker / à l'exchange ne peut pas être (r)établie."""


@dataclass(frozen=True)
class BrokerErrorRecord:
    time: datetime
    broker: str
    operation: str
    kind: ErrorKind
    message: str


_lock = threading.Lock()
_records: deque[BrokerErrorRecord] = deque(maxlen=MAX_RECORDS)
_journal: logging.Logger | None = None


def _get_journal() -> logging.Logger:
    """Logger `brokers.errors`, doublé d'un fichier dédié (créé au premier incident)."""
    global _journal
    if _journal is None:
        journal = get_logger("brokers.errors")
        try:
            journal.addHandler(_rotating_file(settings.log, ERRORS_LOG_FILE, logging.WARNING))
        except OSError:
            journal.exception("Fichier %s inaccessible : incidents dans les logs habituels uniquement", ERRORS_LOG_FILE)
        _journal = journal
    return _journal


def classify(exc: BaseException) -> ErrorKind:
    """Nature de l'erreur (exceptions ccxt reconnues si la bibliothèque est installée)."""
    if isinstance(exc, BrokerConnectionError):
        return "network"
    try:
        import ccxt
    except ImportError:              # broker MT5 seul : ccxt facultatif
        ccxt = None
    if ccxt is not None:
        for cls, kind in ((ccxt.AuthenticationError, "auth"), (ccxt.PermissionDenied, "auth"),
                          (ccxt.RateLimitExceeded, "rate_limit"), (ccxt.DDoSProtection, "rate_limit"),
                          (ccxt.InsufficientFunds, "insufficient_funds"), (ccxt.InvalidOrder, "invalid_order"),
                          (ccxt.NetworkError, "network"), (ccxt.ExchangeError, "exchange")):
            if isinstance(exc, cls):
                return kind  # type: ignore[return-value]
    if isinstance(exc, (ConnectionError, TimeoutError)):
        return "network"
    return "unknown"


def record_error(broker: str, operation: str, exc: BaseException | str) -> BrokerErrorRecord:
    """Journalise un incident broker et le conserve pour le monitoring. Ne lève jamais."""
    kind = classify(exc) if isinstance(exc, BaseException) else "unknown"
    message = str(exc) if isinstance(exc, str) else f"{type(exc).__name__}: {exc}"
    record = BrokerErrorRecord(datetime.now(timezone.utc), broker, operation, kind, message[:500])
    with _lock:
        _records.append(record)
    _get_journal().error("[%s] %s échoué (%s) : %s", broker, operation, kind, record.message)
    return record


def recent_errors(limit: int = 50) -> list[BrokerErrorRecord]:
    """Derniers incidents, du plus récent au plus ancien."""
    with _lock:
        return list(reversed(_records))[:limit]


def clear_errors() -> None:
    with _lock:
        _records.clear()
