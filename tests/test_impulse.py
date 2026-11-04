"""
Stratégie d'impulsion (rattrapage de tendance) : détection d'un mouvement violent et propre,
stop loss ATR obligatoire, ratio net, sessions ; intégration au moteur quand la stratégie
principale dit HOLD ; reprise de l'état après un redémarrage.
"""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pandas as pd
import pytest

from brokers.models import AccountInfo, OrderResult, Position, POSITION_TYPE_BUY, Tick
from config.settings import settings
from core import engine as eng
from core.engine import TradingEngine
from models.schemas import MarketRegime, SetupStatus, Side
from strategies import impulse as impulse_module
from strategies.base import Quote, mirror_frame, mirror_quote, net_reward_risk
from strategies.impulse import ImpulseStrategy, impulse
from tests.fakes import FakeBroker
from tests.test_strategies import _h1_context

FLAT_BARS = 60
IMPULSE_END = pd.Timestamp("2026-11-03 08:00", tz="UTC")         # dernière bougie : 08:00-08:15 (Londres)
STRONG = [(1.1000, 1.1006, 1.0999, 1.1005), (1.1005, 1.1011, 1.1004, 1.1010), (1.1010, 1.1017, 1.1009, 1.1016)]


def _frame(impulse_bars=STRONG, end=IMPULSE_END) -> pd.DataFrame:
    """M15 : range calme (ATR ~0.0006), puis les bougies d'impulsion ; la dernière ouvre à `end`."""
    rows = []
    for i in range(FLAT_BARS):
        o, c = (1.0997, 1.1001) if i % 2 == 0 else (1.1001, 1.0997)
        rows.append((o, max(o, c) + 0.0001, min(o, c) - 0.0001, c))
    rows += list(impulse_bars)
    index = pd.date_range(end=end, periods=len(rows), freq="15min")
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=index)


def _quote(trig, drift=0.00002) -> Quote:
    close = float(trig["close"].iloc[-1])
    return Quote(bid=close + drift - 0.00002, ask=close + drift, tick_size=0.00001)


def _decision_time(trig) -> datetime:
    return (trig.index[-1] + pd.Timedelta(minutes=15)).to_pydatetime()


def _find(trig=None, ctx=None, quote=None):
    trig = _frame() if trig is None else trig
    ctx = _h1_context() if ctx is None else ctx
    quote = quote or _quote(trig)
    return ImpulseStrategy().find_setup("EURUSD", {"H1": ctx, "M15": trig}, _decision_time(trig), quote), trig, quote


# ----------------------------------------------------------------- détection

def test_clean_impulse_is_measured():
    move, body, wick, base_atr, recent_atr = impulse(_frame(), 3)
    assert 1.5 <= move <= 4 and body >= 0.4 and wick <= 0.35
    assert recent_atr > base_atr                                    # l'impulsion élargit l'ATR récent


@pytest.mark.parametrize("bars", [
    [STRONG[0], (1.1005, 1.1008, 1.1002, 1.1003), STRONG[2]],       # bougie baissière au milieu
    [STRONG[0], STRONG[1], (1.1010, 1.1030, 1.1009, 1.1013)],       # rejet : grande mèche haute
])
def test_dirty_moves_are_not_impulses(bars):
    assert impulse(_frame(bars), 3) is None


def test_ready_setup_with_atr_stop_and_net_ratio():
    setup, trig, quote = _find()
    assert setup.status == SetupStatus.READY, setup.notes
    assert setup.side == Side.BUY and setup.strategy == "impulsion" and setup.entry_price == quote.ask
    *_, recent_atr = impulse(trig, 3)
    assert setup.stop_loss == pytest.approx(quote.ask - settings.strategy.impulse_sl_atr * recent_atr, abs=1e-9)
    assert net_reward_risk(Side.BUY, setup.entry_price, setup.stop_loss, setup.take_profit, quote.spread) == \
        pytest.approx(settings.risk.min_net_reward_risk, abs=1e-6)
    assert "Impulsion haussière" in setup.reason


