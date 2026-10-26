"""
Configuration centralisée du bot de trading MT5.

Les valeurs sont lues depuis le fichier `.env` (via python-dotenv) ou depuis les
variables d'environnement du système, puis validées et exposées sous forme de
dataclasses immuables regroupées dans l'objet global `settings`.

Usage :
    from config import settings, get_logger

    log = get_logger(__name__)
    log.info("Symboles : %s", settings.trading.symbols)
"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass, field
from logging.handlers import RotatingFileHandler
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent

# `override=False` : une variable déjà définie dans le système a priorité sur le .env
load_dotenv(BASE_DIR / ".env", override=False)


class ConfigError(ValueError):
    """Erreur levée lorsqu'une valeur de configuration est absente ou invalide."""


# =============================================================================
#  Helpers de lecture typée des variables d'environnement
# =============================================================================

def _get_str(name: str, default: str | None = None, required: bool = False) -> str:
    value = os.getenv(name, default)
    if value is not None:
        value = value.strip()
    if required and not value:
        raise ConfigError(f"Variable d'environnement obligatoire manquante : {name}")
    return value or ""


def _get_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw.strip())
    except ValueError as exc:
        raise ConfigError(f"{name} doit être un entier (reçu : {raw!r})") from exc


def _get_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw.strip().replace(",", "."))
    except ValueError as exc:
        raise ConfigError(f"{name} doit être un nombre (reçu : {raw!r})") from exc


def _get_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "y", "on", "oui"}:
        return True
    if value in {"0", "false", "no", "n", "off", "non"}:
        return False
    raise ConfigError(f"{name} doit être un booléen (reçu : {raw!r})")


def _get_list(name: str, default: str = "") -> tuple[str, ...]:
    raw = os.getenv(name, default)
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def _mask(secret: str) -> str:
    """Masque un secret pour l'affichage dans les logs."""
    if not secret:
        return "<vide>"
    return f"{secret[:2]}***{secret[-2:]}" if len(secret) > 6 else "***"


# =============================================================================
#  Sections de configuration
# =============================================================================

VALID_TIMEFRAMES = ("M1", "M5", "M15", "M30", "H1", "H4", "D1", "W1", "MN1")
VALID_SENTIMENT_ENGINES = ("vader", "finbert", "llm")
VALID_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


@dataclass(frozen=True)
class MT5Config:
    account: int
    password: str = field(repr=False)
    server: str
    path: str | None
    timeout_ms: int

    def validate(self) -> None:
        if self.account <= 0:
            raise ConfigError("MT5_ACCOUNT doit être un numéro de compte valide")
        if not self.password:
            raise ConfigError("MT5_PASSWORD est vide")
        if not self.server:
            raise ConfigError("MT5_SERVER est vide")
        if self.path and not Path(self.path).exists():
            raise ConfigError(f"MT5_PATH introuvable : {self.path}")


@dataclass(frozen=True)
class TradingConfig:
    symbols: tuple[str, ...]
    timeframe: str
    bars_history: int
    magic_number: int
    deviation_points: int

    def validate(self) -> None:
        if not self.symbols:
            raise ConfigError("TRADING_SYMBOLS doit contenir au moins un symbole")
        if self.timeframe not in VALID_TIMEFRAMES:
            raise ConfigError(
                f"TRADING_TIMEFRAME invalide : {self.timeframe} (attendu : {', '.join(VALID_TIMEFRAMES)})"
            )
        if self.bars_history < 50:
            raise ConfigError("TRADING_BARS_HISTORY doit être >= 50 pour calculer les indicateurs")


