"""
Broker du processus, choisi par BROKER_TYPE (mt5 par défaut, ou ccxt).

Un processus = un compte : en mode multi-comptes, le superviseur lance un processus par
compte, chacun avec son propre BROKER_TYPE. Une panne d'exchange reste ainsi confinée
au processus de ce compte.

Usage :
    from brokers.factory import get_broker

    broker = get_broker()
"""

from __future__ import annotations

import threading

from brokers.base import BaseBroker
from config import settings

_lock = threading.Lock()
_broker: BaseBroker | None = None


def create_broker(broker_type: str | None = None) -> BaseBroker:
    """Nouvelle instance de l'adaptateur demandé (import paresseux : ccxt reste facultatif)."""
    kind = (broker_type or settings.broker.type).lower()
    if kind == "mt5":
        from brokers.mt5_broker import MT5Broker
        return MT5Broker()
    if kind == "ccxt":
        try:
            import ccxt  # noqa: F401
        except ImportError as exc:  # pragma: no cover - dépend de l'installation
            raise RuntimeError("BROKER_TYPE=ccxt : installer la bibliothèque (pip install ccxt)") from exc
        from brokers.ccxt_broker import CCXTBroker
        return CCXTBroker()
    raise ValueError(f"BROKER_TYPE inconnu : {kind}")


def get_broker() -> BaseBroker:
    """Broker partagé du processus (moteur, API, risque), créé au premier appel."""
    global _broker
    with _lock:
        if _broker is None:
            _broker = create_broker()
        return _broker


def set_broker(broker: BaseBroker | None) -> None:
    """Remplace le broker du processus (tests, ou moteur créé avec un broker explicite)."""
    global _broker
    with _lock:
        _broker = broker
