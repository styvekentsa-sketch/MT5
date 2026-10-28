"""Notifieur Telegram / Discord : envoi asynchrone, anti-spam, robustesse (HTTP simulé)."""

import logging
import threading
from dataclasses import replace
from types import SimpleNamespace

import pytest
import requests

from config import ConfigError, settings
from utils import notifier as nt
from utils.notifier import DiscordChannel, Level, Notifier, TelegramChannel

TOKEN = "123456:SECRET-TELEGRAM-TOKEN"
DISCORD_URL = "https://discord.com/api/webhooks/42/SECRET-DISCORD-TOKEN"


class FakePost:
    """Remplace requests.post : enregistre les appels et renvoie les réponses programmées."""

    def __init__(self, *responses):
        self.calls: list[dict] = []
        self.responses = list(responses) or [SimpleNamespace(status_code=200, json=lambda: {})]

    def __call__(self, url, json=None, timeout=None):
        self.calls.append({"url": url, "json": json, "timeout": timeout})
        response = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(response, Exception):
            raise response
        return response


def _response(status, body=None):
    return SimpleNamespace(status_code=status, json=lambda: body or {})


@pytest.fixture
def no_sleep(monkeypatch):
    sleeps = []
    monkeypatch.setattr(nt, "_retry_sleep", lambda s: sleeps.append(s))
    return sleeps


def _notifier(*channels, cooldown=900):
    return Notifier(list(channels) or [TelegramChannel(TOKEN, "987", timeout=5)], cooldown_seconds=cooldown)


# ----------------------------------------------------------------- canaux

def test_telegram_payload(monkeypatch):
    post = FakePost()
    monkeypatch.setattr(nt.requests, "post", post)
    notifier = _notifier()
    assert notifier.notify("Bot démarré", "Mode PAPER", Level.INFO) is True
    assert notifier.flush(2)
    call = post.calls[0]
    assert call["url"] == f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    assert call["json"]["chat_id"] == "987" and call["timeout"] == 5
    assert "[MT5 Bot] Bot démarré" in call["json"]["text"] and "Mode PAPER" in call["json"]["text"]


def test_discord_payload_is_truncated_and_mentions_disabled(monkeypatch):
    post = FakePost()
    monkeypatch.setattr(nt.requests, "post", post)
    notifier = _notifier(DiscordChannel(DISCORD_URL, timeout=5))
    notifier.notify("@everyone alerte", "x" * 5000, Level.CRITICAL)
    assert notifier.flush(2)
    payload = post.calls[0]["json"]
    assert post.calls[0]["url"] == DISCORD_URL
    assert len(payload["content"]) == nt.DISCORD_MAX_LENGTH and payload["allowed_mentions"] == {"parse": []}


def test_message_sent_to_every_channel(monkeypatch):
    post = FakePost()
    monkeypatch.setattr(nt.requests, "post", post)
    notifier = _notifier(TelegramChannel(TOKEN, "1", 5), DiscordChannel(DISCORD_URL, 5))
    notifier.notify("Test")
    notifier.flush(2)
    assert len(post.calls) == 2


# ----------------------------------------------------------------- robustesse

def test_network_error_never_raises_and_worker_survives(monkeypatch, no_sleep):
    post = FakePost(requests.ConnectionError("down"), requests.ConnectionError("down"), _response(200))
    monkeypatch.setattr(nt.requests, "post", post)
    notifier = _notifier()
    assert notifier.notify("Premier") is True          # échoue 2 fois (essai + nouvel essai)
    assert notifier.notify("Second") is True           # le thread continue et envoie le suivant
    assert notifier.flush(2)
    assert len(post.calls) == 3 and "Second" in post.calls[2]["json"]["text"]


def test_http_errors_do_not_raise(monkeypatch, no_sleep):
    for status in (400, 401, 403, 404, 500, 503):
        post = FakePost(_response(status))
        monkeypatch.setattr(nt.requests, "post", post)
        notifier = _notifier()
        notifier.notify("Test")
        assert notifier.flush(2)


def test_rate_limit_is_retried_once_after_retry_after(monkeypatch, no_sleep):
    post = FakePost(_response(429, {"parameters": {"retry_after": 3}}), _response(200))
    monkeypatch.setattr(nt.requests, "post", post)
    notifier = _notifier()
    notifier.notify("Test")
    notifier.flush(2)
    assert len(post.calls) == 2 and no_sleep == [3.0]


