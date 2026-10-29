"""
Lecture typée des variables d'environnement (partagée par `settings` et `credentials`).

Les valeurs proviennent du fichier `.env` à la racine du projet (via python-dotenv)
ou des variables d'environnement du système, qui ont priorité.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

# `override=False` : une variable déjà définie dans le système a priorité sur le .env.
# Un processus de compte (multi-comptes) reçoit tout son environnement du superviseur et ne
# relit PAS le .env : il n'a ainsi jamais accès aux identifiants des autres comptes.
if os.environ.get("BOT_ROLE") != "worker":
    load_dotenv(BASE_DIR / ".env", override=False)


class ConfigError(ValueError):
    """Erreur levée lorsqu'une valeur de configuration est absente ou invalide."""


def get_str(name: str, default: str | None = None, required: bool = False) -> str:
    value = os.getenv(name, default)
    if value is not None:
        value = value.strip()
    if required and not value:
        raise ConfigError(f"Variable d'environnement obligatoire manquante : {name}")
    return value or ""


def get_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw.strip())
    except ValueError as exc:
        raise ConfigError(f"{name} doit être un entier (reçu : {raw!r})") from exc


def get_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw.strip().replace(",", "."))
    except ValueError as exc:
        raise ConfigError(f"{name} doit être un nombre (reçu : {raw!r})") from exc


def get_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "y", "on", "oui"}:
        return True
    if value in {"0", "false", "no", "n", "off", "non"}:
        return False
    raise ConfigError(f"{name} doit être un booléen (reçu : {raw!r})")


def get_list(name: str, default: str = "") -> tuple[str, ...]:
    raw = os.getenv(name, default)
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def mask(secret: str) -> str:
    """Masque un secret pour l'affichage dans les logs."""
    if not secret:
        return "<vide>"
    return f"{secret[:2]}***{secret[-2:]}" if len(secret) > 6 else "***"
