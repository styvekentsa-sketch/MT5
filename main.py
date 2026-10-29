"""
Point d'entrée du bot de trading MT5.

Lancement (depuis la racine du projet) :
    python main.py

Compte unique : démarre le moteur de trading et, si API_ENABLED=true, l'API du dashboard.
Multi-comptes (MT5_ACCOUNTS) : démarre un processus par compte et la passerelle API.
Arrêt propre : Ctrl+C.
"""

from __future__ import annotations

from api.main import start_api_server
from config.settings import settings
from core.engine import TradingEngine


def main() -> int:
    if settings.accounts:                      # MT5_ACCOUNTS défini : un processus par compte
        from core.supervisor import Supervisor

        return Supervisor(settings.accounts).run()

    engine = TradingEngine()
    api = start_api_server(engine)
    try:
        return 0 if engine.run() else 1
    finally:
        if api is not None:
            api.stop()


if __name__ == "__main__":
    raise SystemExit(main())
