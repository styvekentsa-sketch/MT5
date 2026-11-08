"""
Orchestrateur du bot : boucle principale reliant le broker, les news, la stratégie et le risque.

Le broker est un `BaseBroker` : terminal MT5 (Forex / CFD) ou exchange crypto via ccxt,
selon BROKER_TYPE. Le moteur ne dépend d'aucune plateforme en particulier.

Séquence par symbole, à chaque tour de boucle (`settings.loop_interval_seconds`) :
  A. annonce à fort impact imminente ?            -> symbole ignoré
  B. evaluate_market()                             -> BUY / SELL / HOLD + SL / TP
  C. calculate_position_size() + validate_trade_risk()
  D. broker.place_order() si le risque est approuvé (simulé en mode PAPER)

Robustesse : chaque symbole et chaque tour sont isolés par des try/except ; une
erreur est loguée et la boucle continue. Ctrl+C (SIGINT) ou SIGTERM arrêtent
proprement la boucle à la fin de l'opération en cours, puis ferment la session broker.

Usage :
    from core.engine import TradingEngine

    TradingEngine().run()
"""

from __future__ import annotations

import signal
import threading
import time
from collections import deque
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Iterable, Literal

from brokers.base import BaseBroker, is_dry_run, set_dry_run
from brokers.factory import get_broker
from config.settings import settings
from learning.trade_memory import TradeMemory, market_features
from risk.risk_manager import calculate_position_size, daily_stop_reached, validate_trade_risk
from risk.verifier import verify_order
from core.news_pipeline import get_pipeline, is_high_impact_news_imminent
from core.position_guard import PositionGuard
from risk.drawdown import DrawdownMonitor
from utils.latency import LatencyTracker
from utils.versioning import rules_version
from models.schemas import SetupStatus
from strategies.base import BaseStrategy
from strategies.impulse import ImpulseStrategy, impulse
from strategies.micro_scalp import MicroScalpStrategy
from strategies.top_down import TopDownStrategy
from strategies.regime import Mode, evaluate_adaptive, manage_positions, position_mode
from strategies.strategy_manager import evaluate_market
from utils.database import get_database
from utils.logger import get_logger
from utils import alerts
from utils.notifier import Level, get_notifier, notify
from utils.server_time import bar_time_to_utc

log = get_logger(__name__)

OutcomeStatus = Literal["traded", "hold", "news_blocked", "cooldown", "rejected", "failed", "error",
                        "managed", "kill_switch", "memory_blocked", "trade_closed", "suspended"]


# =============================================================================
#  Constantes
# =============================================================================

TIMEFRAME_SECONDS: dict[str, int] = {
    "M1": 60, "M5": 300, "M15": 900, "M30": 1800, "H1": 3600,
    "H4": 14_400, "D1": 86_400, "W1": 604_800, "MN1": 2_592_000,
}
ORDER_COMMENT_PREFIX = "Bot"
NOTIFY_FLUSH_TIMEOUT_SECONDS = 5      # délai laissé aux alertes en file avant la fin du processus
RECENT_DECISIONS = 200                # décisions gardées en mémoire (secours si SQLite est indisponible)


# =============================================================================
#  Structures de retour
# =============================================================================

@dataclass(frozen=True)
class SymbolOutcome:
    symbol: str
    status: OutcomeStatus
    detail: str = ""
    # Contexte de la décision, historisé dans la table trade_decisions
    decision: dict[str, Any] | None = field(default=None, compare=False, repr=False)
    volume: float | None = None
    ticket: int | None = None
    # Motif principal, court et chiffré (journal des décisions et rejets)
    verdict: str = ""


# =============================================================================
#  Orchestrateur
# =============================================================================

