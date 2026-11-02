"""Fondations : schémas Pydantic, broker de simulation, détecteur de régime, stratégie pullback, mode analyste."""

from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest
from pydantic import ValidationError

from brokers.models import AccountInfo
from brokers.sim_broker import SimBroker, synthetic_candles
from config import settings
from models.schemas import AccountState, BotConfig, SetupStatus, Side, TradingSetup, TradingStyle
from strategies.pullback import PullbackStrategy
from tests.fakes import FakeBroker

NOW = datetime(2026, 10, 30, 10, 15, 30, tzinfo=timezone.utc)


# ----------------------------------------------------------------- schémas

def _setup(**changes) -> TradingSetup:
    values = dict(symbol="eurusd", side=Side.BUY, style=TradingStyle.DAY, strategy="test",
                  entry_price=1.1000, stop_loss=1.0980, take_profit=1.1040)
    return TradingSetup(**{**values, **changes})


def test_setup_validates_levels_and_computes_ratio():
    setup = _setup()
    assert setup.symbol == "EURUSD" and setup.status == SetupStatus.WATCHING and len(setup.id) == 12
    assert setup.reward_risk == pytest.approx(2.0)
    with pytest.raises(ValidationError, match="stop loss doit être sous"):
        _setup(stop_loss=1.1010)
    with pytest.raises(ValidationError, match="take profit doit être sous"):
        _setup(side=Side.SELL, stop_loss=1.1020, take_profit=1.1050)
    with pytest.raises(ValidationError):
        _setup(volume=-1)
    assert _setup(expires_at=NOW).is_expired(NOW + timedelta(seconds=1))


def test_bot_config_from_settings_and_coherence():
    config = BotConfig.from_settings(settings)
    assert config.symbols == settings.trading.symbols and config.hours.day_close == settings.adaptive.day_close
    with pytest.raises(ValidationError, match="cumulé"):
        config.model_copy(update={"max_total_risk_pct": 0.5}).model_validate(
            {**config.model_dump(), "max_total_risk_pct": 0.5})


