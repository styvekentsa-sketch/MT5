"""
Lancement d'un backtest en ligne de commande.

Exemples (depuis la racine du projet) :
    python -m backtesting.runner --symbol EURUSD --csv data/EURUSD_M15.csv
    python -m backtesting.runner --symbol EURUSD --mt5 --bars 5000 --timeframe M15
    python -m backtesting.runner --symbol XAUUSD --mt5 --bars 3000 --sentiment database --out reports/xau

Les règles de risque et les paramètres de stratégie sont ceux du fichier .env.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import pandas as pd

from backtesting.backtest_engine import BacktestConfig, BacktestResult, Backtester, calendar_blocker, database_sentiment
from backtesting.data import DataError, default_specs, load_calendar_csv, load_csv, load_mt5
from config.settings import settings
from utils.logger import get_logger

log = get_logger("backtest")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m backtesting.runner",
                                     description="Backtest de la stratégie du bot sur données historiques.")
    parser.add_argument("--symbol", required=True, help="symbole, ex : EURUSD")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--csv", type=Path, help="fichier CSV de bougies (standard ou export MT5)")
    source.add_argument("--mt5", action="store_true", help="télécharger l'historique depuis le terminal MT5")
    parser.add_argument("--bars", type=int, default=5000, help="nombre de bougies MT5 (défaut : 5000)")
    parser.add_argument("--timeframe", default=None, help=f"timeframe MT5 (défaut : {settings.trading.timeframe})")
    parser.add_argument("--balance", type=float, default=10_000.0, help="capital initial (défaut : 10000)")
    parser.add_argument("--currency", default="USD", help="devise du compte (défaut : USD)")
    parser.add_argument("--leverage", type=int, default=100, help="levier (défaut : 100)")
    parser.add_argument("--commission", type=float, default=0.0, help="commission par lot et par trade")
    parser.add_argument("--spread", type=int, default=10, help="spread en points si absent des données (défaut : 10)")
    parser.add_argument("--tick-size", type=float, help="CSV : taille du tick (défaut selon le symbole)")
    parser.add_argument("--tick-value", type=float, help="CSV : valeur du tick pour 1 lot (défaut selon le symbole)")
    parser.add_argument("--digits", type=int, help="CSV : nombre de décimales du prix")
    parser.add_argument("--calendar", type=Path, help="CSV d'annonces historiques (time,currency,impact,title)")
    parser.add_argument("--sentiment", choices=("none", "database"), default="none",
                        help="none : neutre (défaut) ; database : articles historisés dans la base SQLite")
    parser.add_argument("--out", type=Path, help="dossier où écrire trades.csv, equity.csv et report.json")
    parser.add_argument("--verbose", action="store_true", help="afficher le détail des règles de risque")
    args = parser.parse_args(argv)
    if args.bars <= 100:
        parser.error("--bars doit être > 100")
    if args.balance <= 0:
        parser.error("--balance doit être > 0")
    return args


def format_report(result: BacktestResult) -> str:
    m = result.metrics
    pf = "∞" if m.profit_factor is not None and math.isinf(m.profit_factor) else (m.profit_factor or "—")
    cur = result.config.currency
    rejected = {k.split(":", 1)[1]: v for k, v in result.stats.items() if k.startswith("rejected:")}
    lines = [
        f"Backtest {result.config.symbol} · {result.start:%Y-%m-%d %H:%M} → {result.end:%Y-%m-%d %H:%M} UTC",
        "─" * 60,
        f"  Capital initial   {m.initial_balance:>14,.2f} {cur}",
        f"  Capital final     {m.final_balance:>14,.2f} {cur}",
        f"  Rendement total   {m.total_return_pct:>+13.2f} %   ({m.net_profit:+,.2f} {cur})",
        f"  Drawdown maximal  {m.max_drawdown_pct:>13.2f} %   ({m.max_drawdown:,.2f} {cur})",
        f"  Trades            {m.trades:>14}   ({m.wins} gagnants / {m.losses} perdants)",
        f"  Taux de réussite  {m.winrate_pct:>13.2f} %",
        f"  Profit factor     {pf!s:>14}",
        f"  Gain moyen        {m.avg_win:>14,.2f}   perte moyenne {m.avg_loss:,.2f}",
        f"  Espérance/trade   {m.expectancy:>+14,.2f}",
        "─" * 60,
        f"  Signaux : {result.stats.get('signal', 0)} · ouverts : {result.stats.get('opened', 0)}"
        f" · bloqués calendrier : {result.stats.get('news_blocked', 0)}",
    ]
    if rejected:
        lines.append("  Refus du risque : " + ", ".join(f"{k} {v}" for k, v in sorted(rejected.items())))
    return "\n".join(lines)


def save_outputs(result: BacktestResult, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    pd.DataFrame([t.__dict__ for t in result.trades]).to_csv(out / "trades.csv", index=False)
    result.equity_curve.to_csv(out / "equity.csv", index_label="time")
    report = {
        "symbol": result.config.symbol,
        "start": result.start.isoformat() if result.start else None,
        "end": result.end.isoformat() if result.end else None,
        "metrics": result.metrics.to_dict(),
        "stats": dict(result.stats),
    }
    (out / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info("Résultats écrits dans %s", out)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.mt5:
            candles, specs = load_mt5(args.symbol, args.bars, args.timeframe)
        else:
            candles = load_csv(args.csv)
            specs = default_specs(args.symbol, args.tick_size, args.tick_value, args.digits)

        news_blocker = calendar_blocker(load_calendar_csv(args.calendar)) if args.calendar else None
        sentiment_fn = None
        if args.sentiment == "database":
            from utils.database import MAX_PAGE_SIZE, get_database

            db = get_database()
            articles, offset = [], 0
            while True:
                page, total = db.list_articles(limit=MAX_PAGE_SIZE, offset=offset)
                articles.extend(page)
                offset += len(page)
                if not page or offset >= total:
                    break
            log.info("%d articles historisés utilisés pour le sentiment", len(articles))
            sentiment_fn = database_sentiment(articles)

        config = BacktestConfig(symbol=args.symbol, initial_balance=args.balance, currency=args.currency,
                                leverage=args.leverage, commission_per_lot=args.commission,
                                default_spread_points=args.spread)
        result = Backtester(candles, specs, config, sentiment_fn=sentiment_fn, news_blocker=news_blocker).run(
            quiet=not args.verbose)
    except (DataError, ValueError) as exc:
        log.error("Backtest impossible : %s", exc)
        return 1

    print(format_report(result))
    if args.out:
        save_outputs(result, args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
