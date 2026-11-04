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
from config.broker import BrokerConfig, load_broker_config
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

# Profils de stratégie : valeurs par défaut des réglages concernés. Une variable renseignée
# dans le .env reste prioritaire ; vide ou absente, la valeur du profil s'applique.
#   standard : swing intraday (M15), signaux rares et confirmés, objectifs larges
#   scalping : M1, indicateurs rapides, seuils bas, SL/TP serrés, boucle toutes les 10 s
#   micro    : M1, aucun seuil (le moindre signal technique ouvre un trade), sentiment ignoré,
#              SL et TP fixes de 5 pips
#   adaptive : choisit à chaque tour, par symbole, entre scalping (M1), day trading (M15/H1)
#              et swing (H1/D1) selon la session, la volatilité et la tendance de fond
#              (strategies/regime.py) ; les réglages ci-dessous sont ceux du mode scalping
TRAILING_MODES = ("bars", "atr", "r")       # risk/trailing.py

# Stratégies à setups disponibles (strategies/registry.py), choisies par SETUP_STRATEGIES
SETUP_STRATEGY_NAMES = ("pullback", "range_rejet", "momentum_breakout", "impulsion", "petits_trades")
# Multiplicateur de la marge de protection b par actif (breakout momentum) : actifs à fortes mèches
DEFAULT_MARGIN_MULTIPLIERS = "XAUUSD:1.5,XAGUSD:1.5"
MAX_MARGIN_MULTIPLIER = 5.0


def parse_multipliers(raw: str) -> tuple[tuple[str, float], ...]:
    """« XAUUSD:1.5,XAGUSD:2 » -> (("XAUUSD", 1.5), ("XAGUSD", 2.0))."""
    pairs = []
    for item in (part.strip() for part in raw.split(",")):
        if not item:
            continue
        symbol, sep, value = item.partition(":")
        try:
            if not sep:
                raise ValueError
            pairs.append((symbol.strip().upper(), float(value.replace(" ", ""))))
        except ValueError:
            raise ConfigError(f"STRATEGY_MARGIN_MULTIPLIERS invalide : « {item} » (attendu : SYMBOLE:coefficient)") from None
    return tuple(pairs)

STRATEGY_PROFILES: dict[str, dict[str, str]] = {
    "standard": {
        "TRADING_TIMEFRAME": "M15", "BOT_LOOP_INTERVAL_SECONDS": "60",
        "STRATEGY_TECHNICAL_WEIGHT": "0.7", "STRATEGY_SENTIMENT_WEIGHT": "0.3",
        "STRATEGY_SIGNAL_THRESHOLD": "0.6", "STRATEGY_TECHNICAL_THRESHOLD": "0.3",
        "STRATEGY_MIN_SL_SPREADS": "0", "STRATEGY_STRUCTURAL_SL": "true",
        "RISK_SL_ATR_MULTIPLIER": "1.5", "RISK_TP_ATR_MULTIPLIER": "3.0",
        "STRATEGY_SL_PIPS": "0", "STRATEGY_TP_PIPS": "0",
    },
    "scalping": {
        "TRADING_TIMEFRAME": "M1", "BOT_LOOP_INTERVAL_SECONDS": "10",
        # le sentiment ne bloque plus un signal technique : il ne fait que le nuancer
        "STRATEGY_TECHNICAL_WEIGHT": "0.85", "STRATEGY_SENTIMENT_WEIGHT": "0.15",
        "STRATEGY_SIGNAL_THRESHOLD": "0.2", "STRATEGY_TECHNICAL_THRESHOLD": "0.2",
        # SL au moins 3 spreads : en dessous, le coût d'entrée mange l'essentiel du gain visé
        "STRATEGY_MIN_SL_SPREADS": "3", "STRATEGY_STRUCTURAL_SL": "false",
        "RISK_SL_ATR_MULTIPLIER": "1.0", "RISK_TP_ATR_MULTIPLIER": "1.2",
        "STRATEGY_SL_PIPS": "0", "STRATEGY_TP_PIPS": "0",
    },
    "micro": {
        "TRADING_TIMEFRAME": "M1", "BOT_LOOP_INTERVAL_SECONDS": "10",
        # seuils à 0 : tout score technique non nul donne un BUY / SELL ; le sentiment ne compte plus
        "STRATEGY_TECHNICAL_WEIGHT": "1.0", "STRATEGY_SENTIMENT_WEIGHT": "0.0",
        "STRATEGY_SIGNAL_THRESHOLD": "0", "STRATEGY_TECHNICAL_THRESHOLD": "0",
        "STRATEGY_MIN_SL_SPREADS": "3", "STRATEGY_STRUCTURAL_SL": "false",
        # SL / TP fixes en pips (prioritaires sur les multiplicateurs ATR, conservés pour référence)
        "RISK_SL_ATR_MULTIPLIER": "1.0", "RISK_TP_ATR_MULTIPLIER": "1.0",
        "STRATEGY_SL_PIPS": "5", "STRATEGY_TP_PIPS": "5",
    },
    "adaptive": {
        "TRADING_TIMEFRAME": "M1", "BOT_LOOP_INTERVAL_SECONDS": "10",
        "STRATEGY_TECHNICAL_WEIGHT": "1.0", "STRATEGY_SENTIMENT_WEIGHT": "0.0",
        "STRATEGY_SIGNAL_THRESHOLD": "0", "STRATEGY_TECHNICAL_THRESHOLD": "0",
        "STRATEGY_MIN_SL_SPREADS": "3", "STRATEGY_STRUCTURAL_SL": "false",
        "RISK_SL_ATR_MULTIPLIER": "1.0", "RISK_TP_ATR_MULTIPLIER": "1.0",
        "STRATEGY_SL_PIPS": "5", "STRATEGY_TP_PIPS": "5",
    },
}


