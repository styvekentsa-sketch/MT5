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
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Literal

from brokers.base import BaseBroker, is_dry_run
from brokers.factory import get_broker
from config.settings import settings
from risk.risk_manager import calculate_position_size, validate_trade_risk
from sentiment.news_scraper import get_upcoming_high_impact_events, is_high_impact_news_imminent
from strategies.strategy_manager import evaluate_market
from utils.database import get_database
from utils.logger import get_logger
from utils.notifier import Level, get_notifier, notify

log = get_logger(__name__)

OutcomeStatus = Literal["traded", "hold", "news_blocked", "cooldown", "rejected", "failed", "error"]


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

    # ------------------------------------------------------------------ cycle de vie

    def start(self) -> bool:
        """Établit la connexion au broker. Retourne False si elle est impossible."""
        log.info("Démarrage du moteur (%s) :\n%s", "PAPER" if is_dry_run() else "LIVE", settings.summary())
        if not self.broker.connect():
            log.critical("Connexion à %s impossible : arrêt du moteur", self.broker.description)
            notify("Démarrage impossible", f"Connexion à {self.broker.description} impossible : le bot s'est arrêté.",
                   Level.CRITICAL)
            return False
        return True

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
        self.last_cycle_at = datetime.now(timezone.utc)
        self.last_cycle_duration = round(duration, 2)
        self.last_outcomes = tuple(outcomes)

        counts: dict[str, int] = {}
        for outcome in outcomes:
            counts[outcome.status] = counts.get(outcome.status, 0) + 1
        log.info("Tour #%d terminé en %.1fs : %s", self._cycle, duration,
                 ", ".join(f"{status} {n}" for status, n in sorted(counts.items())) or "aucun symbole")
        return outcomes

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
        }
        self._decision_ids += 1
        self.recent_decisions.append({**record, "id": self._decision_ids})
        try:
            get_database().insert_decision(record)
        except Exception:  # noqa: BLE001
            log.exception("[%s] Historisation de la décision impossible", outcome.symbol)

    def _alert_blocked(self, symbol: str, cause: str, detail: str) -> None:
        """Alerte « trade bloqué », limitée à une par symbole et par cause pendant NOTIFY_COOLDOWN_SECONDS."""
        notify(f"Trade bloqué · {symbol}", f"{cause} : {detail}", Level.WARNING, dedup_key=f"{cause}:{symbol}")

    def _news_detail(self, symbol: str) -> str:
        """Annonces responsables du blocage (lues seulement si une alerte peut partir)."""
        if not get_notifier().enabled:
            return ""
        events = get_upcoming_high_impact_events(symbol=symbol) or []
        return ", ".join(f"{e.title} ({e.currency}, {e.time:%H:%M} UTC)" for e in events) or "annonce à fort impact"

    def _process_symbol(self, symbol: str) -> SymbolOutcome:
        """Étapes A à D pour un symbole."""
        # A. Sécurité news
        if is_high_impact_news_imminent(symbol=symbol):
            log.info("[%s] Annonce à fort impact imminente : symbole ignoré ce tour", symbol)
            self._alert_blocked(symbol, "Calendrier économique", self._news_detail(symbol))
            return SymbolOutcome(symbol, "news_blocked",
                                 verdict="Annonce économique à fort impact imminente : symbole ignoré ce tour")

        # B. Évaluation stratégique
        decision = evaluate_market(symbol, broker=self.broker)
        action = decision.get("action", "HOLD")
        if action not in ("BUY", "SELL"):
            log.info("[%s] HOLD (confiance %s%%)", symbol, decision.get("confidence", 0))
            return SymbolOutcome(symbol, "hold", str(decision.get("reason", "")), decision,
                                 verdict=str(decision.get("verdict") or decision.get("reason", "")))

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

        # D. Exécution
        comment = f"{ORDER_COMMENT_PREFIX} {action} {decision.get('confidence', 0)}%"
        result = self.broker.place_order(symbol, action, volume, sl=sl, tp=tp, comment=comment)
        if not result.success:
            log.error("[%s] Échec de l'ordre %s %s : %s", symbol, action, volume, result.message)
            return SymbolOutcome(symbol, "failed", result.message, decision, volume=volume,
                                 verdict=f"Ordre refusé par le broker : {result.message}")

        self._last_trade_at[symbol] = time.monotonic()
        log.info("[%s] %s%s %s @ %s | SL %s | TP %s | ticket #%d | confiance %s%%",
                 symbol, "[DRY RUN] " if result.dry_run else "", action, f"{result.volume:g}",
                 result.price, result.sl, result.tp, result.ticket, decision.get("confidence", 0))
        return SymbolOutcome(symbol, "traded", f"ticket #{result.ticket}", decision,
                             volume=result.volume, ticket=result.ticket,
                             verdict=f"{action} {result.volume:g} exécuté{' (simulation PAPER)' if result.dry_run else ''}"
                                     f" · ticket #{result.ticket} · {decision.get('verdict', '')}")
