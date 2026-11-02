"""
Journal intelligent et mémoire des erreurs (boucle de rétroaction).

  1. Journal d'entrée     `trade_decisions.json` : instantané complet de chaque position ouverte
                          (prix, SL / TP, indicateurs, spread, sentiment, raison, pattern)
  2. Analyse post-mortem  `loss_analysis.json` : à la clôture d'une position perdante, durée,
                          excursion défavorable / favorable, facteur d'échec probable
  3. Mémoire des erreurs  `mistakes_memory.json` : chaque trade clos alimente les statistiques de
                          son pattern de marché ; un pattern perdant de façon répétée bloque les
                          nouveaux ordres qui lui ressemblent

Pattern de marché : symbole, mode, sens et six caractéristiques discrétisées au moment de
l'entrée (zone RSI, position par rapport à la tendance, volatilité, spread rapporté au SL,
sentiment, session). Deux trades « se ressemblent » s'ils ont le même pattern.

Règle de blocage : au moins MISTAKE_MIN_LOSSES pertes sur le pattern, une part de pertes
>= MISTAKE_MIN_LOSS_RATE parmi ses trades clos, et une perte dans les MISTAKE_MEMORY_DAYS
derniers jours (un pattern ancien est « oublié »).

Positions simulées (PAPER) : journalisées mais jamais analysées, faute de clôture réelle.
Aucune fonction ne lève d'exception vers le moteur : une erreur de journalisation ne doit
jamais empêcher de trader ni de gérer les positions.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from brokers.base import BaseBroker
from brokers.models import ClosedDeal, OrderResult
from config.settings import settings
from strategies.strategy_manager import pip_size
from strategies.technical_analysis import INDICATOR_PROFILES, compute_indicators
from utils.logger import get_logger

log = get_logger(__name__)

TRADES_FILE = "trade_decisions.json"
LOSSES_FILE = "loss_analysis.json"
MEMORY_FILE = "mistakes_memory.json"
MAX_TRADES = 2000                    # entrées conservées dans le journal (les plus anciennes sont retirées)
MAX_LOSSES = 1000
MAX_REFERENCES = 20                  # tickets perdants gardés par pattern

MODE_TIMEFRAMES = {"scalping": "M1", "day": "M15", "swing": "H1"}
TIMEFRAME_SECONDS = {"M1": 60, "M5": 300, "M15": 900, "M30": 1800, "H1": 3600, "H4": 14_400, "D1": 86_400}
FEATURE_BARS = 200
EXCURSION_MAX_BARS = 5000

# Seuils des caractéristiques discrétisées et de l'analyse
RSI_LOW, RSI_HIGH = 35.0, 65.0
FLAT_TREND_ATR = 0.3                 # |EMA rapide - EMA lente| < 0.3 ATR : marché sans tendance
CALM_ATR_RATIO, HOT_ATR_RATIO = 0.8, 1.3
LOW_SPREAD_SL, HIGH_SPREAD_SL = 0.15, 0.30
SENTIMENT_NEUTRAL = 0.1
QUICK_STOP_BARS = 2                  # stop touché en 2 bougies ou moins : bruit / impulsion
NEVER_IN_FAVOR_TP = 0.2              # meilleure excursion < 20 % du TP : le prix n'est jamais allé dans le bon sens


# =============================================================================
#  Stockage JSON (écriture atomique, fichier corrompu mis de côté)
# =============================================================================

class JsonStore:
    def __init__(self, path: Path, default: Any) -> None:
        self.path = path
        self.default = default

    def load(self) -> Any:
        if not self.path.exists():
            return json.loads(json.dumps(self.default))
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            backup = self.path.with_suffix(f".corrupt-{datetime.now(timezone.utc):%Y%m%d%H%M%S}.json")
            log.error("%s illisible (%s) : mis de côté sous %s, nouveau fichier", self.path.name, exc, backup.name)
            try:
                self.path.replace(backup)
            except OSError:
                pass
            return json.loads(json.dumps(self.default))

    def save(self, data: Any) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        os.replace(tmp, self.path)


# =============================================================================
#  Caractéristiques de marché et pattern
# =============================================================================

def _round(value: Any, digits: int = 5) -> float | None:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return round(value, digits) if np.isfinite(value) else None


def market_features(broker: BaseBroker, symbol: str, decision: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    """
    Instantané technique au moment de l'ordre, sur le timeframe du mode (M1 scalping, M15 day,
    H1 swing, sinon TRADING_TIMEFRAME) : RSI, EMA, MACD, ATR, volatilité relative, spread
    (en points et rapporté au SL), sentiment des actualités, heure UTC.
    """
    now = now or datetime.now(timezone.utc)
    mode = decision.get("mode")
    timeframe = MODE_TIMEFRAMES.get(mode or "", settings.trading.timeframe)
    params = INDICATOR_PROFILES["scalping" if timeframe == "M1" else "standard"]
    features: dict[str, Any] = {"timeframe": timeframe, "ema_periods": f"{params.ema_fast}/{params.ema_slow}",
                                "rsi_period": params.rsi_window, "hour_utc": now.hour,
                                "sentiment": _round(decision.get("sentiment_score") or 0.0, 4),
                                "technical_score": _round(decision.get("technical_score"), 4)}
    bars = broker.get_historical_data(symbol, count=FEATURE_BARS, timeframe=timeframe)
    if bars is not None and len(bars) > 60:
        ind = compute_indicators(bars[["open", "high", "low", "close"]].iloc[:-1], params)
        last = ind.iloc[-1]
        atr = float(last["atr"]) if pd.notna(last["atr"]) else float("nan")
        median_atr = float(ind["atr"].tail(100).median())
        features.update({
            "rsi": _round(last["rsi"], 2), "ema_fast": _round(last["ema_fast"]), "ema_slow": _round(last["ema_slow"]),
            "macd_hist": _round(last["macd_hist"], 7), "atr": _round(atr, 7),
            "ema_gap_atr": _round((last["ema_fast"] - last["ema_slow"]) / atr, 3) if atr > 0 else None,
            "atr_ratio": _round(atr / median_atr, 3) if median_atr > 0 else None,
        })
    tick = broker.get_current_tick(symbol)
    entry, sl = decision.get("entry_price"), decision.get("suggested_sl")
    if tick is not None:
        features["spread_points"] = tick.spread_points
        if entry and sl and entry != sl:
            features["spread_to_sl"] = _round((tick.ask - tick.bid) / abs(entry - sl), 3)
    return features


def _bucket(value: float | None, low: float, high: float, labels: tuple[str, str, str]) -> str:
    if value is None:
        return "inconnu"
    return labels[0] if value < low else labels[2] if value > high else labels[1]


def _session(hour: int) -> str:
    return "asie" if hour < 7 else "londres" if hour < 12 else "new_york" if hour < 17 else "fin_de_journée"


def pattern_labels(side: str, features: dict[str, Any]) -> dict[str, str]:
    """Caractéristiques discrétisées : ce qui définit qu'un trade « ressemble » à un autre."""
    direction = 1 if side == "BUY" else -1
    gap = features.get("ema_gap_atr")
    if gap is None:
        trend = "inconnu"
    elif abs(gap) < FLAT_TREND_ATR:
        trend = "plat"
    else:
        trend = "dans_le_sens" if gap * direction > 0 else "contre"
    sentiment = (features.get("sentiment") or 0.0) * direction
    return {
        "rsi": _bucket(features.get("rsi"), RSI_LOW, RSI_HIGH, ("bas", "neutre", "haut")),
        "tendance": trend,
        "volatilité": _bucket(features.get("atr_ratio"), CALM_ATR_RATIO, HOT_ATR_RATIO, ("calme", "normale", "forte")),
        "spread": _bucket(features.get("spread_to_sl"), LOW_SPREAD_SL, HIGH_SPREAD_SL, ("faible", "moyen", "élevé")),
        "sentiment": "favorable" if sentiment > SENTIMENT_NEUTRAL else "contraire" if sentiment < -SENTIMENT_NEUTRAL
                     else "neutre",
        "session": _session(int(features.get("hour_utc", 0))),
    }


