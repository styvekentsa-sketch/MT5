"""Backtest : métriques, exécution simulée, absence de biais d'anticipation, règles de risque."""

import json
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from backtesting import runner
from backtesting.backtest_engine import (
    BacktestConfig,
    Backtester,
    calendar_blocker,
    database_sentiment,
    risk_rule,
)
from backtesting.broker import SimulatedBroker, Trade
from backtesting.data import DataError, default_specs, load_calendar_csv, load_csv
from backtesting.metrics import compute_metrics, max_drawdown
from sentiment.news_scraper import EconomicEvent
from strategies.technical_analysis import MIN_BARS, TechnicalSignal, compute_indicators, signal_from_indicators, technical_signal

T0 = datetime(2025, 3, 3, tzinfo=timezone.utc)          # lundi
EURUSD = default_specs("EURUSD")


def _trade(profit: float, i: int = 0) -> Trade:
    t = T0 + timedelta(hours=i)
    return Trade(i, "EURUSD", "BUY", 1.0, t, 1.1, t, 1.1, 1.09, 1.12, "tp" if profit > 0 else "sl", 0.0, profit)


def _candles(closes, start=T0, freq="15min", wick=0.0002) -> pd.DataFrame:
    closes = np.asarray(closes, dtype=float)
    opens = np.concatenate([[closes[0]], closes[:-1]])
    index = pd.date_range(start, periods=len(closes), freq=freq, tz="UTC")
    return pd.DataFrame({"open": opens, "high": np.maximum(opens, closes) + wick,
                         "low": np.minimum(opens, closes) - wick, "close": closes}, index=index)


def _always(signal="BUY", atr=0.001, calls=None):
    """Signal technique fixe (pour isoler la mécanique du backtest de la stratégie)."""
    def fn(ind, pos):
        if calls is not None:
            calls.append(pos)
        close = float(ind["close"].iloc[pos])
        return TechnicalSignal(signal=signal, score=1.0 if signal == "BUY" else -1.0, close=close, atr=atr,
                               swing_low=close - 1, swing_high=close + 1, reasons=("test",))
    return fn


# ----------------------------------------------------------------- métriques

def test_metrics_from_known_trades():
    trades = [_trade(200, 0), _trade(-100, 1), _trade(150, 2), _trade(-50, 3), _trade(0, 4)]
    m = compute_metrics(trades, [10_200, 10_100, 10_250, 10_200, 10_200], 10_000)
    assert (m.trades, m.wins, m.losses) == (5, 2, 2)
    assert m.winrate_pct == 40.0 and m.net_profit == 200 and m.total_return_pct == 2.0
    assert m.gross_profit == 350 and m.gross_loss == 150 and m.profit_factor == pytest.approx(2.333, abs=1e-3)
    assert m.expectancy == 40.0 and m.avg_win == 175 and m.avg_loss == 75
    assert m.largest_win == 200 and m.largest_loss == 100


def test_profit_factor_edge_cases():
    assert compute_metrics([_trade(50)], [10_050], 10_000).profit_factor == float("inf")
    assert compute_metrics([_trade(50)], [10_050], 10_000).to_dict()["profit_factor"] is None   # JSON
    assert compute_metrics([], [], 10_000).profit_factor is None
    assert compute_metrics([], [], 10_000).winrate_pct == 0.0


def test_max_drawdown():
    assert max_drawdown([100, 120, 90, 130, 110]) == (30.0, 25.0)
    assert max_drawdown([100, 110, 120]) == (0.0, 0.0)
    # le capital initial compte comme premier plus haut
    assert compute_metrics([], [9_000, 9_500], 10_000).max_drawdown_pct == 10.0


# ----------------------------------------------------------------- broker simulé

@pytest.fixture
def broker():
    b = SimulatedBroker(EURUSD, 10_000)
    b.set_market(T0, 1.1000, 10)                      # Ask = 1.1001
    return b


def test_buy_take_profit(broker):
    broker.open_position("BUY", 1.0, sl=1.0980, tp=1.1040)
    assert broker.positions[0].entry_price == pytest.approx(1.1001)
    [trade] = broker.process_bar(T0, 1.1000, 1.1050, 1.0990, 10)
    assert trade.exit_reason == "tp" and trade.exit_price == 1.1040
    assert trade.profit == pytest.approx(390.0) and broker.balance == pytest.approx(10_390.0)


def test_sl_wins_when_both_levels_touched_in_same_bar(broker):
    broker.open_position("BUY", 1.0, sl=1.0980, tp=1.1040)
    [trade] = broker.process_bar(T0, 1.1000, 1.1050, 1.0970, 10)
    assert trade.exit_reason == "sl" and trade.exit_price == 1.0980


def test_gap_through_stop_fills_at_open(broker):
    broker.open_position("BUY", 1.0, sl=1.0980, tp=1.1040)
    [trade] = broker.process_bar(T0, 1.0950, 1.0960, 1.0940, 10)
    assert trade.exit_price == 1.0950 and trade.profit == pytest.approx(-510.0)