@pytest.mark.parametrize("bars", [
    [(1.1000, 1.1003, 1.0999, 1.1002), (1.1002, 1.1005, 1.1001, 1.1004), (1.1004, 1.1007, 1.1003, 1.1006)],  # mou
    [(1.1000, 1.1031, 1.0999, 1.1030), (1.1030, 1.1061, 1.1029, 1.1060), (1.1060, 1.1091, 1.1059, 1.1090)],  # épuisé
])
def test_too_weak_or_exhausted_moves_are_ignored(bars):
    setup, *_ = _find(_frame(bars))
    assert setup is None or setup.side == Side.SELL


def test_no_chase_when_the_price_already_ran():
    trig = _frame()
    setup, *_ = _find(trig, quote=_quote(trig, drift=0.0004))       # > 0.30 ATR au-dessus de la clôture
    assert setup is None


def test_no_buy_against_a_confirmed_h1_downtrend():
    down = mirror_frame(_h1_context(), 10.0)
    from core.regime_detector import detect_regime
    assert detect_regime(down).regime == MarketRegime.TREND_DOWN
    setup, *_ = _find(ctx=down)
    assert setup is None or setup.side == Side.SELL


def test_sell_impulse_is_the_mirror():
    buy, trig, quote = _find()
    k = 10.0
    sell = ImpulseStrategy().find_setup("EURUSD", {"H1": mirror_frame(_h1_context(), k), "M15": mirror_frame(trig, k)},
                                        _decision_time(trig), mirror_quote(quote, k))
    assert sell.side == Side.SELL and sell.status == SetupStatus.READY
    assert sell.stop_loss == pytest.approx(k - buy.stop_loss) and sell.take_profit == pytest.approx(k - buy.take_profit)


@pytest.mark.parametrize("end, ok", [
    ("2026-11-03 08:00", True), ("2026-11-03 13:15", True),        # Londres, New York
    ("2026-11-03 02:00", False), ("2026-11-03 11:00", False), ("2026-11-03 20:00", False),
])
def test_impulse_only_during_liquidity_sessions(end, ok):
    setup, *_ = _find(_frame(end=pd.Timestamp(end, tz="UTC")))
    assert (setup.status == SetupStatus.READY) == ok
    if not ok:
        assert "Hors session de liquidité" in setup.notes[-1]


def test_session_filter_can_be_disabled(monkeypatch):
    lenient = replace(settings.strategy, impulse_session_only=False)
    monkeypatch.setattr(impulse_module, "settings", SimpleNamespace(strategy=lenient, risk=settings.risk))
    setup, *_ = _find(_frame(end=pd.Timestamp("2026-11-03 02:00", tz="UTC")))
    assert setup.status == SetupStatus.READY


@pytest.mark.parametrize("field, value", [
    ("impulse_timeframe", "H4"), ("impulse_bars", 1), ("impulse_sl_atr", 0), ("impulse_min_move_atr", 5.0),
])
def test_invalid_impulse_settings(field, value):
    from config.env import ConfigError
    with pytest.raises(ConfigError):
        replace(settings.strategy, **{field: value}).validate()


# ----------------------------------------------------------------- moteur : HOLD -> trade d'opportunité

def _closed_plus_forming(frame: pd.DataFrame, step: pd.Timedelta) -> pd.DataFrame:
    return pd.concat([frame, frame.iloc[-1:].set_axis([frame.index[-1] + step])])


@pytest.fixture
def impulse_broker(eurusd_specs):
    trig, ctx = _frame(), _h1_context()
    quote = _quote(trig)
    broker = FakeBroker()
    broker.account = AccountInfo(login=1, balance=10_000, equity=10_000, margin=0, margin_free=10_000, margin_level=0,
                                 profit=0, currency="USD", leverage=100, account_type="demo")
    broker.specs = {"EURUSD": eurusd_specs}
    broker.ticks = {"EURUSD": Tick("EURUSD", quote.bid, quote.ask, 0.0, 2, _decision_time(trig))}
    broker.profit_fn = lambda side, symbol, volume, o, c: round((c - o if side == "BUY" else o - c) / 0.00001 * volume, 2)
    broker.margin_fn = lambda *a: 100.0
    broker.get_historical_data = lambda symbol, count=100, timeframe=None: (
        _closed_plus_forming(trig, pd.Timedelta(minutes=15)) if timeframe in ("M15", "M5")
        else _closed_plus_forming(ctx, pd.Timedelta(hours=1)))
    broker.trig = trig
    return broker