def test_client_errors_are_not_retried(monkeypatch, no_sleep):
    post = FakePost(_response(401))
    monkeypatch.setattr(nt.requests, "post", post)
    notifier = _notifier()
    notifier.notify("Test")
    notifier.flush(2)
    assert len(post.calls) == 1 and no_sleep == []


def test_secrets_never_reach_the_logs(monkeypatch, caplog, no_sleep):
    error = requests.ConnectionError(f"Max retries exceeded with url: /bot{TOKEN}/sendMessage")
    monkeypatch.setattr(nt.requests, "post", FakePost(error))
    notifier = _notifier(TelegramChannel(TOKEN, "1", 5), DiscordChannel(DISCORD_URL, 5))
    with caplog.at_level(logging.DEBUG):
        notifier.notify("Test")
        notifier.flush(2)
    assert "Notification non envoyée" in caplog.text
    assert "SECRET" not in caplog.text


def test_notify_does_not_block_while_sending(monkeypatch):
    release = threading.Event()

    def slow_post(url, json=None, timeout=None):
        release.wait(5)
        return _response(200)

    monkeypatch.setattr(nt.requests, "post", slow_post)
    notifier = _notifier()
    notifier.notify("Lent")
    assert notifier.notify("Pendant l'envoi") is True     # retourne alors que le 1er envoi est bloqué
    release.set()
    assert notifier.flush(2)


def test_full_queue_drops_without_blocking(monkeypatch):
    release = threading.Event()
    monkeypatch.setattr(nt.requests, "post", lambda url, json=None, timeout=None: release.wait(5) or _response(200))
    notifier = Notifier([TelegramChannel(TOKEN, "1", 5)], queue_size=2)
    results = [notifier.notify(f"m{i}") for i in range(10)]
    assert results.count(False) >= 1
    release.set()
    notifier.flush(5)


# ----------------------------------------------------------------- anti-spam et activation

def test_dedup_key_respects_cooldown(monkeypatch):
    monkeypatch.setattr(nt.requests, "post", FakePost())
    clock = [1000.0]
    monkeypatch.setattr(nt.time, "monotonic", lambda: clock[0])
    notifier = _notifier(cooldown=900)
    assert notifier.notify("Bloqué", dedup_key="news:EURUSD") is True
    assert notifier.notify("Bloqué", dedup_key="news:EURUSD") is False
    assert notifier.notify("Bloqué", dedup_key="news:XAUUSD") is True      # autre clé
    assert notifier.notify("Sans clé") is True and notifier.notify("Sans clé") is True
    clock[0] += 901
    assert notifier.notify("Bloqué", dedup_key="news:EURUSD") is True


def test_disabled_without_channels():
    notifier = Notifier([])
    assert notifier.enabled is False and notifier.notify("Test") is False
    assert notifier._worker is None


def test_module_notify_never_raises(monkeypatch):
    def boom():
        raise RuntimeError("panne")

    monkeypatch.setattr(nt, "get_notifier", boom)
    assert nt.notify("Test") is False


def test_channels_built_from_settings(monkeypatch):
    cfg = replace(settings.notifier, telegram_bot_token=TOKEN, telegram_chat_id="1", discord_webhook_url=DISCORD_URL)
    monkeypatch.setattr(nt, "settings", replace(settings, notifier=cfg))
    assert [c.name for c in nt.build_channels()] == ["Telegram", "Discord"]
    monkeypatch.setattr(nt, "settings", settings)          # environnement de test : aucun canal
    assert nt.build_channels() == []


# ----------------------------------------------------------------- configuration

@pytest.mark.parametrize("overrides", [
    {"telegram_bot_token": TOKEN},                          # chat_id manquant
    {"telegram_chat_id": "1"},                              # token manquant
    {"discord_webhook_url": "https://evil.example/hook"},
    {"cooldown_seconds": -1},
    {"timeout_seconds": 0},
])
def test_invalid_notifier_config(overrides):
    with pytest.raises(ConfigError):
        replace(settings.notifier, **overrides).validate()


def test_valid_notifier_config():
    cfg = replace(settings.notifier, telegram_bot_token=TOKEN, telegram_chat_id="1", discord_webhook_url=DISCORD_URL)
    cfg.validate()
    assert cfg.channels == ["Telegram", "Discord"]