def _parse_hhmm(value: str, name: str) -> int:
    """« 07:30 » -> minutes depuis 00:00 UTC."""
    try:
        hours, minutes = value.strip().split(":")
        total = int(hours) * 60 + int(minutes)
    except ValueError as exc:
        raise ConfigError(f"{name} : heure invalide {value!r} (format HH:MM)") from exc
    if not 0 <= total < 24 * 60:
        raise ConfigError(f"{name} : heure hors de 00:00-23:59 ({value!r})")
    return total


def _parse_window(value: str, name: str) -> tuple[int, int]:
    """« 07:00-10:00 » -> (420, 600)."""
    try:
        start, end = value.split("-")
    except ValueError as exc:
        raise ConfigError(f"{name} : plage invalide {value!r} (format HH:MM-HH:MM)") from exc
    window = (_parse_hhmm(start, name), _parse_hhmm(end, name))
    if window[0] >= window[1]:
        raise ConfigError(f"{name} : la plage {value!r} doit finir après son début (même journée UTC)")
    return window


@dataclass(frozen=True)
class LearningConfig:
    """Journal des trades, analyse des pertes et mémoire des erreurs (learning/trade_memory.py)."""
    enabled: bool
    directory: Path                  # trade_decisions.json, loss_analysis.json, mistakes_memory.json
    min_losses: int                  # pertes sur un même pattern avant blocage
    min_loss_rate: float             # part de pertes minimale sur ce pattern (pertes / trades clos)
    memory_days: int                 # un pattern sans nouvelle perte depuis N jours ne bloque plus

    def validate(self) -> None:
        if self.min_losses < 1:
            raise ConfigError("MISTAKE_MIN_LOSSES doit être >= 1")
        if not 0 < self.min_loss_rate <= 1:
            raise ConfigError("MISTAKE_MIN_LOSS_RATE doit être compris entre 0 et 1")
        if self.memory_days < 1:
            raise ConfigError("MISTAKE_MEMORY_DAYS doit être >= 1")


def load_learning_config(account_id: str) -> LearningConfig:
    directory = Path(get_str("LEARNING_DIR") or "data/learning")
    if not directory.is_absolute():
        directory = BASE_DIR / directory
    if account_id:                   # multi-comptes : une mémoire par compte
        directory = directory / account_id
    return LearningConfig(
        enabled=get_bool("LEARNING_ENABLED", True),
        directory=directory,
        min_losses=get_int("MISTAKE_MIN_LOSSES", 2),
        min_loss_rate=get_float("MISTAKE_MIN_LOSS_RATE", 0.6),
        memory_days=get_int("MISTAKE_MEMORY_DAYS", 30),
    )


@dataclass(frozen=True)
class AdaptiveConfig:
    """Fenêtres horaires (UTC) du profil adaptive."""
    day_windows: tuple[tuple[int, int], ...]   # ouvertures de sessions : day trading
    liquid_hours: tuple[int, int]              # heures liquides : scalping possible
    day_close: int                             # fermeture obligatoire des positions day trading

    def validate(self) -> None:
        if not self.day_windows:
            raise ConfigError("ADAPTIVE_DAY_WINDOWS doit contenir au moins une plage")
        if any(end > self.day_close for _, end in self.day_windows):
            raise ConfigError("ADAPTIVE_DAY_CLOSE doit être après la fin des fenêtres de day trading")


