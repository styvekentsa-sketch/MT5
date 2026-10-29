"""Multi-comptes : configuration, isolation des secrets, passerelle API, superviseur, alertes."""

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
import requests
from fastapi.testclient import TestClient

from api import gateway
from config import ConfigError, settings
from config.accounts import WORKER_PORT_BASE, WORKER_ROLE, AccountSpec, load_accounts, validate_accounts, worker_env
from core import supervisor as sup
from tests.conftest import TEST_API_KEY

AUTH = {"X-API-Key": TEST_API_KEY}


@pytest.fixture
def terminals(tmp_path):
    paths = []
    for name in ("main", "test"):
        exe = tmp_path / name / "terminal64.exe"
        exe.parent.mkdir()
        exe.write_text("")
        paths.append(str(exe))
    return paths


def _env(terminals, **extra):
    env = {
        "MT5_ACCOUNTS": "main, test",
        "ACCOUNT_MAIN_LOGIN": "111", "ACCOUNT_MAIN_PASSWORD": "pw-main", "ACCOUNT_MAIN_SERVER": "Broker-Live",
        "ACCOUNT_MAIN_PATH": terminals[0], "ACCOUNT_MAIN_LABEL": "Principal",
        "ACCOUNT_TEST_LOGIN": "222", "ACCOUNT_TEST_PASSWORD": "pw-test", "ACCOUNT_TEST_SERVER": "Broker-Demo",
        "ACCOUNT_TEST_PATH": terminals[1], "ACCOUNT_TEST_RISK_PER_TRADE_PCT": "0.5", "ACCOUNT_TEST_DRY_RUN": "true",
        "API_KEY": "shared-api-key-0123456789",
    }
    env.update(extra)
    return env


# ----------------------------------------------------------------- configuration

def test_load_accounts(terminals):
    main, test = load_accounts(_env(terminals))
    assert (main.id, main.label, main.login, main.port) == ("main", "Principal", 111, WORKER_PORT_BASE)
    assert (test.id, test.label, test.port) == ("test", "test", WORKER_PORT_BASE + 1)
    assert dict(test.overrides) == {"RISK_PER_TRADE_PCT": "0.5", "DRY_RUN": "true"}
    assert "pw-main" not in repr(main)                                     # secret masqué
    validate_accounts((main, test))


def test_single_mode_and_worker_role_have_no_accounts(terminals):
    assert load_accounts({}) == ()
    assert load_accounts({**_env(terminals), "BOT_ROLE": WORKER_ROLE}) == ()  # un compte ne se ré-supervise pas


@pytest.mark.parametrize("change, fragment", [
    ({"MT5_ACCOUNTS": "Main!"}, "invalide"),
    ({"ACCOUNT_MAIN_PASSWORD": ""}, "manquantes"),
    ({"ACCOUNT_MAIN_LOGIN": "abc"}, "LOGIN"),
    ({"ACCOUNT_TEST_API_PORT": "9000"}, "non surchargeables"),
])
def test_invalid_account_config(terminals, change, fragment):
    with pytest.raises(ConfigError, match=fragment):
        load_accounts(_env(terminals, **change))


def test_shared_terminal_or_login_is_rejected(terminals):
    with pytest.raises(ConfigError, match="terminal"):
        validate_accounts(load_accounts(_env(terminals, ACCOUNT_TEST_PATH=terminals[0])))
    with pytest.raises(ConfigError, match="login"):
        validate_accounts(load_accounts(_env(terminals, ACCOUNT_TEST_LOGIN="111")))
    with pytest.raises(ConfigError, match="introuvable"):
        validate_accounts(load_accounts(_env(terminals, ACCOUNT_TEST_PATH="Z:/absent/terminal64.exe")))


