"""
Passerelle sécurisée entre le bot et le terminal MetaTrader 5.

Responsabilités :
  - connexion / déconnexion / reconnexion automatique au terminal
  - récupération des données de marché (bougies OHLCV, ticks)
  - informations du compte
  - exécution des ordres au marché (BUY / SELL / clôture), avec mode DRY_RUN

L'API MetaTrader5 n'est pas thread-safe : tous les appels passent par un verrou
unique (`_lock`). Chaque fonction publique vérifie la connexion avant d'agir et
tente une reconnexion si le terminal a été perdu.

Usage :
    from brokers.mt5_client import connect_mt5, get_historical_data, execute_order, shutdown_mt5

    if connect_mt5():
        df = get_historical_data("EURUSD", count=200)
        result = execute_order("EURUSD", "BUY", 0.10, sl=1.0750, tp=1.0900)
        shutdown_mt5()
"""

from __future__ import annotations

import functools
import itertools
import math
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable, Literal, TypeVar

import MetaTrader5 as mt5
import pandas as pd

from brokers.base import is_dry_run, set_dry_run  # noqa: F401 - réexporté (compatibilité)
from brokers.errors import BrokerConnectionError
from brokers.models import AccountInfo, ClosedDeal, OrderResult, SymbolSpecs, Tick
from models.schemas import DiagnosticFailure, SymbolDiagnostic
from config import settings
from utils.logger import get_logger

log = get_logger(__name__)

OrderSide = Literal["BUY", "SELL"]
F = TypeVar("F", bound=Callable[..., Any])


class MT5ConnectionError(BrokerConnectionError):
    """Levée lorsque la connexion au terminal MT5 ne peut pas être (r)établie."""


# =============================================================================
#  Constantes
# =============================================================================

TIMEFRAMES: dict[str, int] = {
    "M1": mt5.TIMEFRAME_M1,
    "M5": mt5.TIMEFRAME_M5,
    "M15": mt5.TIMEFRAME_M15,
    "M30": mt5.TIMEFRAME_M30,
    "H1": mt5.TIMEFRAME_H1,
    "H4": mt5.TIMEFRAME_H4,
    "D1": mt5.TIMEFRAME_D1,
    "W1": mt5.TIMEFRAME_W1,
    "MN1": mt5.TIMEFRAME_MN1,
}

OHLCV_COLUMNS = ["open", "high", "low", "close", "volume", "spread", "real_volume"]

MAX_CONNECT_ATTEMPTS = 3
CONNECT_RETRY_DELAY_SECONDS = 5      # doublé à chaque tentative (5s, 10s, ...)
MAX_ORDER_ATTEMPTS = 3
ORDER_RETRY_DELAY_SECONDS = 0.5
MT5_COMMENT_MAX_LENGTH = 31          # limite imposée par MT5

SUCCESS_RETCODES = {
    mt5.TRADE_RETCODE_DONE,
    mt5.TRADE_RETCODE_PLACED,
    mt5.TRADE_RETCODE_DONE_PARTIAL,
}
# Codes pour lesquels un nouvel envoi avec un prix rafraîchi est sans risque.
# TIMEOUT / CONNECTION sont volontairement exclus : l'ordre a pu être exécuté
# côté serveur, le renvoyer risquerait d'ouvrir une position en double.
RETRYABLE_RETCODES = {
    mt5.TRADE_RETCODE_REQUOTE,
    mt5.TRADE_RETCODE_PRICE_CHANGED,
    mt5.TRADE_RETCODE_PRICE_OFF,
}

# Drapeaux de `symbol_info.filling_mode` (SYMBOL_FILLING_FOK / SYMBOL_FILLING_IOC)
_SYMBOL_FILLING_FOK = 1
_SYMBOL_FILLING_IOC = 2

# account_info().trade_mode -> type de compte
_ACCOUNT_TYPES = {
    mt5.ACCOUNT_TRADE_MODE_DEMO: "demo",
    mt5.ACCOUNT_TRADE_MODE_CONTEST: "contest",
    mt5.ACCOUNT_TRADE_MODE_REAL: "real",
}

_lock = threading.RLock()


@dataclass(frozen=True)
class Credentials:
    """Identifiants d'un compte MT5. Le mot de passe ne sort jamais du processus (ni log, ni disque, ni API)."""
    login: int
    password: str = field(repr=False)
    server: str


# Compte choisi depuis le dashboard (POST /api/auth/login) ; None = compte du .env. Gardé en
# mémoire uniquement, pour que les reconnexions automatiques restent sur le bon compte.
_session: Credentials | None = None
# Mode « terminal actif » (MT5_ACCOUNT vide) : compte trouvé ouvert dans le terminal à la connexion
_attached: Credentials | None = None
_connected = False
_dry_run_tickets = itertools.count(-1, -1)   # tickets fictifs négatifs : jamais confondus avec des vrais


# =============================================================================
#  Structures de retour
# =============================================================================

# Tick, AccountInfo, OrderResult, SymbolSpecs, ClosedDeal : voir brokers.models (réexportés ici)


# =============================================================================
#  Helpers internes
# =============================================================================

def _last_error() -> str:
    code, message = mt5.last_error()
    return f"[{code}] {message}"


def get_timeframe(timeframe: str) -> int:
    """Convertit une chaîne ("M15", "H1"...) en constante MT5 (mt5.TIMEFRAME_*)."""
    try:
        return TIMEFRAMES[timeframe.strip().upper()]
    except KeyError as exc:
        raise ValueError(
            f"Timeframe inconnu : {timeframe!r} (attendu : {', '.join(TIMEFRAMES)})"
        ) from exc


