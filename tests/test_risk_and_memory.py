"""
Calculateur de taille (arrondi au pas, risque par position et cumulé), vérificateur indépendant
(ratio net, spread, limites journalières) et mémoire des erreurs appliquée aux setups.
"""

import json
from dataclasses import replace
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from brokers.models import POSITION_TYPE_BUY, AccountInfo, ClosedDeal, Position, Tick
from config import settings
from core import engine as eng
from core.engine import TradingEngine
from learning.trade_memory import TradeMemory, market_features, pattern_key, pattern_labels
from models.schemas import DiagnosticFailure, SetupStatus, SymbolDiagnostic
from risk.calculator import floor_to_step, size_position
from risk.verifier import Verdict, verify_order
from strategies.base import BaseStrategy
from tests.fakes import FakeBroker

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
MAGIC = settings.trading.magic_number


def _eurusd_profit(side, symbol, volume, price_open, price_close):
    diff = price_close - price_open if side == "BUY" else price_open - price_close
    return round(diff / 0.00001 * 1.0 * volume, 2)                 # 1 point = 1 USD pour 1 lot


@pytest.fixture
def broker(eurusd_specs):
    fake = FakeBroker()
    fake.account = AccountInfo(login=1, balance=10_000, equity=10_000, margin=0, margin_free=10_000, margin_level=0,
                               profit=0, currency="USD", leverage=100)
    fake.specs = {"EURUSD": eurusd_specs}
    fake.ticks = {"EURUSD": Tick("EURUSD", 1.10000, 1.10002, 0.0, 2, NOW)}
    fake.profit_fn = _eurusd_profit
    fake.margin_fn = lambda *a: 100.0
    return fake


# ----------------------------------------------------------------- arrondi au pas de volume

@pytest.mark.parametrize("volume, step, expected", [
    (0.4999999999999996, 0.01, 0.50),          # artefact flottant : jamais 0.49
    (0.129, 0.01, 0.12),                       # toujours vers le bas
    (0.12999999, 0.01, 0.12),
    (1.99, 0.1, 1.9),
    (7.9, 1.0, 7.0),
    (0.12345, 0.001, 0.123),                   # crypto : 0.001 BTC
    (2.5, 0.5, 2.5),
    (0.009, 0.01, 0.0),
])
def test_floor_to_step(volume, step, expected):
    assert floor_to_step(volume, step) == pytest.approx(expected)
    assert floor_to_step(volume, step) <= volume + 1e-12


@pytest.mark.parametrize("step, expected", [(0.01, 0.5), (0.1, 0.5), (0.2, 0.4), (0.5, 0.5)])
def test_size_rounds_down_to_broker_step(broker, eurusd_specs, step, expected):
    broker.specs = {"EURUSD": replace(eurusd_specs, volume_step=step, volume_min=step)}
    result = size_position("EURUSD", 1.1000, 1.0980, broker)       # 1 % de 10 000 = 100 ; 20 pips = 200 / lot
    assert result.volume == pytest.approx(expected) and result.risk_amount <= result.per_trade_budget + 1e-9
    assert result.limited_by == "risque par position"


def test_minimum_volume_above_budget_is_rejected(broker, eurusd_specs):
    broker.specs = {"EURUSD": replace(eurusd_specs, volume_min=1.0, volume_step=1.0)}
    result = size_position("EURUSD", 1.1000, 1.0980, broker)
    assert result.volume == 0 and "volume minimal" in result.reason


def test_contract_size_used_when_broker_has_no_profit_calculation(broker, eurusd_specs):
    broker.profit_fn = None
    broker.specs = {"EURUSD": replace(eurusd_specs, tick_value=0.0, contract_size=100_000)}
    result = size_position("EURUSD", 1.1000, 1.0980, broker)       # 0.0020 x 100 000 = 200 par lot
    assert result.loss_per_lot == pytest.approx(200.0) and result.volume == pytest.approx(0.5)