def test_required_risk_defaults(monkeypatch):
    import importlib
    settings_module = importlib.import_module("config.settings")
    for key in ("RISK_PER_TRADE_PCT", "RISK_MAX_TOTAL_RISK_PCT", "RISK_MAX_OPEN_POSITIONS", "RISK_MAX_DAILY_LOSS_PCT"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("TRADING_SYMBOLS", "EURUSD,GBPUSD,XAUUSD")
    risk = settings_module.load_settings().risk
    assert (risk.risk_per_trade_pct, risk.max_total_risk_pct, risk.max_open_positions, risk.max_daily_loss_pct) ==         (1.0, 2.0, 2, 3.0)


def test_account_state_from_account():
    account = AccountInfo(login=1, balance=1000, equity=1050, margin=100, margin_free=950, margin_level=1050,
                          profit=50, currency="USD", leverage=100)
    state = AccountState.from_account(account)
    assert state.equity == 1050 and state.margin_level_pct == pytest.approx(1050)


# ----------------------------------------------------------------- broker de simulation

def test_synthetic_candles_are_reproducible_and_consistent():
    a = synthetic_candles("EURUSD", "M15", 200, NOW, seed=7)
    b = synthetic_candles("EURUSD", "M15", 200, NOW, seed=7)
    assert a.equals(b) and len(a) == 200 and str(a.index.tz) == "UTC"
    assert (a["high"] >= a[["open", "close"]].max(axis=1)).all() and (a["low"] <= a[["open", "close"]].min(axis=1)).all()
    assert a.index[-1] == pd.Timestamp("2026-10-30 10:15", tz="UTC")       # bougie en cours
    assert not a.equals(synthetic_candles("EURUSD", "M15", 200, NOW, seed=8))


def test_sim_broker_marks_everything_as_simulation():
    broker = SimBroker(seed=1, balance=10_000, data_dir=None, clock=lambda: NOW)
    assert broker.connect() and broker.name == "sim" and broker.description.startswith("SIMULATION")
    account = broker.get_balance()
    assert account.balance == 10_000 and account.company == "SIMULATION" and account.account_type == "paper"
    tick = broker.get_current_tick("EURUSD")
    assert tick.ask > tick.bid and tick.spread_points == 10
    assert broker.get_symbol_specs("XAUUSD").digits == 2


def test_sim_positions_close_at_tp_and_update_balance():
    clock = {"now": NOW}
    broker = SimBroker(seed=1, balance=10_000, data_dir=None, clock=lambda: clock["now"])
    tick = broker.get_current_tick("EURUSD")
    result = broker.place_order("EURUSD", "BUY", 0.5, sl=tick.bid - 0.5, tp=tick.ask + 0.00001)   # TP touché au moindre mouvement
    assert result.success and result.dry_run and len(broker.get_positions()) <= 1
    for _ in range(120):                                                     # le temps passe : le prix bouge
        clock["now"] += timedelta(seconds=30)
        if not broker.get_positions():
            break
    assert broker.get_positions() == []
    [deal] = broker.get_closed_deals(NOW)
    assert deal.reason == "tp" and deal.net_profit > 0
    assert broker.get_balance().balance == pytest.approx(10_000 + deal.net_profit)
    assert [d.entry for d in broker.get_position_deals(result.ticket)] == ["in", "out"]


def test_sim_broker_reads_local_csv(tmp_path):
    frame = synthetic_candles("EURUSD", "H1", 50, NOW, seed=3).reset_index()
    frame.to_csv(tmp_path / "EURUSD_H1.csv", index=False)
    broker = SimBroker(seed=1, balance=1000, data_dir=tmp_path, clock=lambda: NOW)
    df = broker.get_historical_data("EURUSD", count=20, timeframe="H1")
    assert len(df) == 20 and df["close"].iloc[-1] == pytest.approx(frame["close"].iloc[-1])
    assert "CSV" in broker.description


def test_factory_creates_sim_broker(monkeypatch):
    from brokers import factory
    assert isinstance(factory.create_broker("sim"), SimBroker)


# ----------------------------------------------------------------- données de tendance (vagues : pivots marqués)

def _ohlc(closes, freq="1h", seed=0):
    closes = np.asarray(closes, dtype=float)
    opens = np.concatenate([[closes[0]], closes[:-1]])
    wick_up, wick_down = np.random.default_rng(seed).uniform(0.0001, 0.0005, (2, len(closes)))
    return pd.DataFrame({"open": opens, "high": np.maximum(opens, closes) + wick_up,
                         "low": np.minimum(opens, closes) - wick_down, "close": closes},
                        index=pd.date_range("2026-01-01", periods=len(closes), freq=freq, tz="UTC"))


def _trend(n=260, slope=0.0006, amplitude=0.002, period=10):
    i = np.arange(n)
    return _ohlc(1.10 + slope * i + amplitude * np.sin(2 * np.pi * i / period))


# ----------------------------------------------------------------- taille et contrôle du risque d'un setup prêt

def test_size_applies_risk_rules_to_a_ready_setup(eurusd_specs, account):
    from tests.test_strategies import _pullback
    setup, *_ = _pullback()
    assert setup.status == SetupStatus.READY
    broker = FakeBroker()
    broker.account, broker.specs = account, {"EURUSD": eurusd_specs}
    broker.ticks = {"EURUSD": eurusd_tick_at(setup.entry_price)}
    broker.profit_fn = lambda side, symbol, volume, o, c: (c - o if side == "BUY" else o - c) / 0.00001 * volume
    broker.margin_fn = lambda *a: 100.0
    sized = PullbackStrategy().size(setup, broker)
    assert sized.volume > 0 and sized.risk_amount > 0
    assert sized.status in (SetupStatus.READY, SetupStatus.INVALIDATED)
    if sized.status == SetupStatus.READY:
        assert sized.risk_pct <= settings.risk.risk_per_trade_pct * 1.05


def eurusd_tick_at(price):
    from brokers.models import Tick
    return Tick("EURUSD", price - 0.00002, price, 0.0, 2, NOW)


# ----------------------------------------------------------------- mode analyste

def test_analyst_mode_runs_in_simulation(monkeypatch, capsys):
    import main
    from brokers import factory
    monkeypatch.setattr(factory, "get_broker", lambda: SimBroker(seed=1, balance=10_000, data_dir=None,
                                                                  clock=lambda: NOW))
    assert main.main(["--analyst"]) == 0
    out = capsys.readouterr().out
    assert "MODE SIMULATION" in out and "EURUSD" in out and "XAUUSD" in out