def pattern_key(symbol: str, mode: str | None, side: str, labels: dict[str, str]) -> str:
    return "|".join([symbol, mode or "standard", side, *(f"{k}={v}" for k, v in labels.items())])


def describe(labels: dict[str, str]) -> str:
    return " + ".join(f"{name} {value.replace('_', ' ')}" for name, value in labels.items())


# =============================================================================
#  Analyse post-mortem
# =============================================================================

def excursions(side: str, entry: float, bars: pd.DataFrame) -> tuple[float | None, float | None, float | None]:
    """(excursion défavorable, excursion favorable, plus grande amplitude d'une bougie), en prix."""
    if bars is None or bars.empty:
        return None, None, None
    high, low = float(bars["high"].max()), float(bars["low"].min())
    adverse = entry - low if side == "BUY" else high - entry
    favorable = high - entry if side == "BUY" else entry - low
    return max(adverse, 0.0), max(favorable, 0.0), float((bars["high"] - bars["low"]).max())


def failure_factors(trade: dict[str, Any], close_reason: str, duration_s: float | None,
                    favorable: float | None, max_bar_range: float | None) -> list[str]:
    """Facteurs d'échec probables, du plus déterminant au moins déterminant."""
    f = trade.get("features", {})
    side = trade["side"]
    factors: list[str] = []
    spread_to_sl = f.get("spread_to_sl")
    if (spread_to_sl is not None and spread_to_sl >= HIGH_SPREAD_SL) or \
            (f.get("spread_points") or 0) >= 0.8 * settings.risk.max_spread_points:
        factors.append("Spread élevé par rapport au SL (proche du seuil critique) : coût d'entrée trop lourd")
    tf_seconds = TIMEFRAME_SECONDS.get(f.get("timeframe", ""), 60)
    if close_reason == "sl" and duration_s is not None and duration_s <= QUICK_STOP_BARS * tf_seconds:
        atr = f.get("atr") or 0.0
        impulsive = (f.get("atr_ratio") or 0) > HOT_ATR_RATIO or (atr and max_bar_range and max_bar_range >= 2 * atr)
        factors.append("Bruit de marché / volatilité impulsive : stop touché en moins de "
                       f"{QUICK_STOP_BARS} bougies{' sur une bougie anormalement large' if impulsive else ''}")
    gap = f.get("ema_gap_atr")
    if gap is not None and abs(gap) < FLAT_TREND_ATR:
        factors.append("Faux signal de cassure en range : moyennes mobiles collées, aucune tendance")
    rsi = f.get("rsi")
    if rsi is not None and ((side == "BUY" and rsi > 70) or (side == "SELL" and rsi < 30)):
        factors.append(f"Entrée sur un mouvement épuisé (RSI {rsi:.0f} {'en surachat' if side == 'BUY' else 'en survente'})")
    sentiment = (f.get("sentiment") or 0.0) * (1 if side == "BUY" else -1)
    if sentiment < -SENTIMENT_NEUTRAL:
        factors.append(f"Sentiment des actualités contraire au trade ({f.get('sentiment'):+.2f})")
    tp_distance = abs((trade.get("tp") or trade["entry_price"]) - trade["entry_price"])
    if favorable is not None and tp_distance > 0 and favorable < NEVER_IN_FAVOR_TP * tp_distance:
        factors.append("Signal faux dès l'entrée : le prix n'est jamais allé dans le sens du trade")
    return factors or ["Stop atteint sans cause technique dominante (aléa de marché)"]


