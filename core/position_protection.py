"""
Protection automatique de chaque position ouverte par le robot (réglages PROTECT_*) :

  1. Risque initial R : |prix réel d'entrée - stop initial|, mémorisé définitivement à la première
     observation de la position (stop envoyé avec l'ordre, sinon celui du journal, sinon celui de la
     position). Une position sans stop est reprotégée au stop initial.
  2. Gain latent >= PROTECT_BREAKEVEN_R (1 R) : stop au prix d'entrée + marge de frais
     (PROTECT_COMMISSION_PER_LOT convertie en prix + PROTECT_BREAKEVEN_MARGIN_POINTS).
  3. Gain latent >= PROTECT_TRAILING_START_R (1,5 R) : stop suiveur à PROTECT_TRAILING_DISTANCE_R (1 R)
     du meilleur prix atteint depuis l'ouverture (BUY : meilleur Bid - 1 R ; SELL : meilleur Ask + 1 R).
  4. Le stop ne va que dans le sens qui protège : il ne recule jamais quand le marché revient.
     Calcul : risk/trailing.next_stop, la même règle que le backtest.
  5. Modification seulement si le stop progresse d'au moins PROTECT_MIN_STEP_R (0,1 R). Le stop est
     arrondi au tick du côté prudent et ramené à la distance minimale du courtier (stops level) ;
     aucune modification dans la zone de gel (freeze level) ni sur un symbole non négociable.
  6. TP : celui de l'ordre n'est jamais modifié ; une position qui n'en a pas reçoit
     PROTECT_TAKE_PROFIT_R (3 R). Le stop suiveur peut clôturer la position avant cet objectif.
  7. Une position = son ticket + le magic du robot. Chaque modification acceptée est relue chez le
     courtier ; un refus est journalisé et retenté après PROTECT_RETRY_SECONDS.
  8. R, stop initial et meilleur prix sont sauvegardés (position_protection.json, à côté de la base) :
     après un redémarrage la gestion reprend à l'identique, et le meilleur prix est rattrapé sur les
     bougies M1 écoulées pendant l'arrêt et entre deux tours.
"""

from __future__ import annotations

import json
import math
import os
import threading
import time
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any

from brokers.base import BaseBroker
from brokers.models import SymbolSpecs, Tick
from config.settings import settings
from risk.trailing import TrailingStop, next_stop
from utils.logger import get_logger

log = get_logger(__name__)

EPSILON = 1e-9
STEP_TOLERANCE = 1e-6                    # arrondi au tick : erreurs de virgule flottante (en nombre de ticks)
MAX_CATCH_UP_BARS = 1440                 # rattrapage du meilleur prix : 24 h de bougies M1 au plus
FORGET_AFTER_SECONDS = 86_400            # position disparue sans clôture connue : état oublié après 24 h


@dataclass
class ProtectionState:
    """État persistant d'une position protégée."""
    ticket: int
    magic: int
    symbol: str
    side: str                            # BUY / SELL
    entry: float                         # prix réel d'entrée
    initial_stop: float
    risk: float                          # R en prix, figé à l'ouverture
    best: float                          # meilleur Bid (BUY) / Ask (SELL) depuis l'ouverture
    opened: int = 0                      # ouverture (horodatage du courtier)
    stage: str = "initial"               # initial, breakeven, trailing
    bars_until: int = 0                  # dernière bougie M1 lue pour le meilleur prix
    retry_after: float = 0.0             # pas de nouvel essai avant (time.time())
    last_error: str = ""
    missing_since: float = 0.0           # position absente de la liste du courtier depuis

    @property
    def key(self) -> str:
        return state_key(self.magic, self.ticket)

    @property
    def direction(self) -> int:
        return 1 if self.side == "BUY" else -1

    def r_multiple(self, price: float) -> float:
        return self.direction * (price - self.entry) / self.risk if self.risk > 0 else 0.0


@dataclass(frozen=True)
class ProtectionAction:
    symbol: str
    ticket: int
    kind: str                            # protect_stop, protect_breakeven, protect_trailing, protect_tp, protect_error
    description: str
    success: bool


def state_key(magic: int, ticket: int) -> str:
    return f"{magic}:{ticket}"


