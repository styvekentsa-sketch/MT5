"""
Journalisation du bot : console colorée + fichiers de log avec rotation.

  - console (stdout)  : messages >= LOG_LEVEL, colorés par niveau
  - `logs/bot.log`    : tous les messages >= LOG_LEVEL
  - `logs/errors.log` : uniquement WARNING et plus, pour un diagnostic rapide

La configuration est appliquée automatiquement au premier appel de `get_logger()`.

Usage :
    from utils.logger import get_logger

    log = get_logger(__name__)
    log.info("Démarrage")
"""

from __future__ import annotations

import logging
import os
import sys
import threading
from logging.handlers import RotatingFileHandler

from config import settings
from config.settings import LogConfig

LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)-20s | %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

NOISY_LOGGERS = ("urllib3", "requests", "httpx", "transformers", "filelock")


class _ColorFormatter(logging.Formatter):
    """Formatter console avec couleurs ANSI par niveau (désactivé hors terminal)."""

    COLORS = {
        logging.DEBUG: "\033[36m",     # cyan
        logging.INFO: "\033[32m",      # vert
        logging.WARNING: "\033[33m",   # jaune
        logging.ERROR: "\033[31m",     # rouge
        logging.CRITICAL: "\033[1;41m",  # fond rouge
    }
    RESET = "\033[0m"

    def __init__(self, use_color: bool) -> None:
        super().__init__(LOG_FORMAT, LOG_DATE_FORMAT)
        self.use_color = use_color

    def format(self, record: logging.LogRecord) -> str:
        message = super().format(record)
        if self.use_color and record.levelno in self.COLORS:
            return f"{self.COLORS[record.levelno]}{message}{self.RESET}"
        return message


_setup_lock = threading.Lock()
_logging_configured = False


def _rotating_file(log_cfg: LogConfig, filename: str, level: int) -> RotatingFileHandler:
    handler = RotatingFileHandler(
        log_cfg.directory / filename,
        maxBytes=log_cfg.max_bytes,
        backupCount=log_cfg.backup_count,
        encoding="utf-8",
    )
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter(LOG_FORMAT, LOG_DATE_FORMAT))
    return handler


def setup_logging(log_cfg: LogConfig | None = None) -> None:
    """
    Configure le logger racine (console + `bot.log` + `errors.log`).
    Idempotent : peut être appelé plusieurs fois sans dupliquer les handlers.
    """
    global _logging_configured
    with _setup_lock:
        if _logging_configured:
            return
        log_cfg = log_cfg or settings.log
        log_cfg.directory.mkdir(parents=True, exist_ok=True)
        level = getattr(logging, log_cfg.level)

        if sys.platform == "win32":
            os.system("")  # active le support des séquences ANSI dans la console Windows

        root = logging.getLogger()
        root.setLevel(level)
        root.handlers.clear()

        console = logging.StreamHandler(sys.stdout)
        console.setLevel(level)
        console.setFormatter(_ColorFormatter(use_color=sys.stdout.isatty()))
        root.addHandler(console)
        root.addHandler(_rotating_file(log_cfg, "bot.log", level))
        root.addHandler(_rotating_file(log_cfg, "errors.log", logging.WARNING))

        # Réduit le bruit des bibliothèques tierces
        for noisy in NOISY_LOGGERS:
            logging.getLogger(noisy).setLevel(logging.WARNING)

        _logging_configured = True


def get_logger(name: str) -> logging.Logger:
    """Retourne un logger nommé ; à utiliser dans chaque module (`get_logger(__name__)`)."""
    setup_logging()
    return logging.getLogger(name)
