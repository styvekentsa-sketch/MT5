"""
Point d'entrée du bot de trading MT5.

Lancement (depuis la racine du projet) :
    python main.py                       bot de trading (+ API du dashboard si API_ENABLED=true)
    python main.py --analyst             mode analyste : setups détectés, aucun ordre
    python main.py --analyst --watch 60  idem, rafraîchi toutes les 60 s

Sans terminal MetaTrader 5 : BROKER_TYPE=sim (bougies fictives ou CSV locaux, compte virtuel).
Multi-comptes (MT5_ACCOUNTS) : démarre un processus par compte et la passerelle API.
Arrêt propre : Ctrl+C.
"""

from __future__ import annotations

import argparse
import sys
import time

from config.settings import settings


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python main.py", description="Bot de trading MT5")
    parser.add_argument("--analyst", action="store_true",
                        help="mode analyste : affiche les setups détectés sans jamais passer d'ordre")
    parser.add_argument("--watch", type=int, metavar="SECONDES", default=0,
                        help="mode analyste : relancer l'analyse toutes les N secondes (0 = une seule fois)")
    return parser.parse_args(argv)


def run_analyst(watch_seconds: int = 0) -> int:
    """Analyse les symboles configurés avec les stratégies à setups et affiche le résultat."""
    from brokers.factory import get_broker
    from models.schemas import BotConfig
    from strategies.registry import setup_strategies

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    config = BotConfig.from_settings(settings)
    broker = get_broker()
    if not broker.connect():
        print(f"Connexion à {broker.description} impossible")
        return 1
    banner = "MODE SIMULATION - données fictives, aucun ordre réel" if broker.name == "sim" else broker.description
    strategies = setup_strategies()
    memory = None
    if settings.learning.enabled:
        from learning.trade_memory import TradeMemory
        memory = TradeMemory()
    try:
        while True:
            print(f"\n=== Mode analyste · {banner} · {', '.join(config.symbols)} ===")
            print(f"Risque : {config.risk_per_trade_pct}% par position, {config.max_total_risk_pct}% cumulé, "
                  f"{config.max_open_positions} positions max, perte journalière max {config.max_daily_loss_pct}%")
            for symbol in config.symbols:
                for strategy in strategies:
                    setup = strategy.analyze(broker, symbol, memory=memory)
                    if setup is None:
                        print(f"  {symbol:8} {strategy.name:9} aucun setup")
                        continue
                    rr = f"{setup.net_reward_risk:.2f} net" if setup.net_reward_risk else "—"
                    print(f"  {symbol:8} {strategy.name:9} {setup.status.value.upper():12} {setup.side.value:4} "
                          f"{setup.style.value:8} régime {setup.regime.value:10} entrée {setup.entry_price:.5g} "
                          f"SL {setup.stop_loss:.5g} TP {setup.take_profit or 0:.5g} R/R {rr} "
                          f"lot {setup.volume:g} risque {setup.risk_amount:.2f} ({setup.risk_pct:.2f}%)")
                    print(f"           {setup.reason}")
                    for note in setup.notes:
                        print(f"           ! {note}")
            if watch_seconds <= 0:
                return 0
            time.sleep(watch_seconds)
    except KeyboardInterrupt:
        return 0
    finally:
        broker.disconnect()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.analyst:
        return run_analyst(args.watch)

    from api.main import start_api_server
    from core.engine import TradingEngine

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