def _ensure_symbol(symbol: str) -> Any:
    """Retourne `symbol_info` et ajoute le symbole au Market Watch si nécessaire."""
    info = mt5.symbol_info(symbol)
    if info is None:
        raise ValueError(f"Symbole inconnu chez le broker : {symbol} ({_last_error()})")
    if not info.visible:
        log.debug("Ajout de %s au Market Watch", symbol)
        if not mt5.symbol_select(symbol, True):
            raise ValueError(f"Impossible d'activer le symbole {symbol} ({_last_error()})")
        info = mt5.symbol_info(symbol)
    return info


def _read_tick(symbol: str, info: Any) -> Tick | None:
    tick = mt5.symbol_info_tick(symbol)
    if tick is None or tick.bid <= 0 or tick.ask <= 0:
        log.warning("Aucun prix valide pour %s (marché fermé ?) %s", symbol, _last_error())
        return None
    return Tick(
        symbol=symbol,
        bid=tick.bid,
        ask=tick.ask,
        last=tick.last,
        spread_points=round((tick.ask - tick.bid) / info.point),
        time=datetime.fromtimestamp(tick.time_msc / 1000, tz=timezone.utc),
    )


def _filling_mode(info: Any) -> int:
    """Choisit un mode de remplissage accepté par le symbole."""
    if info.filling_mode & _SYMBOL_FILLING_FOK:
        return mt5.ORDER_FILLING_FOK
    if info.filling_mode & _SYMBOL_FILLING_IOC:
        return mt5.ORDER_FILLING_IOC
    return mt5.ORDER_FILLING_RETURN


def _normalize_volume(info: Any, volume: float) -> float:
    """Arrondit le volume au pas du broker (vers le bas) et applique les limites."""
    if volume <= 0:
        raise ValueError(f"Volume invalide : {volume}")

    capped = min(volume, info.volume_max, settings.risk.max_lot)
    if capped < volume:
        log.warning("Volume %.2f plafonné à %.2f (limite broker/config)", volume, capped)

    step = info.volume_step
    decimals = max(0, -Decimal(str(step)).as_tuple().exponent)
    normalized = round(math.floor(capped / step + 1e-9) * step, decimals)

    if normalized < info.volume_min:
        raise ValueError(f"Volume {volume} inférieur au minimum du broker ({info.volume_min})")
    return normalized


def _validate_stops(
    info: Any, side: OrderSide, tick: Tick, sl: float | None, tp: float | None
) -> tuple[float | None, float | None]:
    """Arrondit SL/TP aux décimales du symbole et vérifie leur cohérence avec le prix."""
    sl = round(sl, info.digits) if sl else None
    tp = round(tp, info.digits) if tp else None
    min_distance = info.trade_stops_level * info.point

    # Les stops d'un BUY se déclenchent sur le Bid, ceux d'un SELL sur l'Ask
    if side == "BUY":
        ref = tick.bid
        if sl is not None and sl >= ref - min_distance:
            raise ValueError(f"SL BUY {sl} trop proche ou au-dessus du prix {ref}")
        if tp is not None and tp <= ref + min_distance:
            raise ValueError(f"TP BUY {tp} trop proche ou en dessous du prix {ref}")
    else:
        ref = tick.ask
        if sl is not None and sl <= ref + min_distance:
            raise ValueError(f"SL SELL {sl} trop proche ou en dessous du prix {ref}")
        if tp is not None and tp >= ref - min_distance:
            raise ValueError(f"TP SELL {tp} trop proche ou au-dessus du prix {ref}")
    return sl, tp


# =============================================================================
#  Connexion / déconnexion
# =============================================================================

def env_credentials() -> Credentials:
    """Compte de référence : celui du .env, ou celui trouvé ouvert dans le terminal (mode terminal actif)."""
    cfg = settings.mt5
    if cfg.use_terminal_session:
        return _attached or Credentials(0, "", "")
    return Credentials(cfg.account, cfg.password, cfg.server)


def active_credentials() -> Credentials:
    """Compte actif : celui choisi depuis le dashboard, sinon celui du .env (ou du terminal)."""
    return _session or env_credentials()


def is_dashboard_session() -> bool:
    return _session is not None


def _verify_session() -> bool:
    """Vérifie que le terminal est connecté au broker et sur le bon compte."""
    terminal = mt5.terminal_info()
    if terminal is None:
        log.error("terminal_info() indisponible : %s", _last_error())
        return False
    if not terminal.connected:
        log.error("Le terminal MT5 n'est pas connecté au serveur du broker")
        return False

    account = mt5.account_info()
    if account is None:
        log.error("account_info() indisponible : %s", _last_error())
        return False
    global _attached
    expected = active_credentials().login
    if expected == 0:                          # terminal actif : on adopte le compte qui y est ouvert
        _attached = Credentials(account.login, "", account.server)
        log.info("Mode terminal actif : compte %d @ %s ouvert dans MT5", account.login, account.server)
    elif account.login != expected:
        log.error("Compte connecté (%d) différent du compte attendu (%d)", account.login, expected)
        return False

    log.info("Connecté à MT5 : compte %d | %s | solde %.2f %s | levier 1:%d",
             account.login, account.server, account.balance, account.currency, account.leverage)

    if not terminal.trade_allowed:
        log.warning("Algo Trading désactivé dans le terminal : les ordres réels seront refusés")
    if not account.trade_allowed:
        log.warning("Le trading n'est pas autorisé sur ce compte")
    if account.trade_mode == mt5.ACCOUNT_TRADE_MODE_REAL and not is_dry_run():
        log.warning("ATTENTION : COMPTE RÉEL en mode LIVE, les ordres engagent de l'argent réel")
    return True