# =============================================================================
#  Mémoire
# =============================================================================

@dataclass(frozen=True)
class MemoryBlock:
    key: str
    message: str
    pattern: dict[str, Any]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class TradeMemory:
    """Journal des trades, analyses des pertes et mémoire des patterns perdants (fichiers JSON)."""

    def __init__(self, directory: Path | None = None) -> None:
        self.directory = Path(directory or settings.learning.directory)
        self.trades = JsonStore(self.directory / TRADES_FILE, [])
        self.losses = JsonStore(self.directory / LOSSES_FILE, [])
        self.memory = JsonStore(self.directory / MEMORY_FILE, {"patterns": {}})
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ 3. avant l'ordre

    @staticmethod
    def is_blocking(pattern: dict[str, Any], now: datetime | None = None) -> bool:
        cfg = settings.learning
        losses, wins = pattern.get("losses", 0), pattern.get("wins", 0)
        if losses < cfg.min_losses or losses / max(losses + wins, 1) < cfg.min_loss_rate:
            return False
        last_loss = pattern.get("last_loss_at")
        if not last_loss:
            return False
        age = (now or datetime.now(timezone.utc)) - datetime.fromisoformat(last_loss)
        return age <= timedelta(days=cfg.memory_days)

    def check(self, symbol: str, side: str, mode: str | None, features: dict[str, Any]) -> MemoryBlock | None:
        """Ordre à bloquer si les conditions actuelles reproduisent un pattern perdant répété."""
        labels = pattern_labels(side, features)
        key = pattern_key(symbol, mode, side, labels)
        with self._lock:
            pattern = self.memory.load()["patterns"].get(key)
        if not pattern or not self.is_blocking(pattern):
            return None
        last_ticket = (pattern.get("loss_tickets") or ["?"])[-1]
        message = (f"Ordre rejeté par la mémoire des erreurs - pattern similaire au trade perdant #{last_ticket} "
                   f"({pattern['losses']} perte(s) / {pattern.get('wins', 0)} gain(s) : {describe(labels)} ; "
                   f"facteur principal : {pattern.get('main_factor', 'inconnu')})")
        return MemoryBlock(key, message, pattern)

    # ------------------------------------------------------------------ 1. à l'ouverture

    def record_entry(self, result: OrderResult, decision: dict[str, Any], features: dict[str, Any]) -> dict[str, Any]:
        """Instantané complet de la position ouverte (journal d'entrée)."""
        side = result.order_type
        labels = pattern_labels(side, features)
        with self._lock:
            trades = self.trades.load()
            entry = {
                "id": (trades[-1]["id"] + 1) if trades else 1,
                "timestamp": _now_iso(),
                "ticket": result.ticket,
                "symbol": result.symbol,
                "side": side,
                "volume": result.volume,
                "entry_price": result.price,
                "sl": result.sl,
                "tp": result.tp,
                "mode": decision.get("mode"),
                "reason": decision.get("verdict") or decision.get("reason", ""),
                "features": features,
                "pattern": pattern_key(result.symbol, decision.get("mode"), side, labels),
                "pattern_labels": labels,
                "dry_run": result.dry_run,
                "status": "simulated" if result.dry_run else "open",
            }
            trades.append(entry)
            self.trades.save(trades[-MAX_TRADES:])
        log.info("[%s] Journal d'entrée #%d : %s %s @ %s | %s", result.symbol, entry["id"], side, result.volume,
                 result.price, describe(labels))
        return entry

    def record_setup(self, setup, features: dict[str, Any]) -> dict[str, Any]:
        """Instantané d'un setup validé (stratégies à setups) : journalisé, jamais analysé (pas d'ordre)."""
        side = setup.side.value
        mode = setup.style.value
        labels = pattern_labels(side, features)
        with self._lock:
            trades = self.trades.load()
            entry = {
                "id": (trades[-1]["id"] + 1) if trades else 1,
                "timestamp": _now_iso(),
                "ticket": None,
                "setup_id": setup.id,
                "strategy": setup.strategy,
                "symbol": setup.symbol,
                "side": side,
                "volume": setup.volume,
                "entry_price": setup.entry_price,
                "sl": setup.stop_loss,
                "tp": setup.take_profit,
                "mode": mode,
                "regime": setup.regime.value,
                "net_reward_risk": setup.net_reward_risk,
                "reason": setup.reason,
                "features": {**features, **{f"setup_{k}": v for k, v in setup.indicators.items()
                                            if isinstance(v, (int, float))}},
                "pattern": pattern_key(setup.symbol, mode, side, labels),
                "pattern_labels": labels,
                "dry_run": True,
                "status": "setup",
            }
            trades.append(entry)
            self.trades.save(trades[-MAX_TRADES:])
        log.info("[%s] Journal : setup %s %s validé (%s) | %s", setup.symbol, setup.strategy, side, setup.id,
                 describe(labels))
        return entry

    # ------------------------------------------------------------------ 2. à la clôture

    def review(self, broker: BaseBroker) -> list[dict[str, Any]]:
        """
        Positions journalisées qui ne sont plus ouvertes : résultat, analyse post-mortem des
        pertes et mise à jour de la mémoire. Retourne les événements (un par trade clos).
        """
        with self._lock:
            trades = self.trades.load()
            pending = [t for t in trades if t.get("status") == "open"]
            if not pending:
                return []
            open_tickets = {p.ticket for p in broker.get_positions(bot_only=False)}
            events = []
            for trade in pending:
                if trade["ticket"] in open_tickets:
                    continue
                deals = broker.get_position_deals(trade["ticket"])
                exits = [d for d in deals if d.entry == "out"]
                if not exits:
                    continue                                   # historique pas encore disponible : tour suivant
                events.append(self._close(broker, trade, deals, exits))
            if events:
                self.trades.save(trades)
            return events

    def _close(self, broker: BaseBroker, trade: dict[str, Any], deals: list[ClosedDeal],
               exits: list[ClosedDeal]) -> dict[str, Any]:
        opening = next((d for d in deals if d.entry == "in"), None)
        last_exit = exits[-1]
        net = round(sum(d.net_profit for d in deals), 2)
        duration = (last_exit.time - opening.time).total_seconds() if opening else None
        trade.update({"status": "closed", "closed_at": _now_iso(), "close_price": last_exit.price,
                      "close_reason": last_exit.reason, "net_profit": net, "duration_seconds": duration})
        outcome = "loss" if net < 0 else "win"
        analysis = None
        if outcome == "loss":
            analysis = self._post_mortem(broker, trade, opening, last_exit, duration)
        self._learn(trade, analysis)
        if analysis:
            message = (f"Perte #{trade['ticket']} {trade['symbol']} {trade['side']} {net:+.2f} : "
                       f"{analysis['main_factor']}")
            log.warning("[%s] Post-mortem : %s", trade["symbol"], message)
        else:
            message = f"Gain #{trade['ticket']} {trade['symbol']} {trade['side']} {net:+.2f} ({last_exit.reason})"
            log.info("[%s] Trade clos : %s", trade["symbol"], message)
        return {"outcome": outcome, "trade": trade, "analysis": analysis, "message": message}

    def _post_mortem(self, broker: BaseBroker, trade: dict[str, Any], opening: ClosedDeal | None,
                     closing: ClosedDeal, duration: float | None) -> dict[str, Any]:
        pip = pip_size(broker.get_symbol_specs(trade["symbol"])) or 0.0001
        adverse = favorable = max_range = None
        if opening is not None:
            minutes = int((closing.time - opening.time).total_seconds() // 60) + 5
            bars = broker.get_historical_data(trade["symbol"], count=min(EXCURSION_MAX_BARS, max(minutes + 60, 10)),
                                              timeframe="M1")
            if bars is not None and not bars.empty:
                window = bars[(bars.index >= opening.time - timedelta(minutes=1)) & (bars.index <= closing.time)]
                adverse, favorable, max_range = excursions(trade["side"], trade["entry_price"], window)
        factors = failure_factors(trade, closing.reason, duration, favorable, max_range)
        sl_distance = abs(trade["entry_price"] - (trade.get("sl") or trade["entry_price"]))
        with self._lock:
            losses = self.losses.load()
            analysis = {
                "id": (losses[-1]["id"] + 1) if losses else 1,
                "analyzed_at": _now_iso(),
                "trade_id": trade["id"], "ticket": trade["ticket"], "symbol": trade["symbol"],
                "side": trade["side"], "mode": trade.get("mode"),
                "opened_at": trade["timestamp"], "duration_seconds": duration,
                "entry_price": trade["entry_price"], "close_price": closing.price, "close_reason": closing.reason,
                "net_profit": trade["net_profit"],
                "adverse_excursion_pips": round(adverse / pip, 1) if adverse is not None else None,
                "favorable_excursion_pips": round(favorable / pip, 1) if favorable is not None else None,
                "sl_distance_pips": round(sl_distance / pip, 1),
                "main_factor": factors[0], "factors": factors,
                "pattern": trade["pattern"], "pattern_description": describe(trade["pattern_labels"]),
                "entry_reason": trade.get("reason", ""),
            }
            losses.append(analysis)
            self.losses.save(losses[-MAX_LOSSES:])
        return analysis

    def _learn(self, trade: dict[str, Any], analysis: dict[str, Any] | None) -> None:
        """Statistiques du pattern du trade ; une perte catégorisée l'inscrit comme erreur."""
        with self._lock:
            memory = self.memory.load()
            pattern = memory["patterns"].setdefault(trade["pattern"], {
                "key": trade["pattern"], "symbol": trade["symbol"], "mode": trade.get("mode"), "side": trade["side"],
                "labels": trade["pattern_labels"], "description": describe(trade["pattern_labels"]),
                "wins": 0, "losses": 0, "total_loss": 0.0, "factors": {}, "loss_tickets": [],
                "first_seen": _now_iso(),
            })
            pattern["last_seen"] = _now_iso()
            if analysis is None:
                pattern["wins"] += 1
            else:
                pattern["losses"] += 1
                pattern["total_loss"] = round(pattern["total_loss"] + trade["net_profit"], 2)
                pattern["last_loss_at"] = _now_iso()
                pattern["loss_tickets"] = (pattern["loss_tickets"] + [trade["ticket"]])[-MAX_REFERENCES:]
                for factor in analysis["factors"]:
                    pattern["factors"][factor] = pattern["factors"].get(factor, 0) + 1
                pattern["main_factor"] = max(pattern["factors"], key=pattern["factors"].get)
            was_blocking = pattern.get("blocking", False)
            pattern["blocking"] = self.is_blocking(pattern)
            self.memory.save(memory)
        if pattern["blocking"] and not was_blocking:
            log.warning("Mémoire des erreurs : nouveau pattern bloquant (%d pertes / %d gains) : %s",
                        pattern["losses"], pattern["wins"], pattern["key"])

    # ------------------------------------------------------------------ 4. rapports

    def report(self, limit: int = 20) -> dict[str, Any]:
        """Synthèse pour l'API : journal, analyses récentes et patterns appris."""
        with self._lock:
            trades, losses = self.trades.load(), self.losses.load()
            patterns = list(self.memory.load()["patterns"].values())
        for pattern in patterns:
            pattern["blocking"] = self.is_blocking(pattern)
        closed = [t for t in trades if t.get("status") == "closed"]
        mistakes = sorted((p for p in patterns if p.get("losses", 0) > 0),
                          key=lambda p: (not p["blocking"], -p["losses"], p.get("total_loss", 0)))
        return {
            "directory": str(self.directory),
            "trades_logged": len(trades),
            "open": sum(t.get("status") == "open" for t in trades),
            "simulated": sum(t.get("status") == "simulated" for t in trades),
            "setups": sum(t.get("status") == "setup" for t in trades),
            "closed": len(closed),
            "wins": sum(t.get("net_profit", 0) >= 0 for t in closed),
            "losses": sum(t.get("net_profit", 0) < 0 for t in closed),
            "blocking_patterns": sum(p["blocking"] for p in mistakes),
            "rules": {"min_losses": settings.learning.min_losses, "min_loss_rate": settings.learning.min_loss_rate,
                      "memory_days": settings.learning.memory_days},
            "recent_trades": list(reversed(trades[-limit:])),
            "recent_losses": list(reversed(losses[-limit:])),
            "patterns": mistakes[:limit],
        }
