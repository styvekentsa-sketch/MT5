"""
Alertes en temps réel : Telegram (bot) et/ou Discord (webhook).

Envoi NON BLOQUANT : `notify()` dépose le message dans une file et rend la main
immédiatement ; un thread démon se charge de l'envoi HTTP. Une panne réseau, une API
indisponible ou une file pleine ne lève jamais d'exception vers l'appelant (moteur,
API) : l'erreur est seulement loguée.

Anti-spam : une alerte portant une `dedup_key` n'est pas renvoyée avant
`NOTIFY_COOLDOWN_SECONDS` (ex : un blocage par le calendrier qui dure 20 minutes).

Les jetons (token Telegram, URL Discord) ne sont jamais écrits dans les logs.

Usage :
    from utils.notifier import Level, notify

    notify("Bot démarré", "Mode PAPER · EURUSD, XAUUSD")
    notify("Trade bloqué", "Spread trop élevé", Level.WARNING, dedup_key="risk:EURUSD")
"""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Protocol

import requests

from config.settings import settings
from utils.logger import get_logger

log = get_logger(__name__)

APP_NAME = "MT5 Bot"
QUEUE_SIZE = 100
MAX_ATTEMPTS = 2                     # 1 envoi + 1 nouvel essai (erreur réseau, 429, 5xx)
MAX_RETRY_AFTER_SECONDS = 10.0       # attente maximale demandée par un 429
TELEGRAM_MAX_LENGTH = 4096
DISCORD_MAX_LENGTH = 2000


def _retry_sleep(seconds: float) -> None:
    """Attente avant un nouvel essai (point d'injection pour les tests)."""
    time.sleep(seconds)