def load_adaptive_config() -> AdaptiveConfig:
    windows = get_str("ADAPTIVE_DAY_WINDOWS") or "07:00-10:00,12:30-15:30"
    return AdaptiveConfig(
        day_windows=tuple(_parse_window(w, "ADAPTIVE_DAY_WINDOWS") for w in windows.split(",") if w.strip()),
        liquid_hours=_parse_window(get_str("ADAPTIVE_LIQUID_HOURS") or "07:00-20:00", "ADAPTIVE_LIQUID_HOURS"),
        day_close=_parse_hhmm(get_str("ADAPTIVE_DAY_CLOSE") or "20:30", "ADAPTIVE_DAY_CLOSE"),
    )


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
    min_lot: float                   # bornes en lots : Forex / CFD (MT5) uniquement
    max_lot: float
    max_spread_pct: float = 0.15     # crypto : spread maximal en % du prix (les « points » n'y sont pas comparables)
    max_spread_sl_pct: float = 10.0  # spread maximal en % de la distance entrée - stop loss
    max_spread_median_ratio: float = 2.0   # spread maximal en multiple de sa médiane sur 60 minutes
    min_net_reward_risk: float = 3.0       # ratio net estimé minimal (vérificateur indépendant)
    allow_manual_real_account: bool = False  # bouton « Exécuter en Live » autorisé sur un compte RÉEL
    max_currency_risk_pct: float = 1.5     # risque cumulé exposé à une même devise dans le même sens (corrélation)
    max_drawdown_pct: float = 10.0         # baisse depuis le plus haut des fonds propres : suspension (0 = aucune)

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
        if not 0 < self.max_spread_pct <= 2:
            raise ConfigError("RISK_MAX_SPREAD_PCT doit être compris entre 0 et 2 (%)")
        if not 0 < self.max_spread_sl_pct <= 100:
            raise ConfigError("RISK_MAX_SPREAD_SL_PCT doit être compris entre 0 et 100 (%)")
        if self.max_spread_median_ratio < 1:
            raise ConfigError("RISK_MAX_SPREAD_MEDIAN_RATIO doit être >= 1")
        if not 0 < self.min_net_reward_risk <= 20:
            raise ConfigError("RISK_MIN_NET_REWARD_RISK doit être compris entre 0 et 20")
        if not self.risk_per_trade_pct <= self.max_currency_risk_pct <= self.max_total_risk_pct:
            raise ConfigError("RISK_MAX_CURRENCY_RISK_PCT doit être compris entre RISK_PER_TRADE_PCT et "
                              "RISK_MAX_TOTAL_RISK_PCT (%)")
        if not 0 <= self.max_drawdown_pct <= 50:
            raise ConfigError("RISK_MAX_DRAWDOWN_PCT doit être compris entre 0 (désactivé) et 50 (%)")


SHIELD_ACTIONS = ("none", "breakeven", "reduce", "close")