def test_worker_env_isolates_each_account(terminals):
    base = _env(terminals)
    main, test = load_accounts(base)
    env = worker_env(test, base)
    assert (env["MT5_ACCOUNT"], env["MT5_PASSWORD"], env["MT5_SERVER"]) == ("222", "pw-test", "Broker-Demo")
    assert env["RISK_PER_TRADE_PCT"] == "0.5" and env["DRY_RUN"] == "true"   # risque propre au compte
    assert env["BOT_ROLE"] == WORKER_ROLE and env["BOT_ACCOUNT_LABEL"] == "test"
    assert (env["API_HOST"], env["API_PORT"]) == ("127.0.0.1", str(WORKER_PORT_BASE + 1))
    assert env["DB_PATH"].endswith("trading_bot_test.db") and env["LOG_DIR"].endswith("test")
    assert "pw-main" not in env.values()                                    # secrets des autres comptes retirés
    assert not any(k.startswith("ACCOUNT_") for k in env) and "MT5_ACCOUNTS" not in env
    assert env["API_KEY"] == base["API_KEY"]


# ----------------------------------------------------------------- passerelle

ACCOUNTS = (AccountSpec("main", "Principal", 111, "x", "Broker-Live", "a", 8101),
            AccountSpec("test", "Test", 222, "y", "Broker-Demo", "b", 8102))


class FakeWorkers:
    """Remplace les API internes des comptes ; le compte « test » est arrêté."""

    def __init__(self):
        self.calls = []

    def __call__(self, account, method, path, *, params=None, body=None, content_type=None, timeout=None):
        self.calls.append((account.id, method, path, params, body))
        if account.id == "test":
            raise requests.ConnectionError("refused")
        payloads = {
            "/api/status": {"state": "running", "mode": "LIVE", "mt5_connected": True},
            "/api/account": {"currency": "USD", "balance": 5000.0, "equity": 5100.0, "daily_pl": 100.0,
                             "daily_pl_pct": 2.0, "floating_pl": 100.0},
            "/api/positions?bot_only=false": [{"ticket": 1}, {"ticket": 2}],
            "/api/positions": [{"ticket": 1}],
        }
        if path.startswith("/api/webhooks/"):
            return SimpleNamespace(status_code=202, content=b'{"accepted": true}',
                                   headers={"content-type": "application/json"}, raise_for_status=lambda: None)
        body_json = payloads.get(path, {"echo": path, "params": params, "body": (body or b"").decode()})
        return SimpleNamespace(status_code=200, content=json.dumps(body_json).encode(),
                               headers={"content-type": "application/json"}, json=lambda: body_json,
                               raise_for_status=lambda: None)


@pytest.fixture
def gateway_client(monkeypatch):
    fake = FakeWorkers()
    monkeypatch.setattr(gateway, "_worker_request", fake)
    client = TestClient(gateway.create_gateway_app(ACCOUNTS))
    client.fake = fake
    return client


def test_accounts_summary_with_one_account_down(gateway_client):
    body = gateway_client.get("/api/accounts", headers=AUTH).json()["accounts"]
    main, test = body
    assert main["reachable"] and main["mode"] == "LIVE" and main["balance"] == 5000.0 and main["open_positions"] == 2
    assert test["reachable"] is False and "arrêté" in test["error"] and test["label"] == "Test"


def test_proxy_forwards_path_query_and_body(gateway_client):
    response = gateway_client.get("/api/accounts/main/api/market/candles?symbol=EURUSD&count=3", headers=AUTH)
    assert response.status_code == 200
    assert response.json()["echo"] == "/api/market/candles"
    assert ("symbol", "EURUSD") in [tuple(p) for p in response.json()["params"]]
    posted = gateway_client.post("/api/accounts/main/api/control/mode", headers=AUTH,
                                 json={"dry_run": False, "confirm": "LIVE"}).json()
    assert json.loads(posted["body"]) == {"dry_run": False, "confirm": "LIVE"}


