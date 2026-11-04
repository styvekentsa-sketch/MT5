"""
Gardien des positions ouvertes du bot, appliqué à chaque tour quel que soit le profil :

  1. Sorties partielles : EXIT_TP1_FRACTION du volume initial fermée à EXIT_TP1_R, puis
     EXIT_TP2_FRACTION à EXIT_TP2_R ; le reste vise le TP de l'ordre (TP3). Après le TP1, le stop
     revient au prix d'entrée plus le spread (EXIT_BREAKEVEN_AFTER_TP1). Ce passage au prix d'entrée
     ne garantit pas une sortie sans perte : frais, glissement et gaps restent possibles.
  2. Mode Bouclier, avant une annonce forte sur une devise de la position (fenêtre de sécurité,
     avant publication) : SHIELD_ACTION = breakeven (stop au prix d'entrée, seulement si la
     position est assez en gain : le stop n'est jamais resserré à l'aveugle), reduce ou close.
  3. Après publication : une position dont le scénario est invalidé (effet de force >=
     SHIELD_CONTRARY_STRENGTH à contre-sens, fiche encore valide) est fermée (SHIELD_CLOSE_CONTRARY).

L'étape d'une position (avant TP1, après TP1, après TP2) se déduit de son volume restant comparé
au volume initial : elle survit donc à un redémarrage. Le volume initial et le stop d'origine
viennent de l'ordre passé par le moteur, sinon du journal des trades, sinon de la position elle-même.
Les bougies ne sont pas relues : le prix courant de la position suffit.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from brokers.base import BaseBroker
from config.settings import settings
from core.news_pipeline import NewsPipeline
from utils.logger import get_logger

log = get_logger(__name__)

VOLUME_EPSILON = 1e-9


@dataclass(frozen=True)
class ExitPlan:
    entry: float
    stop: float                      # stop d'origine : définit 1 R
    volume: float                    # volume initial

    @property
    def risk(self) -> float:
        return abs(self.entry - self.stop)


@dataclass(frozen=True)
class GuardAction:
    symbol: str
    ticket: int
    kind: str                        # tp1, tp2, breakeven, shield_breakeven, shield_reduce, shield_close, invalidated
    description: str
    success: bool


def exit_levels(side: str, entry: float, stop: float, take_profit: float | None) -> dict[str, float | None]:
    """TP1 / TP2 configurés (None s'ils sont désactivés) et TP3 = TP de l'ordre."""
    cfg = settings.exits
    direction = 1 if side == "BUY" else -1
    risk = abs(entry - stop)
    return {"tp1": entry + direction * cfg.tp1_r * risk if cfg.partial_exits else None,
            "tp2": entry + direction * cfg.tp2_r * risk if cfg.tp2_fraction else None,
            "tp3": take_profit}


def _side(position: Any) -> str:
    return position.side if getattr(position, "side", None) in ("BUY", "SELL") else (
        "BUY" if getattr(position, "type", 0) == 0 else "SELL")


class PositionGuard:
    def __init__(self, broker: BaseBroker, memory=None) -> None:
        self.broker = broker
        self.memory = memory
        self.plans: dict[int, ExitPlan] = {}
        self._shielded: set[tuple[int, str]] = set()      # (ticket, annonce) : une seule action par annonce

    # ------------------------------------------------------------------ plan de sortie

    def register(self, ticket: int, entry: float, stop: float, volume: float) -> None:
        if ticket and entry and stop and entry != stop and volume > 0:
            self.plans[ticket] = ExitPlan(entry, stop, volume)

    def _plan(self, position: Any) -> ExitPlan | None:
        plan = self.plans.get(position.ticket)
        if plan is None and self.memory is not None:
            try:
                entry = next((t for t in reversed(self.memory.trades.load()) if t.get("ticket") == position.ticket), None)
            except Exception:  # noqa: BLE001 - journal illisible : repli sur la position
                entry = None
            if entry and entry.get("sl") and entry.get("volume"):
                plan = ExitPlan(float(entry.get("entry_price") or position.price_open), float(entry["sl"]),
                                float(entry["volume"]))
        if plan is None:
            direction = 1 if _side(position) == "BUY" else -1
            if position.sl and direction * (position.price_open - position.sl) > 0:   # stop encore côté perte
                opening = next((d for d in self.broker.get_position_deals(position.ticket) if d.entry == "in"), None)
                volume = opening.volume if opening is not None and opening.volume else position.volume
                plan = ExitPlan(position.price_open, position.sl, max(volume, position.volume))
        if plan is not None:
            self.plans[position.ticket] = plan
        return plan

    def _stage(self, plan: ExitPlan, volume: float) -> int:
        cfg = settings.exits
        remaining = volume / plan.volume
        if remaining > 1 - cfg.tp1_fraction + 0.5 * cfg.tp1_fraction:          # TP1 pas encore pris
            return 0
        if cfg.tp2_fraction and remaining > 1 - cfg.tp1_fraction - 0.5 * cfg.tp2_fraction:
            return 1
        return 2

    # ------------------------------------------------------------------ actions

    def _breakeven_stop(self, position: Any, plan: ExitPlan) -> float | None:
        """Prix d'entrée + spread (coût de sortie) ; None si le prix courant ne le permet pas."""
        tick = self.broker.get_current_tick(position.symbol)
        spread = tick.spread if tick else 0.0
        direction = 1 if _side(position) == "BUY" else -1
        stop = plan.entry + direction * spread
        already = position.sl and direction * (position.sl - stop) >= -VOLUME_EPSILON
        if already or direction * (position.price_current - stop) <= spread:
            return None
        return stop

    def _partial(self, position: Any, fraction: float, plan: ExitPlan, kind: str, why: str) -> GuardAction:
        volume = round(plan.volume * fraction, 8)
        result = self.broker.close_order(position.ticket, comment=f"Bot {kind.upper()}", volume=volume)
        text = (f"{why} : {result.volume:g} / {position.volume:g} fermé(s) sur #{position.ticket}" if result.success
                else f"{why} : clôture partielle refusée ({result.message})")
        return GuardAction(position.symbol, position.ticket, kind, text, result.success)

    def _move_stop(self, position: Any, stop: float, kind: str, why: str) -> GuardAction:
        result = self.broker.modify_stops(position.ticket, stop, None)
        text = (f"{why} : stop déplacé à {stop:.6g} sur #{position.ticket}" if result.success
                else f"{why} : déplacement du stop refusé ({result.message})")
        return GuardAction(position.symbol, position.ticket, kind, text, result.success)

    def _close(self, position: Any, kind: str, why: str) -> GuardAction:
        result = self.broker.close_order(position.ticket, comment=f"Bot {kind.upper()}")
        text = f"{why} : position #{position.ticket} fermée" if result.success else f"{why} : clôture refusée ({result.message})"
        return GuardAction(position.symbol, position.ticket, kind, text, result.success)

    def _exits(self, position: Any) -> list[GuardAction]:
        cfg = settings.exits
        plan = self._plan(position)
        if plan is None or plan.risk <= 0:
            return []
        direction = 1 if _side(position) == "BUY" else -1
        r = direction * (position.price_current - plan.entry) / plan.risk
        stage = self._stage(plan, position.volume)
        if cfg.partial_exits and stage == 0 and r >= cfg.tp1_r:
            action = self._partial(position, cfg.tp1_fraction, plan, "tp1", f"TP1 atteint ({r:.2f} R)")
            return [action] + ([self._after_tp1(position, plan)] if action.success and cfg.breakeven_after_tp1 else [])
        if cfg.tp2_fraction and stage == 1 and r >= cfg.tp2_r:
            return [self._partial(position, cfg.tp2_fraction, plan, "tp2", f"TP2 atteint ({r:.2f} R)")]
        if cfg.partial_exits and cfg.breakeven_after_tp1 and stage >= 1:
            stop = self._breakeven_stop(position, plan)        # TP1 pris avant un redémarrage ou un refus
            if stop is not None:
                return [self._move_stop(position, stop, "breakeven", "après TP1")]
        return []

    def _after_tp1(self, position: Any, plan: ExitPlan) -> GuardAction:
        stop = self._breakeven_stop(position, plan)
        if stop is None:
            return GuardAction(position.symbol, position.ticket, "breakeven",
                               "après TP1 : stop déjà protégé ou prix trop proche de l'entrée", True)
        return self._move_stop(position, stop, "breakeven", "après TP1")

    # ------------------------------------------------------------------ Bouclier

    def _shield(self, position: Any, pipeline: NewsPipeline, now: datetime) -> list[GuardAction]:
        cfg = settings.exits
        actions = []
        block = pipeline.safety_check(position.symbol, now) if cfg.shield_action != "none" else None
        upcoming = [e for e in (block.events if block else []) if e.time > now and e.actual is None]
        for event in upcoming[:1]:
            key = (position.ticket, event.key)
            if key in self._shielded:
                continue
            self._shielded.add(key)
            why = f"Bouclier avant {event.title} ({event.currency}, {event.time:%H:%M} UTC)"
            if cfg.shield_action == "close":
                actions.append(self._close(position, "shield_close", why))
            elif cfg.shield_action == "reduce":
                plan = ExitPlan(position.price_open, position.sl or position.price_open, position.volume)
                actions.append(self._partial(position, cfg.shield_reduce_fraction, plan, "shield_reduce", why))
            else:
                plan = self._plan(position)
                stop = self._breakeven_stop(position, plan) if plan else None
                actions.append(self._move_stop(position, stop, "shield_breakeven", why) if stop is not None else
                               GuardAction(position.symbol, position.ticket, "shield_breakeven",
                                           f"{why} : position pas assez en gain, stop d'origine conservé", True))
        if cfg.shield_close_contrary:
            for card in pipeline.cards(now=now, hours_before=1, hours_after=0):
                effect = card.effects.get(position.symbol)
                key = (position.ticket, f"contraire|{card.fact}")
                if (effect is None or key in self._shielded or card.surprise is None
                        or (card.valid_until and now > card.valid_until)
                        or effect.strength < cfg.shield_contrary_strength):
                    continue
                if (effect.direction == "haussier") == (_side(position) == "BUY") or effect.direction == "neutre":
                    continue
                self._shielded.add(key)
                actions.append(self._close(position, "invalidated",
                                           f"Scénario invalidé par {card.fact} (effet {effect.direction}, "
                                           f"force {effect.strength:.2f})"))
        return actions

    # ------------------------------------------------------------------ tour

    def run(self, pipeline: NewsPipeline | None = None, now: datetime | None = None) -> list[GuardAction]:
        now = now or datetime.now(timezone.utc)
        actions: list[GuardAction] = []
        positions = self.broker.get_positions(bot_only=True)
        for position in positions:
            try:
                shield = self._shield(position, pipeline, now) if pipeline is not None else []
                actions += shield
                if not any(a.success and a.kind in ("shield_close", "invalidated") for a in shield):
                    actions += self._exits(position)
            except Exception:  # noqa: BLE001 - une position en erreur n'empêche pas les autres
                log.exception("[%s] Gestion de la position #%s impossible", position.symbol, position.ticket)
        live = {p.ticket for p in positions}
        for ticket in [t for t in self.plans if t not in live]:
            del self.plans[ticket]
        for action in actions:
            (log.warning if action.success else log.error)("[%s] %s", action.symbol, action.description)
        return actions