@dataclass(frozen=True)
class ExitConfig:
    """Sorties partielles (TP1 / TP2, le TP du broker étant le TP final) et mode Bouclier sur annonce."""
    tp1_r: float = 1.0                     # TP1 à 1 R du prix d'entrée
    tp1_fraction: float = 0.0              # part du volume fermée au TP1 (0 = sorties partielles désactivées)
    tp2_r: float = 2.0
    tp2_fraction: float = 0.0              # part du volume INITIAL fermée au TP2 (0 = pas de TP2)
    breakeven_after_tp1: bool = True       # stop ramené au prix d'entrée (+ spread) après le TP1
    shield_action: str = "none"            # avant une annonce forte : none, breakeven, reduce, close
    shield_reduce_fraction: float = 0.5
    shield_close_contrary: bool = True     # fermer une position invalidée par une publication forte à contre-sens
    shield_contrary_strength: float = 0.66 # force d'effet minimale pour juger le scénario invalidé

    @property
    def partial_exits(self) -> bool:
        return self.tp1_fraction > 0

    def validate(self) -> None:
        if not 0 <= self.tp1_fraction < 1 or not 0 <= self.tp2_fraction < 1:
            raise ConfigError("EXIT_TP1_FRACTION et EXIT_TP2_FRACTION doivent être compris entre 0 et 1 (exclu)")
        if self.tp1_fraction + self.tp2_fraction >= 1:
            raise ConfigError("EXIT_TP1_FRACTION + EXIT_TP2_FRACTION doit rester < 1 : le reste vise le TP final")
        if self.tp2_fraction and not self.tp1_fraction:
            raise ConfigError("EXIT_TP2_FRACTION exige un TP1 (EXIT_TP1_FRACTION > 0)")
        if not 0 < self.tp1_r < self.tp2_r:
            raise ConfigError("Contrainte non respectée : 0 < EXIT_TP1_R < EXIT_TP2_R")
        if self.shield_action not in SHIELD_ACTIONS:
            raise ConfigError(f"SHIELD_ACTION doit valoir {', '.join(SHIELD_ACTIONS)}")
        if not 0 < self.shield_reduce_fraction < 1:
            raise ConfigError("SHIELD_REDUCE_FRACTION doit être compris entre 0 et 1 (exclus)")
        if not 0 < self.shield_contrary_strength <= 1:
            raise ConfigError("SHIELD_CONTRARY_STRENGTH doit être compris entre 0 et 1")


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
    block_minutes_before: int = 30   # fenêtre de sécurité autour des annonces majeures
    block_minutes_after: int = 15
    calendar_provider: str = "forexfactory"   # forexfactory | simulated
    calendar_file: Path | None = None         # événements du fournisseur simulé (JSON)

    @property
    def has_any_source(self) -> bool:
        return bool(self.use_default_feeds or self.rss_feeds or self.newsapi_key or self.finnhub_api_key
                    or self.alphavantage_api_key)

    def validate(self) -> None:
        if not 0 <= self.block_minutes_before <= 240 or not 0 <= self.block_minutes_after <= 240:
            raise ConfigError("NEWS_BLOCK_MINUTES_BEFORE / AFTER doivent être compris entre 0 et 240")
        if self.calendar_provider not in ("forexfactory", "simulated"):
            raise ConfigError("NEWS_CALENDAR_PROVIDER : forexfactory ou simulated")
        if self.calendar_file is not None and not self.calendar_file.exists():
            raise ConfigError(f"NEWS_CALENDAR_FILE introuvable : {self.calendar_file}")
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
    profile: str = "standard"
    technical_threshold: float = 0.3     # |score technique| minimal pour un signal BUY / SELL
    min_sl_spreads: float = 0.0          # distance SL minimale, en multiples du spread (0 = aucune)
    structural_sl: bool = True           # élargir le SL jusqu'au dernier plus bas / plus haut
    sl_pips: float = 0.0                 # SL / TP fixes en pips (0 = distances ATR)
    tp_pips: float = 0.0
    setup_strategies: tuple[str, ...] = SETUP_STRATEGY_NAMES   # mode analyste, dashboard, backtest
    margin_multipliers: tuple[tuple[str, float], ...] = parse_multipliers(DEFAULT_MARGIN_MULTIPLIERS)
    momentum_restart_body_atr: float = 0.0     # corps minimal de la relance en ATR(5) ; 0 = filtre désactivé
    momentum_target_rr: float = 3.0            # ratio net visé par le TP du breakout momentum
    momentum_breakeven_r: float = 0.0             # stop ramené à l'entrée dès +X R de gain latent ; 0 = désactivé
    momentum_trailing_activation_r: float = 0.0   # trailing stop dès +X R de gain latent ; 0 = désactivé
    momentum_trailing_mode: str = "bars"          # bars (plus bas des N bougies), atr (k x ATR), r (distance en R)
    momentum_trailing_bars: int = 3
    momentum_trailing_atr: float = 1.5
    momentum_trailing_distance_r: float = 1.0
    momentum_min_h1_adx: float = 22.0             # ADX(14) H1 minimal ; 0 = filtre désactivé
    momentum_h1_lookback: int = 20                # bougies H1 pour l'obstacle (plus haut / plus bas)
    momentum_h1_obstacle_atr: float = 1.0         # marge minimale jusqu'à l'obstacle H1, en ATR H1 ; 0 = désactivé
    momentum_h1_ema_alignment: bool = True        # EMA rapide au-dessus de la lente pour acheter (inverse pour vendre)
    momentum_h1_ema_fast: int = 50
    momentum_h1_ema_slow: int = 200
    # Impulsion (strategies/impulse.py) : rattrapage d'un mouvement violent et propre en cours
    impulse_enabled: bool = False                 # le moteur ouvre ces trades quand sa stratégie dit HOLD
    impulse_timeframe: str = "M15"
    impulse_bars: int = 3
    impulse_min_body_atr: float = 0.4
    impulse_max_wick_pct: float = 35.0
    impulse_min_move_atr: float = 1.5
    impulse_max_move_atr: float = 4.0
    impulse_sl_atr: float = 1.5
    impulse_session_only: bool = True
    # Petits trades / micro-scalping (strategies/micro_scalp.py) : objectif court, stop serré
    scalp_enabled: bool = False                   # le moteur ouvre ces trades quand sa stratégie dit HOLD
    scalp_tp_r: float = 1.2                       # ratio net visé (et exigé par le vérificateur pour CE mode)
    scalp_sl_atr: float = 1.0
    scalp_timeframe: str = "M5"
    scalp_range_bars: int = 12
    scalp_max_range_atr: float = 2.0
    # Liste blanche des trades d'opportunité (impulsion, petits trades) ; vide = tous les symboles suivis
    opportunity_symbols: tuple[str, ...] = ()

    def opportunity_allowed(self, symbol: str) -> bool:
        return not self.opportunity_symbols or symbol.upper() in self.opportunity_symbols

    def margin_multiplier(self, symbol: str) -> float:
        """Coefficient de la marge b pour `symbol` (1.0 : calcul standard)."""
        return dict(self.margin_multipliers).get(symbol.upper(), 1.0)

    def validate(self) -> None:
        if self.profile not in STRATEGY_PROFILES:
            raise ConfigError(f"STRATEGY_PROFILE invalide : {self.profile} (attendu : {', '.join(STRATEGY_PROFILES)})")
        if not 0 <= self.technical_threshold <= 1:
            raise ConfigError("STRATEGY_TECHNICAL_THRESHOLD doit être compris entre 0 et 1")
        if not 0 <= self.min_sl_spreads <= 20:
            raise ConfigError("STRATEGY_MIN_SL_SPREADS doit être compris entre 0 et 20")
        if abs(self.technical_weight + self.sentiment_weight - 1.0) > 1e-6:
            raise ConfigError("STRATEGY_TECHNICAL_WEIGHT + STRATEGY_SENTIMENT_WEIGHT doit valoir 1.0")
        if not 0 <= self.momentum_restart_body_atr <= 3:
            raise ConfigError("STRATEGY_MOMENTUM_RESTART_BODY_ATR doit être compris entre 0 (désactivé) et 3")
        if not 1 <= self.momentum_target_rr <= 10:
            raise ConfigError("STRATEGY_MOMENTUM_TARGET_RR doit être compris entre 1 et 10")
        for name, value in (("STRATEGY_MOMENTUM_BREAKEVEN_R", self.momentum_breakeven_r),
                            ("STRATEGY_MOMENTUM_TRAILING_ACTIVATION_R", self.momentum_trailing_activation_r)):
            if value and not 0.5 <= value <= 10:
                raise ConfigError(f"{name} doit valoir 0 (désactivé) ou être compris entre 0.5 et 10")
        if self.momentum_trailing_mode not in TRAILING_MODES:
            raise ConfigError(f"STRATEGY_MOMENTUM_TRAILING_MODE invalide : {self.momentum_trailing_mode} "
                              f"(attendu : {', '.join(TRAILING_MODES)})")
        if not 1 <= self.momentum_trailing_bars <= 50:
            raise ConfigError("STRATEGY_MOMENTUM_TRAILING_BARS doit être compris entre 1 et 50")
        if not 0.1 <= self.momentum_trailing_atr <= 10:
            raise ConfigError("STRATEGY_MOMENTUM_TRAILING_ATR doit être compris entre 0.1 et 10")
        if not 0.1 <= self.momentum_trailing_distance_r <= 5:
            raise ConfigError("STRATEGY_MOMENTUM_TRAILING_DISTANCE_R doit être compris entre 0.1 et 5")
        if not 0 <= self.momentum_min_h1_adx <= 60:
            raise ConfigError("STRATEGY_MOMENTUM_MIN_H1_ADX doit être compris entre 0 (désactivé) et 60")
        if not 5 <= self.momentum_h1_lookback <= 200:
            raise ConfigError("STRATEGY_MOMENTUM_H1_LOOKBACK doit être compris entre 5 et 200")
        if not 0 <= self.momentum_h1_obstacle_atr <= 10:
            raise ConfigError("STRATEGY_MOMENTUM_H1_OBSTACLE_ATR doit être compris entre 0 (désactivé) et 10")
        if not 1.0 <= self.scalp_tp_r <= 3.0:
            raise ConfigError("STRATEGY_SCALP_TP_R doit être compris entre 1.0 et 3.0")
        if not 0.3 <= self.scalp_sl_atr <= 3.0:
            raise ConfigError("STRATEGY_SCALP_SL_ATR doit être compris entre 0.3 et 3.0 (stop loss obligatoire)")
        if self.scalp_timeframe not in ("M1", "M5"):
            raise ConfigError("STRATEGY_SCALP_TIMEFRAME doit valoir M1 ou M5")
        if not 4 <= self.scalp_range_bars <= 60:
            raise ConfigError("STRATEGY_SCALP_RANGE_BARS doit être compris entre 4 et 60")
        if not 0.5 <= self.scalp_max_range_atr <= 10:
            raise ConfigError("STRATEGY_SCALP_MAX_RANGE_ATR doit être compris entre 0.5 et 10")
        if self.impulse_timeframe not in ("M5", "M15"):
            raise ConfigError("IMPULSE_TIMEFRAME doit valoir M5 ou M15")
        if not 2 <= self.impulse_bars <= 6:
            raise ConfigError("IMPULSE_BARS doit être compris entre 2 et 6")
        if not 0 < self.impulse_min_move_atr < self.impulse_max_move_atr <= 20:
            raise ConfigError("IMPULSE_MIN_MOVE_ATR / IMPULSE_MAX_MOVE_ATR : 0 < minimum < maximum <= 20")
        if not 0 <= self.impulse_min_body_atr <= 3:
            raise ConfigError("IMPULSE_MIN_BODY_ATR doit être compris entre 0 et 3")
        if not 0 < self.impulse_max_wick_pct <= 100:
            raise ConfigError("IMPULSE_MAX_WICK_PCT doit être compris entre 0 (exclu) et 100")
        if not 0.3 <= self.impulse_sl_atr <= 5:
            raise ConfigError("IMPULSE_SL_ATR doit être compris entre 0.3 et 5 (stop loss obligatoire)")
        if not 2 <= self.momentum_h1_ema_fast < self.momentum_h1_ema_slow <= 390:
            raise ConfigError("STRATEGY_MOMENTUM_H1_EMA_FAST / _SLOW : 2 <= rapide < lente <= 390 "
                              "(400 bougies H1 chargées)")
        for symbol, factor in self.margin_multipliers:
            if not 1.0 <= factor <= MAX_MARGIN_MULTIPLIER:
                raise ConfigError(f"STRATEGY_MARGIN_MULTIPLIERS : coefficient de {symbol} hors de [1, "
                                  f"{MAX_MARGIN_MULTIPLIER:g}] ({factor:g})")
        unknown = [name for name in self.setup_strategies if name not in SETUP_STRATEGY_NAMES]
        if unknown or not self.setup_strategies:
            raise ConfigError(f"SETUP_STRATEGIES invalide : {', '.join(unknown) or 'vide'} "
                              f"(disponibles : {', '.join(SETUP_STRATEGY_NAMES)})")
        if not 0 <= self.signal_threshold <= 1:
            raise ConfigError("STRATEGY_SIGNAL_THRESHOLD doit être compris entre 0 et 1")
        if self.sl_pips < 0 or self.tp_pips < 0 or (self.sl_pips > 0) != (self.tp_pips > 0):
            raise ConfigError("STRATEGY_SL_PIPS et STRATEGY_TP_PIPS : tous deux > 0, ou tous deux à 0 (distances ATR)")
        if self.tp_pips < self.sl_pips:
            raise ConfigError("STRATEGY_TP_PIPS doit être >= STRATEGY_SL_PIPS (le risk manager exige TP >= SL)")


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
    console: bool = False             # alertes aussi affichées dans la console du bot

    @property
    def channels(self) -> list[str]:
        names = ["Console"] if self.console else []
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
    broker: BrokerConfig
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
    adaptive: AdaptiveConfig = field(default_factory=lambda: AdaptiveConfig(((420, 600), (750, 930)), (420, 1200), 1230))
    learning: LearningConfig = field(default_factory=lambda: LearningConfig(False, BASE_DIR / "data" / "learning", 2, 0.6, 30))
    exits: ExitConfig = field(default_factory=ExitConfig)

    @property
    def mode(self) -> str:
        return "PAPER" if self.dry_run else "LIVE"

    def validate(self) -> None:
        if self.loop_interval_seconds < 1:
            raise ConfigError("BOT_LOOP_INTERVAL_SECONDS doit être >= 1")
        validate_accounts(self.accounts)
        if not self.accounts:                  # le superviseur ne se connecte à aucun broker
            self.broker.validate()
            if self.broker.type == "mt5":
                self.mt5.validate()
        untracked = [s for s in self.strategy.opportunity_symbols if s not in self.trading.symbols]
        if untracked:
            raise ConfigError(f"OPPORTUNITY_SYMBOLS : {', '.join(untracked)} absent(s) de TRADING_SYMBOLS "
                              f"({', '.join(self.trading.symbols)})")
        if self.strategy.momentum_target_rr < self.risk.min_net_reward_risk:
            raise ConfigError(
                f"STRATEGY_MOMENTUM_TARGET_RR ({self.strategy.momentum_target_rr:g}) < RISK_MIN_NET_REWARD_RISK "
                f"({self.risk.min_net_reward_risk:g}) : le vérificateur indépendant refuserait tous les setups "
                "momentum ; abaisser aussi RISK_MIN_NET_REWARD_RISK pour tester un objectif plus proche")
        for section in (self.trading, self.risk, self.news,
                        self.sentiment, self.strategy, self.log, self.api, self.notifier,
                        self.database, self.adaptive, self.learning, self.exits):
            section.validate()

    def summary(self) -> str:
        """Résumé lisible de la configuration, secrets masqués."""
        return "\n".join([
            f"  Mode            : {'PAPER / DRY RUN (aucun ordre réel)' if self.dry_run else 'LIVE'}",
            f"  Broker          : {self.broker.summary()}",
            *(["  Compte MT5      : " + ("compte ouvert dans le terminal" if self.mt5.use_terminal_session
                                          else f"{self.mt5.account} @ {self.mt5.server}")]
              if self.broker.type == "mt5" else []),
            f"  Symboles        : {', '.join(self.trading.symbols)} ({self.trading.timeframe})",
            f"  Risque/trade    : {self.risk.risk_per_trade_pct}% | Perte max/jour : {self.risk.max_daily_loss_pct}%",
            f"  Positions max   : {self.risk.max_open_positions} (par symbole : {self.risk.max_positions_per_symbol})"
            f" | risque cumulé max {self.risk.max_total_risk_pct}% (par devise {self.risk.max_currency_risk_pct}%)"
            f" | marge max {self.risk.max_margin_usage_pct}%"
            f" | drawdown max {self.risk.max_drawdown_pct or 'désactivé'}%",
            "  Sorties         : " + (f"TP1 {self.exits.tp1_fraction:.0%} à {self.exits.tp1_r:g} R"
                                      + (f", TP2 {self.exits.tp2_fraction:.0%} à {self.exits.tp2_r:g} R"
                                         if self.exits.tp2_fraction else "") + ", reste au TP final"
                                      if self.exits.partial_exits else "TP unique")
            + f" | Bouclier avant annonce : {self.exits.shield_action}"
            + (" | fermeture si publication à contre-sens" if self.exits.shield_close_contrary else ""),
            f"  Sentiment       : {self.sentiment.engine}",
            f"  Stratégie       : profil {self.strategy.profile} | seuil hybride {self.strategy.signal_threshold}"
            f" | seuil technique {self.strategy.technical_threshold} | "
            + (f"SL {self.strategy.sl_pips:g} pips / TP {self.strategy.tp_pips:g} pips" if self.strategy.sl_pips
               else f"SL {self.risk.sl_atr_multiplier} ATR / TP {self.risk.tp_atr_multiplier} ATR")
            + f" | un tour toutes les {self.loop_interval_seconds} s",
            f"  Pondération     : technique {self.strategy.technical_weight} / sentiment {self.strategy.sentiment_weight}",
            f"  Sources news    : {'catalogue intégré + ' if self.news.use_default_feeds else ''}"
            f"{len(self.news.rss_feeds)} RSS perso | NewsAPI {mask(self.news.newsapi_key)}"
            f" | Finnhub {mask(self.news.finnhub_api_key)} | AlphaVantage {mask(self.news.alphavantage_api_key)}",
            f"  Notifications   : {', '.join(self.notifier.channels) or 'désactivées'}",
            f"  Historique      : {self.database.path} (conservé {self.database.retention_days} jours)",
        ])


