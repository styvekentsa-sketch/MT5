"""
Choix de la plateforme de trading du processus : MetaTrader 5 ou exchange crypto (ccxt).

    BROKER_TYPE=mt5          # Forex / CFD via un terminal MT5 (MetaQuotes, Exness, IC Markets...)
    MT5_SYMBOL_SUFFIX=m      # suffixe des symboles chez le broker (Exness : EURUSDm), facultatif

    BROKER_TYPE=ccxt         # exchange crypto, contrats perpétuels linéaires (USDT)
    CCXT_EXCHANGE=bybit      # bybit, binanceusdm, okx... (identifiant ccxt)
    CCXT_ENVIRONMENT=demo    # demo (compte démo de l'exchange) | testnet | live
    CCXT_API_KEY=...         # facultatif en PAPER : données publiques + capital fictif
    CCXT_API_SECRET=...
    CCXT_LEVERAGE=3

Les secrets ne sont jamais écrits en dur ni affichés : ils viennent du `.env`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from config.env import ConfigError, get_float, get_int, get_str, mask

VALID_BROKER_TYPES = ("mt5", "ccxt")
VALID_CCXT_ENVIRONMENTS = ("demo", "testnet", "live")
SUPPORTED_CCXT_MARKETS = ("swap",)


@dataclass(frozen=True)
class CCXTConfig:
    exchange: str
    api_key: str = field(repr=False)
    api_secret: str = field(repr=False)
    api_password: str = field(repr=False)          # « passphrase » (OKX, KuCoin...)
    environment: str
    market_type: str
    quote: str                                      # devise de règlement et du compte (USDT)
    leverage: int
    paper_balance: float                            # capital fictif en PAPER sans clé API
    timeout_ms: int

    @property
    def has_credentials(self) -> bool:
        return bool(self.api_key and self.api_secret)

    def validate(self) -> None:
        if not self.exchange:
            raise ConfigError("CCXT_EXCHANGE est vide (ex : bybit, binanceusdm, okx)")
        if self.environment not in VALID_CCXT_ENVIRONMENTS:
            raise ConfigError(f"CCXT_ENVIRONMENT invalide : {self.environment} "
                              f"(attendu : {', '.join(VALID_CCXT_ENVIRONMENTS)})")
        if self.market_type not in SUPPORTED_CCXT_MARKETS:
            raise ConfigError(f"CCXT_MARKET_TYPE={self.market_type} non pris en charge : seuls les contrats "
                              "perpétuels (swap) permettent un SL côté exchange et la vente à découvert")
        if bool(self.api_key) != bool(self.api_secret):
            raise ConfigError("CCXT_API_KEY et CCXT_API_SECRET doivent être renseignés ensemble")
        if not 1 <= self.leverage <= 20:
            raise ConfigError("CCXT_LEVERAGE doit être entre 1 et 20")
        if self.paper_balance <= 0:
            raise ConfigError("CCXT_PAPER_BALANCE doit être > 0")


@dataclass(frozen=True)
class BrokerConfig:
    type: str
    mt5_symbol_suffix: str
    ccxt: CCXTConfig

    def validate(self) -> None:
        if self.type not in VALID_BROKER_TYPES:
            raise ConfigError(f"BROKER_TYPE invalide : {self.type} (attendu : {', '.join(VALID_BROKER_TYPES)})")
        if self.type == "ccxt":
            self.ccxt.validate()

    def summary(self) -> str:
        if self.type == "mt5":
            return "MetaTrader 5" + (f" (suffixe symboles « {self.mt5_symbol_suffix} »)" if self.mt5_symbol_suffix else "")
        c = self.ccxt
        return (f"{c.exchange} via ccxt ({c.market_type} {c.quote}, {c.environment}, levier x{c.leverage})"
                f" | clé API {mask(c.api_key) if c.api_key else 'aucune (PAPER sur données publiques)'}")


def load_broker_config() -> BrokerConfig:
    return BrokerConfig(
        type=get_str("BROKER_TYPE", "mt5").lower(),
        mt5_symbol_suffix=get_str("MT5_SYMBOL_SUFFIX"),
        ccxt=CCXTConfig(
            exchange=get_str("CCXT_EXCHANGE").lower(),
            api_key=get_str("CCXT_API_KEY"),
            api_secret=get_str("CCXT_API_SECRET"),
            api_password=get_str("CCXT_API_PASSWORD"),
            environment=get_str("CCXT_ENVIRONMENT", "demo").lower(),
            market_type=get_str("CCXT_MARKET_TYPE", "swap").lower(),
            quote=get_str("CCXT_QUOTE", "USDT").upper(),
            leverage=get_int("CCXT_LEVERAGE", 3),
            paper_balance=get_float("CCXT_PAPER_BALANCE", 10_000.0),
            timeout_ms=get_int("CCXT_TIMEOUT_MS", 15_000),
        ),
    )
