"""
Mode analyste du dashboard (/api/analysis/*) et alertes métier (utils/alerts.py) :
limites de risque, setups avec arguments pour / contre, indicateurs, performances et rapports.
"""

import json
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from api.routes import analysis
from backtesting.setup_engine import CostModel, SetupBacktester, save_report
from brokers.models import ClosedDeal
from config.settings import settings
from core.engine import TradingEngine
from learning.trade_memory import TradeMemory
from models.schemas import MarketRegime, SetupStatus, Side, TradingSetup, TradingStyle
from strategies.base import NEWS_NOTE, BaseStrategy, BuyPlan
from tests.conftest import TEST_API_KEY
from tests.fakes import FakeBroker
from utils import alerts
from utils.notifier import ConsoleChannel, Level

AUTH = {"X-API-Key": TEST_API_KEY}
NOW = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)


def _bars(n=400, seed=3) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = 1.10 + np.cumsum(rng.normal(0, 0.0005, n))
    open_ = np.concatenate([[close[0]], close[:-1]])
    wick = rng.uniform(0.0001, 0.0006, (2, n))
    index = pd.date_range("2026-10-20", periods=n, freq="15min", tz="UTC")
    return pd.DataFrame({"open": open_, "high": np.maximum(open_, close) + wick[0],
                         "low": np.minimum(open_, close) - wick[1], "close": close, "volume": 10, "spread": 10},
                        index=index)


def _setup(**changes) -> TradingSetup:
    values = dict(symbol="EURUSD", side=Side.BUY, style=TradingStyle.DAY, strategy="pullback",
                  regime=MarketRegime.TREND_UP, entry_price=1.1, stop_loss=1.098, take_profit=1.107, volume=0.5,
                  risk_amount=100.0, risk_pct=1.0, net_reward_risk=3.8, status=SetupStatus.READY,
                  reason="Tendance haussière H1 ; retest du creux confirmé", notes=["spread élevé"],
                  indicators={"adx": 31.0}, expires_at=NOW + timedelta(hours=1))
    return TradingSetup(**{**values, **changes})


@pytest.fixture
def broker(account, eurusd_specs, eurusd_tick):
    fake = FakeBroker()
    fake.account = account
    fake.bars = _bars()
    fake.specs = {"EURUSD": eurusd_specs}
    fake.ticks = {"EURUSD": eurusd_tick}
    return fake


@pytest.fixture
def client(broker, tmp_path, monkeypatch):
    monkeypatch.setattr(analysis, "REPORTS_DIR", tmp_path / "reports")
    analysis._cache.clear()
    engine = TradingEngine(symbols=["EURUSD"], broker=broker)
    engine.memory = TradeMemory(tmp_path / "learning")
    test_client = TestClient(create_app(engine))
    test_client.engine = engine
    return test_client


@pytest.fixture
def sent(monkeypatch):
    messages = []
    monkeypatch.setattr(alerts, "notify", lambda title, message="", level=Level.INFO, dedup_key=None:
                        messages.append((title, message, level, dedup_key)) or True)
    return messages


# ----------------------------------------------------------------- limites de risque

def _deal(profit, minutes):
    return ClosedDeal(ticket=minutes, symbol="EURUSD", time=NOW + timedelta(minutes=minutes), net_profit=profit,
                      magic=settings.trading.magic_number)


def test_risk_limits_report_loss_and_streak(client, broker):
    broker.deals = [_deal(50, 1), _deal(-120, 2), _deal(-80, 3)]       # solde actuel 10 000 : début 10 150
    body = client.get("/api/analysis/risk", headers=AUTH).json()
    assert body["consecutive_losses"] == 2 and body["consecutive_losses_reached"] is True
    assert body["daily_loss_pct"] == pytest.approx(150 / 10_150 * 100, abs=1e-3)
    assert body["daily_loss_reached"] is False and body["max_daily_loss_pct"] == settings.risk.max_daily_loss_pct