def connect_mt5(max_attempts: int = MAX_CONNECT_ATTEMPTS) -> bool:
    """
    Initialise le terminal MT5 et se connecte au compte configuré.
    Réessaie avec un délai croissant. Retourne True si la session est active.
    """
    global _connected
    cfg = settings.mt5
    creds = active_credentials()
    args = (cfg.path,) if cfg.path else ()
    kwargs: dict[str, Any] = {"timeout": cfg.timeout_ms}
    if creds.login and creds.password:         # sinon : on s'attache au compte ouvert dans le terminal
        kwargs.update(login=creds.login, password=creds.password, server=creds.server)

    with _lock:
        for attempt in range(1, max_attempts + 1):
            log.info("Connexion à MT5 (tentative %d/%d) : %s", attempt, max_attempts,
                     f"compte {creds.login} @ {creds.server}" if "login" in kwargs else "compte ouvert dans le terminal")

            if not mt5.initialize(*args, **kwargs):
                log.warning("Échec de mt5.initialize() : %s", _last_error())
                mt5.shutdown()
            elif _verify_session():
                _connected = True
                return True
            else:
                mt5.shutdown()

            if attempt < max_attempts:
                delay = CONNECT_RETRY_DELAY_SECONDS * 2 ** (attempt - 1)
                log.info("Nouvelle tentative dans %ds...", delay)
                time.sleep(delay)

        _connected = False
        log.error("Impossible de se connecter à MT5 après %d tentatives", max_attempts)
        return False


# Codes de mt5.last_error() -> message compréhensible pour l'utilisateur
_LOGIN_ERRORS = {
    -6: "identifiants refusés : vérifiez le numéro de compte, le mot de passe de trading et le serveur",
    -10003: "initialisation du terminal impossible",
    -10004: "terminal MT5 non connecté",
    -10005: "terminal MT5 injoignable (délai dépassé) : est-il ouvert ?",
    -2: "paramètres de connexion invalides",
}


def _login_error() -> str:
    code, text = mt5.last_error()
    return _LOGIN_ERRORS.get(code, f"{text} (code {code})")


def switch_account(login: int, password: str, server: str) -> tuple[bool, str]:
    """
    Connecte le terminal à un autre compte (mt5.initialize puis mt5.login). En cas d'échec,
    le compte précédent est rétabli : un mot de passe erroné ne doit pas laisser le bot sans compte.
    Retourne (succès, message lisible). Le mot de passe n'est jamais journalisé.
    """
    global _session, _connected
    cfg = settings.mt5
    with _lock:
        previous = active_credentials()
        if mt5.terminal_info() is None:
            args = (cfg.path,) if cfg.path else ()
            if not mt5.initialize(*args, timeout=cfg.timeout_ms):
                return False, f"Échec de connexion MT5 : {_login_error()}"
        log.warning("Changement de compte MT5 demandé : %d @ %s -> %d @ %s",
                    previous.login, previous.server, login, server)
        ok = mt5.login(login, password=password, server=server, timeout=cfg.timeout_ms)
        account = mt5.account_info() if ok else None
        if ok and account is not None and account.login == login:
            env = env_credentials()
            new = Credentials(login, password, server)
            _session = None if login == env.login and server == env.server else new
            _connected = True
            log.warning("Compte MT5 actif : %d @ %s (%s)", login, server, account.name)
            return True, f"Connecté au compte {login} @ {server}"

        reason = _login_error() if not ok else "le terminal n'a pas basculé sur ce compte"
        log.error("Connexion au compte %d @ %s refusée : %s ; retour au compte %d", login, server, reason,
                  previous.login)
        restore = {"server": previous.server, "timeout": cfg.timeout_ms}
        if previous.password:                  # terminal actif : mot de passe mémorisé par le terminal
            restore["password"] = previous.password
        restored = bool(previous.login) and mt5.login(previous.login, **restore)
        _connected = bool(restored)
        if not restored:
            log.critical("Compte précédent %d non rétabli : reconnexion automatique au prochain tour", previous.login)
        suffix = "" if restored else " (compte précédent en cours de rétablissement)"
        return False, f"Échec de connexion MT5 : {reason}{suffix}"


def terminal_trade_allowed() -> bool | None:
    """État du bouton « Algo Trading » du terminal (None si le terminal ne répond pas)."""
    with _lock:
        terminal = mt5.terminal_info()
    return bool(terminal.trade_allowed) if terminal is not None else None


def reset_account() -> tuple[bool, str]:
    """Retour au compte du .env, ou au compte d'origine du terminal (mode terminal actif)."""
    global _session
    env = env_credentials()
    if env.password:
        return switch_account(env.login, env.password, env.server)
    with _lock:                                # mot de passe mémorisé par le terminal
        ok = bool(env.login) and mt5.login(env.login, server=env.server, timeout=settings.mt5.timeout_ms)
        if ok:
            _session = None
            return True, f"Retour au compte {env.login} @ {env.server}"
        return False, f"Retour au compte d'origine impossible : {_login_error()}"


