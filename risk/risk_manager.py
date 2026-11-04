"""
Gestion du risque : dernier garde-fou avant l'envoi de tout ordre.

  - `calculate_position_size()` : lot calculé pour ne perdre que
    `settings.risk.risk_per_trade_pct` % du capital si le Stop Loss est touché
  - `validate_trade_risk()`     : vérifie spread, exposition, limites journalières,
    distances SL/TP, volume, perte au SL et marge ; tout doute = ordre refusé

Principe : en cas de donnée manquante, incohérente ou d'erreur, on refuse.
Aucune fonction ne lève d'exception.

Multi-marchés : les règles s'appuient sur les spécifications fournies par le broker
(`SymbolSpecs.market`) :
  - Forex / CFD (MT5) : volume en lots, borné par RISK_MIN_LOT / RISK_MAX_LOT et par le broker,
    spread limité en points (RISK_MAX_SPREAD_POINTS)
  - Crypto (ccxt)     : volume en contrats, borné par les limites de l'exchange (minimum, pas,
    valeur minimale d'ordre), spread limité en % du prix (RISK_MAX_SPREAD_PCT)
La perte au SL est toujours calculée par le broker (`calc_profit`), taille de contrat incluse.

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
from typing import Any, Protocol

from brokers.models import POSITION_TYPE_BUY, AccountInfo, ClosedDeal, SymbolSpecs, Tick
from brokers.mt5_client import (
    calc_margin,
    calc_profit,
    get_account_info,
    get_closed_deals,
    get_current_tick,
    get_open_positions,
    get_symbol_specs,
)
from config.settings import settings
from risk.calculator import floor_to_step, size_position
from utils.logger import get_logger

log = get_logger(__name__)


# =============================================================================
#  Constantes
# =============================================================================

MAX_SL_DISTANCE_PCT = 10.0           # SL à plus de 10 % du prix d'entrée : jugé aberrant
MIN_REWARD_RISK = 1.0                # le TP doit être au moins aussi loin que le SL
REWARD_RISK_TOLERANCE = 0.98         # ratio mesuré au prix de validation : 1 tick de décalage toléré
RISK_TOLERANCE = 1.05                # perte au SL tolérée jusqu'à 105 % du budget (arrondis broker)
MAX_MARGIN_USAGE = 0.90              # marge requise <= 90 % de la marge libre
VOLUME_EPSILON = 1e-9


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


_floor_to_step = floor_to_step          # arrondi au pas : une seule implémentation (risk/calculator.py)


def _volume_bounds(specs: SymbolSpecs) -> tuple[float, float]:
    """
    (volume minimal, volume maximal) autorisés. Forex : contraintes broker ET RISK_MIN_LOT /
    RISK_MAX_LOT (exprimés en lots). Crypto : limites de l'exchange (contrats), la taille
    restant plafonnée par le budget de risque et les règles de marge.
    """
    if specs.market != "forex":
        return specs.volume_min, specs.volume_max
    return max(specs.volume_min, settings.risk.min_lot), min(specs.volume_max, settings.risk.max_lot)


def _notional(volume: float, price: float, specs: SymbolSpecs) -> float:
    """Valeur de la position en devise de cotation (volume x taille de contrat x prix)."""
    return volume * specs.contract_size * price


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
    Volume (lots / contrats) tel que la perte au Stop Loss respecte à la fois le risque par
    position (RISK_PER_TRADE_PCT) et le risque cumulé restant (RISK_MAX_TOTAL_RISK_PCT moins
    les positions ouvertes), arrondi VERS LE BAS au pas du broker (voir risk/calculator.py).

    Returns:
        Le volume, ou 0.0 = ne pas trader (prix invalides, compte / symbole indisponible,
        volume minimal qui dépasserait le budget, position ouverte sans SL...).
    """
    broker = broker or LIVE_BROKER
    try:
        if not _valid_price(entry_price) or not _valid_price(sl_price):
            log.error("[%s] Prix invalides (entrée %r, SL %r) : volume 0", symbol, entry_price, sl_price)
            return 0.0
        result = size_position(symbol, entry_price, sl_price, broker)
        if result.volume <= 0:
            log.warning("[%s] Pas de trade : %s", symbol, result.reason or "volume nul")
            return 0.0
        log.info("[%s] Volume %g (brut %.8g, limité par le %s) | risque %.2f (%.2f%% de %.2f) | "
                 "risque ouvert %.2f, budget cumulé restant %.2f",
                 symbol, result.volume, result.raw_volume, result.limited_by, result.risk_amount, result.risk_pct,
                 result.capital, result.open_risk, result.cumulative_budget_left)
        return result.volume
    except Exception:  # noqa: BLE001 - MT5ConnectionError, données broker inattendues...
        log.exception("[%s] Erreur lors du calcul de la taille de position : volume 0", symbol)
        return 0.0