def test_proxy_errors(gateway_client):
    assert gateway_client.get("/api/accounts/unknown/api/status", headers=AUTH).status_code == 404
    assert gateway_client.get("/api/accounts/main/health", headers=AUTH).status_code == 404
    assert gateway_client.get("/api/accounts/main/api/accounts", headers=AUTH).status_code == 404   # pas de boucle
    down = gateway_client.get("/api/accounts/test/api/status", headers=AUTH)
    assert down.status_code == 503 and "Test" in down.json()["detail"]


def test_gateway_requires_api_key(gateway_client):
    assert gateway_client.get("/api/accounts").status_code == 401
    assert gateway_client.get("/api/accounts/main/api/status").status_code == 401
    assert gateway_client.get("/health").status_code == 200


def test_webhook_is_broadcast_to_all_accounts(gateway_client):
    response = gateway_client.post("/api/webhooks/tradingview?token=abc", json={"action": "buy"})
    assert response.status_code == 202
    targets = [c[0] for c in gateway_client.fake.calls if c[2] == "/api/webhooks/tradingview"]
    assert targets == ["main", "test"]


# ----------------------------------------------------------------- superviseur

class FakeProcess:
    def __init__(self, *args, **kwargs):
        self.args, self.kwargs, self.code, self.signals, self.pid = args, kwargs, None, [], 4242

    def poll(self):
        return self.code

    def send_signal(self, sig):
        self.signals.append(sig)
        self.code = 0

    def wait(self, timeout=None):
        return self.code

    def kill(self):
        self.code = -9


@pytest.fixture
def supervised(monkeypatch, tmp_path):
    monkeypatch.setattr(sup, "BASE_DIR", tmp_path)
    alerts = []
    monkeypatch.setattr(sup, "notify", lambda title, message="", level=None, dedup_key=None: alerts.append(title))
    clock = [1000.0]
    processes = []

    def popen(*args, **kwargs):
        processes.append(FakeProcess(*args, **kwargs))
        return processes[-1]

    supervisor = sup.Supervisor(ACCOUNTS, base_env={"API_KEY": "k" * 20}, popen=popen, clock=lambda: clock[0])
    return supervisor, processes, clock, alerts


def test_supervisor_starts_one_process_per_account(supervised):
    supervisor, processes, _, _ = supervised
    supervisor.check()
    assert len(processes) == 2
    envs = [p.kwargs["env"] for p in processes]
    assert [e["BOT_ACCOUNT_ID"] for e in envs] == ["main", "test"]
    assert [e["API_PORT"] for e in envs] == ["8101", "8102"]


def test_crashed_account_restarts_with_backoff(supervised):
    supervisor, processes, clock, alerts = supervised
    supervisor.check()
    processes[0].code = 1                                   # le compte « main » s'arrête
    supervisor.check()
    assert alerts == ["Compte Principal arrêté"] and len(processes) == 2
    clock[0] += sup.RESTART_BASE_SECONDS - 1
    supervisor.check()
    assert len(processes) == 2                              # attente pas encore écoulée
    clock[0] += 1
    supervisor.check()
    assert len(processes) == 3 and processes[2].kwargs["env"]["BOT_ACCOUNT_ID"] == "main"
    processes[2].code = 1
    supervisor.check()
    assert supervisor.workers[0].next_start - clock[0] == sup.RESTART_BASE_SECONDS * 2   # attente doublée


def test_stop_workers_sends_graceful_signal(supervised):
    supervisor, processes, _, _ = supervised
    supervisor.check()
    supervisor.stop_workers()
    assert all(p.signals for p in processes) and all(w.process is None for w in supervisor.workers)


# ----------------------------------------------------------------- alertes

def test_alerts_include_account_label(monkeypatch):
    from utils import notifier as nt

    monkeypatch.setattr(nt, "settings", replace(settings, instance=replace(settings.instance, label="Principal")))
    assert "[MT5 Bot · Principal] Trade bloqué" in nt.Notification("Trade bloqué").render()
    monkeypatch.setattr(nt, "settings", settings)
    assert "[MT5 Bot] Trade bloqué" in nt.Notification("Trade bloqué").render()