def shutdown_mt5() -> None:
    """Ferme proprement la connexion au terminal."""
    global _connected
    with _lock:
        mt5.shutdown()
        _connected = False
    log.info("Connexion MT5 fermée")


def is_connected() -> bool:
    """True si le terminal répond et est connecté au serveur du broker."""
    with _lock:
        terminal = mt5.terminal_info()
        return _connected and terminal is not None and bool(terminal.connected)


def ensure_connection() -> None:
    """Vérifie la connexion et tente une reconnexion si elle a été perdue."""
    if is_connected():
        return
    with _lock:
        if _connected:
            log.warning("Connexion MT5 perdue (%s), reconnexion...", _last_error())
            mt5.shutdown()
        else:
            log.info("MT5 non initialisé, connexion...")
        if not connect_mt5():
            raise MT5ConnectionError("Connexion à MT5 impossible")


def _requires_connection(func: F) -> F:
    """Décorateur : verrouille l'accès à l'API et garantit une connexion active."""
    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        with _lock:
            ensure_connection()
            return func(*args, **kwargs)
    return wrapper  # type: ignore[return-value]


# =============================================================================
#  Données de marché
# =============================================================================

@_requires_connection
def get_historical_data(symbol: str, count: int = 100, timeframe: str | None = None) -> pd.DataFrame:
    """
    Retourne les `count` dernières bougies OHLCV (la dernière est la bougie en cours).
    Index : `time` (DatetimeIndex). MT5 fournit l'heure du serveur du broker,
    étiquetée UTC ici pour garder un index tz-aware cohérent.
    Retourne un DataFrame vide en cas d'échec.
    """
    if count <= 0:
        raise ValueError(f"count doit être > 0 (reçu : {count})")
    tf_name = (timeframe or settings.trading.timeframe).upper()
    tf = get_timeframe(tf_name)
    _ensure_symbol(symbol)

    rates = mt5.copy_rates_from_pos(symbol, tf, 0, count)
    if rates is None or len(rates) == 0:
        log.error("Aucune bougie reçue pour %s %s : %s", symbol, tf_name, _last_error())
        return pd.DataFrame(columns=OHLCV_COLUMNS)

    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
    df = df.rename(columns={"tick_volume": "volume"}).set_index("time")[OHLCV_COLUMNS]

    if len(df) < count:
        log.warning("Historique partiel pour %s %s : %d/%d bougies", symbol, tf_name, len(df), count)
    log.debug("%d bougies %s %s récupérées (%s -> %s)",
              len(df), symbol, tf_name, df.index[0], df.index[-1])
    return df


@_requires_connection
def get_current_tick(symbol: str) -> Tick | None:
    """Retourne le dernier prix (Bid/Ask) du symbole, ou None si indisponible."""
    tick = _read_tick(symbol, _ensure_symbol(symbol))
    if tick:
        log.debug("Tick %s : bid %s | ask %s | spread %d pts", symbol, tick.bid, tick.ask, tick.spread_points)
    return tick


# =============================================================================
#  Compte & positions
# =============================================================================

@_requires_connection
def get_account_info() -> AccountInfo | None:
    """Retourne le solde, les fonds propres et la marge du compte."""
    account = mt5.account_info()
    if account is None:
        log.error("account_info() indisponible : %s", _last_error())
        return None
    return AccountInfo(
        login=account.login,
        balance=account.balance,
        equity=account.equity,
        margin=account.margin,
        margin_free=account.margin_free,
        margin_level=account.margin_level,
        profit=account.profit,
        currency=account.currency,
        leverage=account.leverage,
        name=account.name,
        company=account.company,
        server=account.server,
        account_type=_ACCOUNT_TYPES.get(account.trade_mode, "unknown"),
    )


@_requires_connection
def get_open_positions(symbol: str | None = None, bot_only: bool = True) -> list[Any]:
    """Positions ouvertes (filtrées sur le magic number du bot par défaut)."""
    positions = mt5.positions_get(symbol=symbol) if symbol else mt5.positions_get()
    if positions is None:
        log.error("positions_get() a échoué : %s", _last_error())
        return []
    if bot_only:
        positions = [p for p in positions if p.magic == settings.trading.magic_number]
    return list(positions)


@_requires_connection
def get_closed_deals(since: datetime, bot_only: bool = False) -> list[ClosedDeal]:
    """
    Transactions de clôture (sorties de position) depuis `since`, de la plus ancienne
    à la plus récente. Les dépôts/retraits et les entrées en position sont exclus.
    """
    # `date_to` dans le futur : l'heure serveur du broker est souvent en avance sur UTC
    deals = mt5.history_deals_get(since, datetime.now(timezone.utc) + timedelta(days=1))
    if deals is None:
        log.error("history_deals_get() a échoué : %s", _last_error())
        return []

    closing_entries = {mt5.DEAL_ENTRY_OUT, mt5.DEAL_ENTRY_INOUT, mt5.DEAL_ENTRY_OUT_BY}
    result = [
        ClosedDeal(
            ticket=d.ticket,
            symbol=d.symbol,
            time=datetime.fromtimestamp(d.time, tz=timezone.utc),
            net_profit=d.profit + d.commission + d.swap + getattr(d, "fee", 0.0),
            magic=d.magic,
        )
        for d in deals
        if d.type in (mt5.DEAL_TYPE_BUY, mt5.DEAL_TYPE_SELL) and d.entry in closing_entries
        and (not bot_only or d.magic == settings.trading.magic_number)
    ]
    return sorted(result, key=lambda d: d.time)


