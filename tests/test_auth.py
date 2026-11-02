"""Connexion dynamique à un compte MT5 : bascule, retour arrière, API, sécurité du mot de passe."""

import logging
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from api.routes import auth
from brokers import base as broker_base
from brokers import mt5_client
from brokers.models import AccountInfo
from config import settings
from core.engine import TradingEngine
from tests.conftest import TEST_API_KEY
from tests.fakes import FakeBroker

AUTH = {"X-API-Key": TEST_API_KEY}
SECRET = "S3cret-de-test!"


# ----------------------------------------------------------------- couche MT5 (terminal simulé)

class FakeTerminal:
    """Sous-ensemble de MetaTrader5 : un terminal, un compte actif, des mots de passe connus."""

    def __init__(self, accounts):
        self.accounts = accounts                     # login -> (mot de passe, serveur)
        self.current = settings.mt5.account
        self.error = (1, "Success")
        self.logins = []

    def terminal_info(self):
        return SimpleNamespace(connected=True, trade_allowed=False)

    def initialize(self, *args, **kwargs):
        return True

    def login(self, login, password, server, timeout):
        self.logins.append(login)
        if self.accounts.get(login) == (password, server):
            self.current, self.error = login, (1, "Success")
            return True
        self.current, self.error = None, (-6, "Terminal: Authorization failed")
        return False

    def account_info(self):
        return SimpleNamespace(login=self.current, name="Proche", server="Srv") if self.current else None

    def last_error(self):
        return self.error


@pytest.fixture
def terminal(monkeypatch):
    fake = FakeTerminal({settings.mt5.account: (settings.mt5.password, settings.mt5.server),
                         777: (SECRET, "Broker-Demo")})
    monkeypatch.setattr(mt5_client, "mt5", fake)
    monkeypatch.setattr(mt5_client, "_session", None)
    return fake


def test_switch_account_success_sets_active_credentials(terminal):
    ok, message = mt5_client.switch_account(777, SECRET, "Broker-Demo")
    assert ok and "777" in message
    assert mt5_client.active_credentials().login == 777 and mt5_client.is_dashboard_session()
    assert SECRET not in repr(mt5_client.active_credentials())               # mot de passe absent du repr

    ok, _ = mt5_client.reset_account()
    assert ok and not mt5_client.is_dashboard_session()
    assert mt5_client.active_credentials().login == settings.mt5.account


def test_failed_login_restores_previous_account(terminal):
    ok, message = mt5_client.switch_account(777, "mauvais", "Broker-Demo")
    assert not ok and "identifiants refusés" in message
    assert terminal.logins == [777, settings.mt5.account]                    # retour au compte précédent
    assert terminal.current == settings.mt5.account and not mt5_client.is_dashboard_session()


# ----------------------------------------------------------------- API

class SwitchableBroker(FakeBroker):
    supports_account_switch = True

    def __init__(self):
        super().__init__()
        self.dashboard = False
        self.account = AccountInfo(login=111, balance=1000, equity=1000, margin=0, margin_free=1000, margin_level=0,
                                   profit=0, currency="USD", leverage=100, name="Moi", server="Env-Demo",
                                   account_type="demo")

    def switch_account(self, login, password, server):
        if password != SECRET:
            return False, "Échec de connexion MT5 : identifiants refusés"
        self.dashboard = True
        self.account = AccountInfo(login=login, balance=5000, equity=5000, margin=0, margin_free=5000, margin_level=0,
                                   profit=0, currency="EUR", leverage=30, name="Proche", company="Exness",
                                   server=server, account_type="real")
        return True, f"Connecté au compte {login} @ {server}"

    def reset_account(self):
        self.dashboard = False
        return True, "ok"

    def is_dashboard_session(self):
        return self.dashboard

    def trading_enabled(self):
        return False


