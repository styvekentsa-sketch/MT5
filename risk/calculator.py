"""
Calculateur de taille de position, conscient du contrat (taille, volume minimal / maximal / pas).

    volume = arrondi_bas_au_pas( min(budget par position, budget cumulé restant) / perte pour 1 lot )

  - budget par position   RISK_PER_TRADE_PCT (1 %) du capital
  - budget cumulé restant RISK_MAX_TOTAL_RISK_PCT (2 %) du capital - perte au SL des positions ouvertes
  - perte pour 1 lot      calcul du broker (`calc_profit` : taille du contrat et conversions de devises),
                          à défaut distance / taille du tick x valeur du tick, à défaut distance x taille du contrat
  - capital               min(solde, fonds propres) : le plus prudent des deux
  - arrondi               TOUJOURS vers le bas au pas de volume (Decimal, sans erreur de flottant)
  - plafond               min(volume max du broker, RISK_MAX_LOT en Forex)
  - rejet (volume 0)      si le volume minimal ferait dépasser le budget, si une position ouverte n'a
                          pas de SL (risque cumulé non borné), ou si l'ordre est sous la valeur minimale

Formules de risque d'un setup (partagées par les stratégies et le vérificateur indépendant) :
  marge de protection b = max(0.10 ATR, 2 spreads, 1 tick) ; ratio net = (gain - coût) / (perte + coût).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal

from brokers.models import SymbolSpecs, format_volume
from config.settings import settings
from models.schemas import Side
from utils.logger import get_logger

log = get_logger(__name__)

VOLUME_DECIMALS = 8                  # absorbe les erreurs d'arrondi flottant avant l'arrondi au pas
VOLUME_EPSILON = 1e-9
MARGIN_ATR = 0.10
MARGIN_SPREADS = 2.0


# =============================================================================
#  Formules
# =============================================================================

def floor_to_step(volume: float, step: float) -> float:
    """
    Arrondi VERS LE BAS au pas de volume, en Decimal. Le volume est d'abord arrondi à
    VOLUME_DECIMALS : sans cela, 100 / 200.00000000000017 (= 0.4999999999999996) tomberait
    à 0.49 au lieu de 0.50.
    """
    d_step = Decimal(str(step))
    d_volume = Decimal(str(round(volume, VOLUME_DECIMALS)))
    return float((d_volume / d_step).to_integral_value(rounding=ROUND_FLOOR) * d_step)


def volume_bounds(specs: SymbolSpecs) -> tuple[float, float]:
    """(volume minimal, volume maximal) : broker, et RISK_MIN_LOT / RISK_MAX_LOT en Forex (lots)."""
    if specs.market != "forex":
        return specs.volume_min, specs.volume_max
    return max(specs.volume_min, settings.risk.min_lot), min(specs.volume_max, settings.risk.max_lot)


def protection_margin(atr: float, spread: float, tick_size: float) -> float:
    """b = max(0.10 x ATR, 2 x spread, 1 tick)."""
    return max(MARGIN_ATR * atr, MARGIN_SPREADS * spread, tick_size)


def net_reward_risk(side: Side, entry: float, stop_loss: float, take_profit: float, cost: float) -> float:
    """Ratio net estimé : (gain au TP - coût) / (perte au SL + coût), sur des distances orientées."""
    sign = 1 if side == Side.BUY else -1
    gain = (take_profit - entry) * sign - cost
    loss = (entry - stop_loss) * sign + cost
    return gain / loss if loss > 0 else 0.0


def target_for_ratio(side: Side, entry: float, stop_loss: float, ratio: float, cost: float) -> float:
    """TP donnant exactement le ratio net `ratio` : entrée ± (ratio x (risque + coût) + coût)."""
    sign = 1 if side == Side.BUY else -1
    risk = (entry - stop_loss) * sign
    return entry + sign * (ratio * (risk + cost) + cost)


# =============================================================================
#  Calcul de la taille
# =============================================================================

@dataclass(frozen=True)
class SizingResult:
    volume: float                    # 0.0 = ne pas trader
    raw_volume: float = 0.0          # volume théorique avant arrondi et bornes
    loss_per_lot: float = 0.0        # perte au SL pour 1 lot / contrat, devise du compte
    capital: float = 0.0
    per_trade_budget: float = 0.0
    cumulative_budget_left: float = 0.0
    open_risk: float = 0.0           # perte au SL des positions déjà ouvertes
    limited_by: str = ""             # risque par position, risque cumulé, volume maximal
    reason: str = ""                 # motif du rejet (volume 0)

    @property
    def risk_amount(self) -> float:
        return round(self.volume * self.loss_per_lot, 2)

    @property
    def risk_pct(self) -> float:
        return round(self.risk_amount / self.capital * 100, 4) if self.capital > 0 else 0.0


def loss_per_lot(broker, symbol: str, side: str, entry: float, sl: float, specs: SymbolSpecs) -> float | None:
    """Perte (devise du compte) pour 1 lot si le SL est touché."""
    profit = broker.calc_profit(side, symbol, 1.0, entry, sl)
    if profit is not None and profit < 0:
        return -profit
    if specs.tick_size > 0 and specs.tick_value > 0:
        return abs(entry - sl) / specs.tick_size * specs.tick_value
    if specs.contract_size > 0:                       # cotation dans la devise du compte
        return abs(entry - sl) * specs.contract_size
    return None


def size_position(symbol: str, entry: float, sl: float, broker) -> SizingResult:
    """Volume maximal respectant le risque par position ET le risque cumulé, arrondi vers le bas au pas."""
    from risk.risk_manager import correlated_budget, open_positions_risk   # import local : risk_manager délègue ici

    if entry <= 0 or sl <= 0 or entry == sl:
        return SizingResult(0.0, reason="prix d'entrée ou stop invalides")
    side = "BUY" if sl < entry else "SELL"
    account = broker.get_account_info()
    if account is None:
        return SizingResult(0.0, reason="compte indisponible")
    capital = min(account.balance, account.equity)
    if capital <= 0:
        return SizingResult(0.0, reason="capital nul")
    specs = broker.get_symbol_specs(symbol)
    if specs is None:
        return SizingResult(0.0, capital=capital, reason="spécifications du symbole indisponibles")
    per_lot = loss_per_lot(broker, symbol, side, entry, sl, specs)
    if not per_lot or per_lot <= 0:
        return SizingResult(0.0, capital=capital, reason="perte par lot incalculable")

    per_trade = capital * settings.risk.risk_per_trade_pct / 100
    open_risk, unbounded = open_positions_risk(broker)
    left = max(0.0, capital * settings.risk.max_total_risk_pct / 100 - open_risk)
    base = dict(loss_per_lot=per_lot, capital=capital, per_trade_budget=per_trade, cumulative_budget_left=left,
                open_risk=open_risk)
    if unbounded:
        return SizingResult(0.0, reason=f"position(s) sans SL : risque cumulé non borné ({', '.join(unbounded)})", **base)

    budget, limited_by = (per_trade, "risque par position") if per_trade <= left else (left, "risque cumulé")
    correlated, exposure = correlated_budget(symbol, side, capital, broker)
    if correlated < budget:
        budget, limited_by = correlated, f"exposition corrélée ({exposure})"
    raw = budget / per_lot
    min_volume, max_volume = volume_bounds(specs)
    if raw > max_volume:
        limited_by = "volume maximal"
    volume = floor_to_step(min(raw, max_volume), specs.volume_step)
    if volume < min_volume - VOLUME_EPSILON:
        return SizingResult(0.0, raw, reason=f"volume minimal {format_volume(min_volume, specs.volume_step)} "
                                             f"{specs.volume_unit} : perte {per_lot * min_volume:.2f} > budget "
                                             f"{budget:.2f} ({limited_by})", limited_by=limited_by, **base)
    if specs.min_notional and volume * specs.contract_size * entry < specs.min_notional:
        return SizingResult(0.0, raw, reason=f"valeur de l'ordre < minimum {specs.min_notional} de l'exchange",
                            limited_by=limited_by, **base)
    return SizingResult(volume, raw, limited_by=limited_by, **base)