@dataclass(frozen=True)
class RiskConfig:
    risk_per_trade_pct: float
    max_daily_loss_pct: float
    max_open_positions: int
    max_positions_per_symbol: int
    max_spread_points: int
    sl_atr_multiplier: float
    tp_atr_multiplier: float
    min_lot: float
    max_lot: float

    def validate(self) -> None:
        if not 0 < self.risk_per_trade_pct <= 5:
            raise ConfigError("RISK_PER_TRADE_PCT doit être compris entre 0 et 5 (%)")
        if not 0 < self.max_daily_loss_pct <= 20:
            raise ConfigError("RISK_MAX_DAILY_LOSS_PCT doit être compris entre 0 et 20 (%)")
        if self.max_open_positions < 1 or self.max_positions_per_symbol < 1:
            raise ConfigError("Les limites de positions doivent être >= 1")
        if self.sl_atr_multiplier <= 0 or self.tp_atr_multiplier <= 0:
            raise ConfigError("Les multiplicateurs ATR (SL/TP) doivent être > 0")
        if not 0 < self.min_lot <= self.max_lot:
            raise ConfigError("Contrainte non respectée : 0 < RISK_MIN_LOT <= RISK_MAX_LOT")


@dataclass(frozen=True)
class NewsConfig:
    newsapi_key: str = field(repr=False)
    finnhub_api_key: str = field(repr=False)
    alphavantage_api_key: str = field(repr=False)
    rss_feeds: tuple[str, ...]
    refresh_interval_seconds: int
    lookback_hours: int
    request_timeout_seconds: int

    @property
    def has_any_source(self) -> bool:
        return bool(self.rss_feeds or self.newsapi_key or self.finnhub_api_key or self.alphavantage_api_key)

    def validate(self) -> None:
        if self.refresh_interval_seconds < 30:
            raise ConfigError("NEWS_REFRESH_INTERVAL_SECONDS doit être >= 30 (respect des quotas API)")
        if self.lookback_hours < 1:
            raise ConfigError("NEWS_LOOKBACK_HOURS doit être >= 1")


@dataclass(frozen=True)
class SentimentConfig:
    engine: str
    anthropic_api_key: str = field(repr=False)
    anthropic_model: str

    def validate(self) -> None:
        if self.engine not in VALID_SENTIMENT_ENGINES:
            raise ConfigError(
                f"SENTIMENT_ENGINE invalide : {self.engine} (attendu : {', '.join(VALID_SENTIMENT_ENGINES)})"
            )
        if self.engine == "llm" and not self.anthropic_api_key:
            raise ConfigError("SENTIMENT_ENGINE=llm nécessite ANTHROPIC_API_KEY")


@dataclass(frozen=True)
class StrategyConfig:
    technical_weight: float
    sentiment_weight: float
    signal_threshold: float

    def validate(self) -> None:
        if abs(self.technical_weight + self.sentiment_weight - 1.0) > 1e-6:
            raise ConfigError("STRATEGY_TECHNICAL_WEIGHT + STRATEGY_SENTIMENT_WEIGHT doit valoir 1.0")
        if not 0 < self.signal_threshold <= 1:
            raise ConfigError("STRATEGY_SIGNAL_THRESHOLD doit être compris entre 0 et 1")


@dataclass(frozen=True)
class LogConfig:
    level: str
    directory: Path
    max_bytes: int
    backup_count: int

    def validate(self) -> None:
        if self.level not in VALID_LOG_LEVELS:
            raise ConfigError(f"LOG_LEVEL invalide : {self.level} (attendu : {', '.join(VALID_LOG_LEVELS)})")


@dataclass(frozen=True)
class Settings:
    dry_run: bool
    loop_interval_seconds: int
    mt5: MT5Config
    trading: TradingConfig
    risk: RiskConfig
    news: NewsConfig
    sentiment: SentimentConfig
    strategy: StrategyConfig
    log: LogConfig

    def validate(self) -> None:
        if self.loop_interval_seconds < 1:
            raise ConfigError("BOT_LOOP_INTERVAL_SECONDS doit être >= 1")
        for section in (self.mt5, self.trading, self.risk, self.news,
                        self.sentiment, self.strategy, self.log):
            section.validate()

    def summary(self) -> str:
        """Résumé lisible de la configuration, secrets masqués."""
        return "\n".join([
            f"  Mode            : {'DRY RUN (aucun ordre réel)' if self.dry_run else 'LIVE'}",
            f"  Compte MT5      : {self.mt5.account} @ {self.mt5.server}",
            f"  Symboles        : {', '.join(self.trading.symbols)} ({self.trading.timeframe})",
            f"  Risque/trade    : {self.risk.risk_per_trade_pct}% | Perte max/jour : {self.risk.max_daily_loss_pct}%",
            f"  Positions max   : {self.risk.max_open_positions} (par symbole : {self.risk.max_positions_per_symbol})",
            f"  Sentiment       : {self.sentiment.engine}",
            f"  Pondération     : technique {self.strategy.technical_weight} / sentiment {self.strategy.sentiment_weight}",
            f"  Sources news    : {len(self.news.rss_feeds)} RSS | NewsAPI {_mask(self.news.newsapi_key)}"
            f" | Finnhub {_mask(self.news.finnhub_api_key)} | AlphaVantage {_mask(self.news.alphavantage_api_key)}",
        ])