class TradingEngine:
    """
    Boucle de trading autonome.

    Args:
        symbols: symboles surveillés (par défaut `settings.trading.symbols`).
        interval_seconds: durée d'un tour de boucle (par défaut `settings.loop_interval_seconds`).
        trade_cooldown_seconds: délai minimal entre deux trades sur un même symbole
            (par défaut la durée d'une bougie du timeframe : au plus un trade par bougie).
        broker: plateforme de trading (par défaut le broker du processus, cf. BROKER_TYPE).
    """

    def __init__(
        self,
        symbols: Iterable[str] | None = None,
        interval_seconds: int | None = None,
        trade_cooldown_seconds: int | None = None,
        broker: BaseBroker | None = None,
    ) -> None:
        self.broker = broker or get_broker()
        self.symbols = tuple(symbols) if symbols is not None else settings.trading.symbols
        self.interval_seconds = interval_seconds or settings.loop_interval_seconds
        self.trade_cooldown_seconds = (
            trade_cooldown_seconds if trade_cooldown_seconds is not None
            else TIMEFRAME_SECONDS.get(settings.trading.timeframe, 60)
        )
        self._stop_event = threading.Event()
        self._paused = threading.Event()
        self._last_trade_at: dict[str, float] = {}       # time.monotonic() du dernier trade
        self._cycle = 0
        self._loop_active = False
        # État exposé au monitoring (API) ; remplacé en bloc, lisible depuis un autre thread
        self.started_at: datetime | None = None
        self.last_cycle_at: datetime | None = None
        self.last_cycle_duration: float | None = None
        self.last_outcomes: tuple[SymbolOutcome, ...] = ()
        self.recent_decisions: deque[dict[str, Any]] = deque(maxlen=RECENT_DECISIONS)
        self._decision_ids = 0
        self._position_modes: dict[int, Mode] = {}       # ticket -> mode (profil adaptive)
        self._locked_day = None                          # jour UTC verrouillé par le stop global
        # Journal des trades, post-mortem des pertes et mémoire des erreurs (LEARNING_ENABLED)
        self.memory: TradeMemory | None = TradeMemory() if settings.learning.enabled else None
        # Rattrapage d'impulsion quand la stratégie principale dit HOLD (IMPULSE_ENABLED)
        self.impulse: ImpulseStrategy | None = ImpulseStrategy() if settings.strategy.impulse_enabled else None
        # Petits trades (micro-scalping, objectif court) en dernier recours (STRATEGY_SCALP_ENABLED)
        self.scalp: MicroScalpStrategy | None = MicroScalpStrategy() if settings.strategy.scalp_enabled else None
        self.topdown: TopDownStrategy | None = TopDownStrategy() if settings.strategy.topdown_enabled else None
        self.market_snapshot: dict[str, dict[str, Any]] = {}   # état du marché relu au démarrage
        self._opportunities_taken: dict[tuple[str, str], tuple] = {}   # (symbole, stratégie) -> dernier signal tradé
        # Sorties partielles TP1 / TP2, Bouclier sur annonce, suspension sur drawdown, latence par étape
        self.guard = PositionGuard(self.broker, self.memory)
        self.drawdown = DrawdownMonitor()
        self.suspension: str | None = None
        self.latency = LatencyTracker()

    # ------------------------------------------------------------------ cycle de vie

    def start(self) -> bool:
        """Établit la connexion au broker. Retourne False si elle est impossible."""
        log.info("Démarrage du moteur (%s) :\n%s", "PAPER" if is_dry_run() else "LIVE", settings.summary())
        if not self.broker.connect():
            log.critical("Connexion à %s impossible : arrêt du moteur", self.broker.description)
            notify("Démarrage impossible", f"Connexion à {self.broker.description} impossible : le bot s'est arrêté.",
                   Level.CRITICAL)
            return False
        try:
            self.resync()
        except Exception:  # noqa: BLE001 - la reprise d'état ne doit jamais empêcher le démarrage
            log.exception("Reprise de l'état après redémarrage incomplète")
        return True

    def resync(self) -> None:
        """
        Reprise après redémarrage : l'état en mémoire est reconstruit depuis le broker plutôt que
        repartir de zéro. Positions du bot : mode de gestion (commentaire de l'ordre) et délai
        entre deux trades du symbole (heure d'ouverture) ; marché : dernières bougies M15 / M5 de
        chaque paire relues immédiatement (données disponibles, mouvement récent, impulsion en cours).
        """
        now = datetime.now(timezone.utc)
        clock = getattr(self.broker, "bar_timezone", None)
        positions = self.broker.get_positions(bot_only=True)
        for position in positions:
            mode = position_mode(position, None)
            if mode:
                self._position_modes[position.ticket] = mode
            opened = bar_time_to_utc(datetime.fromtimestamp(position.time, tz=timezone.utc), clock)
            age = (now - opened).total_seconds()
            if 0 <= age < self.trade_cooldown_seconds:
                previous = self._last_trade_at.get(position.symbol, float("-inf"))
                self._last_trade_at[position.symbol] = max(previous, time.monotonic() - age)
        for symbol in self.symbols:
            self.market_snapshot[symbol] = snapshot = self._snapshot(symbol)
            log.info("[%s] Reprise : %s", symbol, snapshot["summary"])
        log.info("État resynchronisé : %d position(s) du bot, %d mode(s) retrouvé(s), %d symbole(s) en délai de "
                 "trade", len(positions), len(self._position_modes), len(self._last_trade_at))

    def _snapshot(self, symbol: str) -> dict[str, Any]:
        """Lecture immédiate des dernières bougies M15 / M5 : disponibilité et dynamique en cours."""
        state: dict[str, Any] = {"symbol": symbol}
        parts = []
        for timeframe in ("M15", "M5"):
            try:
                bars = self.broker.get_historical_data(symbol, count=60, timeframe=timeframe)
            except Exception as exc:  # noqa: BLE001
                bars, state[f"{timeframe}_error"] = None, str(exc)
            if bars is None or len(bars) < 20:
                parts.append(f"{timeframe} indisponible")
                continue
            closed = bars.iloc[:-1]
            state[f"{timeframe}_last_bar"] = closed.index[-1].isoformat()
            direction = None
            for side, frame in (("haussière", closed), ("baissière", -closed[["open", "low", "high", "close"]]
                                                          .set_axis(["open", "high", "low", "close"], axis=1))):
                if impulse(frame, settings.strategy.impulse_bars) is not None:
                    direction = side
            state[f"{timeframe}_impulse"] = direction
            change = float(closed["close"].iloc[-1] - closed["close"].iloc[-4])
            parts.append(f"{timeframe} {len(closed)} bougies, {change:+.5g} sur 3 bougies"
                         + (f", impulsion {direction} en cours" if direction else ""))
        state["summary"] = " | ".join(parts)
        return state

    def stop(self) -> None:
        """Demande l'arrêt de la boucle (effectif à la fin de l'opération en cours)."""
        if not self._stop_event.is_set():
            log.info("Arrêt demandé, fin du tour en cours...")
        self._stop_event.set()

    def pause(self) -> None:
        """Suspend l'évaluation des symboles ; la boucle et la connexion au broker restent actives."""
        if not self._paused.is_set():
            log.warning("Moteur mis en pause : aucun nouveau trade jusqu'à la reprise")
        self._paused.set()

    def resume(self) -> None:
        """Reprend l'évaluation des symboles au prochain tour."""
        if self._paused.is_set():
            log.info("Reprise du moteur")
        self._paused.clear()

    @property
    def is_running(self) -> bool:
        """True tant que la boucle principale tourne (même en pause)."""
        return self._loop_active and not self._stop_event.is_set()

    @property
    def is_paused(self) -> bool:
        return self._paused.is_set()

    @property
    def cycle_count(self) -> int:
        return self._cycle

    def _install_signal_handlers(self) -> None:
        """SIGINT (Ctrl+C), SIGTERM et SIGBREAK (Ctrl+Break sous Windows) -> arrêt propre."""
        def handler(signum: int, _frame: Any) -> None:
            log.info("Signal %s reçu", signal.Signals(signum).name)
            self.stop()

        for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
            sig = getattr(signal, name, None)
            if sig is None:
                continue
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):
                # Hors du thread principal : seul stop() permet d'arrêter la boucle
                log.debug("Impossible d'installer le gestionnaire %s", name)

    def run(self) -> bool:
        """
        Lance la boucle principale jusqu'à l'arrêt (signal ou `stop()`).

        Returns:
            False si la connexion initiale au broker a échoué, True après un arrêt normal.
        """
        self._stop_event.clear()
        self._install_signal_handlers()
        if not self.start():
            get_notifier().flush(NOTIFY_FLUSH_TIMEOUT_SECONDS)
            return False

        log.info("Boucle démarrée : %s | un tour toutes les %ds | 1 trade max par symbole toutes les %ds",
                 ", ".join(self.symbols), self.interval_seconds, self.trade_cooldown_seconds)
        self._log_opportunities()
        self._loop_active = True
        self.started_at = datetime.now(timezone.utc)
        get_database().purge_older_than(settings.database.retention_days)
        notify("Bot démarré", f"Mode {'PAPER' if is_dry_run() else 'LIVE'} · {', '.join(self.symbols)} "
                              f"({settings.trading.timeframe}) · {self.broker.description}")
        try:
            while not self._stop_event.is_set():
                started = time.monotonic()
                if self._paused.is_set():
                    log.debug("Moteur en pause : tour ignoré")
                else:
                    self.step()
                remaining = self.interval_seconds - (time.monotonic() - started)
                if remaining > 0:
                    self._stop_event.wait(remaining)     # interrompu immédiatement par stop()
        except KeyboardInterrupt:
            log.info("Interruption clavier")
        finally:
            self._loop_active = False
            self.broker.disconnect()
            log.info("Moteur arrêté après %d tour(s)", self._cycle)
            notify("Bot arrêté", f"Arrêt propre après {self._cycle} tour(s).", Level.WARNING)
            get_notifier().flush(NOTIFY_FLUSH_TIMEOUT_SECONDS)
        return True

    # ------------------------------------------------------------------ un tour de boucle

    def step(self) -> list[SymbolOutcome]:
        """Exécute un tour complet sur tous les symboles. Ne lève jamais d'exception."""
        self._cycle += 1
        started = time.monotonic()
        outcomes: list[SymbolOutcome] = []
        try:
            self.broker.ensure_connection()
        except Exception as exc:  # noqa: BLE001 - la reconnexion sera retentée au prochain tour
            log.error("Tour #%d ignoré : %s indisponible (%s)", self._cycle, self.broker.name, exc)
            return outcomes

        # Sécurité : stop global journalier (kill switch + verrouillage), puis gestion des positions
        if self._enforce_daily_stop():
            self.last_cycle_at = datetime.now(timezone.utc)
            return outcomes
        if self.adaptive:
            self._manage_positions()
        self._guard_positions()
        self._review_closed_trades()

        if self._drawdown_suspended():                    # positions gérées, aucune nouvelle entrée
            self.last_cycle_at = datetime.now(timezone.utc)
            self.last_outcomes = tuple(SymbolOutcome(s, "suspended", self.suspension or "",
                                                     verdict=self.suspension or "") for s in self.symbols)
            return list(self.last_outcomes)

        for symbol in self.symbols:
            if self._stop_event.is_set():
                break
            try:
                outcome = self._process_symbol(symbol)
            except Exception as exc:  # noqa: BLE001 - un symbole en erreur n'arrête pas les autres
                log.exception("[%s] Erreur inattendue pendant le traitement", symbol)
                outcome = SymbolOutcome(symbol, "error", str(exc), verdict=f"Erreur interne : {exc}")
            outcomes.append(outcome)
            self._record_decision(outcome)

        duration = time.monotonic() - started
        self.latency.record("tour", duration)
        self.last_cycle_at = datetime.now(timezone.utc)
        self.last_cycle_duration = round(duration, 2)
        self.last_outcomes = tuple(outcomes)

        counts: dict[str, int] = {}
        for outcome in outcomes:
            counts[outcome.status] = counts.get(outcome.status, 0) + 1
        log.info("Tour #%d terminé en %.1fs : %s", self._cycle, duration,
                 ", ".join(f"{status} {n}" for status, n in sorted(counts.items())) or "aucun symbole")
        return outcomes

    @property
    def adaptive(self) -> bool:
        """Profil adaptive : style choisi par symbole et gestion active des positions."""
        return settings.strategy.profile == "adaptive"

    def _enforce_daily_stop(self) -> bool:
        """
        Perte journalière >= RISK_MAX_DAILY_LOSS_PCT : KILL SWITCH (fermeture de toutes les positions
        du bot, clôture réelle même en PAPER), moteur mis en pause et journée verrouillée.
        Retourne True si le trading est verrouillé pour ce tour.
        """
        try:
            reason = daily_stop_reached(self.broker)
        except Exception:  # noqa: BLE001 - données indisponibles : les règles de risque refuseront les ordres
            log.exception("Contrôle de la perte journalière impossible")
            return False
        if reason is None:
            return False
        today = datetime.now(timezone.utc).date()
        positions = self.broker.get_positions(bot_only=True)
        if self._locked_day == today and not positions:
            return True
        results = [self.broker.close_order(p.ticket, comment="Kill switch journalier", force=True) for p in positions]
        failed = sum(not r.success for r in results)
        self._locked_day = today
        self.pause()
        message = (f"Stop global : {reason} · {len(results) - failed} position(s) fermée(s)"
                   f"{f', {failed} ÉCHEC(S) : vérifier le terminal' if failed else ''} · moteur verrouillé (pause)")
        log.critical(message)
        notify("KILL SWITCH · perte journalière", message, Level.CRITICAL, dedup_key="daily-stop")
        self._record_decision(SymbolOutcome("TOUS", "kill_switch", reason, verdict=message))
        return True

    def _manage_positions(self) -> None:
        """Trailing stop (swing) et fermeture de fin de session (day) ; chaque action est journalisée."""
        try:
            actions = manage_positions(self.broker, known=self._position_modes)
        except Exception:  # noqa: BLE001
            log.exception("Gestion des positions impossible ce tour")
            return
        for action in actions:
            log.warning("[%s] %s", action.symbol, action.description)
            self._record_decision(SymbolOutcome(action.symbol, "managed", action.description,
                                                {"mode": action.mode}, ticket=action.ticket,
                                                verdict=action.description))

    def _guard_positions(self) -> None:
        """
        Sorties partielles, retour au prix d'entrée, Bouclier avant / après annonce, protection automatique
        (prix d'entrée à +1 R, stop suiveur dès +1,5 R) ; actions journalisées, refus notifiés.
        """
        try:
            actions = self.guard.run(get_pipeline())
        except Exception:  # noqa: BLE001
            log.exception("Gardien des positions indisponible ce tour")
            return
        for action in actions:
            self._record_decision(SymbolOutcome(action.symbol, "managed", action.description, {"mode": action.kind},
                                                ticket=action.ticket, verdict=action.description))
            if action.kind in ("shield_close", "shield_reduce", "invalidated") or not action.success:
                notify(f"Bouclier · {action.symbol}" if action.kind.startswith(("shield", "invalid"))
                       else f"Gestion de position · {action.symbol}", action.description,
                       Level.WARNING if action.success else Level.CRITICAL, dedup_key=f"guard:{action.ticket}:{action.kind}")

    def _drawdown_suspended(self) -> bool:
        """Drawdown depuis le plus haut des fonds propres >= RISK_MAX_DRAWDOWN_PCT : nouvelles entrées suspendues."""
        try:
            reason = self.drawdown.check(self.broker.get_account_info())
        except Exception:  # noqa: BLE001 - données indisponibles : les règles de risque refuseront les ordres
            log.exception("Contrôle du drawdown impossible")
            return False
        if reason and reason != self.suspension:
            log.critical(reason)
            notify("Suspension · drawdown", reason, Level.CRITICAL, dedup_key="drawdown")
            self._record_decision(SymbolOutcome("TOUS", "suspended", reason, verdict=reason))
        elif self.suspension and not reason:
            log.warning("Drawdown repassé sous la limite : nouvelles entrées de nouveau autorisées")
        self.suspension = reason
        return reason is not None

    def _confirm_protection(self, result, sl: float | None, tp: float | None) -> str | None:
        """
        Après exécution réelle : la position doit porter son stop. Absent, il est reposé ; si c'est
        impossible, la position est fermée (jamais de position sans protection). Retourne l'incident éventuel.
        """
        if result.dry_run or sl is None:
            return None
        try:
            position = next((p for p in self.broker.get_positions(result.symbol, bot_only=True)
                             if p.ticket == result.ticket), None)
        except Exception:  # noqa: BLE001
            log.exception("[%s] Vérification des protections du ticket #%s impossible", result.symbol, result.ticket)
            return None
        if position is None or position.sl:
            return None
        fixed = self.broker.modify_stops(result.ticket, sl, tp)
        if fixed.success:
            message = f"stop absent après exécution du ticket #{result.ticket} : reposé à {sl}"
        else:
            closed = self.broker.close_order(result.ticket, comment="Bot sans stop", force=True)
            message = (f"stop absent sur #{result.ticket} et impossible à poser ({fixed.message}) : position "
                       f"{'fermée' if closed.success else 'NON FERMÉE, intervenir dans le terminal'}")
        log.critical("[%s] %s", result.symbol, message)
        notify(f"Protection · {result.symbol}", message, Level.CRITICAL, dedup_key=f"protection:{result.ticket}")
        return message

    def _after_fill(self, result, sl: float | None, tp: float | None) -> None:
        self.guard.register(result.ticket, result.price, sl, result.volume)
        self._confirm_protection(result, sl, tp)

    def _review_closed_trades(self) -> None:
        """Positions du journal fermées depuis le dernier tour : résultat, post-mortem des pertes, mémoire."""
        if self.memory is None:
            return
        try:
            events = self.memory.review(self.broker)
        except Exception:  # noqa: BLE001 - l'apprentissage ne doit jamais bloquer le trading
            log.exception("Analyse des trades clos impossible ce tour")
            return
        for event in events:
            trade = event["trade"]
            alerts.trade_closed(event)                     # pertes : conclusion du post-mortem
            self._record_decision(SymbolOutcome(
                trade["symbol"], "trade_closed", event["message"],
                {"mode": trade.get("mode"), "direction": trade["side"]}, ticket=trade["ticket"],
                verdict=event["message"]))

    def _memory_check(self, symbol: str, action: str, decision: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
        """Caractéristiques du marché et, si elles reproduisent un pattern perdant répété, le motif du blocage."""
        if self.memory is None:
            return {}, None
        try:
            features = market_features(self.broker, symbol, decision)
            block = self.memory.check(symbol, action, decision.get("mode"), features)
        except Exception:  # noqa: BLE001 - mémoire indisponible : on ne bloque pas sur une erreur interne
            log.exception("[%s] Consultation de la mémoire des erreurs impossible", symbol)
            return {}, None
        return features, block.message if block else None

    def on_account_switched(self) -> None:
        """
        Le compte du broker vient de changer (connexion depuis le dashboard) : moteur en pause et
        mode PAPER, pour qu'aucun ordre ne parte sur ce compte sans action explicite ; l'état propre
        à l'ancien compte (modes des positions, verrou du stop journalier, cooldowns) est oublié.
        """
        self.pause()
        set_dry_run(True)
        self._position_modes.clear()
        self._last_trade_at.clear()
        self._locked_day = None

    @property
    def opportunities(self) -> list[BaseStrategy]:
        """
        Stratégies d'opportunité tentées, dans cet ordre, quand la stratégie principale dit HOLD : analyse
        descendante (la plus sélective) d'abord, puis impulsion et petits trades.
        """
        return [s for s in (self.topdown, self.impulse, self.scalp) if s is not None]

    def _opportunity_trade(self, symbol: str, hold: SymbolOutcome) -> SymbolOutcome:
        """
        HOLD de la stratégie principale : trade d'opportunité de la première stratégie prête
        (impulsion, puis petits trades). La stratégie applique déjà la fenêtre d'annonces, la taille
        (1 %), les règles de risque (positions max, perte journalière) et le vérificateur ; ici :
        délai entre trades, un seul trade par signal, mémoire des erreurs, ordre et journal.
        """
        if not settings.strategy.opportunity_allowed(symbol):
            return hold                                  # hors de OPPORTUNITY_SYMBOLS : stratégie principale seule
        notes = []
        for strategy in self.opportunities:
            if self._in_cooldown(symbol):
                break
            outcome, note = self._try_opportunity(strategy, symbol, hold)
            if outcome is not None:
                return outcome
            if note:
                notes.append(note)
        return replace(hold, verdict=" · ".join([hold.verdict, *notes])) if notes else hold

    def opportunity_summary(self) -> list[dict[str, Any]]:
        """Modes d'opportunité actifs, leur rôle et leurs symboles (logs, API, dashboard)."""
        symbols = [s for s in self.symbols if settings.strategy.opportunity_allowed(s)]
        return [{"name": s.name, "label": s.label, "role": s.role, "timeframe": s.timeframes[-1], "symbols": symbols}
                for s in self.opportunities]

    def _log_opportunities(self) -> None:
        modes = self.opportunity_summary()
        if not modes:
            log.info("Trades d'opportunité : désactivés (TOPDOWN_ENABLED / IMPULSE_ENABLED / STRATEGY_SCALP_ENABLED)")
            return
        excluded = [s for s in self.symbols if not settings.strategy.opportunity_allowed(s)]
        log.info("Trades d'opportunité (quand la stratégie principale dit HOLD), par priorité, sur %s%s :",
                 ", ".join(modes[0]["symbols"]) or "aucun symbole",
                 f" (exclus : {', '.join(excluded)})" if excluded else "")
        for mode in modes:
            log.info("  - %s : %s", mode["label"], mode["role"])
        log.info("  Risque commun : %g %% par trade, %g %% cumulé, %d positions max, perte journalière %g %%, spread "
                 "<= %g %% du stop, vérificateur indépendant", settings.risk.risk_per_trade_pct,
                 settings.risk.max_total_risk_pct, settings.risk.max_open_positions, settings.risk.max_daily_loss_pct,
                 settings.risk.max_spread_sl_pct)

    def _try_opportunity(self, strategy: BaseStrategy, symbol: str,
                         hold: SymbolOutcome) -> tuple[SymbolOutcome | None, str | None]:
        label = strategy.label
        try:
            with self.latency.measure("analyse"):
                setup = strategy.analyze(self.broker, symbol)
        except Exception:  # noqa: BLE001 - une stratégie d'opportunité ne doit jamais casser le tour
            log.exception("[%s] Évaluation « %s » impossible", symbol, label)
            return None, None
        if setup is None:
            return None, None
        if setup.status != SetupStatus.READY:
            return None, f"{label.lower()} : signal écarté ({setup.notes[-1] if setup.notes else setup.reason})"

        side = setup.side.value
        # un seul trade par signal : le délai entre trades (1 bougie de TRADING_TIMEFRAME) peut être plus
        # court que la bougie du signal, une sortie rapide ne doit pas relancer le même signal
        key = (side, round(float(setup.indicators.get(strategy.anchor, 0.0)), 8))
        if self._opportunities_taken.get((symbol, strategy.name)) == key:
            return None, f"{label.lower()} : signal déjà tradé"
        decision = {"symbol": symbol, "action": side, "direction": side, "confidence": 100,
                    "entry_price": setup.entry_price, "suggested_sl": setup.stop_loss, "suggested_tp": setup.take_profit,
                    "mode": setup.style.value, "strategy": strategy.name, "reason": setup.reason,
                    "verdict": f"[{label}] {setup.reason}"}
        features, blocked = self._memory_check(symbol, side, decision)
        if blocked:
            self._alert_blocked(symbol, "Mémoire des erreurs", blocked)
            return SymbolOutcome(symbol, "memory_blocked", blocked, decision, volume=setup.volume, verdict=blocked), None
        with self.latency.measure("ordre"):
            result = self.broker.place_order(symbol, side, setup.volume, sl=setup.stop_loss, tp=setup.take_profit,
                                             comment=f"{ORDER_COMMENT_PREFIX} {strategy.order_tag} {side}")
        if not result.success:
            log.error("[%s] Échec de l'ordre « %s » %s : %s", symbol, label, side, result.message)
            return SymbolOutcome(symbol, "failed", result.message, decision, volume=setup.volume,
                                 verdict=f"[{label}] ordre refusé par le broker : {result.message}"), None
        self._last_trade_at[symbol] = time.monotonic()
        self._opportunities_taken[(symbol, strategy.name)] = key
        self._after_fill(result, setup.stop_loss, setup.take_profit)
        if self.memory is not None:
            try:
                self.memory.record_entry(result, decision, features)
            except Exception:  # noqa: BLE001
                log.exception("[%s] Journal d'entrée impossible pour le ticket #%s", symbol, result.ticket)
        log.info("[%s] %s[%s] %s %s @ %s | SL %s | TP %s | ticket #%d", symbol, "[DRY RUN] " if result.dry_run else "",
                 label.upper(), side, f"{result.volume:g}", result.price, result.sl, result.tp, result.ticket)
        return SymbolOutcome(symbol, "traded", f"ticket #{result.ticket}", decision, volume=result.volume,
                             ticket=result.ticket, verdict=f"[{label}] {side} : {setup.reason}"), None

    def _in_cooldown(self, symbol: str) -> bool:
        last = self._last_trade_at.get(symbol)
        return last is not None and time.monotonic() - last < self.trade_cooldown_seconds

    def _record_decision(self, outcome: SymbolOutcome) -> None:
        """
        Historise la décision (table trade_decisions, plus une copie en mémoire servie par l'API
        si SQLite est indisponible) ; une erreur de base n'arrête jamais le tour.
        """
        decision = outcome.decision or {}
        action = decision.get("action")
        record = {
            "decided_at": datetime.now(timezone.utc),
            "cycle": self._cycle,
            "symbol": outcome.symbol,
            "status": outcome.status,
            "action": action,
            "confidence": decision.get("confidence"),
            "technical_score": decision.get("technical_score"),
            "sentiment_score": decision.get("sentiment_score"),
            "hybrid_score": decision.get("hybrid_score"),
            "entry_price": decision.get("entry_price"),
            "sl": decision.get("suggested_sl"),
            "tp": decision.get("suggested_tp"),
            "volume": outcome.volume,
            "ticket": outcome.ticket,
            "dry_run": is_dry_run(),
            "detail": outcome.detail or decision.get("reason", ""),
            "direction": decision.get("direction") or (action if action in ("BUY", "SELL") else None),
            "verdict": outcome.verdict or decision.get("verdict") or outcome.detail,
            "mode": decision.get("mode"),
        }
        if outcome.status == "traded":
            record["detail"] = f"{record['detail']} · {rules_version()}"
        self._decision_ids += 1
        self.recent_decisions.append({**record, "id": self._decision_ids})
        try:
            get_database().insert_decision(record)
        except Exception:  # noqa: BLE001
            log.exception("[%s] Historisation de la décision impossible", outcome.symbol)

    def _alert_blocked(self, symbol: str, cause: str, detail: str) -> None:
        """Alerte « trade bloqué », limitée à une par symbole et par cause pendant NOTIFY_COOLDOWN_SECONDS."""
        notify(f"Trade bloqué · {symbol}", f"{cause} : {detail}", Level.WARNING, dedup_key=f"{cause}:{symbol}")

    def _process_symbol(self, symbol: str) -> SymbolOutcome:
        """Étapes A à D pour un symbole."""
        # A. Fenêtre de sécurité : 30 min avant -> 15 min après une annonce majeure sur l'actif
        with self.latency.measure("actualités"):
            imminent = is_high_impact_news_imminent(symbol=symbol)
        if imminent:
            block = get_pipeline().safety_check(symbol)
            reason = block.reason if block else "annonce économique à fort impact imminente"
            log.info("[%s] Fenêtre de sécurité : %s", symbol, reason)
            if block is None or get_pipeline().first_alert(block):     # une alerte par annonce, pas une par tour
                self._alert_blocked(symbol, "Calendrier économique", reason)
            return SymbolOutcome(symbol, "news_blocked", verdict=f"Fenêtre de sécurité : {reason}")

        # B. Évaluation stratégique
        with self.latency.measure("analyse"):
            decision = (evaluate_adaptive(symbol, self.broker) if self.adaptive
                        else evaluate_market(symbol, broker=self.broker))
        action = decision.get("action", "HOLD")
        if action not in ("BUY", "SELL"):
            log.info("[%s] HOLD (confiance %s%%)", symbol, decision.get("confidence", 0))
            hold = SymbolOutcome(symbol, "hold", str(decision.get("reason", "")), decision,
                                 verdict=str(decision.get("verdict") or decision.get("reason", "")))
            return self._opportunity_trade(symbol, hold) if self.opportunities else hold

        if self._in_cooldown(symbol):
            log.info("[%s] Signal %s ignoré : un trade a déjà été ouvert sur cette bougie", symbol, action)
            return SymbolOutcome(symbol, "cooldown", decision=decision,
                                 verdict=f"Signal {action} ignoré : un trade a déjà été ouvert sur cette bougie "
                                         f"({settings.trading.timeframe})")

        # C. Taille de position et validation du risque
        entry = decision.get("entry_price")
        sl, tp = decision.get("suggested_sl"), decision.get("suggested_tp")
        if entry is None or sl is None:
            log.warning("[%s] Signal %s sans prix d'entrée ou SL : trade abandonné", symbol, action)
            return SymbolOutcome(symbol, "rejected", "prix d'entrée ou SL manquant", decision,
                                 verdict="Signal sans prix d'entrée ou sans Stop Loss : trade abandonné")

        risk_started = time.monotonic()
        volume = calculate_position_size(symbol, entry, sl, broker=self.broker)
        if volume <= 0:
            log.warning("[%s] Signal %s abandonné : taille de position nulle", symbol, action)
            self._alert_blocked(symbol, "Risque", f"signal {action} abandonné, taille de position nulle")
            return SymbolOutcome(symbol, "rejected", "taille de position nulle", decision, volume=0.0,
                                 verdict="Taille de position nulle : même le volume minimal dépasserait le risque "
                                         f"autorisé ({settings.risk.risk_per_trade_pct}% par trade)")

        check = validate_trade_risk(symbol, action, volume, sl, tp, broker=self.broker)
        if not check.get("is_approved"):
            self._alert_blocked(symbol, "Risque", f"signal {action} refusé, {check.get('reason', '')}")
            return SymbolOutcome(symbol, "rejected", str(check.get("reason", "")), decision, volume=volume,
                                 verdict=f"Risque : {check.get('reason', 'règle non respectée')}")

        # C bis. Vérificateur indépendant : ratio net, spread, limites journalières, recalculés à part
        verdict = verify_order(symbol, action, entry, sl, tp, self.broker)
        self.latency.record("risque", time.monotonic() - risk_started)
        if not verdict.approved:
            self._alert_blocked(symbol, "Vérificateur", verdict.reason)
            return SymbolOutcome(symbol, "rejected", verdict.reason, decision, volume=volume,
                                 verdict=f"Vérificateur indépendant : {verdict.reason}")

        # D. Mémoire des erreurs : pas de nouvelle position dans un contexte déjà perdant à répétition
        features, blocked = self._memory_check(symbol, action, decision)
        if blocked:
            log.warning("[%s] %s", symbol, blocked)
            self._alert_blocked(symbol, "Mémoire des erreurs", blocked)
            return SymbolOutcome(symbol, "memory_blocked", blocked, decision, volume=volume, verdict=blocked)

        # E. Exécution
        comment = decision.get("order_comment") or f"{ORDER_COMMENT_PREFIX} {action} {decision.get('confidence', 0)}%"
        with self.latency.measure("ordre"):
            result = self.broker.place_order(symbol, action, volume, sl=sl, tp=tp, comment=comment)
        if not result.success:
            log.error("[%s] Échec de l'ordre %s %s : %s", symbol, action, volume, result.message)
            return SymbolOutcome(symbol, "failed", result.message, decision, volume=volume,
                                 verdict=f"Ordre refusé par le broker : {result.message}")

        self._last_trade_at[symbol] = time.monotonic()
        self._after_fill(result, sl, tp)
        if decision.get("mode"):
            self._position_modes[result.ticket] = decision["mode"]
        if self.memory is not None:
            try:
                self.memory.record_entry(result, decision, features)
            except Exception:  # noqa: BLE001
                log.exception("[%s] Journal d'entrée impossible pour le ticket #%s", symbol, result.ticket)
        log.info("[%s] %s%s %s @ %s | SL %s | TP %s | ticket #%d | confiance %s%%",
                 symbol, "[DRY RUN] " if result.dry_run else "", action, f"{result.volume:g}",
                 result.price, result.sl, result.tp, result.ticket, decision.get("confidence", 0))
        return SymbolOutcome(symbol, "traded", f"ticket #{result.ticket}", decision,
                             volume=result.volume, ticket=result.ticket,
                             verdict=f"{action} {result.volume:g} exécuté{' (simulation PAPER)' if result.dry_run else ''}"
                                     f" · ticket #{result.ticket} · {decision.get('verdict', '')}")