def _open_position(ticket, risk_pips, sl=True):
    entry = 1.2000
    return Position(ticket=ticket, symbol="EURUSD", type=POSITION_TYPE_BUY, volume=0.5, price_open=entry,
                    price_current=entry, sl=(entry - risk_pips * 0.0001) if sl else 0.0, tp=0.0, profit=0.0, swap=0.0,
                    time=0, magic=MAGIC)


def test_cumulative_cap_reduces_size(broker):
    broker.positions = [_open_position(1, risk_pips=30)]           # 0.5 lot x 30 pips = 150 de risque ouvert
    result = size_position("EURUSD", 1.1000, 1.1020, broker)       # budget cumulé restant : 200 - 150 = 50
    assert result.open_risk == pytest.approx(150) and result.cumulative_budget_left == pytest.approx(50)
    assert result.volume == pytest.approx(0.25) and result.limited_by == "risque cumulé"   # vente : pas corrélée
    assert result.volume * result.loss_per_lot + result.open_risk <= 0.02 * 10_000 + 1e-9


def test_correlated_exposure_reduces_size_in_the_same_direction(broker):
    broker.positions = [_open_position(1, risk_pips=20)]           # achat EURUSD : EUR acheteur / USD vendeur, 100
    same = size_position("EURUSD", 1.1000, 1.0980, broker)          # limite par devise 1.5 % = 150 : reste 50
    assert same.volume == pytest.approx(0.25) and same.limited_by.startswith("exposition corrélée")
    opposite = size_position("EURUSD", 1.1000, 1.1020, broker)      # sens inverse : seule la limite cumulée joue
    assert opposite.volume == pytest.approx(0.5) and opposite.limited_by == "risque par position"


def test_currency_exposure_of_gold_and_pairs():
    from risk.risk_manager import currency_exposure
    assert currency_exposure("EURUSD", "BUY") == {"EUR": 1, "USD": -1}
    assert currency_exposure("XAUUSD", "BUY") == {"XAU": 1, "USD": -1}      # même sens USD qu'un achat EURUSD
    assert currency_exposure("USDJPY", "SELL") == {"USD": -1, "JPY": 1}


def test_cumulative_cap_reached_and_unbounded_positions(broker):
    broker.positions = [_open_position(1, risk_pips=40)]           # 200 : tout le risque cumulé est utilisé
    assert size_position("EURUSD", 1.1000, 1.0980, broker).volume == 0
    broker.positions = [_open_position(2, risk_pips=10, sl=False)]
    result = size_position("EURUSD", 1.1000, 1.0980, broker)
    assert result.volume == 0 and "sans SL" in result.reason


# ----------------------------------------------------------------- vérificateur indépendant

def test_verifier_approves_a_clean_order(broker):
    verdict = verify_order("EURUSD", "BUY", 1.10002, 1.09802, 1.10702, broker)   # ratio net ~3.5
    assert verdict.approved, verdict.reason
    assert verdict.net_reward_risk >= 3 and verdict.spread_to_sl_pct == pytest.approx(1.0)


@pytest.mark.parametrize("setup_broker, order, needle", [
    (lambda b: None, (1.10002, 1.09802, 1.10402), "ratio net estimé"),                      # 2R : insuffisant
    (lambda b: None, (1.10002, 1.09802, None), "TP) absent"),
    (lambda b: b.ticks.update(EURUSD=Tick("EURUSD", 1.1, 1.10025, 0, 25, NOW)), (1.10025, 1.10005, 1.1015),
     "distance entrée-stop"),                                                                 # spread 25 % du stop
    (lambda b: b.ticks.update(EURUSD=Tick("EURUSD", 1.1, 1.10035, 0, 35, NOW)), (1.10035, 1.09535, 1.1260),
     "plafond 30 pts"),
])
def test_verifier_blocks(broker, setup_broker, order, needle):
    setup_broker(broker)
    verdict = verify_order("EURUSD", "BUY", *order, broker)
    assert not verdict.approved and any(needle in f for f in verdict.failures), verdict.failures


