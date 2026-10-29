"""
Paramètres globaux du bot : mode Paper/Live, symboles, timeframe, risque, news, stratégie, logs.

Les valeurs sont lues depuis le `.env` puis validées et exposées sous forme de
dataclasses immuables regroupées dans l'objet global `settings`.

Usage :
    from config import settings

    print(settings.trading.symbols, settings.dry_run)
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

from config.accounts import AccountSpec, load_accounts, validate_accounts
from config.credentials import MT5Config, load_mt5_credentials
from config.env import (
    BASE_DIR,
    ConfigError,
    get_bool,
    get_float,
    get_int,
    get_list,
    get_str,
    mask,
)

VALID_TIMEFRAMES = ("M1", "M5", "M15", "M30", "H1", "H4", "D1", "W1", "MN1")
VALID_SENTIMENT_ENGINES = ("vader", "keywords", "finbert", "llm")
VALID_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


# =============================================================================
#  Sections de configuration
# =============================================================================

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
    max_consecutive_losses: int
    max_open_positions: int          # positions simultanées du bot, toutes paires confondues
    max_positions_per_symbol: int
    max_total_risk_pct: float        # somme des pertes si tous les SL sont touchés (positions + nouveau trade)
    max_margin_usage_pct: float      # marge totale utilisée après le trade, en % des fonds propres
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
        if self.max_consecutive_losses < 1:
            raise ConfigError("RISK_MAX_CONSECUTIVE_LOSSES doit être >= 1")
        if self.max_open_positions < 1 or self.max_positions_per_symbol < 1:
            raise ConfigError("Les limites de positions doivent être >= 1")
        if self.max_open_positions > 20:
            raise ConfigError("RISK_MAX_OPEN_POSITIONS doit être <= 20")
        if self.max_positions_per_symbol > self.max_open_positions:
            raise ConfigError("RISK_MAX_POSITIONS_PER_SYMBOL ne peut pas dépasser RISK_MAX_OPEN_POSITIONS")
        if not self.risk_per_trade_pct <= self.max_total_risk_pct <= 20:
            raise ConfigError("RISK_MAX_TOTAL_RISK_PCT doit être compris entre RISK_PER_TRADE_PCT et 20 (%)")
        if not 0 < self.max_margin_usage_pct <= 90:
            raise ConfigError("RISK_MAX_MARGIN_USAGE_PCT doit être compris entre 0 et 90 (%)")
        if self.sl_atr_multiplier <= 0 or self.tp_atr_multiplier <= 0:
            raise ConfigError("Les multiplicateurs ATR (SL/TP) doivent être > 0")
        if not 0 < self.min_lot <= self.max_lot:
            raise ConfigError("Contrainte non respectée : 0 < RISK_MIN_LOT <= RISK_MAX_LOT")


@dataclass(frozen=True)
class NewsConfig:
    newsapi_key: str = field(repr=False)
    finnhub_api_key: str = field(repr=False)
    alphavantage_api_key: str = field(repr=False)
    rss_feeds: tuple[str, ...]       # flux personnels, ajoutés au catalogue intégré
    refresh_interval_seconds: int
    lookback_hours: int
    request_timeout_seconds: int
    use_default_feeds: bool          # catalogue intégré (Bloomberg, Reuters, FXStreet, banques centrales...)
    max_per_feed: int                # équilibrage : articles retenus au plus par flux

    @property
    def has_any_source(self) -> bool:
        return bool(self.use_default_feeds or self.rss_feeds or self.newsapi_key or self.finnhub_api_key
                    or self.alphavantage_api_key)

    def validate(self) -> None:
        if self.refresh_interval_seconds < 30:
            raise ConfigError("NEWS_REFRESH_INTERVAL_SECONDS doit être >= 30 (respect des quotas API)")
        if self.lookback_hours < 1:
            raise ConfigError("NEWS_LOOKBACK_HOURS doit être >= 1")
        if not 1 <= self.max_per_feed <= 100:
            raise ConfigError("NEWS_MAX_PER_FEED doit être compris entre 1 et 100")


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


API_KEY_MIN_LENGTH = 16
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


@dataclass(frozen=True)
class ApiConfig:
    enabled: bool
    host: str
    port: int
    api_key: str = field(repr=False)
    cors_origins: tuple[str, ...]
    webhook_secret: str = field(default="", repr=False)   # vide = réception de webhooks désactivée

    @property
    def is_local_only(self) -> bool:
        return self.host in LOOPBACK_HOSTS

    @property
    def webhooks_enabled(self) -> bool:
        return bool(self.webhook_secret)

    def validate(self) -> None:
        if not self.enabled:
            return
        if self.webhook_secret and len(self.webhook_secret) < API_KEY_MIN_LENGTH:
            raise ConfigError(f"WEBHOOK_SECRET doit faire au moins {API_KEY_MIN_LENGTH} caractères")
        if self.webhook_secret and self.webhook_secret == self.api_key:
            raise ConfigError("WEBHOOK_SECRET doit être différent d'API_KEY (il est collé dans TradingView)")
        if len(self.api_key) < API_KEY_MIN_LENGTH:
            raise ConfigError(f"API_ENABLED=true nécessite une API_KEY d'au moins {API_KEY_MIN_LENGTH} caractères")
        if not 1 <= self.port <= 65535:
            raise ConfigError(f"API_PORT invalide : {self.port}")
        if "*" in self.cors_origins:
            raise ConfigError("API_CORS_ORIGINS ne doit pas contenir '*' : listez les origines du dashboard")


DISCORD_WEBHOOK_PREFIXES = ("https://discord.com/api/webhooks/", "https://discordapp.com/api/webhooks/")


@dataclass(frozen=True)
class NotifierConfig:
    telegram_bot_token: str = field(repr=False)
    telegram_chat_id: str
    discord_webhook_url: str = field(repr=False)
    cooldown_seconds: int
    timeout_seconds: int

    @property
    def channels(self) -> list[str]:
        names = []
        if self.telegram_bot_token and self.telegram_chat_id:
            names.append("Telegram")
        if self.discord_webhook_url:
            names.append("Discord")
        return names

    def validate(self) -> None:
        if bool(self.telegram_bot_token) != bool(self.telegram_chat_id):
            raise ConfigError("TELEGRAM_BOT_TOKEN et TELEGRAM_CHAT_ID doivent être renseignés ensemble")
        if self.discord_webhook_url and not self.discord_webhook_url.startswith(DISCORD_WEBHOOK_PREFIXES):
            raise ConfigError("DISCORD_WEBHOOK_URL doit commencer par https://discord.com/api/webhooks/")
        if self.cooldown_seconds < 0:
            raise ConfigError("NOTIFY_COOLDOWN_SECONDS doit être >= 0")
        if not 1 <= self.timeout_seconds <= 60:
            raise ConfigError("NOTIFY_TIMEOUT_SECONDS doit être compris entre 1 et 60")


@dataclass(frozen=True)
class DatabaseConfig:
    path: Path
    retention_days: int

    def validate(self) -> None:
        if self.retention_days < 1:
            raise ConfigError("DB_RETENTION_DAYS doit être >= 1")


@dataclass(frozen=True)
class InstanceConfig:
    """Identité du processus : compte unique, superviseur multi-comptes, ou compte géré par un superviseur."""
    account_id: str                  # vide en mode compte unique
    label: str

    @property
    def is_worker(self) -> bool:
        return bool(self.account_id)


@dataclass(frozen=True)
class Settings:
    dry_run: bool                    # mode au démarrage : True = Paper, False = Live (modifiable via l'API)
    loop_interval_seconds: int
    mt5: MT5Config
    trading: TradingConfig
    risk: RiskConfig
    news: NewsConfig
    sentiment: SentimentConfig
    strategy: StrategyConfig
    log: LogConfig
    api: ApiConfig
    notifier: NotifierConfig
    database: DatabaseConfig
    instance: InstanceConfig
    accounts: tuple[AccountSpec, ...] = ()     # non vide = superviseur multi-comptes

    @property
    def mode(self) -> str:
        return "PAPER" if self.dry_run else "LIVE"

    def validate(self) -> None:
        if self.loop_interval_seconds < 1:
            raise ConfigError("BOT_LOOP_INTERVAL_SECONDS doit être >= 1")
        validate_accounts(self.accounts)
        if not self.accounts:                  # le superviseur ne se connecte à aucun terminal
            self.mt5.validate()
        for section in (self.trading, self.risk, self.news,
                        self.sentiment, self.strategy, self.log, self.api, self.notifier,
                        self.database):
            section.validate()

    def summary(self) -> str:
        """Résumé lisible de la configuration, secrets masqués."""
        return "\n".join([
            f"  Mode            : {'PAPER / DRY RUN (aucun ordre réel)' if self.dry_run else 'LIVE'}",
            f"  Compte MT5      : {self.mt5.account} @ {self.mt5.server}",
            f"  Symboles        : {', '.join(self.trading.symbols)} ({self.trading.timeframe})",
            f"  Risque/trade    : {self.risk.risk_per_trade_pct}% | Perte max/jour : {self.risk.max_daily_loss_pct}%",
            f"  Positions max   : {self.risk.max_open_positions} (par symbole : {self.risk.max_positions_per_symbol})"
            f" | risque cumulé max {self.risk.max_total_risk_pct}% | marge max {self.risk.max_margin_usage_pct}%",
            f"  Sentiment       : {self.sentiment.engine}",
            f"  Pondération     : technique {self.strategy.technical_weight} / sentiment {self.strategy.sentiment_weight}",
            f"  Sources news    : {'catalogue intégré + ' if self.news.use_default_feeds else ''}"
            f"{len(self.news.rss_feeds)} RSS perso | NewsAPI {mask(self.news.newsapi_key)}"
            f" | Finnhub {mask(self.news.finnhub_api_key)} | AlphaVantage {mask(self.news.alphavantage_api_key)}",
            f"  Notifications   : {', '.join(self.notifier.channels) or 'désactivées'}",
            f"  Historique      : {self.database.path} (conservé {self.database.retention_days} jours)",
        ])


def load_settings() -> Settings:
    """Construit et valide la configuration à partir de l'environnement."""
    log_dir = Path(get_str("LOG_DIR", "logs"))
    if not log_dir.is_absolute():
        log_dir = BASE_DIR / log_dir
    db_path = Path(get_str("DB_PATH", "data/trading_bot.db"))
    if not db_path.is_absolute():
        db_path = BASE_DIR / db_path

    accounts = load_accounts()
    cfg = Settings(
        dry_run=get_bool("DRY_RUN", True),
        loop_interval_seconds=get_int("BOT_LOOP_INTERVAL_SECONDS", 60),
        mt5=load_mt5_credentials(required=not accounts),
        trading=TradingConfig(
            symbols=tuple(s.upper() for s in get_list("TRADING_SYMBOLS", "EURUSD")),
            timeframe=get_str("TRADING_TIMEFRAME", "M15").upper(),
            bars_history=get_int("TRADING_BARS_HISTORY", 500),
            magic_number=get_int("TRADING_MAGIC_NUMBER", 20261026),
            deviation_points=get_int("TRADING_DEVIATION_POINTS", 20),
        ),
        risk=RiskConfig(
            risk_per_trade_pct=get_float("RISK_PER_TRADE_PCT", 1.0),
            max_daily_loss_pct=get_float("RISK_MAX_DAILY_LOSS_PCT", 3.0),
            max_consecutive_losses=get_int("RISK_MAX_CONSECUTIVE_LOSSES", 3),
            max_open_positions=get_int("RISK_MAX_OPEN_POSITIONS", 3),
            max_positions_per_symbol=get_int("RISK_MAX_POSITIONS_PER_SYMBOL", 1),
            max_total_risk_pct=get_float("RISK_MAX_TOTAL_RISK_PCT", 3.0),
            max_margin_usage_pct=get_float("RISK_MAX_MARGIN_USAGE_PCT", 50.0),
            max_spread_points=get_int("RISK_MAX_SPREAD_POINTS", 30),
            sl_atr_multiplier=get_float("RISK_SL_ATR_MULTIPLIER", 1.5),
            tp_atr_multiplier=get_float("RISK_TP_ATR_MULTIPLIER", 3.0),
            min_lot=get_float("RISK_MIN_LOT", 0.01),
            max_lot=get_float("RISK_MAX_LOT", 1.0),
        ),
        news=NewsConfig(
            newsapi_key=get_str("NEWSAPI_KEY"),
            finnhub_api_key=get_str("FINNHUB_API_KEY"),
            alphavantage_api_key=get_str("ALPHAVANTAGE_API_KEY"),
            rss_feeds=get_list("NEWS_RSS_FEEDS"),
            refresh_interval_seconds=get_int("NEWS_REFRESH_INTERVAL_SECONDS", 300),
            lookback_hours=get_int("NEWS_LOOKBACK_HOURS", 6),
            request_timeout_seconds=get_int("NEWS_REQUEST_TIMEOUT_SECONDS", 10),
            use_default_feeds=get_bool("NEWS_DEFAULT_FEEDS", True),
            max_per_feed=get_int("NEWS_MAX_PER_FEED", 15),
        ),
        sentiment=SentimentConfig(
            engine=get_str("SENTIMENT_ENGINE", "vader").lower(),
            anthropic_api_key=get_str("ANTHROPIC_API_KEY"),
            anthropic_model=get_str("ANTHROPIC_MODEL", "claude-haiku-5-5"),
        ),
        strategy=StrategyConfig(
            technical_weight=get_float("STRATEGY_TECHNICAL_WEIGHT", 0.7),
            sentiment_weight=get_float("STRATEGY_SENTIMENT_WEIGHT", 0.3),
            signal_threshold=get_float("STRATEGY_SIGNAL_THRESHOLD", 0.6),
        ),
        log=LogConfig(
            level=get_str("LOG_LEVEL", "INFO").upper(),
            directory=log_dir,
            max_bytes=get_int("LOG_MAX_BYTES", 5 * 1024 * 1024),
            backup_count=get_int("LOG_BACKUP_COUNT", 10),
        ),
        api=ApiConfig(
            enabled=get_bool("API_ENABLED", False),
            host=get_str("API_HOST", "127.0.0.1"),
            port=get_int("API_PORT", 8000),
            api_key=get_str("API_KEY"),
            cors_origins=get_list("API_CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173"),
            webhook_secret=get_str("WEBHOOK_SECRET"),
        ),
        notifier=NotifierConfig(
            telegram_bot_token=get_str("TELEGRAM_BOT_TOKEN"),
            telegram_chat_id=get_str("TELEGRAM_CHAT_ID"),
            discord_webhook_url=get_str("DISCORD_WEBHOOK_URL"),
            cooldown_seconds=get_int("NOTIFY_COOLDOWN_SECONDS", 900),
            timeout_seconds=get_int("NOTIFY_TIMEOUT_SECONDS", 10),
        ),
        instance=InstanceConfig(
            account_id=get_str("BOT_ACCOUNT_ID"),
            label=get_str("BOT_ACCOUNT_LABEL"),
        ),
        accounts=accounts,
        database=DatabaseConfig(
            path=db_path,
            retention_days=get_int("DB_RETENTION_DAYS", 90),
        ),
    )
    cfg.validate()
    return cfg


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
