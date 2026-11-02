"""
Alertes métier formatées, envoyées par les canaux du notifieur (console, Telegram, Discord) :

  - setup validé (stratégies à setups, mode analyste ou dashboard) ;
  - trade fermé en perte, avec la conclusion de l'analyse post-mortem ;
  - blocage de sécurité (kill switch, limites de risque, fenêtre d'annonce majeure).

Chaque fonction `format_*` est pure (titre, message, niveau, clé anti-doublon) ; les fonctions
d'envoi ne lèvent jamais d'exception (cf. utils/notifier.py). Une même alerte n'est pas renvoyée
avant NOTIFY_COOLDOWN_SECONDS.

Usage :
    from utils import alerts

    alerts.setup_validated(setup)
    alerts.trade_closed(event)                 # événement de TradeMemory.review()
    alerts.security_block("Kill switch", "perte journalière 3.1 % >= 3 %", critical=True)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from utils.notifier import Level, notify


@dataclass(frozen=True)
class Alert:
    title: str
    message: str
    level: Level = Level.INFO
    dedup_key: str | None = None


def _price(value: float | None) -> str:
    return "—" if value is None else f"{value:.5g}"


def format_setup(setup) -> Alert:
    """Setup prêt à exécuter (TradingSetup au statut READY)."""
    side = setup.side.value
    ratio = f"{setup.net_reward_risk:.2f}" if setup.net_reward_risk is not None else "—"
    lines = [
        f"{setup.strategy} · {setup.style.value} · régime {setup.regime.value}",
        f"Entrée {_price(setup.entry_price)} | SL {_price(setup.stop_loss)} | TP {_price(setup.take_profit)}",
        f"Ratio net {ratio} | lot {setup.volume:g} | risque {setup.risk_amount:.2f} ({setup.risk_pct:.2f} %)",
        f"Pour : {setup.reason}",
    ]
    lines += [f"Contre : {note}" for note in setup.notes]
    if setup.expires_at is not None:
        lines.append(f"Valable jusqu'à {setup.expires_at:%H:%M} UTC")
    # un setup est recalculé à chaque analyse (nouvel identifiant) : clé sur le niveau du stop
    key = f"setup:{setup.strategy}:{setup.symbol}:{side}:{setup.stop_loss:.5g}"
    return Alert(f"Setup validé · {setup.symbol} {side}", "\n".join(lines), Level.INFO, key)


def format_loss(event: dict[str, Any]) -> Alert | None:
    """Trade clos en perte (événement de TradeMemory.review) ; None pour un gain."""
    if event.get("outcome") != "loss":
        return None
    trade, analysis = event["trade"], event.get("analysis") or {}
    lines = [
        f"Ticket #{trade.get('ticket')} · {trade.get('strategy') or trade.get('mode') or '—'} · "
        f"clôture {trade.get('close_reason', '—')} à {_price(trade.get('close_price'))}",
    ]
    if analysis:
        lines.append(f"Post-mortem : {analysis['main_factor']}")
        lines += [f"  - {factor}" for factor in analysis.get("factors", [])[1:]]
        excursion = analysis.get("adverse_excursion_pips"), analysis.get("favorable_excursion_pips")
        if excursion != (None, None):
            lines.append(f"Excursions : défavorable {excursion[0]} pips, favorable {excursion[1]} pips "
                         f"(SL à {analysis.get('sl_distance_pips')} pips)")
        lines.append(f"Contexte : {analysis.get('pattern_description', '—')}")
    return Alert(f"Perte · {trade['symbol']} {trade['side']} {trade.get('net_profit', 0):+.2f}", "\n".join(lines),
                 Level.WARNING, f"loss:{trade.get('ticket') or trade.get('id')}")


def format_block(title: str, reason: str, symbol: str | None = None, critical: bool = False) -> Alert:
    """Blocage de sécurité : limite de risque, kill switch, annonce majeure."""
    where = f" · {symbol}" if symbol else ""
    return Alert(f"Blocage{where} · {title}", reason, Level.CRITICAL if critical else Level.WARNING,
                 f"block:{title}:{symbol or '*'}")


def send(alert: Alert | None) -> bool:
    return alert is not None and notify(alert.title, alert.message, alert.level, alert.dedup_key)


def setup_validated(setup) -> bool:
    return send(format_setup(setup))


def trade_closed(event: dict[str, Any]) -> bool:
    """Alerte uniquement les pertes (déclencheurs de l'analyse post-mortem)."""
    return send(format_loss(event))


def security_block(title: str, reason: str, symbol: str | None = None, critical: bool = False) -> bool:
    return send(format_block(title, reason, symbol, critical))
