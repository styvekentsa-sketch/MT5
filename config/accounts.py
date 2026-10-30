"""
Configuration multi-comptes / multi-plateformes : un processus bot par compte.

Activation dans le .env :
    MT5_ACCOUNTS=main,exness,bybit
    ACCOUNT_MAIN_LOGIN=12345678                          # compte MetaTrader 5 (BROKER=mt5 par défaut)
    ACCOUNT_MAIN_PASSWORD=...
    ACCOUNT_MAIN_SERVER=MetaQuotes-Demo
    ACCOUNT_MAIN_PATH=D:\\MT5 App\\terminal64.exe        # un terminal distinct par compte MT5
    ACCOUNT_MAIN_LABEL=Principal                         # facultatif
    ACCOUNT_EXNESS_SYMBOL_SUFFIX=m                       # Exness : EURUSD -> EURUSDm
    ACCOUNT_BYBIT_BROKER=ccxt                            # exchange crypto via ccxt
    ACCOUNT_BYBIT_EXCHANGE=bybit
    ACCOUNT_BYBIT_API_KEY=...                            # facultatif en PAPER
    ACCOUNT_BYBIT_API_SECRET=...
    ACCOUNT_BYBIT_TRADING_SYMBOLS=BTCUSDT,ETHUSDT
    ACCOUNT_TEST_RISK_PER_TRADE_PCT=0.5                  # surcharge de n'importe quel réglage du bot

Toute variable `ACCOUNT_<ID>_<NOM>` devient `<NOM>` dans le processus du compte, avec des
raccourcis : LOGIN, PASSWORD, SERVER, PATH, SYMBOL_SUFFIX -> MT5_* ; EXCHANGE, API_KEY,
API_SECRET, API_PASSWORD, ENVIRONMENT, LEVERAGE -> CCXT_* ; BROKER -> BROKER_TYPE.
Chaque compte tourne dans son propre processus : la panne d'un exchange ou d'un terminal
n'interrompt jamais les autres comptes (le superviseur relance le processus en échec).
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

# Raccourcis : ACCOUNT_<ID>_LOGIN -> MT5_ACCOUNT, ACCOUNT_<ID>_EXCHANGE -> CCXT_EXCHANGE, etc.
ALIASES = {
    "LOGIN": "MT5_ACCOUNT", "PASSWORD": "MT5_PASSWORD", "SERVER": "MT5_SERVER", "PATH": "MT5_PATH",
    "SYMBOL_SUFFIX": "MT5_SYMBOL_SUFFIX", "BROKER": "BROKER_TYPE",
    "EXCHANGE": "CCXT_EXCHANGE", "API_KEY": "CCXT_API_KEY", "API_SECRET": "CCXT_API_SECRET",
    "API_PASSWORD": "CCXT_API_PASSWORD", "ENVIRONMENT": "CCXT_ENVIRONMENT", "LEVERAGE": "CCXT_LEVERAGE",
}
REQUIRED = {"mt5": ("LOGIN", "PASSWORD", "SERVER", "PATH"), "ccxt": ("EXCHANGE",)}
IDENTITY = {"LOGIN", "PASSWORD", "SERVER", "PATH", "LABEL", "BROKER", "EXCHANGE"}
# Réglages imposés par le superviseur : non surchargeables par compte
RESERVED = {"API_ENABLED", "API_HOST", "API_PORT", "DB_PATH", "LOG_DIR", "BOT_ROLE", "BOT_ACCOUNT_ID",
            "BOT_ACCOUNT_LABEL", "MT5_ACCOUNTS"}


@dataclass(frozen=True)
class AccountSpec:
    id: str
    label: str
    login: int                                     # 0 pour un exchange crypto
    password: str = field(repr=False)
    server: str = ""                               # serveur MT5, ou identifiant de l'exchange
    path: str = ""
    port: int = WORKER_PORT_BASE
    broker: str = "mt5"                            # mt5 | ccxt
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
        broker = (values.get("BROKER") or "mt5").lower()
        if broker not in REQUIRED:
            raise ConfigError(f"{prefix}BROKER invalide : {broker} (attendu : {', '.join(REQUIRED)})")
        missing = [f"{prefix}{key}" for key in REQUIRED[broker] if not values.get(key)]
        if missing:
            raise ConfigError(f"Compte {account_id} : variables manquantes : {', '.join(missing)}")
        try:
            login = int(values["LOGIN"]) if broker == "mt5" else 0
        except ValueError as exc:
            raise ConfigError(f"{prefix}LOGIN doit être un numéro de compte") from exc
        overrides = tuple(sorted(
            (name, value) for name, value in values.items() if name not in IDENTITY
        ))
        reserved = [name for name, _ in overrides if name in RESERVED]
        if reserved:
            raise ConfigError(f"Compte {account_id} : réglages non surchargeables : {', '.join(reserved)}")
        accounts.append(AccountSpec(
            id=account_id,
            label=values.get("LABEL") or account_id,
            login=login,
            password=values.get("PASSWORD", ""),
            server=values["SERVER"] if broker == "mt5" else values["EXCHANGE"].lower(),
            path=values.get("PATH", ""),
            port=WORKER_PORT_BASE + index,
            broker=broker,
            overrides=overrides,
        ))
    return tuple(accounts)


def validate_accounts(accounts: tuple[AccountSpec, ...]) -> None:
    """Cohérence globale : identifiants distincts ; comptes MT5 : terminaux et logins distincts et existants."""
    if not accounts:
        return
    mt5_accounts = [a for a in accounts if a.broker == "mt5"]
    for group, attribute, what in ((accounts, "id", "identifiant"), (mt5_accounts, "login", "login"),
                                   (mt5_accounts, "path", "terminal (PATH)")):
        values = [str(getattr(a, attribute)).lower() for a in group]
        duplicates = sorted({v for v in values if values.count(v) > 1})
        if duplicates:
            raise ConfigError(f"Plusieurs comptes partagent le même {what} : {', '.join(duplicates)} "
                              "(un terminal MT5 ne peut être connecté qu'à un compte à la fois)")
    for account in mt5_accounts:
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
        "BROKER_TYPE": account.broker,
        "API_ENABLED": "true",
        "API_HOST": "127.0.0.1",                       # API interne : jamais exposée au réseau
        "API_PORT": str(account.port),
        "DB_PATH": str(BASE_DIR / "data" / f"trading_bot_{account.id}.db"),
        "LOG_DIR": str(BASE_DIR / "logs" / account.id),
        "PYTHONIOENCODING": "utf-8",
    })
    if account.broker == "mt5":
        env.update({"MT5_ACCOUNT": str(account.login), "MT5_PASSWORD": account.password,
                    "MT5_SERVER": account.server, "MT5_PATH": account.path})
    else:
        env["CCXT_EXCHANGE"] = account.server
    return env
