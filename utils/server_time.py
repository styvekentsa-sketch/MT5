"""
Heure du serveur du broker et heure UTC réelle.

MT5 horodate les bougies à l'heure de son serveur ; brokers/mt5_client.py les étiquette « UTC »
sans les décaler (index tz-aware cohérent). Pour juger une heure réelle (sessions de liquidité,
annonces du calendrier), il faut convertir avec le fuseau du serveur, heure d'été comprise :
MetaQuotes-Demo suit l'heure d'Europe de l'Est (UTC+2 l'hiver, UTC+3 l'été, changement aux dates
européennes - vérifié le 30/10/2026 : +2 alors que New York était encore à l'heure d'été).

Configuration : MT5_SERVER_TIMEZONE (fuseau IANA, défaut Europe/Athens) ; MT5_SERVER_UTC_OFFSET_HOURS,
s'il est renseigné, impose un décalage fixe. Les autres brokers (ccxt, simulation) horodatent en
UTC réel : fuseau None, aucune conversion.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone, tzinfo

import pandas as pd

from config.settings import settings

DEFAULT_SERVER_TIMEZONE = "Europe/Athens"

BarClock = tzinfo | str | None       # fuseau des étiquettes de bougies (None : UTC réel)


def mt5_server_timezone() -> BarClock:
    """Fuseau du serveur MT5 : décalage fixe configuré, sinon MT5_SERVER_TIMEZONE."""
    if settings.mt5.server_utc_offset_hours is not None:
        return timezone(timedelta(hours=settings.mt5.server_utc_offset_hours))
    return settings.mt5.server_timezone or DEFAULT_SERVER_TIMEZONE


def bar_time_to_utc(at: datetime | pd.Timestamp, clock: BarClock) -> datetime:
    """Heure UTC réelle d'une étiquette de bougie (heure du serveur étiquetée UTC)."""
    stamp = pd.Timestamp(at)
    stamp = stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")
    if clock is None:
        return stamp.to_pydatetime()
    wall = stamp.tz_localize(None).tz_localize(clock, ambiguous=True, nonexistent="shift_forward")
    return wall.tz_convert("UTC").to_pydatetime()


def utc_to_bar_time(at: datetime | pd.Timestamp, clock: BarClock) -> datetime:
    """Inverse : heure UTC réelle -> étiquette de bougie (pour rejouer un calendrier en UTC)."""
    stamp = pd.Timestamp(at)
    stamp = stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")
    if clock is None:
        return stamp.to_pydatetime()
    return stamp.tz_convert(clock).tz_localize(None).tz_localize("UTC").to_pydatetime()
