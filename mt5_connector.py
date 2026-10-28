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
    from mt5_connector import connect_mt5, get_historical_data, execute_order, shutdown_mt5

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
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable, Literal, TypeVar

import MetaTrader5 as mt5
import pandas as pd

from config import get_logger, settings

log = get_logger(__name__)

OrderSide = Literal["BUY", "SELL"]
F = TypeVar("F", bound=Callable[..., Any])


class MT5ConnectionError(RuntimeError):
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

_lock = threading.RLock()
_connected = False
_dry_run_tickets = itertools.count(-1, -1)   # tickets fictifs négatifs : jamais confondus avec des vrais


# =============================================================================
#  Structures de retour
# =============================================================================

@dataclass(frozen=True)
class Tick:
    symbol: str
    bid: float
    ask: float
    last: float
    spread_points: int
    time: datetime

    @property
    def spread(self) -> float:
        return self.ask - self.bid


@dataclass(frozen=True)
class AccountInfo:
    login: int
    balance: float
    equity: float
    margin: float
    margin_free: float
    margin_level: float
    profit: float
    currency: str
    leverage: int


@dataclass(frozen=True)
class OrderResult:
    success: bool
    symbol: str
    order_type: str
    volume: float
    price: float = 0.0
    sl: float | None = None
    tp: float | None = None
    ticket: int = 0
    retcode: int = 0
    message: str = ""
    dry_run: bool = False


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
    if account.login != settings.mt5.account:
        log.error("Compte connecté (%d) différent du compte configuré (%d)",
                  account.login, settings.mt5.account)
        return False

    log.info("Connecté à MT5 : compte %d | %s | solde %.2f %s | levier 1:%d",
             account.login, account.server, account.balance, account.currency, account.leverage)

    if not terminal.trade_allowed:
        log.warning("Algo Trading désactivé dans le terminal : les ordres réels seront refusés")
    if not account.trade_allowed:
        log.warning("Le trading n'est pas autorisé sur ce compte")
    if account.trade_mode == mt5.ACCOUNT_TRADE_MODE_REAL and not settings.dry_run:
        log.warning("ATTENTION : COMPTE RÉEL en mode LIVE, les ordres engagent de l'argent réel")
    return True


def connect_mt5(max_attempts: int = MAX_CONNECT_ATTEMPTS) -> bool:
    """
    Initialise le terminal MT5 et se connecte au compte configuré.
    Réessaie avec un délai croissant. Retourne True si la session est active.
    """
    global _connected
    cfg = settings.mt5
    args = (cfg.path,) if cfg.path else ()
    kwargs = {
        "login": cfg.account,
        "password": cfg.password,
        "server": cfg.server,
        "timeout": cfg.timeout_ms,
    }

    with _lock:
        for attempt in range(1, max_attempts + 1):
            log.info("Connexion à MT5 (tentative %d/%d) : compte %d @ %s",
                     attempt, max_attempts, cfg.account, cfg.server)

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

            if settings.dry_run:
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


def close_position(ticket: int, comment: str = "Bot Close") -> OrderResult:
    """Clôture entièrement une position ouverte par son ticket."""
    try:
        with _lock:
            ensure_connection()
            positions = mt5.positions_get(ticket=ticket)
            if not positions:
                if settings.dry_run and ticket < 0:
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

            if settings.dry_run:
                log.info("[DRY RUN] Clôture simulée #%d : %s %.2f %s @ %s (P/L actuel %.2f)",
                         ticket, side, pos.volume, pos.symbol, price, pos.profit)
                return OrderResult(True, pos.symbol, side, pos.volume, price, ticket=ticket,
                                   retcode=mt5.TRADE_RETCODE_DONE, message="simulation", dry_run=True)

            request: dict[str, Any] = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": pos.symbol,
                "volume": pos.volume,
                "type": mt5.ORDER_TYPE_SELL if is_buy else mt5.ORDER_TYPE_BUY,
                "position": ticket,
                "price": price,
                "deviation": settings.trading.deviation_points,
                "magic": settings.trading.magic_number,
                "comment": comment[:MT5_COMMENT_MAX_LENGTH],
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": _filling_mode(info),
            }
            log.info("Clôture de la position #%d (%s %.2f, P/L %.2f)",
                     ticket, pos.symbol, pos.volume, pos.profit)
            return _send_order(request, side, pos.symbol)

    except (MT5ConnectionError, ValueError) as exc:
        log.error("Clôture de la position #%d refusée : %s", ticket, exc)
        return OrderResult(False, "", "CLOSE", 0.0, ticket=ticket, message=str(exc))


if __name__ == "__main__":
    # `python mt5_connector.py` : test rapide de la connexion et des données
    if connect_mt5():
        try:
            log.info("Compte : %s", get_account_info())
            first = settings.trading.symbols[0]
            log.info("Tick %s : %s", first, get_current_tick(first))
            log.info("Dernières bougies %s :\n%s", first, get_historical_data(first, count=5))
            if settings.dry_run:
                execute_order(first, "BUY", settings.risk.min_lot, comment="Test DRY RUN")
        finally:
            shutdown_mt5()