def test_verifier_blocks_spread_twice_its_median_and_stale_quotes(broker):
    broker.ticks = {"EURUSD": Tick("EURUSD", 1.1, 1.10012, 0, 12, NOW)}
    broker.diagnose_symbol = lambda symbol, entry=None, sl=None: SymbolDiagnostic(
        symbol=symbol, found=True, median_spread_points_60m=5.0,
        failures=[DiagnosticFailure(code="stale_tick", message="prix figé depuis 600 s")])
    verdict = verify_order("EURUSD", "BUY", 1.10012, 1.09612, 1.1180, broker)
    assert not verdict.approved
    assert any("2 x sa médiane" in f for f in verdict.failures) and any("prix figé" in f for f in verdict.failures)


def test_gold_spread_cap(broker, eurusd_specs):
    broker.specs["XAUUSD"] = replace(eurusd_specs, symbol="XAUUSD", digits=2, point=0.01, tick_size=0.01)
    broker.ticks["XAUUSD"] = Tick("XAUUSD", 2400.00, 2400.40, 0, 40, NOW)
    verdict = verify_order("XAUUSD", "BUY", 2400.40, 2380.40, 2470.0, broker)               # ratio et stop corrects
    assert not verdict.approved and verdict.failures == ("spread 40 pts > plafond 30 pts",)


def test_verifier_blocks_daily_loss_and_two_consecutive_losses(broker):
    broker.account = replace(broker.account, balance=9_700, equity=9_700)
    broker.deals = [ClosedDeal(1, "EURUSD", NOW, -300.0, MAGIC)]                         # -3 % aujourd'hui
    verdict = verify_order("EURUSD", "BUY", 1.10002, 1.09802, 1.10702, broker)
    assert any("perte journalière 3.00%" in f for f in verdict.failures)

    broker.account = replace(broker.account, balance=9_980, equity=9_980)
    broker.deals = [ClosedDeal(1, "EURUSD", NOW, -10.0, MAGIC), ClosedDeal(2, "EURUSD", NOW, -10.0, MAGIC)]
    verdict = verify_order("EURUSD", "BUY", 1.10002, 1.09802, 1.10702, broker)
    assert verdict.failures == ("2 pertes consécutives aujourd'hui (maximum 2)",)
    broker.deals.append(ClosedDeal(3, "EURUSD", NOW, 5.0, MAGIC))                       # un gain remet à zéro
    assert verify_order("EURUSD", "BUY", 1.10002, 1.09802, 1.10702, broker).approved


def test_verifier_never_raises(broker):
    broker.get_account_info = lambda: (_ for _ in ()).throw(RuntimeError("terminal planté"))
    verdict = verify_order("EURUSD", "BUY", 1.10002, 1.09802, 1.10702, broker)
    assert not verdict.approved and "erreur du vérificateur" in verdict.failures[0]


@pytest.mark.real_verifier
def test_engine_blocks_orders_rejected_by_the_verifier(monkeypatch):
    fake = FakeBroker()
    monkeypatch.setattr(eng, "is_high_impact_news_imminent", lambda symbol: False)
    monkeypatch.setattr(eng, "evaluate_market", lambda symbol, broker: {
        "action": "BUY", "entry_price": 1.1, "suggested_sl": 1.098, "suggested_tp": 1.102, "confidence": 50})
    monkeypatch.setattr(eng, "calculate_position_size", lambda *a, **k: 0.5)
    monkeypatch.setattr(eng, "validate_trade_risk", lambda *a, **k: {"is_approved": True, "reason": "ok"})
    monkeypatch.setattr(eng, "verify_order", lambda *a, **k: Verdict(False, ("ratio net estimé 0.95 < 3",)))
    [outcome] = TradingEngine(symbols=["EURUSD"], broker=fake).step()
    assert outcome.status == "rejected" and outcome.verdict.startswith("Vérificateur indépendant : ratio net")
    assert fake.orders == []