def test_risk_limits_require_api_key(client):
    assert client.get("/api/analysis/risk").status_code == 401


# ----------------------------------------------------------------- setups

def test_arguments_for_and_against():
    pros, cons = analysis.arguments(_setup())
    assert pros[:2] == ["Tendance haussière H1", "retest du creux confirmé"] and "ratio net 3.80" in pros[2]
    assert cons == ["spread élevé"]
    _, weak = analysis.arguments(_setup(net_reward_risk=3.1, indicators={"adx": 21.0}, notes=[]))
    assert any("proche du minimum" in c for c in weak) and any("tendance encore faible" in c for c in weak)


class _Ready(BaseStrategy):
    name = "jouet"

    def plan_buy(self, data, quote):
        return BuyPlan(SetupStatus.READY, 1.1000, 1.0980, 1.1070, 3.4, MarketRegime.TREND_UP,
                       "contexte favorable ; déclencheur présent")


def test_setups_endpoint_lists_proposals_with_arguments_and_caches(client, monkeypatch):
    calls = []

    def strategies():
        calls.append(1)
        return [_Ready()]
    monkeypatch.setattr(analysis, "_strategies", strategies)
    body = client.get("/api/analysis/setups", headers=AUTH).json()
    assert body["setups"] and {"pros", "cons", "entry_price", "stop_loss", "take_profit", "regime"} <= set(body["setups"][0])
    assert body["setups"][0]["pros"][0] == "contexte favorable"
    client.get("/api/analysis/setups", headers=AUTH)
    assert len(calls) == 1                                               # second appel servi par le cache
    client.get("/api/analysis/setups?refresh=true", headers=AUTH)
    assert len(calls) == 2


# ----------------------------------------------------------------- indicateurs

def test_indicators_are_computed_on_closed_candles(client, broker):
    body = client.get("/api/analysis/indicators?symbol=eurusd&timeframe=M15&count=300", headers=AUTH).json()
    assert len(body["times"]) == len(body["atr"]) == len(body["adx"]) == len(broker.bars) - 1   # bougie en cours exclue
    assert body["atr"][0] is None and body["atr"][-1] > 0 and 0 <= body["adx"][-1] <= 100
    assert body["pivots"] and all(p["confirmed_at"] > p["time"] for p in body["pivots"])
    assert body["regime"] in {r.value for r in MarketRegime}
    assert {lvl["name"] for lvl in body["levels"]} == {"S2", "S1", "P", "R1", "R2"}


def test_indicators_validation(client):
    assert client.get("/api/analysis/indicators?symbol=USDJPY", headers=AUTH).status_code == 404
    assert client.get("/api/analysis/indicators?symbol=EURUSD&timeframe=M7", headers=AUTH).status_code == 422


# ----------------------------------------------------------------- performances