def load_settings() -> Settings:
    """Construit et valide la configuration à partir de l'environnement."""
    log_dir = Path(_get_str("LOG_DIR", "logs"))
    if not log_dir.is_absolute():
        log_dir = BASE_DIR / log_dir

    cfg = Settings(
        dry_run=_get_bool("DRY_RUN", True),
        loop_interval_seconds=_get_int("BOT_LOOP_INTERVAL_SECONDS", 60),
        mt5=MT5Config(
            account=_get_int("MT5_ACCOUNT", 0),
            password=_get_str("MT5_PASSWORD", required=True),
            server=_get_str("MT5_SERVER", required=True),
            path=_get_str("MT5_PATH") or None,
            timeout_ms=_get_int("MT5_TIMEOUT_MS", 60_000),
        ),
        trading=TradingConfig(
            symbols=tuple(s.upper() for s in _get_list("TRADING_SYMBOLS", "EURUSD")),
            timeframe=_get_str("TRADING_TIMEFRAME", "M15").upper(),
            bars_history=_get_int("TRADING_BARS_HISTORY", 500),
            magic_number=_get_int("TRADING_MAGIC_NUMBER", 20261026),
            deviation_points=_get_int("TRADING_DEVIATION_POINTS", 20),
        ),
        risk=RiskConfig(
            risk_per_trade_pct=_get_float("RISK_PER_TRADE_PCT", 1.0),
            max_daily_loss_pct=_get_float("RISK_MAX_DAILY_LOSS_PCT", 3.0),
            max_open_positions=_get_int("RISK_MAX_OPEN_POSITIONS", 3),
            max_positions_per_symbol=_get_int("RISK_MAX_POSITIONS_PER_SYMBOL", 1),
            max_spread_points=_get_int("RISK_MAX_SPREAD_POINTS", 30),
            sl_atr_multiplier=_get_float("RISK_SL_ATR_MULTIPLIER", 1.5),
            tp_atr_multiplier=_get_float("RISK_TP_ATR_MULTIPLIER", 3.0),
            min_lot=_get_float("RISK_MIN_LOT", 0.01),
            max_lot=_get_float("RISK_MAX_LOT", 1.0),
        ),
        news=NewsConfig(
            newsapi_key=_get_str("NEWSAPI_KEY"),
            finnhub_api_key=_get_str("FINNHUB_API_KEY"),
            alphavantage_api_key=_get_str("ALPHAVANTAGE_API_KEY"),
            rss_feeds=_get_list("NEWS_RSS_FEEDS"),
            refresh_interval_seconds=_get_int("NEWS_REFRESH_INTERVAL_SECONDS", 300),
            lookback_hours=_get_int("NEWS_LOOKBACK_HOURS", 6),
            request_timeout_seconds=_get_int("NEWS_REQUEST_TIMEOUT_SECONDS", 10),
        ),
        sentiment=SentimentConfig(
            engine=_get_str("SENTIMENT_ENGINE", "vader").lower(),
            anthropic_api_key=_get_str("ANTHROPIC_API_KEY"),
            anthropic_model=_get_str("ANTHROPIC_MODEL", "claude-haiku-5-5"),
        ),
        strategy=StrategyConfig(
            technical_weight=_get_float("STRATEGY_TECHNICAL_WEIGHT", 0.7),
            sentiment_weight=_get_float("STRATEGY_SENTIMENT_WEIGHT", 0.3),
            signal_threshold=_get_float("STRATEGY_SIGNAL_THRESHOLD", 0.6),
        ),
        log=LogConfig(
            level=_get_str("LOG_LEVEL", "INFO").upper(),
            directory=log_dir,
            max_bytes=_get_int("LOG_MAX_BYTES", 5 * 1024 * 1024),
            backup_count=_get_int("LOG_BACKUP_COUNT", 10),
        ),
    )
    cfg.validate()
    return cfg


