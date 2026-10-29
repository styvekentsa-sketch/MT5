"""
Configuration multi-comptes : un processus bot (et un terminal MT5) par compte.

Activation dans le .env :
    MT5_ACCOUNTS=main,test
    ACCOUNT_MAIN_LOGIN=12345678
    ACCOUNT_MAIN_PASSWORD=...
    ACCOUNT_MAIN_SERVER=MetaQuotes-Demo
    ACCOUNT_MAIN_PATH=D:\\MT5 App\\terminal64.exe        # un terminal distinct par compte
    ACCOUNT_MAIN_LABEL=Principal                         # facultatif
    ACCOUNT_TEST_RISK_PER_TRADE_PCT=0.5                  # surcharge de n'importe quel réglage du bot

Toute variable `ACCOUNT_<ID>_<NOM>` devient `<NOM>` dans le processus du compte
(LOGIN, PASSWORD, SERVER et PATH deviennent MT5_ACCOUNT, MT5_PASSWORD, MT5_SERVER, MT5_PATH).
Sans MT5_ACCOUNTS, le bot fonctionne en mode compte unique, comme avant.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from config.env import BASE_DIR, ConfigError

ACCOUNT_ID_PATTERN = re.compile(r"^[a-z0-9_]{1,20}$")
WORKER_PORT_BASE = 8101              # API interne du compte n : 127.0.0.1:8101, 8102...
WORKER_ROLE = "worker"               # BOT_ROLE des processus de compte (jamais superviseurs eux-mêmes)

# Raccourcis : ACCOUNT_<ID>_LOGIN -> MT5_ACCOUNT, etc.
ALIASES = {"LOGIN": "MT5_ACCOUNT", "PASSWORD": "MT5_PASSWORD", "SERVER": "MT5_SERVER", "PATH": "MT5_PATH"}
REQUIRED = ("LOGIN", "PASSWORD", "SERVER", "PATH")
# Réglages imposés par le superviseur : non surchargeables par compte
RESERVED = {"API_ENABLED", "API_HOST", "API_PORT", "DB_PATH", "LOG_DIR", "BOT_ROLE", "BOT_ACCOUNT_ID",
            "BOT_ACCOUNT_LABEL", "MT5_ACCOUNTS"}


@dataclass(frozen=True)
class AccountSpec:
    id: str
    label: str
    login: int
    password: str = field(repr=False)
    server: str = ""
    path: str = ""
    port: int = WORKER_PORT_BASE
    overrides: tuple[tuple[str, str], ...] = ()    # réglages propres au compte (hors identifiants)

    @property
    def api_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"


def _prefix(account_id: str) -> str:
    return f"ACCOUNT_{account_id.upper()}_"


def load_accounts(environ: Mapping[str, str] | None = None) -> tuple[AccountSpec, ...]:
    """Comptes déclarés par MT5_ACCOUNTS ; tuple vide en mode compte unique ou dans un processus de compte."""
    env = os.environ if environ is None else environ
    if env.get("BOT_ROLE") == WORKER_ROLE:
        return ()
    ids = [i.strip().lower() for i in env.get("MT5_ACCOUNTS", "").split(",") if i.strip()]
    accounts = []
    for index, account_id in enumerate(ids):
        if not ACCOUNT_ID_PATTERN.match(account_id):
            raise ConfigError(f"Identifiant de compte invalide : {account_id!r} (a-z, 0-9, _ ; 20 caractères max)")
        prefix = _prefix(account_id)
        values = {k[len(prefix):]: v.strip() for k, v in env.items() if k.startswith(prefix)}
        missing = [f"{prefix}{key}" for key in REQUIRED if not values.get(key)]
        if missing:
            raise ConfigError(f"Compte {account_id} : variables manquantes : {', '.join(missing)}")
        try:
            login = int(values["LOGIN"])
        except ValueError as exc:
            raise ConfigError(f"{prefix}LOGIN doit être un numéro de compte") from exc
        overrides = tuple(sorted(
            (name, value) for name, value in values.items()
            if name not in REQUIRED and name != "LABEL"
        ))
        reserved = [name for name, _ in overrides if name in RESERVED]
        if reserved:
            raise ConfigError(f"Compte {account_id} : réglages non surchargeables : {', '.join(reserved)}")
        accounts.append(AccountSpec(
            id=account_id,
            label=values.get("LABEL") or account_id,
            login=login,
            password=values["PASSWORD"],
            server=values["SERVER"],
            path=values["PATH"],
            port=WORKER_PORT_BASE + index,
            overrides=overrides,
        ))
    return tuple(accounts)


def validate_accounts(accounts: tuple[AccountSpec, ...]) -> None:
    """Cohérence globale : identifiants, terminaux et logins tous distincts, terminaux existants."""
    if not accounts:
        return
    for attribute, what in (("id", "identifiant"), ("login", "login"), ("path", "terminal (PATH)")):
        values = [str(getattr(a, attribute)).lower() for a in accounts]
        duplicates = sorted({v for v in values if values.count(v) > 1})
        if duplicates:
            raise ConfigError(f"Plusieurs comptes partagent le même {what} : {', '.join(duplicates)} "
                              "(un terminal MT5 ne peut être connecté qu'à un compte à la fois)")
    for account in accounts:
        if account.login <= 0:
            raise ConfigError(f"Compte {account.id} : login invalide")
        if not Path(account.path).exists():
            raise ConfigError(f"Compte {account.id} : terminal introuvable ({account.path})")


def worker_env(account: AccountSpec, base_env: Mapping[str, str]) -> dict[str, str]:
    """
    Environnement du processus d'un compte : identifiants et surcharges de ce compte,
    API interne locale sur son port, base et logs séparés. Les identifiants des AUTRES
    comptes sont retirés de l'environnement transmis.
    """
    env = {k: v for k, v in base_env.items() if not k.startswith("ACCOUNT_") and k != "MT5_ACCOUNTS"}
    for name, value in account.overrides:
        env[ALIASES.get(name, name)] = value
    env.update({
        "BOT_ROLE": WORKER_ROLE,
        "BOT_ACCOUNT_ID": account.id,
        "BOT_ACCOUNT_LABEL": account.label,
        "MT5_ACCOUNT": str(account.login),
        "MT5_PASSWORD": account.password,
        "MT5_SERVER": account.server,
        "MT5_PATH": account.path,
        "API_ENABLED": "true",
        "API_HOST": "127.0.0.1",                       # API interne : jamais exposée au réseau
        "API_PORT": str(account.port),
        "DB_PATH": str(BASE_DIR / "data" / f"trading_bot_{account.id}.db"),
        "LOG_DIR": str(BASE_DIR / "logs" / account.id),
        "PYTHONIOENCODING": "utf-8",
    })
    return env