@pytest.fixture
def client():
    auth.reset_rate_limit()
    broker = SwitchableBroker()
    engine = TradingEngine(symbols=["EURUSD"], broker=broker)
    broker_base.set_dry_run(False)                                            # bot en LIVE avant la connexion
    test_client = TestClient(create_app(engine))
    test_client.engine = engine
    yield test_client
    broker_base.set_dry_run(True)
    auth.reset_rate_limit()


def test_login_switches_account_pauses_and_forces_paper(client, caplog):
    caplog.set_level(logging.DEBUG)
    response = client.post("/api/auth/login", headers=AUTH,
                           json={"login": 777, "password": SECRET, "server": " Exness-MT5Real "})
    body = response.json()
    assert response.status_code == 200
    assert (body["source"], body["login"], body["server"], body["name"]) == ("dashboard", 777, "Exness-MT5Real", "Proche")
    assert body["mode"] == "PAPER" and body["engine_paused"] is True and client.engine.is_paused
    assert body["algo_trading"] is False and body["account_type"] == "real"
    assert SECRET not in response.text and SECRET not in caplog.text          # jamais renvoyé ni journalisé

    session = client.get("/api/auth/session", headers=AUTH).json()
    assert session["source"] == "dashboard" and session["login"] == 777


def test_wrong_password_returns_clear_error(client):
    response = client.post("/api/auth/login", headers=AUTH, json={"login": 777, "password": "faux", "server": "X-Demo"})
    assert response.status_code == 403 and "identifiants refusés" in response.json()["detail"]
    assert client.get("/api/auth/session", headers=AUTH).json()["source"] == "env"


def test_failed_attempts_are_rate_limited(client):
    for _ in range(auth.MAX_FAILED_ATTEMPTS):
        assert client.post("/api/auth/login", headers=AUTH,
                           json={"login": 777, "password": "faux", "server": "X-Demo"}).status_code == 403
    blocked = client.post("/api/auth/login", headers=AUTH, json={"login": 777, "password": SECRET, "server": "X-Demo"})
    assert blocked.status_code == 429 and "Trop de tentatives" in blocked.json()["detail"]


@pytest.mark.parametrize("payload", [
    {"login": 0, "password": SECRET, "server": "X-Demo"},
    {"login": 777, "password": "", "server": "X-Demo"},
    {"login": 777, "password": SECRET, "server": ""},
    {"login": "abc", "password": SECRET, "server": "X-Demo"},
])
def test_invalid_payloads(client, payload):
    assert client.post("/api/auth/login", headers=AUTH, json=payload).status_code == 422


def test_logout_returns_to_env_account(client):
    client.post("/api/auth/login", headers=AUTH, json={"login": 777, "password": SECRET, "server": "X-Demo"})
    body = client.post("/api/auth/logout", headers=AUTH).json()
    assert body["source"] == "env" and body["engine_paused"] is True and "Déconnecté" in body["message"]
    again = client.post("/api/auth/logout", headers=AUTH).json()
    assert "Aucune session" in again["message"]


def test_auth_routes_require_api_key_and_mt5(client):
    assert client.post("/api/auth/login", json={"login": 777, "password": SECRET, "server": "X"}).status_code == 401
    other = TestClient(create_app(TradingEngine(symbols=["EURUSD"], broker=FakeBroker())))
    response = other.post("/api/auth/login", headers=AUTH, json={"login": 777, "password": SECRET, "server": "X-Demo"})
    assert response.status_code == 400 and "MT5" in response.json()["detail"]


def test_failed_attempt_keeps_previous_trading_state(client):
    """Une faute de frappe d'un proche ne doit pas arrêter le bot sur le compte en cours."""
    assert not client.engine.is_paused and not broker_base.is_dry_run()
    client.post("/api/auth/login", headers=AUTH, json={"login": 777, "password": "faux", "server": "X-Demo"})
    assert not client.engine.is_paused and not broker_base.is_dry_run()         # LIVE et actif comme avant
