"""
Modèles de données métier, validés par Pydantic.

  BotConfig     vue validée de la configuration (actifs, risques, horaires) construite depuis
                `config.settings` (lui-même chargé et validé depuis le .env)
  TradingSetup  opportunité de trade : actif, sens, style, stratégie, régime, niveaux, taille,
                risque, statut (surveillance / prêt / expiré / invalidé)
  AccountState  état du compte : équité, solde, marge, devise

Les invariants sont vérifiés à la construction : un SL du mauvais côté de l'entrée, un risque
négatif ou des limites incohérentes lèvent une ValidationError.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

if TYPE_CHECKING:
    from brokers.models import AccountInfo
    from config.settings import Settings


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class TradingStyle(str, Enum):
    SCALPING = "scalping"
    DAY = "day"
    SWING = "swing"


class SetupStatus(str, Enum):
    WATCHING = "surveillance"        # contexte favorable, déclencheur pas encore apparu
    READY = "prêt"                   # déclencheur présent, niveaux et taille validés
    EXPIRED = "expiré"               # fenêtre de validité dépassée
    INVALIDATED = "invalidé"         # niveaux franchis, ou refus des règles de risque


class MarketRegime(str, Enum):
    """Régimes du détecteur causal (core/regime_detector.py)."""
    TREND_UP = "tendance_haussière"
    TREND_DOWN = "tendance_baissière"
    RANGE = "range"
    TRANSITION = "transition"
    SHOCK = "choc"
    UNKNOWN = "inconnu"


# =============================================================================
#  Configuration
# =============================================================================

class TradingHours(BaseModel):
    """Plages horaires UTC, en minutes depuis minuit."""
    model_config = ConfigDict(frozen=True)

    liquid: tuple[int, int] = Field(description="Heures liquides (scalping)")
    day_windows: tuple[tuple[int, int], ...] = Field(description="Ouvertures de sessions (day trading)")
    day_close: int = Field(ge=0, lt=24 * 60, description="Fermeture obligatoire des positions day trading")


class BotConfig(BaseModel):
    """Vue validée de la configuration utile aux stratégies et au contrôle du risque."""
    model_config = ConfigDict(frozen=True)

    symbols: tuple[str, ...] = Field(min_length=1)
    timeframe: str
    profile: str
    dry_run: bool
    risk_per_trade_pct: float = Field(gt=0, le=5, description="Plafond de risque par position, % de l'équité")
    max_total_risk_pct: float = Field(gt=0, le=20, description="Risque ouvert cumulé maximal, % de l'équité")
    max_open_positions: int = Field(ge=1, le=20)
    max_positions_per_symbol: int = Field(ge=1)
    max_daily_loss_pct: float = Field(gt=0, le=20)
    hours: TradingHours

    @model_validator(mode="after")
    def _coherent(self) -> BotConfig:
        if self.max_total_risk_pct < self.risk_per_trade_pct:
            raise ValueError("le risque cumulé maximal doit être >= au risque par position")
        if self.max_positions_per_symbol > self.max_open_positions:
            raise ValueError("les positions par symbole ne peuvent dépasser les positions simultanées")
        return self

    @classmethod
    def from_settings(cls, cfg: Settings) -> BotConfig:
        return cls(
            symbols=cfg.trading.symbols, timeframe=cfg.trading.timeframe, profile=cfg.strategy.profile,
            dry_run=cfg.dry_run, risk_per_trade_pct=cfg.risk.risk_per_trade_pct,
            max_total_risk_pct=cfg.risk.max_total_risk_pct, max_open_positions=cfg.risk.max_open_positions,
            max_positions_per_symbol=cfg.risk.max_positions_per_symbol,
            max_daily_loss_pct=cfg.risk.max_daily_loss_pct,
            hours=TradingHours(liquid=cfg.adaptive.liquid_hours, day_windows=cfg.adaptive.day_windows,
                               day_close=cfg.adaptive.day_close),
        )


# =============================================================================
#  Diagnostic d'un symbole (spécifications du contrat + qualité de cotation)
# =============================================================================

class DiagnosticFailure(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: str = Field(description="unknown, disabled, close_only, no_price, stale_tick, spread_max, "
                                  "spread_median, spread_sl, unavailable")
    message: str


class SymbolDiagnostic(BaseModel):
    """Contrat et cotation d'un symbole, et verdict : l'actif est-il tradable maintenant ?"""
    symbol: str
    found: bool = False
    tradable: bool = False
    failures: list[DiagnosticFailure] = Field(default_factory=list)
    trade_mode: str | None = None              # full, long_only, short_only, close_only, disabled
    description: str = ""
    currency_profit: str | None = None
    contract_size: float | None = None         # unités de l'actif par lot (100 000 en Forex, 100 sur l'or)
    volume_min: float | None = None
    volume_max: float | None = None
    volume_step: float | None = None
    digits: int | None = None
    point: float | None = None
    tick_size: float | None = None
    tick_value: float | None = None            # valeur d'un tick pour 1 lot, devise du compte
    stops_level_points: int | None = None
    bid: float | None = None
    ask: float | None = None
    spread_points: int | None = None
    spread_price: float | None = None
    median_spread_points_60m: float | None = None
    tick_time_server: datetime | None = None
    tick_age_seconds: float | None = None
    spread_to_sl_pct: float | None = None

    @property
    def reasons(self) -> list[str]:
        return [f.message for f in self.failures]


# =============================================================================
#  Compte
# =============================================================================

class AccountState(BaseModel):
    model_config = ConfigDict(frozen=True)

    equity: float
    balance: float
    margin: float = Field(ge=0)
    margin_free: float
    currency: str = Field(min_length=3, max_length=8)

    @property
    def margin_level_pct(self) -> float | None:
        return self.equity / self.margin * 100 if self.margin > 0 else None

    @classmethod
    def from_account(cls, account: AccountInfo) -> AccountState:
        return cls(equity=account.equity, balance=account.balance, margin=account.margin,
                   margin_free=account.margin_free, currency=account.currency)


# =============================================================================
#  Setup de trading
# =============================================================================

class TradingSetup(BaseModel):
    """Opportunité de trade, de sa détection (surveillance) à son exécution ou son abandon."""

    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    expires_at: datetime | None = None
    symbol: str = Field(min_length=1)
    side: Side
    style: TradingStyle
    strategy: str
    regime: MarketRegime = MarketRegime.UNKNOWN
    entry_price: float = Field(gt=0)
    stop_loss: float = Field(gt=0)
    take_profit: float | None = Field(default=None, gt=0)
    volume: float = Field(default=0.0, ge=0, description="Taille en lots / contrats (0 tant que non calculée)")
    risk_amount: float = Field(default=0.0, ge=0, description="Perte au SL, en devise du compte")
    risk_pct: float = Field(default=0.0, ge=0, description="Perte au SL en % de l'équité")
    net_reward_risk: float | None = Field(default=None, description="Ratio net estimé (gain - coûts) / (perte + coûts)")
    status: SetupStatus = SetupStatus.WATCHING
    reason: str = ""
    notes: list[str] = Field(default_factory=list)
    indicators: dict[str, Any] = Field(default_factory=dict)

    @field_validator("symbol")
    @classmethod
    def _upper(cls, value: str) -> str:
        return value.strip().upper()

    @model_validator(mode="after")
    def _levels_on_the_right_side(self) -> TradingSetup:
        if self.side == Side.BUY:
            if self.stop_loss >= self.entry_price:
                raise ValueError("achat : le stop loss doit être sous le prix d'entrée")
            if self.take_profit is not None and self.take_profit <= self.entry_price:
                raise ValueError("achat : le take profit doit être au-dessus du prix d'entrée")
        else:
            if self.stop_loss <= self.entry_price:
                raise ValueError("vente : le stop loss doit être au-dessus du prix d'entrée")
            if self.take_profit is not None and self.take_profit >= self.entry_price:
                raise ValueError("vente : le take profit doit être sous le prix d'entrée")
        return self

    @property
    def risk_distance(self) -> float:
        return abs(self.entry_price - self.stop_loss)

    @property
    def reward_risk(self) -> float | None:
        if self.take_profit is None or self.risk_distance == 0:
            return None
        return abs(self.take_profit - self.entry_price) / self.risk_distance

    def is_expired(self, now: datetime | None = None) -> bool:
        return self.expires_at is not None and (now or datetime.now(timezone.utc)) >= self.expires_at