# =============================================================================
#  Contrôles avant ordre (chacun retourne la liste des violations)
# =============================================================================

def _check_spread(tick: Tick, specs: SymbolSpecs) -> list[str]:
    """Forex : spread en points. Crypto : en % du prix (un « point » BTC n'a pas le sens d'un pip)."""
    if specs.market != "forex":
        if tick.spread_pct > settings.risk.max_spread_pct:
            return [f"spread {tick.spread_pct:.3f}% > maximum {settings.risk.max_spread_pct}%"]
        return []
    if tick.spread_points > settings.risk.max_spread_points:
        return [f"spread {tick.spread_points} pts > maximum {settings.risk.max_spread_points} pts"]
    return []


def _check_spread_vs_stop(tick: Tick, entry: float, sl: float) -> list[str]:
    """Spread rapporté à la distance entrée - stop : au-delà de RISK_MAX_SPREAD_SL_PCT, le coût d'entrée est trop lourd."""
    distance = abs(entry - sl)
    if distance <= 0:
        return []
    ratio = (tick.ask - tick.bid) / distance * 100
    if ratio > settings.risk.max_spread_sl_pct:
        return [f"spread = {ratio:.1f}% de la distance entrée-stop (maximum {settings.risk.max_spread_sl_pct:g}%)"]
    return []


# Motifs du diagnostic déjà couverts par les autres contrôles (pas de doublon dans le motif)
_DIAGNOSTIC_DUPLICATES = {"spread_max", "spread_sl"}


def _check_diagnostic(symbol: str, broker: Broker) -> list[str]:
    """Verdict de tradabilité du broker (tick périmé, spread anormal vs sa médiane...), s'il le fournit."""
    diagnose = getattr(broker, "diagnose_symbol", None)
    diagnostic = diagnose(symbol) if diagnose else None
    if diagnostic is None:
        return []
    return [f.message for f in diagnostic.failures if f.code not in _DIAGNOSTIC_DUPLICATES]


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


def daily_loss_pct(account: AccountInfo, deals: list[ClosedDeal]) -> float | None:
    """
    Perte du jour en % du solde de début de journée (réalisée depuis 00:00 UTC + latente,
    tout le compte) ; positive = perte. None si le solde de début de journée est nul.
    """
    realized = sum(d.net_profit for d in deals)
    start_balance = account.balance - realized
    if start_balance <= 0:
        return None
    return -(realized + account.profit) / start_balance * 100


def daily_stop_reached(broker: Broker | None = None) -> str | None:
    """Motif si la perte journalière maximale (RISK_MAX_DAILY_LOSS_PCT) est atteinte, sinon None."""
    broker = broker or LIVE_BROKER
    account = broker.get_account_info()
    if account is None:
        return None
    start_of_day = broker.now().replace(hour=0, minute=0, second=0, microsecond=0)
    loss = daily_loss_pct(account, broker.get_closed_deals(start_of_day))
    if loss is not None and loss >= settings.risk.max_daily_loss_pct:
        return f"perte journalière {loss:.2f}% >= limite {settings.risk.max_daily_loss_pct}%"
    return None


def consecutive_losses(deals: list[ClosedDeal]) -> int:
    """Pertes consécutives les plus récentes du bot (numéro magique) parmi `deals`."""
    streak = 0
    for deal in reversed([d for d in deals if d.magic == settings.trading.magic_number]):
        if deal.net_profit >= 0:
            break
        streak += 1
    return streak


def _check_daily_limits(account: AccountInfo, broker: Broker) -> list[str]:
    """
    Perte journalière (réalisée depuis 00:00 UTC + latente, tout le compte) et
    série de pertes consécutives du bot sur la journée.
    """
    start_of_day = broker.now().replace(hour=0, minute=0, second=0, microsecond=0)
    deals = broker.get_closed_deals(start_of_day)
    failures = []

    loss = daily_loss_pct(account, deals)
    if loss is None:
        return ["solde de début de journée nul ou négatif"]
    if loss >= settings.risk.max_daily_loss_pct:
        failures.append(f"perte journalière {loss:.2f}% >= limite {settings.risk.max_daily_loss_pct}%")

    streak = consecutive_losses(deals)
    if streak >= settings.risk.max_consecutive_losses:
        failures.append(f"{streak} pertes consécutives aujourd'hui (maximum {settings.risk.max_consecutive_losses})")
    return failures


