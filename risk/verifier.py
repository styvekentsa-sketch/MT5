"""
Vérificateur indépendant (« juge de paix ») : dernier contrôle bloquant avant tout ordre ou
toute validation de setup. Il recalcule lui-même ses règles à partir des données brutes du
broker, sans réutiliser le calcul de taille ni les autres contrôles de risque : une erreur
dans ces modules ne peut pas le contourner.

Règles (toutes évaluées, motif complet) :
  1. Ratio net estimé (gain - coût) / (perte + coût) >= RISK_MIN_NET_REWARD_RISK (3), coût = spread ;
     un ordre sans TP est refusé (ratio incalculable)
  2. Spread actuel :
       - <= RISK_MAX_SPREAD_SL_PCT (10 %) de la distance entrée - stop
       - <= RISK_MAX_SPREAD_MEDIAN_RATIO (2) x sa médiane des 60 dernières minutes (si le broker la fournit)
       - <= plafond de l'actif : RISK_MAX_SPREAD_POINTS (30 points, ex. l'or) ; RISK_MAX_SPREAD_PCT en crypto
     ainsi que les refus du diagnostic du broker (prix figé, marché fermé, symbole désactivé)
  3. Limites journalières : perte du jour < RISK_MAX_DAILY_LOSS_PCT (3 %) et moins de
     RISK_MAX_CONSECUTIVE_LOSSES (2) pertes consécutives du bot aujourd'hui (UTC)

Ne lève jamais : une donnée manquante ou une erreur donne un refus.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from config.settings import settings
from models.schemas import Side
from risk.calculator import net_reward_risk
from risk.risk_manager import daily_loss_pct
from utils.logger import get_logger

log = get_logger(__name__)

RATIO_TOLERANCE = 1e-6

# Refus du diagnostic broker repris par le vérificateur (les règles de spread sont recalculées ici)
DIAGNOSTIC_CODES = {"unknown", "disabled", "close_only", "no_price", "stale_tick", "unavailable"}


@dataclass(frozen=True)
class Verdict:
    approved: bool
    failures: tuple[str, ...] = ()
    net_reward_risk: float | None = None
    spread_points: int | None = None
    spread_to_sl_pct: float | None = None
    median_spread_points: float | None = None
    daily_loss_pct: float | None = None
    losing_streak: int | None = None
    checks: dict[str, bool] = field(default_factory=dict)

    @property
    def reason(self) -> str:
        return "toutes les vérifications sont passées" if self.approved else "; ".join(self.failures)


def _losing_streak(deals) -> int:
    streak = 0
    for deal in reversed([d for d in deals if d.magic == settings.trading.magic_number]):
        if deal.net_profit >= 0:
            break
        streak += 1
    return streak


def verify_order(symbol: str, side: str, entry: float, sl: float, tp: float | None, broker) -> Verdict:
    """Vérification indépendante d'un ordre (ou d'un setup) ; `approved` False au moindre doute."""
    risk = settings.risk
    failures: list[str] = []
    checks: dict[str, bool] = {}
    values: dict = {}
    try:
        side_enum = Side(side.upper())
        tick, specs = broker.get_current_tick(symbol), broker.get_symbol_specs(symbol)
        if tick is None or specs is None or tick.ask < tick.bid:
            return Verdict(False, ("prix ou spécifications du symbole indisponibles",))
        spread = tick.ask - tick.bid
        values["spread_points"] = tick.spread_points

        # 1. Ratio net
        if tp is None:
            checks["ratio_net"] = False
            failures.append("objectif (TP) absent : ratio net incalculable")
        else:
            ratio = net_reward_risk(side_enum, entry, sl, tp, cost=spread)
            values["net_reward_risk"] = round(ratio, 3)
            # tolérance : un TP calculé exactement au ratio minimal donne 2.4999999 en virgule flottante
            checks["ratio_net"] = ratio >= risk.min_net_reward_risk - RATIO_TOLERANCE
            if not checks["ratio_net"]:
                failures.append(f"ratio net estimé {ratio:.2f} < {risk.min_net_reward_risk:g}")

        # 2. Spread : distance au stop, médiane 60 minutes, plafond de l'actif
        distance = abs(entry - sl)
        if distance <= 0:
            failures.append("distance entrée - stop nulle")
        else:
            pct = spread / distance * 100
            values["spread_to_sl_pct"] = round(pct, 2)
            checks["spread_stop"] = pct <= risk.max_spread_sl_pct
            if not checks["spread_stop"]:
                failures.append(f"spread = {pct:.1f}% de la distance entrée-stop (maximum {risk.max_spread_sl_pct:g}%)")
        if specs.market == "forex":
            checks["spread_plafond"] = tick.spread_points <= risk.max_spread_points
            if not checks["spread_plafond"]:
                failures.append(f"spread {tick.spread_points} pts > plafond {risk.max_spread_points} pts")
        else:
            checks["spread_plafond"] = tick.spread_pct <= risk.max_spread_pct
            if not checks["spread_plafond"]:
                failures.append(f"spread {tick.spread_pct:.3f}% > plafond {risk.max_spread_pct}%")
        diagnose = getattr(broker, "diagnose_symbol", None)
        diagnostic = diagnose(symbol) if diagnose else None
        if diagnostic is not None:
            median = diagnostic.median_spread_points_60m
            values["median_spread_points"] = median
            if median:
                checks["spread_mediane"] = tick.spread_points <= risk.max_spread_median_ratio * median
                if not checks["spread_mediane"]:
                    failures.append(f"spread {tick.spread_points} pts > {risk.max_spread_median_ratio:g} x sa médiane "
                                    f"des 60 dernières minutes ({median:g} pts)")
            failures.extend(f.message for f in diagnostic.failures if f.code in DIAGNOSTIC_CODES)

        # 3. Limites journalières
        account = broker.get_account_info()
        if account is None:
            failures.append("compte indisponible : limites journalières invérifiables")
        else:
            start = broker.now().replace(hour=0, minute=0, second=0, microsecond=0)
            deals = broker.get_closed_deals(start)
            loss = daily_loss_pct(account, deals)
            streak = _losing_streak(deals)
            values.update(daily_loss_pct=round(loss, 3) if loss is not None else None, losing_streak=streak)
            checks["perte_journaliere"] = loss is not None and loss < risk.max_daily_loss_pct
            if not checks["perte_journaliere"]:
                failures.append("perte journalière incalculable" if loss is None else
                                f"perte journalière {loss:.2f}% >= limite {risk.max_daily_loss_pct}%")
            checks["pertes_consecutives"] = streak < risk.max_consecutive_losses
            if not checks["pertes_consecutives"]:
                failures.append(f"{streak} pertes consécutives aujourd'hui (maximum {risk.max_consecutive_losses})")
    except Exception as exc:  # noqa: BLE001 - au moindre doute, refus
        log.exception("[%s] Vérificateur indépendant en erreur", symbol)
        return Verdict(False, (f"erreur du vérificateur : {exc}",))

    verdict = Verdict(not failures, tuple(failures), checks=checks, **values)
    if not verdict.approved:
        log.warning("[%s] Vérificateur indépendant : ordre %s BLOQUÉ (%s)", symbol, side, verdict.reason)
    return verdict