@pytest.fixture
def hold_engine(impulse_broker, monkeypatch):
    monkeypatch.setattr(eng, "evaluate_market", lambda symbol, broker: {
        "symbol": symbol, "action": "HOLD", "confidence": 0, "reason": "marché en expansion",
        "verdict": "Aucun mode actif : marché en expansion"})
    monkeypatch.setattr(eng, "is_high_impact_news_imminent", lambda symbol=None: False)
    engine = TradingEngine(symbols=["EURUSD"], broker=impulse_broker)
    engine.impulse = ImpulseStrategy()
    # heure de décision : clôture de la dernière bougie M15 (session de Londres)
    monkeypatch.setattr(impulse_module, "liquidity_session", lambda at: "Londres")
    return engine


def test_engine_trades_the_impulse_instead_of_holding(hold_engine, impulse_broker):
    [outcome] = hold_engine.step()
    assert outcome.status == "traded", outcome.verdict
    [order] = impulse_broker.orders
    assert order["action"] == "BUY" and order["sl"] < order["tp"] and order["volume"] > 0
    assert outcome.verdict.startswith("[Impulsion] BUY")
    entry = impulse_broker.ticks["EURUSD"].ask
    risk = (entry - order["sl"]) / 0.00001 * order["volume"]                 # perte au SL
    assert risk <= 0.01 * 10_000 + 1e-6                                      # 1 % maximum


def test_engine_respects_the_cooldown_after_an_impulse_trade(hold_engine, impulse_broker):
    hold_engine.step()
    [second] = hold_engine.step()
    assert second.status == "hold" and len(impulse_broker.orders) == 1


def test_the_same_impulse_is_traded_only_once(hold_engine, impulse_broker):
    hold_engine.step()
    hold_engine._last_trade_at.clear()                           # délai écoulé (ex : stop touché en 1 minute)
    [again] = hold_engine.step()
    assert again.status == "hold" and "impulsion : signal déjà tradé" in again.verdict
    assert len(impulse_broker.orders) == 1


def test_engine_keeps_holding_when_the_impulse_is_refused(hold_engine, impulse_broker, monkeypatch):
    monkeypatch.setattr(impulse_module, "liquidity_session", lambda at: None)     # hors session
    [outcome] = hold_engine.step()
    assert outcome.status == "hold" and "impulsion : signal écarté" in outcome.verdict
    assert "Hors session" in outcome.verdict and impulse_broker.orders == []


def test_engine_without_impulse_mode_only_holds(hold_engine, impulse_broker):
    hold_engine.impulse = None
    [outcome] = hold_engine.step()
    assert outcome.status == "hold" and impulse_broker.orders == []


def test_broker_rejection_of_an_impulse_order(hold_engine, impulse_broker):
    impulse_broker.place_order = lambda *a, **k: OrderResult(False, "EURUSD", "BUY", 0.1, message="requote")
    [outcome] = hold_engine.step()
    assert outcome.status == "failed" and "requote" in outcome.verdict


# ----------------------------------------------------------------- reprise après redémarrage

def _position(ticket, comment, minutes_ago, symbol="EURUSD"):
    opened = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return Position(ticket=ticket, symbol=symbol, type=POSITION_TYPE_BUY, volume=0.1, price_open=1.1, price_current=1.1,
                    sl=1.09, tp=1.12, profit=0.0, swap=0.0, time=int(opened.timestamp()),
                    magic=settings.trading.magic_number, comment=comment)


def test_restart_rebuilds_modes_cooldowns_and_market_snapshot(impulse_broker):
    impulse_broker.positions = [_position(1, "Bot SWING BUY 64%", 5), _position(2, "Bot IMPULSE BUY", 600, "XAUUSD")]
    engine = TradingEngine(symbols=["EURUSD"], broker=impulse_broker, trade_cooldown_seconds=900)
    assert engine.start()
    assert engine._position_modes == {1: "swing"}                            # mode retrouvé par le commentaire
    assert engine._in_cooldown("EURUSD") and not engine._in_cooldown("XAUUSD")   # ouverte il y a 5 min / 10 h
    snapshot = engine.market_snapshot["EURUSD"]
    assert snapshot["M15_impulse"] == "haussière" and "impulsion haussière en cours" in snapshot["summary"]