def _check_volume(volume: float, specs: SymbolSpecs, entry: float) -> list[str]:
    min_volume, max_volume = _volume_bounds(specs)
    failures = []
    if specs.min_notional and _notional(volume, entry, specs) < specs.min_notional:
        failures.append(f"valeur de l'ordre {_notional(volume, entry, specs):.2f} < minimum {specs.min_notional} "
                        "exigé par l'exchange")
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
        if reward_risk < MIN_REWARD_RISK * REWARD_RISK_TOLERANCE:
            failures.append(f"ratio rendement/risque {reward_risk:.2f} < minimum {MIN_REWARD_RISK}")
    return failures


def _check_risk_and_margin(
    symbol: str, side: str, volume: float, entry: float, sl: float,
    account: AccountInfo, specs: SymbolSpecs, broker: Broker,
) -> tuple[list[str], float | None, float | None]:
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
    return failures, potential_loss, margin


def _position_side(position: Any) -> str:
    """BUY / SELL pour une position MT5 (`type` 0 = achat) ou simulée (`side`)."""
    side = getattr(position, "side", None)
    if side in ("BUY", "SELL"):
        return side
    return "BUY" if getattr(position, "type", None) == POSITION_TYPE_BUY else "SELL"


def open_positions_risk(broker: Broker) -> tuple[float, list[str]]:
    """
    Perte totale (devise du compte) si les SL de toutes les positions du bot étaient touchés.
    Une position sans SL rend le risque non borné : elle est signalée dans la liste retournée.
    Une position dont le SL est déjà au-delà du prix d'entrée (gain verrouillé) compte pour 0.
    """
    total, unbounded = 0.0, []
    for position in broker.get_open_positions():
        sl = getattr(position, "sl", None)
        entry = getattr(position, "price_open", None) or getattr(position, "entry_price", None)
        label = f"{getattr(position, 'symbol', '?')} #{getattr(position, 'ticket', '?')}"
        if not _valid_price(sl) or not _valid_price(entry):
            unbounded.append(label)
            continue
        profit = broker.calc_profit(_position_side(position), position.symbol, position.volume, entry, sl)
        if profit is None:
            unbounded.append(label)
        elif profit < 0:
            total += -profit
    return total, unbounded


def currency_exposure(symbol: str, side: str) -> dict[str, int]:
    """
    Sens de l'exposition par devise : EURUSD achat -> {EUR: +1, USD: -1} ; XAUUSD achat -> {XAU: +1, USD: -1}.
    Symbole non décomposable : exposition propre au symbole.
    """
    from sentiment.sentiment_analyzer import split_symbol

    sign = 1 if side == "BUY" else -1
    base, quote = split_symbol(symbol)
    if base is None:
        return {symbol.upper(): sign}
    return {base: sign, **({quote: -sign} if quote else {})}


def _exposure_label(currency: str, sign: int) -> str:
    return f"{currency} {'acheteur' if sign > 0 else 'vendeur'}"


def currency_risk(broker: Broker) -> dict[tuple[str, int], float]:
    """Perte au SL des positions du bot, cumulée par (devise, sens) : mesure simple de la corrélation."""
    totals: dict[tuple[str, int], float] = {}
    for position in broker.get_open_positions():
        sl = getattr(position, "sl", None)
        entry = getattr(position, "price_open", None) or getattr(position, "entry_price", None)
        if not _valid_price(sl) or not _valid_price(entry):
            continue                                       # signalé comme risque non borné par ailleurs
        side = _position_side(position)
        profit = broker.calc_profit(side, position.symbol, position.volume, entry, sl)
        if profit is None or profit >= 0:
            continue
        for currency, sign in currency_exposure(position.symbol, side).items():
            totals[(currency, sign)] = totals.get((currency, sign), 0.0) - profit
    return totals


