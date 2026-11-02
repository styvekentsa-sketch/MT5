"""
Connexion dynamique à un compte MetaTrader 5 depuis le dashboard, sans modifier le .env.

  POST /api/auth/login    connecte le terminal au compte (login, mot de passe, serveur)
  POST /api/auth/logout   revient au compte du .env
  GET  /api/auth/session  compte actif et son origine (.env ou dashboard)

Le terminal MT5 du bot n'a qu'un compte actif à la fois : la connexion change le compte
du bot lui-même (vu par tous les écrans du dashboard). Par sécurité, chaque changement de
compte met le moteur en pause et en mode PAPER : aucun ordre ne part sur le nouveau compte
sans une reprise explicite. Un échec rétablit le compte précédent.

Le mot de passe n'est jamais journalisé, jamais renvoyé, jamais écrit sur disque ni dans le
navigateur : il reste en mémoire du processus pour les reconnexions automatiques.
Les tentatives échouées sont limitées (MAX_FAILED_ATTEMPTS par FAILED_WINDOW_SECONDS).
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, SecretStr, field_validator

from api.dependencies import get_engine
from api.security import require_api_key
from brokers.base import is_dry_run, set_dry_run
from core.engine import TradingEngine
from utils.logger import get_logger
from utils.notifier import Level, notify

log = get_logger(__name__)

router = APIRouter(prefix="/api/auth", tags=["Compte"], dependencies=[Depends(require_api_key)])

MAX_FAILED_ATTEMPTS = 5
FAILED_WINDOW_SECONDS = 300

_failures: deque[float] = deque()
_failures_lock = threading.Lock()


class LoginIn(BaseModel):
    login: int = Field(gt=0, lt=10**12, description="Numéro de compte MT5")
    password: SecretStr = Field(min_length=1, max_length=128, description="Mot de passe de trading (jamais stocké)")
    server: str = Field(min_length=2, max_length=64, description="Serveur du broker, ex : MetaQuotes-Demo")

    @field_validator("server")
    @classmethod
    def _strip(cls, value: str) -> str:
        return value.strip()


class SessionOut(BaseModel):
    source: Literal["env", "dashboard"] = Field(description="env = compte du .env, dashboard = choisi en ligne")
    login: int | None
    server: str | None
    name: str = ""
    company: str = ""
    account_type: str = "unknown"
    currency: str | None = None
    balance: float | None = None
    mode: Literal["PAPER", "LIVE"]
    engine_paused: bool
    algo_trading: bool | None = Field(description="Bouton « Algo Trading » du terminal ; False : ordres refusés")
    message: str = ""


def _session(engine: TradingEngine, message: str = "") -> SessionOut:
    broker = engine.broker
    account = None
    try:
        account = broker.get_balance()
    except Exception as exc:  # noqa: BLE001 - la session doit toujours répondre
        log.warning("Compte actif indisponible : %s", exc)
    return SessionOut(
        source="dashboard" if broker.is_dashboard_session() else "env",
        login=account.login if account else None,
        server=account.server if account else None,
        name=account.name if account else "",
        company=account.company if account else "",
        account_type=account.account_type if account else "unknown",
        currency=account.currency if account else None,
        balance=account.balance if account else None,
        mode="PAPER" if is_dry_run() else "LIVE",
        engine_paused=engine.is_paused,
        algo_trading=broker.trading_enabled(),
        message=message,
    )


def _check_rate_limit() -> None:
    now = time.monotonic()
    with _failures_lock:
        while _failures and now - _failures[0] > FAILED_WINDOW_SECONDS:
            _failures.popleft()
        if len(_failures) >= MAX_FAILED_ATTEMPTS:
            retry = int(FAILED_WINDOW_SECONDS - (now - _failures[0])) + 1
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS,
                                f"Trop de tentatives échouées : réessayez dans {retry} s")


def _record_failure() -> None:
    with _failures_lock:
        _failures.append(time.monotonic())


def reset_rate_limit() -> None:
    with _failures_lock:
        _failures.clear()


@router.get("/session", response_model=SessionOut, summary="Compte MT5 actif")
def get_session(engine: TradingEngine = Depends(get_engine)) -> SessionOut:
    """Compte sur lequel le bot travaille, son origine (.env ou dashboard), mode et état du moteur."""
    return _session(engine)


@router.post("/login", response_model=SessionOut, summary="Se connecter à un compte MT5")
def login(payload: LoginIn, engine: TradingEngine = Depends(get_engine)) -> SessionOut:
    """
    Connecte le bot au compte indiqué. Le moteur est mis en pause et passe en PAPER avant le
    changement ; en cas d'échec (403), le compte précédent est rétabli.
    """
    broker = engine.broker
    if not broker.supports_account_switch:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            f"La connexion depuis le dashboard n'est disponible qu'avec MT5 (broker actuel : {broker.name})")
    _check_rate_limit()
    was_paused, was_dry_run = engine.is_paused, is_dry_run()
    engine.pause()                                     # aucun ordre pendant la bascule
    set_dry_run(True)
    ok, message = broker.switch_account(payload.login, payload.password.get_secret_value(), payload.server)
    if not ok:
        _record_failure()
        log.warning("API : connexion au compte %d @ %s refusée", payload.login, payload.server)
        if broker.is_connected():                      # compte précédent rétabli : on reprend comme avant
            set_dry_run(was_dry_run)
            if not was_paused:
                engine.resume()
        raise HTTPException(status.HTTP_403_FORBIDDEN, message)   # 401 est réservé à la clé API
    engine.on_account_switched()                       # nouveau compte : pause + PAPER, état de l'ancien oublié
    reset_rate_limit()
    log.warning("API : bot connecté au compte %d @ %s (moteur en pause, mode PAPER)", payload.login, payload.server)
    notify("Changement de compte MT5", f"Compte actif : {payload.login} @ {payload.server}. "
                                       "Moteur en pause et en PAPER jusqu'à reprise manuelle.", Level.WARNING)
    return _session(engine, f"{message} · moteur en pause, mode PAPER : reprenez quand vous êtes prêt")


@router.post("/logout", response_model=SessionOut, summary="Revenir au compte du .env")
def logout(engine: TradingEngine = Depends(get_engine)) -> SessionOut:
    """Ferme la session du compte choisi depuis le dashboard et revient au compte configuré (.env)."""
    broker = engine.broker
    if not broker.is_dashboard_session():
        return _session(engine, "Aucune session ouverte depuis le dashboard : compte du .env déjà actif")
    engine.on_account_switched()
    ok, message = broker.reset_account()
    if not ok:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"Retour au compte du .env impossible : {message}")
    log.warning("API : déconnexion, retour au compte du .env (moteur en pause, mode PAPER)")
    notify("Changement de compte MT5", "Retour au compte du .env. Moteur en pause et en PAPER.", Level.WARNING)
    return _session(engine, "Déconnecté : retour au compte du .env · moteur en pause, mode PAPER")