# ----------------------------------------------------------------- mémoire des erreurs appliquée aux setups

def _ready_setup():
    from tests.test_strategies import _pullback
    setup, *_ = _pullback()
    assert setup.status == SetupStatus.READY
    return setup


@pytest.fixture
def market_broker(broker):
    i = np.arange(300)
    closes = 1.1 + 0.0004 * np.sin(i / 3)
    broker.bars = pd.DataFrame({"open": closes, "high": closes + 0.0003, "low": closes - 0.0003, "close": closes},
                               index=pd.date_range("2026-10-01", periods=300, freq="15min", tz="UTC"))
    return broker


def test_validated_setup_is_journaled(market_broker, tmp_path):
    memory = TradeMemory(tmp_path)
    setup = BaseStrategy.remember(_ready_setup(), market_broker, memory)
    assert setup.status == SetupStatus.READY
    [entry] = json.loads((tmp_path / "trade_decisions.json").read_text(encoding="utf-8"))
    assert entry["status"] == "setup" and entry["setup_id"] == setup.id and entry["strategy"] == "pullback"
    assert entry["sl"] == setup.stop_loss and entry["tp"] == setup.take_profit and entry["reason"] == setup.reason
    assert {"rsi", "atr", "spread_points"} <= set(entry["features"]) and "setup_marge_b" in entry["features"]
    assert memory.review(market_broker) == []                                 # un setup n'est jamais « clos »


def test_setup_resembling_a_repeated_losing_pattern_is_blocked(market_broker, tmp_path):
    setup = _ready_setup()
    features = market_features(market_broker, setup.symbol, {
        "mode": setup.style.value, "entry_price": setup.entry_price, "suggested_sl": setup.stop_loss,
        "sentiment_score": 0.0})
    key = pattern_key(setup.symbol, setup.style.value, setup.side.value, pattern_labels(setup.side.value, features))
    (tmp_path / "mistakes_memory.json").write_text(json.dumps({"patterns": {key: {
        "key": key, "losses": 2, "wins": 0, "loss_tickets": [7701, 7702], "main_factor": "Bruit de marché",
        "last_loss_at": datetime.now(timezone.utc).isoformat()}}}), encoding="utf-8")

    blocked = BaseStrategy.remember(setup, market_broker, TradeMemory(tmp_path))
    assert blocked.status == SetupStatus.INVALIDATED
    assert "Ordre rejeté par la mémoire des erreurs - pattern similaire au trade perdant #7702" in blocked.notes[-1]
    assert not (tmp_path / "trade_decisions.json").exists()                  # rien de journalisé


@pytest.mark.parametrize("target", [2.5, 3.0])
def test_verifier_accepts_a_target_computed_exactly_at_the_minimum(broker, monkeypatch, target):
    """TP calculé au ratio minimal exact : l'arrondi flottant (2.4999999) ne doit pas faire refuser l'ordre."""
    from types import SimpleNamespace

    from models.schemas import Side
    from risk import verifier
    from risk.calculator import net_reward_risk, target_for_ratio

    risk = replace(settings.risk, min_net_reward_risk=target)
    monkeypatch.setattr(verifier, "settings", SimpleNamespace(risk=risk, trading=settings.trading))
    tick = broker.ticks["EURUSD"]
    spread = tick.ask - tick.bid                                  # le coût utilisé par le vérificateur
    orders = [(tick.ask, round(tick.ask - d / 100_000, 5)) for d in range(80, 400)]
    orders = [(e, s, target_for_ratio(Side.BUY, e, s, target, spread)) for e, s in orders]
    below = [o for o in orders if net_reward_risk(Side.BUY, *o, spread) < target]       # 2.4999999...
    if target == 2.5:
        assert below, "aucun cas d'arrondi reproduit"
    for entry, stop, tp in (below or orders)[:20]:
        verdict = verify_order("EURUSD", "BUY", entry, stop, tp, broker)
        assert not any("ratio net" in f for f in verdict.failures), verdict.failures