def _side(position: Any) -> str:
    return position.side if getattr(position, "side", None) in ("BUY", "SELL") else (
        "BUY" if getattr(position, "type", 0) == 0 else "SELL")


class ProtectionStore:
    """États des positions protégées, sauvegardés en JSON (écriture atomique)."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or settings.database.path.parent / "position_protection.json"
        self._lock = threading.Lock()
        self.states: dict[str, ProtectionState] = self._load()

    def _load(self) -> dict[str, ProtectionState]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            log.error("États de protection illisibles (%s) : R recalculé depuis le journal des trades", exc)
            return {}
        known = {f.name for f in fields(ProtectionState)}
        states = {}
        for item in raw.values() if isinstance(raw, dict) else []:
            try:
                state = ProtectionState(**{k: v for k, v in item.items() if k in known})
            except TypeError as exc:
                log.warning("État de protection ignoré (%s) : %s", exc, item)
                continue
            states[state.key] = state
        return states

    def save(self) -> None:
        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.path.with_suffix(".tmp")
                tmp.write_text(json.dumps({k: asdict(s) for k, s in self.states.items()}, indent=2),
                               encoding="utf-8")
                os.replace(tmp, self.path)
            except OSError as exc:
                log.error("États de protection non enregistrés : %s", exc)


class PositionProtection:
    def __init__(self, broker: BaseBroker, store: ProtectionStore | None = None) -> None:
        self.broker = broker
        self.store = store or ProtectionStore()

    @property
    def config(self):
        return settings.protection

    # ------------------------------------------------------------------ état

    def state_for(self, position: Any, initial_stop: float | None) -> ProtectionState | None:
        """État de la position ; créé à la première observation (R figé ensuite)."""
        key = state_key(position.magic, position.ticket)
        state = self.store.states.get(key)
        if state is not None:
            state.missing_since = 0.0
            return state
        side = _side(position)
        direction = 1 if side == "BUY" else -1
        stop = initial_stop or position.sl
        if not stop or direction * (position.price_open - stop) <= 0:
            return None                  # stop initial inconnu ou déjà côté gain : R impossible à établir
        state = ProtectionState(ticket=position.ticket, magic=position.magic, symbol=position.symbol, side=side,
                                entry=position.price_open, initial_stop=stop,
                                risk=abs(position.price_open - stop), best=position.price_open,
                                opened=int(position.time or 0))
        self.store.states[key] = state
        log.info("[%s] Protection #%s : entrée %s, stop initial %s, R = %s", position.symbol, position.ticket,
                 state.entry, stop, f"{state.risk:.6g}")
        return state

    def _bar_extremes(self, state: ProtectionState, specs: SymbolSpecs | None, tick: Tick | None) -> list[float]:
        """Meilleurs prix des bougies M1 depuis la dernière lecture (hors minute d'ouverture)."""
        try:
            bars = self.broker.get_historical_data(state.symbol, count=3, timeframe="M1")
            if bars is None or bars.empty:
                return []
            since = max(state.bars_until, state.opened)
            first = int(bars.index[0].timestamp())
            if since and first > since + 60:                  # arrêt du bot ou tours espacés
                count = min(MAX_CATCH_UP_BARS, (int(bars.index[-1].timestamp()) - since) // 60 + 3)
                bars = self.broker.get_historical_data(state.symbol, count=count, timeframe="M1")
        except Exception as exc:  # noqa: BLE001 - le tick courant suffit à défaut
            log.debug("[%s] Bougies M1 indisponibles pour le meilleur prix : %s", state.symbol, exc)
            return []
        opening_minute = state.opened - state.opened % 60
        times = [int(t.timestamp()) for t in bars.index]
        keep = [t > opening_minute and t >= state.bars_until for t in times]
        recent = bars[keep]
        if times:
            state.bars_until = times[-1]                     # la bougie en cours sera relue au tour suivant
        if recent.empty:
            return []
        if state.side == "BUY":
            return [float(v) for v in recent["high"]]       # bougies MT5 : prix Bid
        point = specs.point if specs else 0.0
        tick_spread = tick.spread if tick else 0.0
        spreads = recent["spread"] if "spread" in recent else [0] * len(recent)
        return [float(low) + max(float(s) * point, tick_spread) for low, s in zip(recent["low"], spreads)]

    def _update_best(self, state: ProtectionState, tick: Tick | None, specs: SymbolSpecs | None) -> bool:
        prices = self._bar_extremes(state, specs, tick)
        if tick is not None:
            prices.append(tick.bid if state.side == "BUY" else tick.ask)
        prices = [p for p in prices if p > 0]
        if not prices:
            return False
        best = max(prices) if state.side == "BUY" else min(prices)
        if state.direction * (best - state.best) > EPSILON:
            state.best = best
            return True
        return False

    # ------------------------------------------------------------------ contraintes du courtier

    @staticmethod
    def _step(specs: SymbolSpecs) -> float:
        return specs.tick_size or specs.point or 0.0

    def _legal_stop(self, side: str, stop: float, tick: Tick, specs: SymbolSpecs) -> float:
        """Stop arrondi au tick du côté prudent et tenu à la distance minimale du prix de déclenchement."""
        step = self._step(specs)
        distance = specs.stops_level * specs.point
        if side == "BUY":
            limit = tick.bid - distance - (step if not distance else 0.0)
            stop = min(stop, limit)
            if step:
                stop = math.floor(stop / step + STEP_TOLERANCE) * step
        else:
            limit = tick.ask + distance + (step if not distance else 0.0)
            stop = max(stop, limit)
            if step:
                stop = math.ceil(stop / step - STEP_TOLERANCE) * step
        return round(stop, specs.digits)

    def _legal_take_profit(self, side: str, target: float, tick: Tick, specs: SymbolSpecs) -> float | None:
        step = self._step(specs)
        if step:
            target = round(round(target / step) * step, specs.digits)
        distance = specs.stops_level * specs.point + step
        if side == "BUY" and target < tick.bid + distance:
            return None                                       # objectif déjà dépassé ou trop proche
        if side == "SELL" and target > tick.ask - distance:
            return None
        return target

    @staticmethod
    def _frozen(position: Any, side: str, tick: Tick, specs: SymbolSpecs) -> bool:
        """Prix dans la zone de gel autour du SL ou du TP : le courtier refuse toute modification."""
        freeze = specs.freeze_level * specs.point
        if freeze <= 0:
            return False
        price = tick.bid if side == "BUY" else tick.ask
        return any(level and abs(price - level) <= freeze for level in (position.sl, position.tp))

    def _fees_offset(self, specs: SymbolSpecs) -> float:
        """Marge du breakeven en prix : commission par lot convertie + marge fixe en points."""
        cfg = self.config
        offset = cfg.breakeven_margin_points * specs.point
        if cfg.commission_per_lot and specs.tick_value > 0 and specs.tick_size > 0:
            offset += cfg.commission_per_lot / specs.tick_value * specs.tick_size
        return offset

    # ------------------------------------------------------------------ protection d'une position

    def protect(self, position: Any, initial_stop: float | None = None, now: float | None = None) -> list[ProtectionAction]:
        now = time.time() if now is None else now
        if position.magic != settings.trading.magic_number:
            return []                                         # jamais une position d'un autre robot ou manuelle
        created = state_key(position.magic, position.ticket) not in self.store.states
        state = self.state_for(position, initial_stop)
        if state is None:
            return []
        tick = self.broker.get_current_tick(position.symbol)
        specs = self.broker.get_symbol_specs(position.symbol)
        changed = self._update_best(state, tick, specs) or created
        actions: list[ProtectionAction] = []
        if (tick is not None and specs is not None and specs.trade_allowed and now >= state.retry_after
                and not self._frozen(position, state.side, tick, specs)):
            actions = self._apply(position, state, tick, specs, now)
        if changed or actions:
            self.store.save()                                 # R, meilleur prix et étape survivent au redémarrage
        return actions

    def _apply(self, position: Any, state: ProtectionState, tick: Tick, specs: SymbolSpecs,
               now: float) -> list[ProtectionAction]:
        cfg = self.config
        missing = not position.sl
        current = position.sl or state.initial_stop
        rule = TrailingStop(breakeven_r=cfg.breakeven_r, activation_r=cfg.trailing_start_r, mode="r",
                            distance_r=cfg.trailing_distance_r)
        update = next_stop(state.side, state.entry, state.initial_stop, current, state.best, (), None, rule,
                           breakeven_offset=self._fees_offset(specs))
        target = self._legal_stop(state.side, update.stop, tick, specs)
        gain = state.direction * (target - current) / state.risk
        new_sl = target if missing or gain >= cfg.min_step_r - EPSILON else None
        if new_sl is not None and not missing and state.direction * (new_sl - current) <= EPSILON:
            new_sl = None                                     # jamais un stop moins protecteur
        new_tp = None
        if not position.tp and cfg.take_profit_r > 0:
            new_tp = self._legal_take_profit(state.side, state.entry + state.direction * cfg.take_profit_r * state.risk,
                                             tick, specs)
        if new_sl is None and new_tp is None:
            return []

        stage = update.stage if new_sl is not None else state.stage
        if new_sl is None:
            kind = "protect_tp"
        elif missing or stage == "initial":
            kind = "protect_stop"
        else:
            kind = f"protect_{stage}"
        reached = state.r_multiple(state.best)
        why = {"protect_breakeven": f"+{reached:.2f} R atteint : stop au prix d'entrée + frais",
               "protect_trailing": f"stop suiveur ({cfg.trailing_distance_r:g} R du meilleur prix {state.best:.6g}, "
                                   f"+{reached:.2f} R)",
               "protect_stop": "stop absent : stop initial reposé",
               "protect_tp": f"TP posé à {cfg.take_profit_r:g} R"}[kind]
        result = self.broker.modify_stops(position.ticket, new_sl, new_tp)
        accepted = result.success and (result.dry_run or self._verified(position, new_sl, new_tp, specs))
        if not accepted:
            reason = result.message if not result.success else "modification non appliquée chez le courtier"
            state.retry_after = now + cfg.retry_seconds
            state.last_error = reason
            text = (f"Protection #{position.ticket} ({why}) : modification refusée ({reason}), "
                    f"nouvel essai dans {cfg.retry_seconds} s")
            return [ProtectionAction(position.symbol, position.ticket, "protect_error", text, False)]
        state.retry_after, state.last_error = 0.0, ""
        if new_sl is not None:
            state.stage = stage
        moved = [f"stop {current:.6g} -> {new_sl:.6g}" if new_sl is not None else "",
                 f"TP {new_tp:.6g}" if new_tp is not None else ""]
        text = f"Protection #{position.ticket} : {why} · {', '.join(m for m in moved if m)}"
        return [ProtectionAction(position.symbol, position.ticket, kind, text, True)]

    def _verified(self, position: Any, sl: float | None, tp: float | None, specs: SymbolSpecs) -> bool:
        """Relit la position chez le courtier : la modification doit y figurer."""
        try:
            fresh = next((p for p in self.broker.get_positions(position.symbol, bot_only=True)
                          if p.ticket == position.ticket), None)
        except Exception:  # noqa: BLE001
            log.exception("[%s] Relecture de la position #%s impossible", position.symbol, position.ticket)
            return False
        if fresh is None:
            return True                                       # fermée entre-temps (stop ou TP touché)
        tolerance = (self._step(specs) or specs.point) / 2 + EPSILON
        return ((sl is None or abs(fresh.sl - sl) <= tolerance)
                and (tp is None or abs(fresh.tp - tp) <= tolerance))

    # ------------------------------------------------------------------ positions fermées

    def forget_closed(self, positions: list[Any], now: float | None = None) -> None:
        """Oublie les positions fermées (clôture confirmée par le courtier, ou absentes depuis 24 h)."""
        now = time.time() if now is None else now
        live = {state_key(p.magic, p.ticket) for p in positions}
        changed = False
        for key, state in list(self.store.states.items()):
            if key in live:
                continue
            try:
                closed = any(d.entry == "out" for d in self.broker.get_position_deals(state.ticket))
            except Exception:  # noqa: BLE001
                closed = False
            if not state.missing_since:
                state.missing_since, changed = now, True
            if closed or now - state.missing_since >= FORGET_AFTER_SECONDS:
                del self.store.states[key]
                changed = True
                log.info("[%s] Protection #%s terminée (position fermée)", state.symbol, state.ticket)
        if changed:
            self.store.save()

    def snapshot(self) -> list[dict[str, Any]]:
        """États courants (API / dashboard)."""
        return [{**asdict(s), "best_r": round(s.r_multiple(s.best), 2)} for s in self.store.states.values()]