def test_restart_uses_the_server_clock_for_position_age(impulse_broker):
    """MT5 : heure d'ouverture à l'heure du serveur (UTC+2) ; une position de 5 min n'a pas 2 h 05."""
    server_now = datetime.now(timezone.utc) + timedelta(hours=2)
    position = _position(1, "Bot DAY BUY", 0)
    impulse_broker.positions = [replace(position, time=int((server_now - timedelta(minutes=5)).timestamp()))]
    impulse_broker.bar_timezone = timezone(timedelta(hours=2))
    engine = TradingEngine(symbols=["EURUSD"], broker=impulse_broker, trade_cooldown_seconds=900)
    engine.resync()
    assert engine._in_cooldown("EURUSD")


def test_restart_survives_missing_market_data(impulse_broker):
    impulse_broker.get_historical_data = lambda *a, **k: pd.DataFrame()
    engine = TradingEngine(symbols=["EURUSD"], broker=impulse_broker)
    assert engine.start()
    assert "M15 indisponible" in engine.market_snapshot["EURUSD"]["summary"]


# ----------------------------------------------------------------- liste blanche (OPPORTUNITY_SYMBOLS)

def _only(monkeypatch, *symbols):
    strategy = replace(settings.strategy, opportunity_symbols=tuple(symbols))
    monkeypatch.setattr(eng, "settings", SimpleNamespace(**{**vars(settings), "strategy": strategy}))


def test_symbol_outside_the_whitelist_keeps_the_main_strategy_only(hold_engine, impulse_broker, monkeypatch):
    _only(monkeypatch, "XAUUSD")                                     # EURUSD exclu des trades d'opportunité
    [outcome] = hold_engine.step()
    assert outcome.status == "hold" and impulse_broker.orders == []
    assert "impulsion" not in outcome.verdict                         # aucune évaluation, aucun bruit


def test_whitelisted_symbol_still_trades(hold_engine, impulse_broker, monkeypatch):
    _only(monkeypatch, "EURUSD", "XAUUSD")
    [outcome] = hold_engine.step()
    assert outcome.status == "traded" and len(impulse_broker.orders) == 1


def test_opportunity_summary_lists_roles_and_allowed_symbols(hold_engine, monkeypatch):
    from strategies.micro_scalp import MicroScalpStrategy
    hold_engine.symbols = ("EURUSD", "GBPUSD", "XAUUSD")
    hold_engine.scalp = MicroScalpStrategy()
    _only(monkeypatch, "EURUSD", "XAUUSD")
    summary = hold_engine.opportunity_summary()
    assert [m["label"] for m in summary] == ["Impulsion", "Petits trades"]
    assert all(m["symbols"] == ["EURUSD", "XAUUSD"] for m in summary)
    assert summary[0]["role"].startswith("grandes impulsions M15") and summary[1]["role"].startswith("micro-structures M5")


def test_whitelist_must_only_contain_tracked_symbols():
    from config.env import ConfigError
    bad = replace(settings, strategy=replace(settings.strategy, opportunity_symbols=("USDJPY",)))
    with pytest.raises(ConfigError, match="OPPORTUNITY_SYMBOLS"):
        bad.validate()
    assert replace(settings.strategy, opportunity_symbols=()).opportunity_allowed("GBPUSD")   # vide : tous


def test_status_exposes_the_opportunity_modes(hold_engine):
    from fastapi.testclient import TestClient

    from api.main import create_app
    from tests.conftest import TEST_API_KEY
    body = TestClient(create_app(hold_engine)).get("/api/status", headers={"X-API-Key": TEST_API_KEY}).json()
    [mode] = body["opportunities"]
    assert mode["label"] == "Impulsion" and mode["symbols"] == ["EURUSD"] and mode["timeframe"] == "M15"
