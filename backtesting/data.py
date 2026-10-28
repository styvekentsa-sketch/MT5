"""
Chargement des données de backtest : bougies (CSV ou terminal MT5), spécifications
du symbole et calendrier économique historique (CSV).

Formats CSV acceptés (séparateur détecté automatiquement) :
  - standard : time,open,high,low,close[,volume][,spread]
  - export MT5 (Centre d'historique) : <DATE> <TIME> <OPEN> <HIGH> <LOW> <CLOSE> <TICKVOL> <VOL> <SPREAD>
Les prix sont des prix Bid, comme dans MetaTrader ; `spread` est en points.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from brokers.mt5_client import SymbolSpecs
from sentiment.news_scraper import EconomicEvent
from utils.logger import get_logger

log = get_logger(__name__)

REQUIRED_COLUMNS = ("open", "high", "low", "close")
TIME_COLUMNS = ("time", "datetime", "timestamp", "date")
MT5_DATE_FORMAT = "%Y.%m.%d %H:%M:%S"


class DataError(ValueError):
    """Données historiques absentes ou inexploitables."""


def _normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    df.columns = [str(c).strip().strip("<>").lower() for c in df.columns]
    return df.rename(columns={"tickvol": "volume", "tick_volume": "volume"})


def _parse_time(df: pd.DataFrame) -> pd.DatetimeIndex:
    if "date" in df.columns and "time" in df.columns:          # export MT5 : date et heure séparées
        raw = df["date"].astype(str) + " " + df["time"].astype(str)
        parsed = pd.to_datetime(raw, format=MT5_DATE_FORMAT, errors="coerce", utc=True)
        if parsed.isna().all():
            parsed = pd.to_datetime(raw, errors="coerce", utc=True)
        return pd.DatetimeIndex(parsed)
    column = next((c for c in TIME_COLUMNS if c in df.columns), None)
    if column is None:
        raise DataError(f"colonne de date introuvable (attendu : {', '.join(TIME_COLUMNS)})")
    values = df[column]
    if pd.api.types.is_numeric_dtype(values):                  # horodatage Unix en secondes
        return pd.DatetimeIndex(pd.to_datetime(values, unit="s", utc=True))
    # « 2024.01.02 ... » (style MT5) -> « 2024-01-02 ... » ; les fractions de seconde ISO sont préservées
    normalized = values.astype(str).str.replace(r"^(\d{4})\.(\d{2})\.(\d{2})", r"\1-\2-\3", regex=True)
    return pd.DatetimeIndex(pd.to_datetime(normalized, errors="coerce", utc=True, format="mixed"))


def clean_candles(df: pd.DataFrame) -> pd.DataFrame:
    """Colonnes numériques, index UTC trié sans doublon, lignes OHLC incomplètes ou incohérentes retirées."""
    for column in REQUIRED_COLUMNS:
        if column not in df.columns:
            raise DataError(f"colonne « {column} » manquante")
    out = df[[c for c in (*REQUIRED_COLUMNS, "volume", "spread") if c in df.columns]].apply(
        pd.to_numeric, errors="coerce")
    out = out[out.index.notna()].dropna(subset=list(REQUIRED_COLUMNS))
    out = out[(out["high"] >= out[["open", "close"]].max(axis=1)) & (out["low"] <= out[["open", "close"]].min(axis=1))]
    out = out[~out.index.duplicated(keep="last")].sort_index()
    if out.empty:
        raise DataError("aucune bougie exploitable")
    return out


def load_csv(path: str | Path) -> pd.DataFrame:
    """Bougies depuis un fichier CSV (format standard ou export MT5)."""
    path = Path(path)
    if not path.exists():
        raise DataError(f"fichier introuvable : {path}")
    try:
        raw = pd.read_csv(path, sep=None, engine="python")
    except (ValueError, OSError) as exc:
        raise DataError(f"lecture impossible de {path} : {exc}") from exc
    raw = _normalize_columns(raw)
    raw.index = _parse_time(raw)
    candles = clean_candles(raw)
    log.info("%d bougies chargées depuis %s (%s -> %s)", len(candles), path.name, candles.index[0], candles.index[-1])
    return candles


def load_mt5(symbol: str, bars: int, timeframe: str | None = None) -> tuple[pd.DataFrame, SymbolSpecs]:
    """Bougies clôturées et spécifications réelles du symbole depuis le terminal MT5."""
    from brokers.mt5_client import connect_mt5, get_historical_data, get_symbol_specs, shutdown_mt5

    if not connect_mt5():
        raise DataError("connexion à MetaTrader 5 impossible")
    try:
        df = get_historical_data(symbol, count=bars + 1, timeframe=timeframe)
        specs = get_symbol_specs(symbol)
    finally:
        shutdown_mt5()
    if df is None or df.empty or specs is None:
        raise DataError(f"aucune donnée MT5 pour {symbol}")
    return clean_candles(df.iloc[:-1]), specs          # dernière bougie : en cours de formation


def default_specs(symbol: str, tick_size: float | None = None, tick_value: float | None = None,
                  digits: int | None = None) -> SymbolSpecs:
    """
    Spécifications usuelles quand les données viennent d'un CSV (sans terminal) :
    forex 5 décimales (3 pour le JPY), or 100 oz, argent 5 000 oz. `tick_value` est
    exprimé dans la devise de cotation, supposée être la devise du compte.
    """
    sym = symbol.upper()
    if sym.startswith("XAU"):
        preset = (2, 0.01, 1.0)
    elif sym.startswith("XAG"):
        preset = (3, 0.001, 5.0)
    elif "JPY" in sym[3:6]:
        preset = (3, 0.001, 100.0)                     # 100 000 x 0.001, en JPY
    else:
        preset = (5, 0.00001, 1.0)                     # 100 000 x 0.00001
    d, size, value = digits if digits is not None else preset[0], tick_size or preset[1], tick_value or preset[2]
    return SymbolSpecs(symbol=symbol, digits=d, point=10 ** -d, tick_size=size, tick_value=value,
                       volume_min=0.01, volume_max=100.0, volume_step=0.01, stops_level=0, freeze_level=0,
                       trade_allowed=True)


def load_calendar_csv(path: str | Path) -> list[EconomicEvent]:
    """Annonces économiques historiques : colonnes time,currency,impact[,title][,forecast][,previous]."""
    path = Path(path)
    try:
        raw = _normalize_columns(pd.read_csv(path, sep=None, engine="python"))
    except (ValueError, OSError) as exc:
        raise DataError(f"lecture impossible de {path} : {exc}") from exc
    for column in ("currency", "impact"):
        if column not in raw.columns:
            raise DataError(f"calendrier : colonne « {column} » manquante")
    times = _parse_time(raw)
    events = [
        EconomicEvent(
            title=str(row.get("title", "") or ""),
            currency=str(row["currency"]).strip().upper(),
            time=when.to_pydatetime(),
            impact=str(row["impact"]).strip().capitalize(),
            forecast=str(row.get("forecast", "") or ""),
            previous=str(row.get("previous", "") or ""),
        )
        for when, (_, row) in zip(times, raw.iterrows())
        if pd.notna(when)
    ]
    log.info("%d annonces chargées depuis %s", len(events), path.name)
    return sorted(events, key=lambda e: e.time)