def test_sell_levels_use_ask(broker):
    broker.open_position("SELL", 1.0, sl=1.1020, tp=1.0960)
    assert broker.positions[0].entry_price == 1.1000
    assert broker.process_bar(T0, 1.1000, 1.1018, 1.0990, 10) == []         # Ask max 1.1019 < SL
    [trade] = broker.process_bar(T0, 1.1000, 1.1005, 1.0955, 10)            # Ask min 1.0956 <= TP
    assert trade.exit_reason == "tp" and trade.profit == pytest.approx(400.0)


def test_account_margin_equity_and_commission():
    b = SimulatedBroker(EURUSD, 10_000, commission_per_lot=7.0)
    b.set_market(T0, 1.1000, 0)
    b.open_position("BUY", 1.0, sl=1.09, tp=None)
    b.set_market(T0, 1.1010, 0)
    account = b.get_account_info()
    assert account.margin == pytest.approx(1100.0) and account.profit == pytest.approx(100.0)
    assert account.equity == pytest.approx(10_100.0)
    [trade] = b.close_all(T0, 1.1010)
    assert trade.profit == pytest.approx(93.0) and trade.exit_reason == "end"


# ----------------------------------------------------------------- moteur de backtest

def test_entry_at_next_bar_open_without_lookahead():
    candles = _candles(1.10 + 0.0001 * np.sin(np.arange(200) / 5))
    calls: list[int] = []
    result = Backtester(candles, EURUSD, BacktestConfig("EURUSD"), signal_fn=_always(calls=calls)).run()
    first = result.trades[0]
    entry_pos = candles.index.get_loc(first.entry_time)
    assert calls[0] == MIN_BARS and max(calls) == len(candles) - 2           # jamais la dernière bougie
    assert entry_pos == MIN_BARS + 1                                         # décision i -> entrée i + 1
    assert first.entry_price == pytest.approx(candles["open"].iloc[entry_pos] + 10 * EURUSD.point)


def test_position_sized_by_risk_manager_and_one_position_at_a_time():
    candles = _candles(1.10 + 0.0001 * np.sin(np.arange(300) / 5))
    result = Backtester(candles, EURUSD, BacktestConfig("EURUSD"), signal_fn=_always(atr=0.001)).run()
    # SL = 1.5 ATR = 0.0015 -> 150 USD/lot ; 1 % de 10 000 = 100 USD -> 0.66 lot
    assert result.trades[0].volume == 0.66
    spans = sorted((t.entry_time, t.exit_time) for t in result.trades)
    assert all(nxt[0] >= cur[1] for cur, nxt in zip(spans, spans[1:]))    # jamais 2 positions simultanées
    assert result.stats["rejected:exposition"] > 0


def test_daily_loss_rules_stop_trading_then_resume_next_day():
    # baisse continue : chaque achat touche son SL ; 3 jours de bougies M15
    candles = _candles(1.20 - 0.0004 * np.arange(96 * 3), wick=0.0001)
    result = Backtester(candles, EURUSD, BacktestConfig("EURUSD"), signal_fn=_always()).run()
    per_day = pd.Series([t.entry_time.date() for t in result.trades]).value_counts()
    assert per_day.max() <= 3                                                # limite : 3 pertes consécutives
    assert len(per_day) >= 2                                                 # reprise le lendemain
    assert result.stats["rejected:pertes_consecutives"] + result.stats["rejected:perte_journaliere"] > 0
    assert all(t.profit < 0 for t in result.trades)


def test_news_blocker_prevents_entries():
    candles = _candles(1.10 + 0.0001 * np.sin(np.arange(150) / 5))
    result = Backtester(candles, EURUSD, BacktestConfig("EURUSD"), signal_fn=_always(),
                        news_blocker=lambda symbol, now: True).run()
    assert result.trades == [] and result.stats["news_blocked"] > 0


def test_sentiment_alone_never_trades():
    candles = _candles(1.10 + 0.0001 * np.sin(np.arange(150) / 5))
    result = Backtester(candles, EURUSD, BacktestConfig("EURUSD"), signal_fn=_always(signal="NEUTRAL"),
                        sentiment_fn=lambda symbol, now: 1.0).run()
    assert result.trades == []


def test_real_strategy_end_to_end():
    rng = np.random.default_rng(3)
    candles = _candles(1.10 * np.exp(np.cumsum(rng.normal(0, 0.0006, 1500))))
    result = Backtester(candles, EURUSD, BacktestConfig("EURUSD")).run()
    assert result.metrics.trades == len(result.trades) > 0
    assert result.metrics.final_balance == pytest.approx(10_000 + sum(t.profit for t in result.trades), abs=0.05)
    assert result.equity_curve.iloc[-1] == pytest.approx(result.metrics.final_balance, abs=0.05)


def test_too_few_candles():
    with pytest.raises(ValueError):
        Backtester(_candles([1.1] * 30), EURUSD, BacktestConfig("EURUSD")).run()