def test_performance_groups_closed_trades_and_reads_backtest_reports(client, tmp_path):
    memory = client.engine.memory
    memory.trades.save([
        {"id": 1, "status": "closed", "symbol": "EURUSD", "side": "BUY", "mode": "day", "entry_price": 1.1,
         "sl": 1.099, "close_price": 1.103, "net_profit": 300.0},
        {"id": 2, "status": "closed", "symbol": "EURUSD", "side": "SELL", "strategy": "pullback", "entry_price": 1.1,
         "sl": 1.101, "close_price": 1.101, "net_profit": -100.0},
        {"id": 3, "status": "open", "symbol": "EURUSD", "side": "BUY"},
    ])
    m15 = _bars(600, seed=1)
    h1 = m15.resample("1h").agg({"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()
    from tests.test_backtest import EURUSD, Breakout
    costs = CostModel(slippage_points=0)
    result = SetupBacktester([Breakout()], "EURUSD", {"M15": m15, "H1": h1}, EURUSD, costs=costs).run()
    path = save_report(result.to_report("EURUSD", costs), tmp_path / "reports")
    assert json.loads(path.read_text(encoding="utf-8"))["stats"]["trades"] == result.stats.trades

    body = client.get("/api/analysis/performance", headers=AUTH).json()
    live = body["live"]
    assert live["overall"]["trades"] == 2 and live["overall"]["net_profit"] == 200.0
    assert live["by_strategy"]["adaptive · day"]["expectancy_r"] == pytest.approx(3.0)
    assert live["by_strategy"]["pullback"]["losses"] == 1
    [report] = body["backtests"]
    assert report["symbol"] == "EURUSD" and "stratégie" in report["segments"] and report["equity"]


# ----------------------------------------------------------------- alertes

def test_setup_alert_is_formatted_with_levels_and_arguments():
    alert = alerts.format_setup(_setup())
    assert alert.title == "Setup validé · EURUSD BUY" and alert.level == Level.INFO
    assert "SL 1.098" in alert.message and "Ratio net 3.80" in alert.message and "Contre : spread élevé" in alert.message
    assert alert.dedup_key == alerts.format_setup(_setup(id="autre")).dedup_key        # même setup recalculé


def test_loss_alert_carries_the_post_mortem():
    event = {"outcome": "loss", "trade": {"ticket": 42, "symbol": "EURUSD", "side": "BUY", "net_profit": -95.5,
                                          "close_reason": "sl", "close_price": 1.098, "mode": "day"},
             "analysis": {"main_factor": "stop trop serré", "factors": ["stop trop serré", "contre-tendance H1"],
                          "adverse_excursion_pips": 21.0, "favorable_excursion_pips": 3.0, "sl_distance_pips": 20.0,
                          "pattern_description": "London · ADX faible"}}
    alert = alerts.format_loss(event)
    assert alert.title == "Perte · EURUSD BUY -95.50" and alert.level == Level.WARNING
    assert "Post-mortem : stop trop serré" in alert.message and "contre-tendance H1" in alert.message
    assert alerts.format_loss({"outcome": "win", "trade": {}}) is None


def test_block_alert_levels():
    assert alerts.format_block("kill switch", "perte 3.2 %", critical=True).level == Level.CRITICAL
    assert alerts.format_block("annonce majeure", "NFP", "EURUSD").title == "Blocage · EURUSD · annonce majeure"


def test_analysis_sends_alerts_for_ready_and_news_blocked_setups(broker, sent, monkeypatch):
    strategy = _Ready()
    monkeypatch.setattr(strategy, "evaluate", lambda *a, **k: _setup())
    strategy.analyze(broker, "EURUSD", NOW)
    blocked = _setup(status=SetupStatus.INVALIDATED, notes=[f"{NEWS_NOTE} : NFP 13:30 UTC"])
    monkeypatch.setattr(strategy, "evaluate", lambda *a, **k: blocked)
    strategy.analyze(broker, "EURUSD", NOW)
    assert [title for title, *_ in sent] == ["Setup validé · EURUSD BUY", "Blocage · EURUSD · annonce majeure"]


def test_engine_alerts_losses_after_review(broker, sent):
    engine = TradingEngine(symbols=["EURUSD"], broker=broker)
    loss = {"outcome": "loss", "message": "perte", "analysis": None,
            "trade": {"ticket": 7, "symbol": "EURUSD", "side": "SELL", "net_profit": -20.0, "mode": "day"}}
    win = {"outcome": "win", "message": "gain", "analysis": None,
           "trade": {"ticket": 8, "symbol": "EURUSD", "side": "BUY", "net_profit": 20.0, "mode": "day"}}
    engine.memory = type("Memory", (), {"review": lambda self, b: [loss, win]})()
    engine._review_closed_trades()
    assert [title for title, *_ in sent] == ["Perte · EURUSD SELL -20.00"]


def test_console_channel_writes_text():
    import io
    stream = io.StringIO()
    ConsoleChannel(stream).send("ℹ️ [MT5 Bot] Setup validé")
    assert stream.getvalue() == "ℹ️ [MT5 Bot] Setup validé\n"
