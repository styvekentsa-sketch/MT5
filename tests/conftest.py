"""
Configuration commune des tests : environnement de test déterministe, sans MT5 ni réseau.

Les variables sont fixées AVANT l'import de `config` (chargé à l'import des modules testés) ;
elles priment sur un éventuel `.env` (python-dotenv n'écrase pas l'environnement).
"""

from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

TEST_API_KEY = "test-api-key-0123456789"
TEST_WEBHOOK_SECRET = "test-webhook-secret-987654"

os.environ.update({
    "DRY_RUN": "true",
    "MT5_ACCOUNT": "12345678",
    "MT5_PASSWORD": "test-password",
    "MT5_SERVER": "Test-Server",
    "MT5_PATH": "",
    "TRADING_SYMBOLS": "EURUSD,XAUUSD",
    "TRADING_TIMEFRAME": "M15",
    "STRATEGY_PROFILE": "standard",                     # indépendant du profil choisi dans le .env
    # Breakout momentum : valeurs par défaut du code, quels que soient les réglages du .env
    "STRATEGY_MOMENTUM_RESTART_BODY_ATR": "0",
    "IMPULSE_ENABLED": "false",                        # moteur : activée test par test
    "STRATEGY_SCALP_ENABLED": "false",
    "OPPORTUNITY_SYMBOLS": "",
    "STRATEGY_MOMENTUM_TARGET_RR": "3.0",
    "STRATEGY_MOMENTUM_BREAKEVEN_R": "0",
    "STRATEGY_MOMENTUM_TRAILING_ACTIVATION_R": "0",
    "STRATEGY_MOMENTUM_MIN_H1_ADX": "22",
    "STRATEGY_MOMENTUM_H1_OBSTACLE_ATR": "1.0",
    "STRATEGY_MOMENTUM_H1_EMA_ALIGNMENT": "true",
    "STRATEGY_MARGIN_MULTIPLIERS": "XAUUSD:1.5,XAGUSD:1.5",
    "RISK_MIN_NET_REWARD_RISK": "3.0",
    "RISK_ALLOW_MANUAL_REAL_ACCOUNT": "false",
    "RISK_MAX_CURRENCY_RISK_PCT": "1.5",
    "RISK_MAX_DRAWDOWN_PCT": "0",                      # suspension activée test par test (plus haut persistant)
    "EXIT_TP1_FRACTION": "0",
    "EXIT_TP2_FRACTION": "0",
    "SHIELD_ACTION": "none",
    "PROTECT_ENABLED": "false",                        # protection automatique : activée test par test
    "TOPDOWN_ENABLED": "false",                        # analyse descendante : activée test par test
    "API_VIEWER_KEY": "",                              # clé en lecture seule : activée test par test
    "SHIELD_CLOSE_CONTRARY": "true",
    "LEARNING_ENABLED": "false",                       # mémoire des erreurs : activée test par test (tmp_path)
    "NEWS_RSS_FEEDS": "",
    "NEWS_DEFAULT_FEEDS": "false",                     # aucun accès réseau pendant les tests
    "NEWS_CALENDAR_PROVIDER": "simulated",             # calendrier vide sans réseau (tests : fournisseur injecté)
    "SENTIMENT_ENGINE": "vader",
    "API_ENABLED": "false",
    "API_KEY": TEST_API_KEY,
    "WEBHOOK_SECRET": TEST_WEBHOOK_SECRET,
    "LOG_LEVEL": "WARNING",
    "LOG_DIR": str(Path(tempfile.gettempdir()) / "mt5bot-tests-logs"),
    "DB_PATH": str(Path(tempfile.gettempdir()) / "mt5bot-tests.db"),
})

from brokers.mt5_client import AccountInfo, SymbolSpecs, Tick  # noqa: E402
from utils import database  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_database(monkeypatch):
    """Chaque test dispose d'une base SQLite en mémoire, vide et isolée."""
    db = database.Database(database.MEMORY)
    monkeypatch.setattr(database, "_database", db)
    yield db
    db.close()


@pytest.fixture
def account() -> AccountInfo:
    return AccountInfo(login=12345678, balance=10_000.0, equity=10_000.0, margin=0.0, margin_free=10_000.0,
                       margin_level=0.0, profit=0.0, currency="EUR", leverage=100)


@pytest.fixture
def eurusd_specs() -> SymbolSpecs:
    # 1 tick (0.00001) vaut 1.0 en devise du compte pour 1 lot
    return SymbolSpecs(symbol="EURUSD", digits=5, point=0.00001, tick_size=0.00001, tick_value=1.0,
                       volume_min=0.01, volume_max=100.0, volume_step=0.01, stops_level=10, freeze_level=0,
                       trade_allowed=True)


@pytest.fixture
def eurusd_tick() -> Tick:
    return Tick(symbol="EURUSD", bid=1.10000, ask=1.10010, last=0.0, spread_points=10,
                time=datetime.now(timezone.utc))


def fake_calc_profit(side: str, symbol: str, volume: float, price_open: float, price_close: float) -> float:
    """Équivalent de mt5.order_calc_profit pour EURUSD (tick 0.00001 = 1.0 par lot)."""
    diff = price_close - price_open if side == "BUY" else price_open - price_close
    return diff / 0.00001 * 1.0 * volume


def pytest_configure(config):
    config.addinivalue_line("markers", "real_verifier: le vérificateur indépendant n'est pas court-circuité")


@pytest.fixture(autouse=True)
def _approving_verifier(request, monkeypatch):
    """
    Les tests du moteur simulent le pipeline (prix, risque) : le vérificateur indépendant y est
    court-circuité. Les tests qui l'éprouvent portent le marqueur `real_verifier`.
    """
    if "real_verifier" in request.keywords:
        return
    from core import engine
    from risk.verifier import Verdict

    monkeypatch.setattr(engine, "verify_order", lambda *args, **kwargs: Verdict(True))