# =============================================================================
#  Spécifications symbole & calculs broker
# =============================================================================

@_requires_connection
def get_symbol_specs(symbol: str) -> SymbolSpecs | None:
    """Contraintes de trading du symbole (volumes, stops, valeur du tick), ou None."""
    try:
        info = _ensure_symbol(symbol)
    except ValueError as exc:
        log.error("%s", exc)
        return None
    return SymbolSpecs(
        symbol=symbol,
        digits=info.digits,
        point=info.point,
        tick_size=info.trade_tick_size,
        tick_value=info.trade_tick_value,
        volume_min=info.volume_min,
        volume_max=info.volume_max,
        volume_step=info.volume_step,
        stops_level=info.trade_stops_level,
        freeze_level=info.trade_freeze_level,
        trade_allowed=info.trade_mode != mt5.SYMBOL_TRADE_MODE_DISABLED,
        contract_size=float(getattr(info, "trade_contract_size", 0.0) or 1.0),
    )


# =============================================================================
#  Diagnostic d'un symbole : contrat, cotation, verdict de tradabilité
# =============================================================================

OFFSET_STEP_SECONDS = 900              # décalages horaires des serveurs : multiples de 15 minutes
OFFSET_FRESH_SECONDS = 300             # un tick sert à estimer le décalage s'il a moins de 5 minutes
OFFSET_CACHE_SECONDS = 600
OFFSET_ANCHORS = ("EURUSD", "GBPUSD", "USDJPY", "BTCUSD")   # BTCUSD cote aussi le week-end
MEDIAN_SPREAD_MINUTES = 60

_offset_cache: tuple[float, float] | None = None            # (horodatage monotone, décalage en secondes)


def server_time_offset() -> float | None:
    """
    Décalage (secondes) entre l'heure du serveur du broker et l'heure UTC, nécessaire pour
    mesurer l'âge d'un tick. MT5_SERVER_UTC_OFFSET_HOURS s'il est configuré ; sinon estimé sur
    le tick le plus récent de quelques symboles très liquides (arrondi au quart d'heure).
    """
    global _offset_cache
    configured = settings.mt5.server_utc_offset_hours
    if configured is not None:
        return configured * 3600
    if _offset_cache and time.monotonic() - _offset_cache[0] < OFFSET_CACHE_SECONDS:
        return _offset_cache[1]
    now = datetime.now(timezone.utc).timestamp()
    latest = max((t.time for s in {*settings.trading.symbols, *OFFSET_ANCHORS}
                  if (t := mt5.symbol_info_tick(s)) is not None and t.time), default=None)
    if latest is None:
        return _offset_cache[1] if _offset_cache else None
    offset = round((latest - now) / OFFSET_STEP_SECONDS) * OFFSET_STEP_SECONDS
    if abs(latest - now - offset) > OFFSET_FRESH_SECONDS:      # marchés fermés : estimation impossible
        return _offset_cache[1] if _offset_cache else None
    _offset_cache = (time.monotonic(), float(offset))
    return float(offset)


_TRADE_MODES = {"SYMBOL_TRADE_MODE_DISABLED": "disabled", "SYMBOL_TRADE_MODE_LONGONLY": "long_only",
                "SYMBOL_TRADE_MODE_SHORTONLY": "short_only", "SYMBOL_TRADE_MODE_CLOSEONLY": "close_only",
                "SYMBOL_TRADE_MODE_FULL": "full"}


def _trade_mode_name(value: int) -> str:
    return next((name for const, name in _TRADE_MODES.items() if getattr(mt5, const, None) == value), str(value))