def correlated_budget(symbol: str, side: str, capital: float, broker: Broker) -> tuple[float, str]:
    """Risque encore disponible pour ce trade sous RISK_MAX_CURRENCY_RISK_PCT, et l'exposition qui le limite."""
    limit = capital * settings.risk.max_currency_risk_pct / 100
    open_risk = currency_risk(broker)
    left, label = math.inf, ""
    for currency, sign in currency_exposure(symbol, side).items():
        room = max(0.0, limit - open_risk.get((currency, sign), 0.0))
        if room < left:
            left, label = room, _exposure_label(currency, sign)
    return left, label


def _check_correlation(symbol: str, side: str, potential_loss: float | None, capital: float,
                       broker: Broker) -> list[str]:
    if potential_loss is None or capital <= 0:
        return []
    limit = settings.risk.max_currency_risk_pct
    open_risk = currency_risk(broker)
    failures = []
    for currency, sign in currency_exposure(symbol, side).items():
        total_pct = (open_risk.get((currency, sign), 0.0) + potential_loss) / capital * 100
        if total_pct > limit * RISK_TOLERANCE:
            failures.append(f"exposition corrélée {_exposure_label(currency, sign)} {total_pct:.2f}% > limite {limit}% "
                            f"(positions dans le même sens + ce trade)")
    return failures


def _check_portfolio(
    potential_loss: float | None, margin: float | None, account: AccountInfo, broker: Broker,
) -> tuple[list[str], float | None]:
    """
    Règles globales, indispensables avec plusieurs positions simultanées :
      - risque cumulé (tous les SL touchés, nouveau trade compris) <= RISK_MAX_TOTAL_RISK_PCT
      - marge totale après le trade <= RISK_MAX_MARGIN_USAGE_PCT des fonds propres
    Retourne les violations et le risque cumulé en % du capital.
    """
    failures = []
    capital = _risk_capital(account)
    open_risk, unbounded = open_positions_risk(broker)
    if unbounded:
        failures.append(f"risque global non borné : position(s) sans SL ({', '.join(unbounded)})")

    total_risk_pct = None
    if potential_loss is not None and capital > 0:
        total_risk_pct = (open_risk + potential_loss) / capital * 100
        limit = settings.risk.max_total_risk_pct
        if total_risk_pct > limit * RISK_TOLERANCE:
            failures.append(f"risque cumulé {total_risk_pct:.2f}% > limite {limit}% "
                            f"(positions ouvertes {open_risk / capital * 100:.2f}% + ce trade)")

    if margin is not None and account.equity > 0:
        usage_pct = (account.margin + margin) / account.equity * 100
        if usage_pct > settings.risk.max_margin_usage_pct:
            failures.append(f"marge totale {usage_pct:.1f}% des fonds propres après ce trade "
                            f"> limite {settings.risk.max_margin_usage_pct}%")
    return failures, total_risk_pct


# =============================================================================
#  Validation avant ordre
# =============================================================================

def _verdict(
    symbol: str, action: str, volume: Any, sl: Any, tp: Any, failures: list[str],
    entry: float | None = None, potential_loss: float | None = None, capital: float | None = None,
    total_risk_pct: float | None = None,
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
        "portfolio_risk_pct": round(total_risk_pct, 3) if total_risk_pct is not None else None,
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

    `broker` : source des données (MT5 réel par défaut, `BaseBroker` du moteur pour un
    exchange crypto, broker simulé en backtest).
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
            _check_spread(tick, specs)
            + _check_spread_vs_stop(tick, entry, sl)
            + _check_diagnostic(symbol, broker)
            + _check_exposure(symbol, broker)
            + _check_daily_limits(account, broker)
            + _check_volume(volume, specs, entry)
            + _check_stops(side, tick, entry, sl, tp, specs)
        )
        risk_failures, potential_loss, margin = _check_risk_and_margin(
            symbol, side, volume, entry, sl, account, specs, broker)
        portfolio_failures, total_risk_pct = _check_portfolio(potential_loss, margin, account, broker)
        failures += risk_failures + portfolio_failures + _check_correlation(symbol, side, potential_loss, capital,
                                                                            broker)
        return _verdict(symbol, side, volume, sl, tp, failures, entry, potential_loss, capital, total_risk_pct)
    except Exception as exc:  # noqa: BLE001 - MT5ConnectionError, données broker inattendues...
        log.exception("[%s] Erreur pendant la validation du risque", symbol)
        return _verdict(symbol, side or str(action), volume, sl, tp, [f"erreur interne : {exc}"])
