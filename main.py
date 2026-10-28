"""
Point d'entrée du bot de trading MT5.

Lancement (depuis la racine du projet) :
    python main.py

Démarre le moteur de trading et, si API_ENABLED=true, l'API du dashboard.
Arrêt propre : Ctrl+C.
"""

from __future__ import annotations

from api.main import start_api_server
from core.engine import TradingEngine


def main() -> int:
    engine = TradingEngine()
    api = start_api_server(engine)
    try:
        return 0 if engine.run() else 1
    finally:
        if api is not None:
            api.stop()


if __name__ == "__main__":
    raise SystemExit(main())
