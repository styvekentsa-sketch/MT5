"""
Gestion du risque : dernier garde-fou avant l'envoi de tout ordre.

  - `calculate_position_size()` : lot calculé pour ne perdre que
    `settings.risk.risk_per_trade_pct` % du capital si le Stop Loss est touché
  - `validate_trade_risk()`     : vérifie spread, exposition, limites journalières,
    distances SL/TP, volume, perte au SL et marge ; tout doute = ordre refusé

Principe : en cas de donnée manquante, incohérente ou d'erreur, on refuse.
Aucune fonction ne lève d'exception.

Usage :
    from risk.risk_manager import calculate_position_size, validate_trade_risk

    volume = calculate_position_size("EURUSD", entry_price=1.0850, sl_price=1.0820)
    check = validate_trade_risk("EURUSD", "BUY", volume, sl=1.0820, tp=1.0910)
    if check["is_approved"]:
        execute_order("EURUSD", "BUY", volume, sl=1.0820, tp=1.0910)
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from decimal import ROUND_FLOOR, Decimal
from typing import Any, Protocol

from brokers.mt5_client import (
    AccountInfo,
    ClosedDeal,
    SymbolSpecs,
    Tick,
    calc_margin,
    calc_profit,
    get_account_info,
    get_closed_deals,
    get_current_tick,
    get_open_positions,
    get_symbol_specs,
)
from config.settings import settings
from utils.logger import get_logger

log = get_logger(__name__)


# =============================================================================
#  Constantes
# =============================================================================

MAX_SL_DISTANCE_PCT = 10.0           # SL à plus de 10 % du prix d'entrée : jugé aberrant
MIN_REWARD_RISK = 1.0                # le TP doit être au moins aussi loin que le SL
RISK_TOLERANCE = 1.05                # perte au SL tolérée jusqu'à 105 % du budget (arrondis broker)
MAX_MARGIN_USAGE = 0.90              # marge requise <= 90 % de la marge libre
VOLUME_EPSILON = 1e-9
VOLUME_DECIMALS = 8                  # absorbe les erreurs d'arrondi flottant avant l'arrondi au pas


# =============================================================================
#  Accès au broker (MT5 en réel, broker simulé en backtest)
# =============================================================================

class Broker(Protocol):
    """Données de marché et de compte nécessaires aux règles de risque."""

    def now(self) -> datetime: ...
    def get_account_info(self) -> AccountInfo | None: ...
    def get_symbol_specs(self, symbol: str) -> SymbolSpecs | None: ...
    def get_current_tick(self, symbol: str) -> Tick | None: ...
    def get_open_positions(self) -> list[Any]: ...
    def get_closed_deals(self, since: datetime) -> list[ClosedDeal]: ...
    def calc_profit(self, side: str, symbol: str, volume: float, price_open: float, price_close: float) -> float | None: ...
    def calc_margin(self, side: str, symbol: str, volume: float, price: float) -> float | None: ...


class _LiveBroker:
    """Terminal MetaTrader 5 (fonctions de brokers.mt5_client, résolues à chaque appel)."""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def get_account_info(self) -> AccountInfo | None:
        return get_account_info()

    def get_symbol_specs(self, symbol: str) -> SymbolSpecs | None:
        return get_symbol_specs(symbol)

    def get_current_tick(self, symbol: str) -> Tick | None:
        return get_current_tick(symbol)

    def get_open_positions(self) -> list[Any]:
        return get_open_positions()

    def get_closed_deals(self, since: datetime) -> list[ClosedDeal]:
        return get_closed_deals(since)

    def calc_profit(self, side: str, symbol: str, volume: float, price_open: float, price_close: float) -> float | None:
        return calc_profit(side, symbol, volume, price_open, price_close)

    def calc_margin(self, side: str, symbol: str, volume: float, price: float) -> float | None:
        return calc_margin(side, symbol, volume, price)


LIVE_BROKER: Broker = _LiveBroker()


# =============================================================================
#  Helpers internes
# =============================================================================

def _valid_price(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0


def _risk_capital(account: AccountInfo) -> float:
    """Capital de référence : le plus prudent entre solde et fonds propres."""
    return min(account.balance, account.equity)


def _risk_budget(account: AccountInfo) -> float:
    return _risk_capital(account) * settings.risk.risk_per_trade_pct / 100


def _floor_to_step(volume: float, step: float) -> float:
    """
    Arrondit vers le bas au pas de volume du broker (Decimal : pas d'erreur de flottant).
    Le volume est d'abord arrondi à VOLUME_DECIMALS : sans cela, 100 / 200.00000000000017
    (= 0.4999999999999996) tomberait à 0.49 au lieu de 0.50.
    """
    d_step = Decimal(str(step))
    d_volume = Decimal(str(round(volume, VOLUME_DECIMALS)))
    return float((d_volume / d_step).to_integral_value(rounding=ROUND_FLOOR) * d_step)


def _volume_bounds(specs: SymbolSpecs) -> tuple[float, float]:
    """(volume minimal, volume maximal) autorisés : contraintes broker ET configuration."""
    return max(specs.volume_min, settings.risk.min_lot), min(specs.volume_max, settings.risk.max_lot)


def _loss_per_lot(symbol: str, side: str, entry: float, sl: float, specs: SymbolSpecs,
                  broker: Broker) -> float | None:
    """
    Perte (devise du compte) pour 1 lot si le SL est touché.
    Calcul broker (`order_calc_profit`, conversions incluses), sinon via la valeur du tick.
    """
    profit = broker.calc_profit(side, symbol, 1.0, entry, sl)
    if profit is not None and profit < 0:
        return -profit
    if specs.tick_size > 0 and specs.tick_value > 0:
        log.debug("%s : perte par lot estimée via tick_value (order_calc_profit indisponible)", symbol)
        return abs(entry - sl) / specs.tick_size * specs.tick_value
    return None


# =============================================================================
#  Calcul de la taille de position
# =============================================================================

def calculate_position_size(symbol: str, entry_price: float, sl_price: float, broker: Broker | None = None) -> float:
    """
    Volume (lots) tel que la perte au Stop Loss vaille `risk_per_trade_pct` % du capital.

    lot = (capital x risque %) / perte_par_lot, où perte_par_lot est la perte en devise
    du compte pour 1 lot entre `entry_price` et `sl_price`. Le résultat est arrondi
    VERS LE BAS au pas du broker et plafonné à min(volume_max broker, RISK_MAX_LOT).

    Returns:
        Le volume, ou 0.0 = ne pas trader : prix invalides, compte/symbole indisponible,
        ou lot minimal (broker / RISK_MIN_LOT) qui ferait dépasser le risque autorisé.
    """
    broker = broker or LIVE_BROKER
    try:
        if not _valid_price(entry_price) or not _valid_price(sl_price):
            log.error("[%s] Prix invalides (entrée %r, SL %r) : volume 0", symbol, entry_price, sl_price)
            return 0.0
        if entry_price == sl_price:
            log.error("[%s] SL égal au prix d'entrée : volume 0", symbol)
            return 0.0
        side = "BUY" if sl_price < entry_price else "SELL"

        account = broker.get_account_info()
        if account is None or _risk_capital(account) <= 0:
            log.error("[%s] Capital indisponible ou nul : volume 0", symbol)
            return 0.0
        specs = broker.get_symbol_specs(symbol)
        if specs is None:
            return 0.0

        loss_per_lot = _loss_per_lot(symbol, side, entry_price, sl_price, specs, broker)
        if not loss_per_lot or loss_per_lot <= 0:
            log.error("[%s] Perte par lot incalculable : volume 0", symbol)
            return 0.0

        budget = _risk_budget(account)
        raw_volume = budget / loss_per_lot
        min_volume, max_volume = _volume_bounds(specs)
        volume = _floor_to_step(min(raw_volume, max_volume), specs.volume_step)

        if volume < min_volume - VOLUME_EPSILON:
            log.warning("[%s] Lot calculé %.4f < minimum %.2f : %.2f lot risquerait %.2f %s (budget %.2f) : pas de trade",
                        symbol, raw_volume, min_volume, min_volume, loss_per_lot * min_volume,
                        account.currency, budget)
            return 0.0

        log.info("[%s] Lot %.2f (brut %.4f) | risque %.2f %s (%.2f%% de %.2f) | SL à %.5f du prix",
                 symbol, volume, raw_volume, loss_per_lot * volume, account.currency,
                 settings.risk.risk_per_trade_pct, _risk_capital(account), abs(entry_price - sl_price))
        return volume
    except Exception:  # noqa: BLE001 - MT5ConnectionError, données broker inattendues...
        log.exception("[%s] Erreur lors du calcul de la taille de position : volume 0", symbol)
        return 0.0


# =============================================================================
#  Contrôles avant ordre (chacun retourne la liste des violations)
# =============================================================================

def _check_spread(tick: Tick) -> list[str]:
    if tick.spread_points > settings.risk.max_spread_points:
        return [f"spread {tick.spread_points} pts > maximum {settings.risk.max_spread_points} pts"]
    return []


def _check_exposure(symbol: str, broker: Broker) -> list[str]:
    """Nombre de positions du bot, au total et sur ce symbole."""
    positions = broker.get_open_positions()
    failures = []
    if len(positions) >= settings.risk.max_open_positions:
        failures.append(f"{len(positions)} positions ouvertes (maximum {settings.risk.max_open_positions})")
    on_symbol = sum(1 for p in positions if p.symbol == symbol)
    if on_symbol >= settings.risk.max_positions_per_symbol:
        failures.append(f"{on_symbol} position(s) déjà ouverte(s) sur {symbol} "
                        f"(maximum {settings.risk.max_positions_per_symbol})")
    return failures


def _check_daily_limits(account: AccountInfo, broker: Broker) -> list[str]:
    """
    Perte journalière (réalisée depuis 00:00 UTC + latente, tout le compte) et
    série de pertes consécutives du bot sur la journée.
    """
    start_of_day = broker.now().replace(hour=0, minute=0, second=0, microsecond=0)
    deals = broker.get_closed_deals(start_of_day)
    realized = sum(d.net_profit for d in deals)
    start_balance = account.balance - realized
    failures = []

    if start_balance <= 0:
        return ["solde de début de journée nul ou négatif"]
    daily_loss_pct = -(realized + account.profit) / start_balance * 100
    if daily_loss_pct >= settings.risk.max_daily_loss_pct:
        failures.append(f"perte journalière {daily_loss_pct:.2f}% >= limite {settings.risk.max_daily_loss_pct}%")

    streak = 0
    for deal in reversed([d for d in deals if d.magic == settings.trading.magic_number]):
        if deal.net_profit >= 0:
            break
        streak += 1
    if streak >= settings.risk.max_consecutive_losses:
        failures.append(f"{streak} pertes consécutives aujourd'hui (maximum {settings.risk.max_consecutive_losses})")
    return failures


def _check_volume(volume: float, specs: SymbolSpecs) -> list[str]:
    min_volume, max_volume = _volume_bounds(specs)
    failures = []
    if volume < min_volume - VOLUME_EPSILON:
        failures.append(f"volume {volume} < minimum {min_volume}")
    if volume > max_volume + VOLUME_EPSILON:
        failures.append(f"volume {volume} > maximum {max_volume}")
    steps = volume / specs.volume_step
    if abs(steps - round(steps)) > 1e-6:
        failures.append(f"volume {volume} non multiple du pas {specs.volume_step}")
    return failures


def _check_stops(side: str, tick: Tick, entry: float, sl: float, tp: float | None, specs: SymbolSpecs) -> list[str]:
    """
    Côté et distance minimale (stops level broker) des SL/TP, SL non aberrant,
    ratio rendement/risque minimal. Les stops d'un BUY se déclenchent sur le Bid,
    ceux d'un SELL sur l'Ask.
    """
    min_distance = specs.stops_level * specs.point
    failures = []
    if side == "BUY":
        if sl >= tick.bid - min_distance:
            failures.append(f"SL {sl} doit être sous {tick.bid - min_distance:.{specs.digits}f} "
                            f"(Bid - stops level {specs.stops_level} pts)")
        if tp is not None and tp <= tick.bid + min_distance:
            failures.append(f"TP {tp} doit être au-dessus de {tick.bid + min_distance:.{specs.digits}f}")
    else:
        if sl <= tick.ask + min_distance:
            failures.append(f"SL {sl} doit être au-dessus de {tick.ask + min_distance:.{specs.digits}f} "
                            f"(Ask + stops level {specs.stops_level} pts)")
        if tp is not None and tp >= tick.ask - min_distance:
            failures.append(f"TP {tp} doit être sous {tick.ask - min_distance:.{specs.digits}f}")

    sl_distance = abs(entry - sl)
    if sl_distance / entry * 100 > MAX_SL_DISTANCE_PCT:
        failures.append(f"SL aberrant : à {sl_distance / entry * 100:.1f}% du prix (maximum {MAX_SL_DISTANCE_PCT}%)")
    if tp is not None and sl_distance > 0:
        reward_risk = abs(tp - entry) / sl_distance
        if reward_risk < MIN_REWARD_RISK:
            failures.append(f"ratio rendement/risque {reward_risk:.2f} < minimum {MIN_REWARD_RISK}")
    return failures


def _check_risk_and_margin(
    symbol: str, side: str, volume: float, entry: float, sl: float,
    account: AccountInfo, specs: SymbolSpecs, broker: Broker,
) -> tuple[list[str], float | None]:
    """Perte au SL comparée au budget de risque, et marge requise vs marge libre."""
    failures = []
    loss_per_lot = _loss_per_lot(symbol, side, entry, sl, specs, broker)
    potential_loss = loss_per_lot * volume if loss_per_lot else None
    budget = _risk_budget(account)

    if potential_loss is None:
        failures.append("perte au SL incalculable")
    elif potential_loss > budget * RISK_TOLERANCE:
        failures.append(f"perte au SL {potential_loss:.2f} {account.currency} > budget {budget:.2f} "
                        f"({settings.risk.risk_per_trade_pct}% du capital)")

    margin = broker.calc_margin(side, symbol, volume, entry)
    if margin is None:
        failures.append("marge requise incalculable")
    elif margin > account.margin_free * MAX_MARGIN_USAGE:
        failures.append(f"marge requise {margin:.2f} > {MAX_MARGIN_USAGE:.0%} de la marge libre "
                        f"({account.margin_free:.2f} {account.currency})")
    return failures, potential_loss


# =============================================================================
#  Validation avant ordre
# =============================================================================

def _verdict(
    symbol: str, action: str, volume: Any, sl: Any, tp: Any, failures: list[str],
    entry: float | None = None, potential_loss: float | None = None, capital: float | None = None,
) -> dict[str, Any]:
    approved = not failures
    reason = "toutes les règles de risque sont respectées" if approved else "; ".join(failures)
    if approved:
        log.info("[%s] Ordre %s %s APPROUVÉ | SL %s | TP %s | perte max %.2f",
                 symbol, action, volume, sl, tp, potential_loss or 0.0)
    else:
        log.warning("[%s] Ordre %s %s REFUSÉ : %s", symbol, action, volume, reason)
    return {
        "is_approved": approved,
        "reason": reason,
        "symbol": symbol,
        "action": action,
        "volume": volume,
        "sl": sl,
        "tp": tp,
        "entry_price": entry,
        "risk_amount": round(potential_loss, 2) if potential_loss is not None else None,
        "risk_pct": round(potential_loss / capital * 100, 3) if potential_loss and capital else None,
        "failed_checks": failures,
    }


def validate_trade_risk(symbol: str, action: str, volume: float, sl: float, tp: float | None,
                        broker: Broker | None = None) -> dict[str, Any]:
    """
    Vérifie qu'un ordre respecte toutes les règles de risque avant son envoi.

    Contrôles : paramètres cohérents (SL obligatoire), capital et marge, symbole
    tradable, prix disponible, spread <= RISK_MAX_SPREAD_POINTS, nombre de positions
    (total et par symbole), perte journalière <= RISK_MAX_DAILY_LOSS_PCT, pertes
    consécutives < RISK_MAX_CONSECUTIVE_LOSSES, volume (min/max/pas), SL/TP du bon
    côté et au-delà du stops level, SL non aberrant, rendement/risque >= MIN_REWARD_RISK,
    perte au SL <= budget de risque.

    Returns:
        dict avec `is_approved` (bool), `reason` (motif lisible, toutes violations
        listées), `failed_checks`, `entry_price`, `risk_amount`, `risk_pct` et les
        paramètres de l'ordre. Toute erreur ou donnée manquante => `is_approved: False`.

    `broker` : source des données (MT5 réel par défaut, broker simulé en backtest).
    """
    broker = broker or LIVE_BROKER
    side = action.strip().upper() if isinstance(action, str) else ""
    try:
        # 1. Paramètres de l'ordre
        if side not in ("BUY", "SELL"):
            return _verdict(symbol, str(action), volume, sl, tp, [f"action invalide : {action!r}"])
        if not _valid_price(sl):
            return _verdict(symbol, side, volume, sl, tp, ["Stop Loss manquant ou invalide : SL obligatoire"])
        if tp is not None and not _valid_price(tp):
            return _verdict(symbol, side, volume, sl, tp, [f"Take Profit invalide : {tp!r}"])
        if not _valid_price(volume):
            return _verdict(symbol, side, volume, sl, tp, [f"volume invalide : {volume!r}"])

        # 2. Compte, symbole, prix
        account = broker.get_account_info()
        if account is None:
            return _verdict(symbol, side, volume, sl, tp, ["informations du compte indisponibles"])
        capital = _risk_capital(account)
        if capital <= 0 or account.margin_free <= 0:
            return _verdict(symbol, side, volume, sl, tp,
                            [f"capital insuffisant (fonds propres {account.equity:.2f}, "
                             f"marge libre {account.margin_free:.2f})"])
        specs = broker.get_symbol_specs(symbol)
        if specs is None:
            return _verdict(symbol, side, volume, sl, tp, ["symbole inconnu ou indisponible"])
        if not specs.trade_allowed:
            return _verdict(symbol, side, volume, sl, tp, ["trading désactivé sur ce symbole"])
        tick = broker.get_current_tick(symbol)
        if tick is None or tick.ask < tick.bid:
            return _verdict(symbol, side, volume, sl, tp, ["prix indisponible ou incohérent (marché fermé ?)"])
        entry = tick.ask if side == "BUY" else tick.bid

        # 3. Règles de risque (toutes évaluées pour un motif complet)
        failures = (
            _check_spread(tick)
            + _check_exposure(symbol, broker)
            + _check_daily_limits(account, broker)
            + _check_volume(volume, specs)
            + _check_stops(side, tick, entry, sl, tp, specs)
        )
        risk_failures, potential_loss = _check_risk_and_margin(symbol, side, volume, entry, sl, account, specs, broker)
        failures += risk_failures
        return _verdict(symbol, side, volume, sl, tp, failures, entry, potential_loss, capital)
    except Exception as exc:  # noqa: BLE001 - MT5ConnectionError, données broker inattendues...
        log.exception("[%s] Erreur pendant la validation du risque", symbol)
        return _verdict(symbol, side or str(action), volume, sl, tp, [f"erreur interne : {exc}"])