def diagnose_symbol(symbol: str, entry: float | None = None, sl: float | None = None,
                    max_spread_points: int | None = None) -> SymbolDiagnostic:
    """
    Spécifications du contrat et qualité de cotation d'un symbole, et verdict `tradable`.
    L'actif est rejeté si : symbole introuvable, trading désactivé ou clôture seule, aucun prix
    (marché fermé), tick périmé (> MT5_MAX_TICK_AGE_SECONDS), spread > plafond (RISK_MAX_SPREAD_POINTS),
    spread > RISK_MAX_SPREAD_MEDIAN_RATIO x sa médiane des 60 dernières minutes, ou spread >
    RISK_MAX_SPREAD_SL_PCT % de la distance entrée - stop (si `entry` et `sl` sont fournis).
    Ne lève jamais : un terminal injoignable donne un diagnostic « unavailable ».
    """
    risk = settings.risk
    diag = SymbolDiagnostic(symbol=symbol)

    def fail(code: str, message: str) -> None:
        diag.failures.append(DiagnosticFailure(code=code, message=message))

    try:
        with _lock:
            ensure_connection()
            info = mt5.symbol_info(symbol)
            if info is None:
                fail("unknown", f"symbole {symbol} introuvable chez le broker")
                return diag
            if not info.visible:
                mt5.symbol_select(symbol, True)
            diag.found = True
            diag.trade_mode = _trade_mode_name(info.trade_mode)
            diag.description = getattr(info, "description", "") or ""
            diag.currency_profit = getattr(info, "currency_profit", None)
            diag.contract_size = float(getattr(info, "trade_contract_size", 0.0) or 0.0) or None
            diag.volume_min, diag.volume_max, diag.volume_step = info.volume_min, info.volume_max, info.volume_step
            diag.digits, diag.point = info.digits, info.point
            diag.tick_size, diag.tick_value = info.trade_tick_size, info.trade_tick_value
            diag.stops_level_points = info.trade_stops_level
            if diag.trade_mode == "disabled":
                fail("disabled", "trading désactivé sur ce symbole")
            elif diag.trade_mode == "close_only":
                fail("close_only", "symbole en clôture seule (aucune nouvelle position)")

            tick = mt5.symbol_info_tick(symbol)
            if tick is None or tick.bid <= 0 or tick.ask <= 0:
                fail("no_price", "aucun prix : marché fermé")
                return diag
            diag.bid, diag.ask = tick.bid, tick.ask
            diag.spread_price = round(tick.ask - tick.bid, info.digits + 1)
            diag.spread_points = round((tick.ask - tick.bid) / info.point)
            diag.tick_time_server = datetime.fromtimestamp(tick.time, tz=timezone.utc)
            offset = server_time_offset()
            if offset is not None:
                diag.tick_age_seconds = round(datetime.now(timezone.utc).timestamp() + offset - tick.time, 1)
                if diag.tick_age_seconds > settings.mt5.max_tick_age_seconds:
                    fail("stale_tick", f"prix figé depuis {diag.tick_age_seconds:.0f} s "
                                       f"(maximum {settings.mt5.max_tick_age_seconds} s) : marché fermé ou flux interrompu")

            rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M1, 0, MEDIAN_SPREAD_MINUTES)
            if rates is not None and len(rates):
                spreads = [int(r["spread"]) for r in rates if int(r["spread"]) > 0]
                if spreads:
                    diag.median_spread_points_60m = float(sorted(spreads)[len(spreads) // 2])
    except (MT5ConnectionError, ValueError) as exc:
        fail("unavailable", f"terminal MT5 indisponible : {exc}")
        return diag

    cap = max_spread_points if max_spread_points is not None else risk.max_spread_points
    if diag.spread_points > cap:
        fail("spread_max", f"spread {diag.spread_points} pts > plafond {cap} pts")
    median = diag.median_spread_points_60m
    if median and diag.spread_points > risk.max_spread_median_ratio * median:
        fail("spread_median", f"spread {diag.spread_points} pts > {risk.max_spread_median_ratio:g} x sa médiane "
                              f"des 60 dernières minutes ({median:g} pts) : élargissement anormal")
    if entry is not None and sl is not None and entry != sl:
        diag.spread_to_sl_pct = round(diag.spread_price / abs(entry - sl) * 100, 2)
        if diag.spread_to_sl_pct > risk.max_spread_sl_pct:
            fail("spread_sl", f"spread = {diag.spread_to_sl_pct:.1f} % de la distance entrée-stop "
                              f"(maximum {risk.max_spread_sl_pct:g} %)")
    diag.tradable = not diag.failures
    return diag


def _order_type(side: str) -> int:
    return mt5.ORDER_TYPE_BUY if side.upper() == "BUY" else mt5.ORDER_TYPE_SELL


@_requires_connection
def calc_profit(side: str, symbol: str, volume: float, price_open: float, price_close: float) -> float | None:
    """Profit (négatif = perte) en devise du compte, conversions de devises incluses ; None si échec."""
    profit = mt5.order_calc_profit(_order_type(side), symbol, volume, price_open, price_close)
    if profit is None:
        log.warning("order_calc_profit(%s) a échoué : %s", symbol, _last_error())
    return profit


@_requires_connection
def calc_margin(side: str, symbol: str, volume: float, price: float) -> float | None:
    """Marge requise en devise du compte pour ouvrir la position ; None si échec."""
    margin = mt5.order_calc_margin(_order_type(side), symbol, volume, price)
    if margin is None:
        log.warning("order_calc_margin(%s) a échoué : %s", symbol, _last_error())
    return margin


# =============================================================================
#  Exécution des ordres
# =============================================================================

def _send_order(request: dict[str, Any], side: str, symbol: str) -> OrderResult:
    """Valide (`order_check`) puis envoie la requête, avec retry sur requote."""
    def failure(message: str, retcode: int = 0) -> OrderResult:
        log.error("Ordre %s %s échoué : %s", side, symbol, message)
        return OrderResult(False, symbol, side, request["volume"], request["price"],
                           request.get("sl"), request.get("tp"), retcode=retcode, message=message)

    check = mt5.order_check(request)
    if check is None:
        return failure(f"order_check() indisponible : {_last_error()}")
    if check.retcode != 0:
        return failure(f"order_check refusé : {check.comment}", check.retcode)

    for attempt in range(1, MAX_ORDER_ATTEMPTS + 1):
        log.info("Envoi ordre %s %.2f %s @ %s (tentative %d/%d)",
                 side, request["volume"], symbol, request["price"], attempt, MAX_ORDER_ATTEMPTS)
        result = mt5.order_send(request)

        if result is None:
            # Pas de renvoi automatique : on ne sait pas si l'ordre est parti
            hint = "" if is_connected() else " (connexion perdue, vérifiez les positions manuellement)"
            return failure(f"order_send() sans réponse : {_last_error()}{hint}")

        if result.retcode in SUCCESS_RETCODES:
            log.info("Ordre exécuté : ticket #%d | %s %.2f %s @ %s | SL %s | TP %s",
                     result.order, side, result.volume, symbol, result.price,
                     request.get("sl"), request.get("tp"))
            return OrderResult(True, symbol, side, result.volume, result.price,
                               request.get("sl"), request.get("tp"), ticket=result.order,
                               retcode=result.retcode, message=result.comment)

        if result.retcode in RETRYABLE_RETCODES and attempt < MAX_ORDER_ATTEMPTS:
            log.warning("Prix modifié / requote (%d : %s), nouvelle tentative",
                        result.retcode, result.comment)
            time.sleep(ORDER_RETRY_DELAY_SECONDS)
            tick = mt5.symbol_info_tick(symbol)
            if tick is None:
                return failure(f"Impossible de rafraîchir le prix : {_last_error()}")
            request["price"] = tick.ask if request["type"] == mt5.ORDER_TYPE_BUY else tick.bid
            continue

        return failure(f"rejeté par le serveur : {result.comment}", result.retcode)

    return failure("nombre maximal de tentatives atteint")


def execute_order(
    symbol: str,
    order_type: str,
    volume: float,
    sl: float | None = None,
    tp: float | None = None,
    comment: str = "Bot Order",
) -> OrderResult:
    """
    Ouvre une position au marché (`order_type` = "BUY" ou "SELL").
    En DRY_RUN, l'ordre est entièrement validé (symbole, spread, volume, stops)
    puis simulé : aucun appel à `mt5.order_send()`.
    Ne lève pas d'exception : le résultat est toujours un `OrderResult`.
    """
    side = order_type.strip().upper()
    if side not in ("BUY", "SELL"):
        log.error("Type d'ordre invalide : %r (attendu : BUY ou SELL)", order_type)
        return OrderResult(False, symbol, side, volume, message="type d'ordre invalide")

    try:
        with _lock:
            ensure_connection()
            info = _ensure_symbol(symbol)
            if info.trade_mode == mt5.SYMBOL_TRADE_MODE_DISABLED:
                raise ValueError(f"Trading désactivé sur {symbol}")

            tick = _read_tick(symbol, info)
            if tick is None:
                raise ValueError("aucun prix disponible")
            if tick.spread_points > settings.risk.max_spread_points:
                raise ValueError(f"spread trop élevé ({tick.spread_points} > "
                                 f"{settings.risk.max_spread_points} pts)")

            lots = _normalize_volume(info, volume)
            sl, tp = _validate_stops(info, side, tick, sl, tp)  # type: ignore[arg-type]
            price = tick.ask if side == "BUY" else tick.bid

            if is_dry_run():
                ticket = next(_dry_run_tickets)
                log.info("[DRY RUN] %s %.2f %s @ %s | SL %s | TP %s | ticket fictif #%d | %s",
                         side, lots, symbol, price, sl, tp, ticket, comment)
                return OrderResult(True, symbol, side, lots, price, sl, tp, ticket=ticket,
                                   retcode=mt5.TRADE_RETCODE_DONE, message="simulation", dry_run=True)

            request: dict[str, Any] = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": symbol,
                "volume": lots,
                "type": mt5.ORDER_TYPE_BUY if side == "BUY" else mt5.ORDER_TYPE_SELL,
                "price": price,
                "deviation": settings.trading.deviation_points,
                "magic": settings.trading.magic_number,
                "comment": comment[:MT5_COMMENT_MAX_LENGTH],
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": _filling_mode(info),
            }
            if sl is not None:
                request["sl"] = sl
            if tp is not None:
                request["tp"] = tp
            return _send_order(request, side, symbol)

    except (MT5ConnectionError, ValueError) as exc:
        log.error("Ordre %s %.2f %s refusé : %s", side, volume, symbol, exc)
        return OrderResult(False, symbol, side, volume, sl=sl, tp=tp, message=str(exc))


@_requires_connection
def get_position_deals(position_id: int) -> list[ClosedDeal]:
    """
    Transactions d'une position (ouverture puis clôture(s)), avec prix, motif (sl, tp, bot,
    manuel...) et P/L net. Heures du serveur du broker, comme les bougies.
    """
    deals = mt5.history_deals_get(position=position_id)
    if deals is None:
        log.error("history_deals_get(position=%d) a échoué : %s", position_id, _last_error())
        return []
    reasons = {mt5.DEAL_REASON_SL: "sl", mt5.DEAL_REASON_TP: "tp", mt5.DEAL_REASON_SO: "stop_out",
               mt5.DEAL_REASON_EXPERT: "bot", mt5.DEAL_REASON_CLIENT: "manuel", mt5.DEAL_REASON_MOBILE: "manuel",
               mt5.DEAL_REASON_WEB: "manuel"}
    entries = {mt5.DEAL_ENTRY_IN: "in", mt5.DEAL_ENTRY_OUT: "out", mt5.DEAL_ENTRY_INOUT: "out",
               mt5.DEAL_ENTRY_OUT_BY: "out"}
    result = [
        ClosedDeal(ticket=d.ticket, symbol=d.symbol, time=datetime.fromtimestamp(d.time, tz=timezone.utc),
                   net_profit=d.profit + d.commission + d.swap + getattr(d, "fee", 0.0), magic=d.magic,
                   position_id=d.position_id, price=d.price, reason=reasons.get(d.reason, str(d.reason)),
                   entry=entries.get(d.entry, "out"), volume=d.volume)
        for d in deals if d.type in (mt5.DEAL_TYPE_BUY, mt5.DEAL_TYPE_SELL)
    ]
    return sorted(result, key=lambda d: d.time)


def modify_position_stops(ticket: int, sl: float | None, tp: float | None) -> OrderResult:
    """
    Déplace le SL et/ou le TP d'une position ouverte (TRADE_ACTION_SLTP), arrondis aux
    décimales du symbole. En DRY_RUN, la modification est simulée.
    """
    try:
        with _lock:
            ensure_connection()
            positions = mt5.positions_get(ticket=ticket)
            if not positions:
                if is_dry_run() and ticket < 0:
                    return OrderResult(True, "", "MODIFY", 0.0, sl=sl, tp=tp, ticket=ticket,
                                       message="simulation", dry_run=True)
                raise ValueError(f"position #{ticket} introuvable")
            pos = positions[0]
            info = _ensure_symbol(pos.symbol)
            new_sl = round(sl, info.digits) if sl is not None else pos.sl
            new_tp = round(tp, info.digits) if tp is not None else pos.tp
            if is_dry_run():
                log.info("[DRY RUN] Position #%d : SL %s -> %s | TP %s -> %s", ticket, pos.sl, new_sl, pos.tp, new_tp)
                return OrderResult(True, pos.symbol, "MODIFY", pos.volume, sl=new_sl, tp=new_tp, ticket=ticket,
                                   message="simulation", dry_run=True)
            request = {"action": mt5.TRADE_ACTION_SLTP, "position": ticket, "symbol": pos.symbol,
                       "sl": new_sl, "tp": new_tp, "magic": settings.trading.magic_number}
            result = mt5.order_send(request)
            if result is None or result.retcode not in SUCCESS_RETCODES:
                reason = result.comment if result is not None else _last_error()
                raise ValueError(f"modification refusée : {reason}")
            log.info("Position #%d : SL %s -> %s | TP %s -> %s", ticket, pos.sl, new_sl, pos.tp, new_tp)
            return OrderResult(True, pos.symbol, "MODIFY", pos.volume, sl=new_sl, tp=new_tp, ticket=ticket,
                               retcode=result.retcode, message=result.comment)
    except (MT5ConnectionError, ValueError) as exc:
        log.error("Modification SL/TP de la position #%d refusée : %s", ticket, exc)
        return OrderResult(False, "", "MODIFY", 0.0, sl=sl, tp=tp, ticket=ticket, message=str(exc))


def _closing_volume(info: Any, position_volume: float, volume: float | None) -> float:
    """Volume à fermer : tout, ou `volume` arrondi au pas ; un reliquat sous le minimum est fermé avec."""
    if volume is None or volume >= position_volume:
        return position_volume
    lots = _normalize_volume(info, volume)
    return position_volume if position_volume - lots < info.volume_min - 1e-9 else lots


def close_position(ticket: int, comment: str = "Bot Close", force: bool = False,
                   volume: float | None = None) -> OrderResult:
    """
    Clôture une position ouverte par son ticket : entièrement, ou `volume` lots (clôture partielle).
    `force=True` envoie une clôture réelle même en mode PAPER (kill-switch d'urgence).
    """
    try:
        with _lock:
            ensure_connection()
            positions = mt5.positions_get(ticket=ticket)
            if not positions:
                if is_dry_run() and ticket < 0:
                    log.info("[DRY RUN] Clôture simulée de la position fictive #%d", ticket)
                    return OrderResult(True, "", "CLOSE", 0.0, ticket=ticket,
                                       message="simulation", dry_run=True)
                raise ValueError(f"position #{ticket} introuvable")

            pos = positions[0]
            info = _ensure_symbol(pos.symbol)
            tick = _read_tick(pos.symbol, info)
            if tick is None:
                raise ValueError("aucun prix disponible")

            is_buy = pos.type == mt5.POSITION_TYPE_BUY
            side = "SELL" if is_buy else "BUY"
            price = tick.bid if is_buy else tick.ask
            lots = _closing_volume(info, pos.volume, volume)

            if is_dry_run() and not force:
                log.info("[DRY RUN] Clôture simulée #%d : %s %.2f / %.2f %s @ %s (P/L actuel %.2f)",
                         ticket, side, lots, pos.volume, pos.symbol, price, pos.profit)
                return OrderResult(True, pos.symbol, side, lots, price, ticket=ticket,
                                   retcode=mt5.TRADE_RETCODE_DONE, message="simulation", dry_run=True)

            request: dict[str, Any] = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": pos.symbol,
                "volume": lots,
                "type": mt5.ORDER_TYPE_SELL if is_buy else mt5.ORDER_TYPE_BUY,
                "position": ticket,
                "price": price,
                "deviation": settings.trading.deviation_points,
                "magic": settings.trading.magic_number,
                "comment": comment[:MT5_COMMENT_MAX_LENGTH],
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": _filling_mode(info),
            }
            log.info("Clôture de la position #%d (%s %.2f / %.2f, P/L %.2f)",
                     ticket, pos.symbol, lots, pos.volume, pos.profit)
            return _send_order(request, side, pos.symbol)

    except (MT5ConnectionError, ValueError) as exc:
        log.error("Clôture de la position #%d refusée : %s", ticket, exc)
        return OrderResult(False, "", "CLOSE", 0.0, ticket=ticket, message=str(exc))


if __name__ == "__main__":
    # `python -m brokers.mt5_client` : test rapide de la connexion et des données
    if connect_mt5():
        try:
            log.info("Compte : %s", get_account_info())
            first = settings.trading.symbols[0]
            log.info("Tick %s : %s", first, get_current_tick(first))
            log.info("Dernières bougies %s :\n%s", first, get_historical_data(first, count=5))
            if is_dry_run():
                execute_order(first, "BUY", settings.risk.min_lot, comment="Test DRY RUN")
        finally:
            shutdown_mt5()