def test_signal_matches_live_technical_signal():
    rng = np.random.default_rng(1)
    candles = _candles(1.10 * np.exp(np.cumsum(rng.normal(0, 0.0005, 400))))
    live = technical_signal(candles)                                         # ignore la dernière bougie
    replay = signal_from_indicators(compute_indicators(candles.iloc[:-1]), -1)
    assert replay == live


# ----------------------------------------------------------------- fournisseurs historiques

def test_calendar_blocker_window():
    event = EconomicEvent("NFP", "USD", T0 + timedelta(hours=1), "High")
    blocked = calendar_blocker([event, EconomicEvent("Low", "USD", T0, "Low")])
    assert blocked("EURUSD", T0 + timedelta(minutes=50)) is True             # 10 min avant
    assert blocked("EURUSD", T0 + timedelta(minutes=64)) is True             # 4 min après
    assert blocked("EURUSD", T0 + timedelta(minutes=30)) is False
    assert blocked("EURUSD", T0) is False                                    # impact faible ignoré
    assert blocked("GBPJPY", T0 + timedelta(minutes=55)) is False


def test_database_sentiment_uses_only_past_articles():
    articles = [{"title": "Euro surges", "snippet": "", "published_at": (T0 + timedelta(hours=2)).isoformat()}]
    sentiment = database_sentiment(articles, lookback_hours=6)
    assert sentiment("EURUSD", T0 + timedelta(hours=1)) == 0.0               # article pas encore publié
    assert sentiment("EURUSD", T0 + timedelta(hours=3)) > 0
    assert sentiment("EURUSD", T0 + timedelta(hours=9)) == 0.0               # hors fenêtre


def test_risk_rule_categories():
    assert risk_rule("3 pertes consécutives aujourd'hui (maximum 3)") == "pertes_consecutives"
    assert risk_rule("1 position(s) déjà ouverte(s) sur EURUSD (maximum 1)") == "exposition"
    assert risk_rule("perte journalière 3.10% >= limite 3.0%") == "perte_journaliere"
    assert risk_rule("spread 40 pts > maximum 30 pts") == "spread"


# ----------------------------------------------------------------- données et CLI

def test_load_csv_standard_and_mt5_formats(tmp_path):
    standard = tmp_path / "std.csv"
    standard.write_text("time,open,high,low,close,spread\n2025-01-02 00:15:00,1.1,1.2,1.0,1.15,12\n"
                        "2025-01-02 00:00:00,1.0,1.1,0.9,1.1,10\n")
    df = load_csv(standard)
    assert list(df.index.strftime("%H:%M")) == ["00:00", "00:15"] and df["spread"].iloc[0] == 10

    mt5 = tmp_path / "mt5.csv"
    mt5.write_text("<DATE>\t<TIME>\t<OPEN>\t<HIGH>\t<LOW>\t<CLOSE>\t<TICKVOL>\t<VOL>\t<SPREAD>\n"
                   "2025.01.02\t00:00:00\t1.1\t1.2\t1.0\t1.15\t100\t0\t8\n")
    df = load_csv(mt5)
    assert df.index[0] == pd.Timestamp("2025-01-02", tz="UTC") and df["volume"].iloc[0] == 100


def test_load_csv_rejects_bad_files(tmp_path):
    with pytest.raises(DataError):
        load_csv(tmp_path / "absent.csv")
    bad = tmp_path / "bad.csv"
    bad.write_text("time,open,close\n2025-01-01,1,1\n")
    with pytest.raises(DataError):
        load_csv(bad)
    incoherent = tmp_path / "incoherent.csv"
    incoherent.write_text("time,open,high,low,close\n2025-01-01,1.1,1.0,1.2,1.1\n")       # high < low
    with pytest.raises(DataError):
        load_csv(incoherent)


def test_load_calendar_csv(tmp_path):
    path = tmp_path / "cal.csv"
    path.write_text("time,currency,impact,title\n2025-03-07 13:30:00,usd,high,Non-Farm Payrolls\n")
    [event] = load_calendar_csv(path)
    assert event.currency == "USD" and event.impact == "High" and event.time.hour == 13


def test_default_specs():
    assert default_specs("XAUUSD").tick_size == 0.01 and default_specs("USDJPY").digits == 3
    assert default_specs("EURUSD", tick_value=2.0).tick_value == 2.0


def test_runner_end_to_end(tmp_path, capsys):
    rng = np.random.default_rng(5)
    candles = _candles(1.10 * np.exp(np.cumsum(rng.normal(0, 0.0006, 800))))
    csv = tmp_path / "eurusd.csv"
    candles.rename_axis("time").to_csv(csv)
    out = tmp_path / "report"
    assert runner.main(["--symbol", "EURUSD", "--csv", str(csv), "--out", str(out)]) == 0
    assert "Profit factor" in capsys.readouterr().out
    report = json.loads((out / "report.json").read_text(encoding="utf-8"))
    assert report["symbol"] == "EURUSD" and "max_drawdown_pct" in report["metrics"]
    assert (out / "trades.csv").exists() and (out / "equity.csv").exists()
    assert runner.main(["--symbol", "EURUSD", "--csv", str(tmp_path / "absent.csv")]) == 1