class Level(str, Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


LEVEL_PREFIX = {Level.INFO: "ℹ️", Level.WARNING: "⚠️", Level.CRITICAL: "🛑"}


@dataclass(frozen=True)
class Notification:
    title: str
    message: str = ""
    level: Level = Level.INFO
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def render(self) -> str:
        # Multi-comptes : le compte concerné figure dans chaque alerte (« [MT5 Bot · Principal] »)
        name = f"{APP_NAME} · {settings.instance.label}" if settings.instance.label else APP_NAME
        header = f"{LEVEL_PREFIX[self.level]} [{name}] {self.title}"
        stamp = self.created_at.strftime("%H:%M:%S UTC")
        return f"{header}\n{self.message}\n{stamp}" if self.message else f"{header}\n{stamp}"


class ChannelError(Exception):
    """Échec d'envoi sur un canal ; le message ne contient aucun secret."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


# =============================================================================
#  Canaux
# =============================================================================

class Channel(Protocol):
    name: str

    def send(self, text: str) -> None:
        """Envoie le texte ; lève ChannelError en cas d'échec."""


def _post_json(url: str, payload: dict, timeout: float, channel: str) -> None:
    """POST JSON ; traduit toute erreur en ChannelError SANS l'URL (qui contient le secret)."""
    try:
        response = requests.post(url, json=payload, timeout=timeout)
    except requests.RequestException as exc:
        raise ChannelError(f"{channel} : erreur réseau ({type(exc).__name__})", retry_after=1.0) from None
    if response.status_code == 429:
        retry_after = None
        try:
            body = response.json()
            retry_after = body.get("retry_after") or body.get("parameters", {}).get("retry_after")
        except ValueError:
            pass
        raise ChannelError(f"{channel} : limite de débit atteinte (429)", float(retry_after or 1.0))
    if response.status_code >= 400:
        raise ChannelError(f"{channel} : HTTP {response.status_code}",
                           retry_after=1.0 if response.status_code >= 500 else None)


class TelegramChannel:
    name = "Telegram"
    API_URL = "https://api.telegram.org/bot{token}/sendMessage"

    def __init__(self, token: str, chat_id: str, timeout: float) -> None:
        self._url = self.API_URL.format(token=token)
        self._chat_id = chat_id
        self._timeout = timeout

    def send(self, text: str) -> None:
        payload = {"chat_id": self._chat_id, "text": text[:TELEGRAM_MAX_LENGTH], "disable_web_page_preview": True}
        _post_json(self._url, payload, self._timeout, self.name)


class DiscordChannel:
    name = "Discord"

    def __init__(self, webhook_url: str, timeout: float) -> None:
        self._url = webhook_url
        self._timeout = timeout

    def send(self, text: str) -> None:
        # allowed_mentions vide : un titre contenant « @everyone » ne notifie personne
        payload = {"content": text[:DISCORD_MAX_LENGTH], "allowed_mentions": {"parse": []}}
        _post_json(self._url, payload, self._timeout, self.name)


# =============================================================================
#  Notifieur
# =============================================================================

class Notifier:
    """File d'envoi asynchrone vers un ou plusieurs canaux, avec anti-spam par clé."""

    def __init__(self, channels: list[Channel], cooldown_seconds: float = 900, queue_size: int = QUEUE_SIZE) -> None:
        self.channels = channels
        self.cooldown_seconds = cooldown_seconds
        self._queue: queue.Queue[Notification] = queue.Queue(maxsize=queue_size)
        self._last_sent: dict[str, float] = {}
        self._lock = threading.Lock()
        self._worker: threading.Thread | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.channels)

    def notify(self, title: str, message: str = "", level: Level = Level.INFO, dedup_key: str | None = None) -> bool:
        """
        Met une alerte en file d'envoi. Ne bloque pas et ne lève jamais d'exception.

        Returns:
            True si l'alerte est en file ; False si aucun canal n'est configuré, si la même
            `dedup_key` a déjà été envoyée depuis moins de `cooldown_seconds`, ou si la file est pleine.
        """
        try:
            if not self.enabled:
                return False
            if dedup_key is not None:
                with self._lock:
                    now = time.monotonic()
                    last = self._last_sent.get(dedup_key)
                    if last is not None and now - last < self.cooldown_seconds:
                        log.debug("Alerte « %s » ignorée (anti-spam %s)", title, dedup_key)
                        return False
                    self._last_sent[dedup_key] = now
            self._ensure_worker()
            self._queue.put_nowait(Notification(title, message, level))
            return True
        except queue.Full:
            log.warning("File de notifications pleine : alerte « %s » abandonnée", title)
            return False
        except Exception:  # noqa: BLE001 - une alerte ne doit jamais casser l'appelant
            log.exception("Erreur inattendue lors de la mise en file d'une alerte")
            return False

    def flush(self, timeout: float = 5.0) -> bool:
        """Attend l'envoi des alertes en file (ex : à l'arrêt du bot). True si la file est vide."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._queue.unfinished_tasks == 0:
                return True
            time.sleep(0.05)
        return self._queue.unfinished_tasks == 0

    def _ensure_worker(self) -> None:
        with self._lock:
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(target=self._run, name="notifier", daemon=True)
                self._worker.start()

    def _run(self) -> None:
        while True:
            notification = self._queue.get()
            try:
                text = notification.render()
                for channel in self.channels:
                    self._deliver(channel, text)
            except Exception:  # noqa: BLE001 - le thread d'envoi ne doit jamais mourir
                log.exception("Erreur inattendue dans le thread de notifications")
            finally:
                self._queue.task_done()

    def _deliver(self, channel: Channel, text: str) -> None:
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                channel.send(text)
                return
            except ChannelError as exc:
                if attempt < MAX_ATTEMPTS and exc.retry_after is not None:
                    _retry_sleep(min(exc.retry_after, MAX_RETRY_AFTER_SECONDS))
                    continue
                log.warning("Notification non envoyée : %s", exc)
                return
            except Exception as exc:  # noqa: BLE001
                log.warning("Notification non envoyée (%s) : %s", channel.name, type(exc).__name__)
                return


def build_channels() -> list[Channel]:
    """Canaux configurés dans settings.notifier (Telegram et/ou Discord)."""
    cfg = settings.notifier
    channels: list[Channel] = []
    if cfg.telegram_bot_token and cfg.telegram_chat_id:
        channels.append(TelegramChannel(cfg.telegram_bot_token, cfg.telegram_chat_id, cfg.timeout_seconds))
    if cfg.discord_webhook_url:
        channels.append(DiscordChannel(cfg.discord_webhook_url, cfg.timeout_seconds))
    return channels


_notifier: Notifier | None = None
_notifier_lock = threading.Lock()


def get_notifier() -> Notifier:
    """Notifieur partagé, construit au premier appel depuis la configuration."""
    global _notifier
    with _notifier_lock:
        if _notifier is None:
            _notifier = Notifier(build_channels(), cooldown_seconds=settings.notifier.cooldown_seconds)
            if _notifier.enabled:
                log.info("Notifications actives : %s", ", ".join(c.name for c in _notifier.channels))
        return _notifier


def notify(title: str, message: str = "", level: Level = Level.INFO, dedup_key: str | None = None) -> bool:
    """Raccourci vers `get_notifier().notify(...)` ; ne lève jamais d'exception."""
    try:
        return get_notifier().notify(title, message, level, dedup_key)
    except Exception:  # noqa: BLE001
        log.exception("Notifieur indisponible")
        return False