# =============================================================================
#  Système de logs
# =============================================================================

LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)-20s | %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


class _ColorFormatter(logging.Formatter):
    """Formatter console avec couleurs ANSI par niveau (désactivé hors terminal)."""

    COLORS = {
        logging.DEBUG: "\033[36m",     # cyan
        logging.INFO: "\033[32m",      # vert
        logging.WARNING: "\033[33m",   # jaune
        logging.ERROR: "\033[31m",     # rouge
        logging.CRITICAL: "\033[1;41m",  # fond rouge
    }
    RESET = "\033[0m"

    def __init__(self, use_color: bool) -> None:
        super().__init__(LOG_FORMAT, LOG_DATE_FORMAT)
        self.use_color = use_color

    def format(self, record: logging.LogRecord) -> str:
        message = super().format(record)
        if self.use_color and record.levelno in self.COLORS:
            return f"{self.COLORS[record.levelno]}{message}{self.RESET}"
        return message


_logging_configured = False


def setup_logging(log_cfg: LogConfig) -> None:
    """
    Configure le logger racine :
      - console colorée (stdout)
      - `logs/bot.log`    : tous les messages >= LOG_LEVEL (rotation par taille)
      - `logs/errors.log` : uniquement WARNING et plus, pour un diagnostic rapide
    Idempotent : peut être appelé plusieurs fois sans dupliquer les handlers.
    """
    global _logging_configured
    if _logging_configured:
        return

    log_cfg.directory.mkdir(parents=True, exist_ok=True)
    level = getattr(logging, log_cfg.level)

    if sys.platform == "win32":
        os.system("")  # active le support des séquences ANSI dans la console Windows

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(level)
    console.setFormatter(_ColorFormatter(use_color=sys.stdout.isatty()))
    root.addHandler(console)

    file_formatter = logging.Formatter(LOG_FORMAT, LOG_DATE_FORMAT)

    main_file = RotatingFileHandler(
        log_cfg.directory / "bot.log",
        maxBytes=log_cfg.max_bytes,
        backupCount=log_cfg.backup_count,
        encoding="utf-8",
    )
    main_file.setLevel(level)
    main_file.setFormatter(file_formatter)
    root.addHandler(main_file)

    error_file = RotatingFileHandler(
        log_cfg.directory / "errors.log",
        maxBytes=log_cfg.max_bytes,
        backupCount=log_cfg.backup_count,
        encoding="utf-8",
    )
    error_file.setLevel(logging.WARNING)
    error_file.setFormatter(file_formatter)
    root.addHandler(error_file)

    # Réduit le bruit des bibliothèques tierces
    for noisy in ("urllib3", "requests", "httpx", "transformers", "filelock"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _logging_configured = True


def get_logger(name: str) -> logging.Logger:
    """Retourne un logger nommé ; à utiliser dans chaque module (`get_logger(__name__)`)."""
    return logging.getLogger(name)


# =============================================================================
#  Chargement au moment de l'import
# =============================================================================

try:
    settings: Settings = load_settings()
except ConfigError as err:
    # Logs pas encore configurés : on écrit directement sur stderr et on stoppe.
    sys.stderr.write(f"[CONFIG] Erreur de configuration : {err}\n"
                     f"[CONFIG] Vérifiez votre fichier .env (modèle : .env.example)\n")
    raise SystemExit(1) from err

setup_logging(settings.log)


if __name__ == "__main__":
    # `python config.py` : vérifie rapidement la configuration
    get_logger("config").info("Configuration chargée avec succès :\n%s", settings.summary())
