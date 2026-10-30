"""
Superviseur multi-comptes : un processus bot par compte MetaTrader 5, plus la passerelle API.

  - démarre `python main.py` pour chaque compte, avec l'environnement de ce compte
    (identifiants, surcharges de risque, port d'API interne, base et logs séparés)
  - redémarre automatiquement un compte arrêté (attente croissante : 5 s, 10 s... 2 min),
    avec une alerte Telegram / Discord
  - arrêt propre (Ctrl+C) : chaque compte termine son tour et ferme MT5, puis la passerelle s'arrête

La sortie console de chaque compte est écrite dans logs/<id>/console.log.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any, Callable

from config.accounts import AccountSpec, worker_env
from config.env import BASE_DIR
from config.settings import settings
from utils.logger import get_logger
from utils.notifier import Level, get_notifier, notify

log = get_logger(__name__)

CHECK_INTERVAL_SECONDS = 2
RESTART_BASE_SECONDS = 5
RESTART_MAX_SECONDS = 120
STABLE_RUN_SECONDS = 300             # au-delà, un arrêt n'est plus compté comme un échec de démarrage
STOP_TIMEOUT_SECONDS = 20            # délai laissé à un compte pour terminer son tour et fermer MT5
IS_WINDOWS = sys.platform == "win32"


@dataclass
class Worker:
    account: AccountSpec
    process: Any = None              # subprocess.Popen
    console: IO[bytes] | None = None
    started_at: float = 0.0
    restarts: int = 0
    next_start: float = 0.0


class Supervisor:
    """Lance, surveille et arrête les processus des comptes."""

    def __init__(self, accounts: tuple[AccountSpec, ...], base_env: dict[str, str] | None = None,
                 popen: Callable[..., Any] = subprocess.Popen, clock: Callable[[], float] = time.monotonic) -> None:
        self.workers = [Worker(a) for a in accounts]
        self.base_env = dict(os.environ if base_env is None else base_env)
        self._popen = popen
        self._clock = clock
        self._stop = threading.Event()

    # ------------------------------------------------------------------ processus

    def start_worker(self, worker: Worker) -> None:
        account = worker.account
        log_dir = BASE_DIR / "logs" / account.id
        log_dir.mkdir(parents=True, exist_ok=True)
        worker.console = open(log_dir / "console.log", "ab")    # noqa: SIM115 - fermé à l'arrêt du processus
        kwargs: dict[str, Any] = {"cwd": str(BASE_DIR), "env": worker_env(account, self.base_env),
                                  "stdout": worker.console, "stderr": subprocess.STDOUT}
        if IS_WINDOWS:   # groupe séparé : Ctrl+Break ciblé pour un arrêt propre, Ctrl+C non propagé
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        worker.process = self._popen([sys.executable, str(Path(BASE_DIR) / "main.py")], **kwargs)
        worker.started_at = self._clock()
        log.info("Compte %s (%d @ %s) démarré : PID %s, API interne %s",
                 account.label, account.login or account.broker, account.server, getattr(worker.process, "pid", "?"),
                 account.api_url)

    def _close_console(self, worker: Worker) -> None:
        if worker.console is not None:
            worker.console.close()
            worker.console = None

    def check(self) -> None:
        """Détecte les comptes arrêtés et redémarre ceux dont l'attente est écoulée."""
        now = self._clock()
        for worker in self.workers:
            if worker.process is not None:
                code = worker.process.poll()
                if code is None:
                    continue
                self._close_console(worker)
                worker.process = None
                if now - worker.started_at >= STABLE_RUN_SECONDS:
                    worker.restarts = 0                     # le compte tournait normalement
                delay = min(RESTART_BASE_SECONDS * 2 ** worker.restarts, RESTART_MAX_SECONDS)
                worker.restarts += 1
                worker.next_start = now + delay
                log.error("Compte %s arrêté (code %s) : redémarrage dans %ds", worker.account.label, code, delay)
                notify(f"Compte {worker.account.label} arrêté",
                       f"Processus terminé (code {code}) : redémarrage automatique dans {delay} s. "
                       f"Voir logs/{worker.account.id}/.", Level.CRITICAL, dedup_key=f"worker:{worker.account.id}")
            elif now >= worker.next_start and not self._stop.is_set():
                self.start_worker(worker)

    def stop_workers(self) -> None:
        """Arrêt propre de chaque compte (Ctrl+Break sous Windows, SIGTERM ailleurs), puis arrêt forcé si besoin."""
        running = [w for w in self.workers if w.process is not None and w.process.poll() is None]
        for worker in running:
            try:
                worker.process.send_signal(signal.CTRL_BREAK_EVENT if IS_WINDOWS else signal.SIGTERM)
            except (OSError, ValueError) as exc:
                log.warning("Signal d'arrêt non envoyé au compte %s : %s", worker.account.label, exc)
        deadline = self._clock() + STOP_TIMEOUT_SECONDS
        for worker in running:
            try:
                worker.process.wait(timeout=max(0.1, deadline - self._clock()))
            except subprocess.TimeoutExpired:
                log.warning("Compte %s : arrêt forcé (pas de réponse en %ds)", worker.account.label, STOP_TIMEOUT_SECONDS)
                worker.process.kill()
            self._close_console(worker)
            worker.process = None

    # ------------------------------------------------------------------ boucle

    def stop(self) -> None:
        self._stop.set()

    def _install_signal_handlers(self) -> None:
        for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
            sig = getattr(signal, name, None)
            if sig is not None:
                try:
                    signal.signal(sig, lambda signum, _frame: self.stop())
                except (ValueError, OSError):
                    pass

    def run(self) -> int:
        """Démarre comptes et passerelle, surveille jusqu'à Ctrl+C, puis arrête tout proprement."""
        from api.gateway import create_gateway_app
        from api.main import ApiServer

        self._install_signal_handlers()
        accounts = [w.account for w in self.workers]
        log.info("Mode multi-comptes : %s", ", ".join(
            f"{a.label} ({a.login} @ {a.server})" if a.broker == "mt5" else f"{a.label} ({a.server} via ccxt)"
            for a in accounts))
        gateway = None
        if settings.api.enabled:
            gateway = ApiServer(app=create_gateway_app(tuple(accounts)))
            gateway.start()
        notify("Superviseur démarré", f"{len(accounts)} compte(s) : {', '.join(a.label for a in accounts)}")
        try:
            while not self._stop.is_set():
                self.check()
                self._stop.wait(CHECK_INTERVAL_SECONDS)
        except KeyboardInterrupt:
            pass
        finally:
            log.info("Arrêt de tous les comptes...")
            self.stop_workers()
            if gateway is not None:
                gateway.stop()
            notify("Superviseur arrêté", "Tous les comptes ont été arrêtés.", Level.WARNING)
            get_notifier().flush(5)
        return 0