def _profile_name() -> str:
    return (get_str("STRATEGY_PROFILE") or "standard").lower()


def _profiled(name: str) -> str:
    """Valeur du .env si elle est renseignée, sinon celle du profil de stratégie."""
    defaults = STRATEGY_PROFILES.get(_profile_name(), STRATEGY_PROFILES["standard"])
    return get_str(name) or defaults[name]


def load_settings() -> Settings:
    """Construit et valide la configuration à partir de l'environnement."""
    log_dir = Path(get_str("LOG_DIR", "logs"))
    if not log_dir.is_absolute():
        log_dir = BASE_DIR / log_dir
    db_path = Path(get_str("DB_PATH", "data/trading_bot.db"))
    if not db_path.is_absolute():
        db_path = BASE_DIR / db_path

    accounts = load_accounts()
    broker = load_broker_config()
    cfg = Settings(
        dry_run=get_bool("DRY_RUN", True),
        loop_interval_seconds=int(_profiled("BOT_LOOP_INTERVAL_SECONDS")),
        mt5=load_mt5_credentials(required=not accounts and broker.type == "mt5"),
        broker=broker,
        trading=TradingConfig(
            symbols=tuple(s.upper() for s in get_list("TRADING_SYMBOLS", "EURUSD")),
            timeframe=_profiled("TRADING_TIMEFRAME").upper(),
            bars_history=get_int("TRADING_BARS_HISTORY", 500),
            magic_number=get_int("TRADING_MAGIC_NUMBER", 20261026),
            deviation_points=get_int("TRADING_DEVIATION_POINTS", 20),
        ),
        risk=RiskConfig(
            risk_per_trade_pct=get_float("RISK_PER_TRADE_PCT", 1.0),
            max_daily_loss_pct=get_float("RISK_MAX_DAILY_LOSS_PCT", 3.0),
            allow_manual_real_account=get_bool("RISK_ALLOW_MANUAL_REAL_ACCOUNT", False),
            max_consecutive_losses=get_int("RISK_MAX_CONSECUTIVE_LOSSES", 2),
            max_open_positions=get_int("RISK_MAX_OPEN_POSITIONS", 2),
            max_positions_per_symbol=get_int("RISK_MAX_POSITIONS_PER_SYMBOL", 1),
            max_total_risk_pct=get_float("RISK_MAX_TOTAL_RISK_PCT", 2.0),
            max_margin_usage_pct=get_float("RISK_MAX_MARGIN_USAGE_PCT", 50.0),
            max_spread_points=get_int("RISK_MAX_SPREAD_POINTS", 30),
            sl_atr_multiplier=float(_profiled("RISK_SL_ATR_MULTIPLIER").replace(",", ".")),
            tp_atr_multiplier=float(_profiled("RISK_TP_ATR_MULTIPLIER").replace(",", ".")),
            min_lot=get_float("RISK_MIN_LOT", 0.01),
            max_lot=get_float("RISK_MAX_LOT", 1.0),
            max_spread_pct=get_float("RISK_MAX_SPREAD_PCT", 0.15),
            max_spread_sl_pct=get_float("RISK_MAX_SPREAD_SL_PCT", 10.0),
            max_spread_median_ratio=get_float("RISK_MAX_SPREAD_MEDIAN_RATIO", 2.0),
            min_net_reward_risk=get_float("RISK_MIN_NET_REWARD_RISK", 3.0),
            max_currency_risk_pct=get_float("RISK_MAX_CURRENCY_RISK_PCT", 1.5),
            max_drawdown_pct=get_float("RISK_MAX_DRAWDOWN_PCT", 10.0),
        ),
        exits=ExitConfig(
            tp1_r=get_float("EXIT_TP1_R", 1.0),
            tp1_fraction=get_float("EXIT_TP1_FRACTION", 0.0),
            tp2_r=get_float("EXIT_TP2_R", 2.0),
            tp2_fraction=get_float("EXIT_TP2_FRACTION", 0.0),
            breakeven_after_tp1=get_bool("EXIT_BREAKEVEN_AFTER_TP1", True),
            shield_action=(get_str("SHIELD_ACTION", "none") or "none").lower(),
            shield_reduce_fraction=get_float("SHIELD_REDUCE_FRACTION", 0.5),
            shield_close_contrary=get_bool("SHIELD_CLOSE_CONTRARY", True),
            shield_contrary_strength=get_float("SHIELD_CONTRARY_STRENGTH", 0.66),
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
            block_minutes_before=get_int("NEWS_BLOCK_MINUTES_BEFORE", 30),
            block_minutes_after=get_int("NEWS_BLOCK_MINUTES_AFTER", 15),
            calendar_provider=(get_str("NEWS_CALENDAR_PROVIDER") or "forexfactory").lower(),
            calendar_file=(BASE_DIR / get_str("NEWS_CALENDAR_FILE")) if get_str("NEWS_CALENDAR_FILE") else None,
        ),
        sentiment=SentimentConfig(
            engine=get_str("SENTIMENT_ENGINE", "vader").lower(),
            anthropic_api_key=get_str("ANTHROPIC_API_KEY"),
            anthropic_model=get_str("ANTHROPIC_MODEL", "claude-haiku-5-5"),
        ),
        strategy=StrategyConfig(
            technical_weight=float(_profiled("STRATEGY_TECHNICAL_WEIGHT").replace(",", ".")),
            sentiment_weight=float(_profiled("STRATEGY_SENTIMENT_WEIGHT").replace(",", ".")),
            signal_threshold=float(_profiled("STRATEGY_SIGNAL_THRESHOLD").replace(",", ".")),
            profile=_profile_name(),
            technical_threshold=float(_profiled("STRATEGY_TECHNICAL_THRESHOLD").replace(",", ".")),
            min_sl_spreads=float(_profiled("STRATEGY_MIN_SL_SPREADS").replace(",", ".")),
            structural_sl=_profiled("STRATEGY_STRUCTURAL_SL").lower() in ("1", "true", "yes", "on", "oui"),
            sl_pips=float(_profiled("STRATEGY_SL_PIPS").replace(",", ".")),
            tp_pips=float(_profiled("STRATEGY_TP_PIPS").replace(",", ".")),
            setup_strategies=tuple(name.lower() for name in get_list("SETUP_STRATEGIES", ",".join(SETUP_STRATEGY_NAMES))),
            margin_multipliers=parse_multipliers(get_str("STRATEGY_MARGIN_MULTIPLIERS", DEFAULT_MARGIN_MULTIPLIERS)),
            momentum_restart_body_atr=get_float("STRATEGY_MOMENTUM_RESTART_BODY_ATR", 0.0),
            momentum_target_rr=get_float("STRATEGY_MOMENTUM_TARGET_RR", 3.0),
            momentum_breakeven_r=get_float("STRATEGY_MOMENTUM_BREAKEVEN_R", 0.0),
            momentum_trailing_activation_r=get_float("STRATEGY_MOMENTUM_TRAILING_ACTIVATION_R", 0.0),
            momentum_trailing_mode=(get_str("STRATEGY_MOMENTUM_TRAILING_MODE", "bars") or "bars").lower(),
            momentum_trailing_bars=get_int("STRATEGY_MOMENTUM_TRAILING_BARS", 3),
            momentum_trailing_atr=get_float("STRATEGY_MOMENTUM_TRAILING_ATR", 1.5),
            momentum_trailing_distance_r=get_float("STRATEGY_MOMENTUM_TRAILING_DISTANCE_R", 1.0),
            momentum_min_h1_adx=get_float("STRATEGY_MOMENTUM_MIN_H1_ADX", 22.0),
            momentum_h1_lookback=get_int("STRATEGY_MOMENTUM_H1_LOOKBACK", 20),
            momentum_h1_obstacle_atr=get_float("STRATEGY_MOMENTUM_H1_OBSTACLE_ATR", 1.0),
            momentum_h1_ema_alignment=get_bool("STRATEGY_MOMENTUM_H1_EMA_ALIGNMENT", True),
            momentum_h1_ema_fast=get_int("STRATEGY_MOMENTUM_H1_EMA_FAST", 50),
            momentum_h1_ema_slow=get_int("STRATEGY_MOMENTUM_H1_EMA_SLOW", 200),
            impulse_enabled=get_bool("IMPULSE_ENABLED", False),
            impulse_timeframe=(get_str("IMPULSE_TIMEFRAME", "M15") or "M15").upper(),
            impulse_bars=get_int("IMPULSE_BARS", 3),
            impulse_min_body_atr=get_float("IMPULSE_MIN_BODY_ATR", 0.4),
            impulse_max_wick_pct=get_float("IMPULSE_MAX_WICK_PCT", 35.0),
            impulse_min_move_atr=get_float("IMPULSE_MIN_MOVE_ATR", 1.5),
            impulse_max_move_atr=get_float("IMPULSE_MAX_MOVE_ATR", 4.0),
            impulse_sl_atr=get_float("IMPULSE_SL_ATR", 1.5),
            impulse_session_only=get_bool("IMPULSE_SESSION_ONLY", True),
            scalp_enabled=get_bool("STRATEGY_SCALP_ENABLED", get_bool("STRATEGY_SCALP_MODE", False)),
            scalp_tp_r=get_float("STRATEGY_SCALP_TP_R", 1.2),
            scalp_sl_atr=get_float("STRATEGY_SCALP_SL_ATR", 1.0),
            scalp_timeframe=(get_str("STRATEGY_SCALP_TIMEFRAME", "M5") or "M5").upper(),
            scalp_range_bars=get_int("STRATEGY_SCALP_RANGE_BARS", 12),
            scalp_max_range_atr=get_float("STRATEGY_SCALP_MAX_RANGE_ATR", 2.0),
            opportunity_symbols=tuple(s.upper() for s in (get_list("OPPORTUNITY_SYMBOLS")
                                                          or get_list("TRADED_SYMBOLS"))),
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
            console=get_bool("NOTIFY_CONSOLE", False),
        ),
        instance=InstanceConfig(
            account_id=get_str("BOT_ACCOUNT_ID"),
            label=get_str("BOT_ACCOUNT_LABEL"),
        ),
        accounts=accounts,
        adaptive=load_adaptive_config(),
        learning=load_learning_config(get_str("BOT_ACCOUNT_ID")),
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
