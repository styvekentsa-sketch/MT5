"""
Identifiants de connexion au terminal MetaTrader 5 (login, mot de passe, serveur).

Les secrets ne sont jamais écrits en dur : ils proviennent exclusivement du `.env`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from config.env import ConfigError, get_int, get_str


@dataclass(frozen=True)
class MT5Config:
    account: int
    password: str = field(repr=False)
    server: str
    path: str | None
    timeout_ms: int

    def validate(self) -> None:
        if self.account <= 0:
            raise ConfigError("MT5_ACCOUNT doit être un numéro de compte valide")
        if not self.password:
            raise ConfigError("MT5_PASSWORD est vide")
        if not self.server:
            raise ConfigError("MT5_SERVER est vide")
        if self.path and not Path(self.path).exists():
            raise ConfigError(f"MT5_PATH introuvable : {self.path}")


def load_mt5_credentials(required: bool = True) -> MT5Config:
    """
    Lit les identifiants MT5 depuis l'environnement (validation faite par `Settings`).
    `required=False` en mode multi-comptes : le superviseur ne se connecte à aucun terminal,
    chaque compte a ses propres identifiants (ACCOUNT_<ID>_*).
    """
    return MT5Config(
        account=get_int("MT5_ACCOUNT", 0),
        password=get_str("MT5_PASSWORD", required=required),
        server=get_str("MT5_SERVER", required=required),
        path=get_str("MT5_PATH") or None,
        timeout_ms=get_int("MT5_TIMEOUT_MS", 60_000),
    )
