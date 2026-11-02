"""
Identifiants de connexion au terminal MetaTrader 5 (login, mot de passe, serveur).

Deux façons de se connecter :
  - identifiants explicites : MT5_ACCOUNT, MT5_PASSWORD, MT5_SERVER
  - terminal actif : MT5_ACCOUNT vide (ou 0) -> le bot s'attache au compte déjà ouvert dans
    le terminal, sans mot de passe dans le .env

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
    max_tick_age_seconds: int = 120          # au-delà, le dernier prix est jugé périmé (marché figé)
    server_utc_offset_hours: float | None = None   # décalage horaire du serveur ; None = estimé
    server_timezone: str = "Europe/Athens"         # fuseau du serveur (heure d'été) : bougies -> UTC réel

    @property
    def use_terminal_session(self) -> bool:
        """True : aucun compte configuré, le bot utilise le compte ouvert dans le terminal."""
        return self.account == 0

    def validate(self) -> None:
        if self.max_tick_age_seconds < 5:
            raise ConfigError("MT5_MAX_TICK_AGE_SECONDS doit être >= 5")
        if self.server_utc_offset_hours is not None and not -12 <= self.server_utc_offset_hours <= 14:
            raise ConfigError("MT5_SERVER_UTC_OFFSET_HOURS doit être compris entre -12 et 14")
        try:
            import pandas as pd

            pd.Timestamp("2026-01-01").tz_localize(self.server_timezone)
        except Exception:  # noqa: BLE001 - nom de fuseau inconnu
            raise ConfigError(f"MT5_SERVER_TIMEZONE inconnu : {self.server_timezone} (ex : Europe/Athens)") from None
        if self.path and not Path(self.path).exists():
            raise ConfigError(f"MT5_PATH introuvable : {self.path}")
        if self.use_terminal_session:
            return
        if self.account < 0:
            raise ConfigError("MT5_ACCOUNT doit être un numéro de compte valide")
        if not self.password:
            raise ConfigError("MT5_PASSWORD est vide")
        if not self.server:
            raise ConfigError("MT5_SERVER est vide")


def load_mt5_credentials(required: bool = True) -> MT5Config:
    """
    Lit les identifiants MT5 depuis l'environnement (validation faite par `Settings`).
    `required=False` en mode multi-comptes : le superviseur ne se connecte à aucun terminal,
    chaque compte a ses propres identifiants (ACCOUNT_<ID>_*).
    """
    account = get_int("MT5_ACCOUNT", 0)
    required = required and account != 0        # terminal actif : ni mot de passe ni serveur
    offset = get_str("MT5_SERVER_UTC_OFFSET_HOURS")
    return MT5Config(
        account=account,
        password=get_str("MT5_PASSWORD", required=required),
        server=get_str("MT5_SERVER", required=required),
        path=get_str("MT5_PATH") or None,
        timeout_ms=get_int("MT5_TIMEOUT_MS", 60_000),
        max_tick_age_seconds=get_int("MT5_MAX_TICK_AGE_SECONDS", 120),
        server_utc_offset_hours=float(offset.replace(",", ".")) if offset else None,
        server_timezone=get_str("MT5_SERVER_TIMEZONE", "Europe/Athens") or "Europe/Athens",
    )
