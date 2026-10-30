"""
Interface commune à tous les brokers : MetaTrader 5 (Forex, CFD, Exness...) et exchanges
crypto via ccxt (Binance, Bybit...).

Le moteur, la gestion du risque et l'API ne parlent qu'à `BaseBroker` : ajouter une
plateforme revient à écrire un adaptateur qui implémente les méthodes abstraites.

  Méthodes unifiées      : connect(), disconnect(), get_balance(), get_positions(),
                           place_order(), close_order()
  Données de marché      : get_historical_data(), get_current_tick(), get_symbol_specs()
  Calculs pour le risque : calc_profit(), calc_margin(), get_closed_deals()

Le mode PAPER / LIVE est commun au processus (un processus = un compte) : en PAPER,
`place_order()` valide l'ordre puis le simule, sans rien envoyer.

Usage :
    from brokers.factory import get_broker

    broker = get_broker()                 # MT5Broker ou CCXTBroker selon BROKER_TYPE
    if broker.connect():
        print(broker.get_balance(), broker.get_positions())
"""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from datetime import datetime, timezone

import pandas as pd

from brokers.errors import BrokerConnectionError, record_error
from brokers.models import AccountInfo, ClosedDeal, Market, OrderResult, Position, SymbolSpecs, Tick
from config import settings
from utils.logger import get_logger
from utils.notifier import Level, notify

log = get_logger(__name__)

# Bougies : noms de timeframes du bot (ceux de MT5), traduits par chaque adaptateur
TIMEFRAME_NAMES = ("M1", "M5", "M15", "M30", "H1", "H4", "D1", "W1", "MN1")


# =============================================================================
#  Mode PAPER / LIVE (partagé par tous les brokers du processus)
# =============================================================================

_mode_lock = threading.Lock()
_dry_run = settings.dry_run                 # modifiable à chaud via set_dry_run() (API de contrôle)


def is_dry_run() -> bool:
    """True en mode PAPER (ordres simulés), False en mode LIVE."""
    return _dry_run


def set_dry_run(enabled: bool) -> None:
    """Bascule à chaud entre PAPER (True) et LIVE (False) ; effectif dès l'ordre suivant."""
    global _dry_run
    with _mode_lock:
        if enabled == _dry_run:
            return
        _dry_run = enabled
    if enabled:
        log.warning("Mode PAPER activé : les ordres sont désormais simulés")
    else:
        log.warning("Mode LIVE activé : les ordres seront ENVOYÉS au broker")


# =============================================================================
#  Classe de base
# =============================================================================

class BaseBroker(ABC):
    """
    Broker unifié. Conventions :
      - symboles « bot » (ceux de TRADING_SYMBOLS) en entrée comme en sortie : l'adaptateur
        traduit vers le nom natif (EURUSDm chez Exness, BTC/USDT:USDT chez ccxt...)
      - les méthodes d'ordre ne lèvent jamais : le résultat est toujours un `OrderResult`
      - les erreurs passent par `_report()` (journal central `brokers.errors`)
    """

    #: Identifiant court affiché dans les logs et le dashboard (mt5, bybit, binanceusdm...)
    name: str = "broker"
    #: Type de marché : détermine les unités (lots / contrats) et certaines règles de risque
    market: Market = "forex"

    def __init__(self) -> None:
        self._connected_state: bool | None = None       # None = jamais connecté

    # ------------------------------------------------------------------ connexion

    @abstractmethod
    def connect(self) -> bool:
        """Ouvre la session (identifiants du .env). Retourne False si impossible."""

    @abstractmethod
    def disconnect(self) -> None:
        """Ferme proprement la session."""

    @abstractmethod
    def is_connected(self) -> bool:
        """True si la session est active et le serveur joignable."""

    def ensure_connection(self) -> None:
        """Reconnecte si la session a été perdue ; lève BrokerConnectionError si c'est impossible."""
        if self.is_connected():
            return
        log.warning("[%s] Session inactive : reconnexion...", self.name)
        if not self.connect():
            raise BrokerConnectionError(f"Connexion à {self.name} impossible")

    @property
    def description(self) -> str:
        """Résumé lisible du broker (compte, serveur, environnement), sans secret."""
        return self.name

    # ------------------------------------------------------------------ compte & positions

    @abstractmethod
    def get_balance(self) -> AccountInfo | None:
        """Solde, fonds propres, marge et P/L latent du compte."""

    @abstractmethod
    def get_positions(self, symbol: str | None = None, bot_only: bool = True) -> list[Position]:
        """Positions ouvertes (celles du bot uniquement par défaut)."""

    @abstractmethod
    def get_closed_deals(self, since: datetime, bot_only: bool = False) -> list[ClosedDeal]:
        """Clôtures (P/L réalisé) depuis `since`, de la plus ancienne à la plus récente."""

    # ------------------------------------------------------------------ ordres

    @abstractmethod
    def place_order(self, symbol: str, side: str, volume: float, sl: float | None = None,
                    tp: float | None = None, comment: str = "Bot Order") -> OrderResult:
        """Ouvre une position au marché (simulée en mode PAPER)."""

    @abstractmethod
    def close_order(self, ticket: int, comment: str = "Bot Close", force: bool = False) -> OrderResult:
        """Ferme une position. `force=True` : clôture réelle même en PAPER (kill-switch)."""

    # ------------------------------------------------------------------ marché

    @abstractmethod
    def get_historical_data(self, symbol: str, count: int = 100, timeframe: str | None = None) -> pd.DataFrame:
        """Dernières bougies (index UTC ; colonnes open, high, low, close, volume, spread, real_volume)."""

    @abstractmethod
    def get_current_tick(self, symbol: str) -> Tick | None:
        """Dernier Bid / Ask."""

    @abstractmethod
    def get_symbol_specs(self, symbol: str) -> SymbolSpecs | None:
        """Contraintes de trading : pas de prix et de volume, minimums, taille de contrat..."""

    @abstractmethod
    def calc_profit(self, side: str, symbol: str, volume: float, price_open: float, price_close: float) -> float | None:
        """Profit (négatif = perte) en devise du compte pour `volume` entre deux prix."""

    @abstractmethod
    def calc_margin(self, side: str, symbol: str, volume: float, price: float) -> float | None:
        """Marge requise en devise du compte."""

    # ------------------------------------------------------------------ protocole du risk manager

    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def get_account_info(self) -> AccountInfo | None:
        return self.get_balance()

    def get_open_positions(self) -> list[Position]:
        return self.get_positions(bot_only=True)

    # ------------------------------------------------------------------ journal central

    def _report(self, operation: str, exc: BaseException | str) -> None:
        """Incident consigné dans le journal central des brokers."""
        record_error(self.name, operation, exc)

    def _set_connected(self, connected: bool, reason: str = "") -> None:
        """
        Signale une seule fois chaque perte / retour de connexion (log + alerte). L'erreur
        elle-même est consignée par l'appelant via `_report()`, avec sa nature exacte.
        """
        previous, self._connected_state = self._connected_state, connected
        if previous is None or previous == connected:
            return
        if connected:
            log.info("[%s] Connexion rétablie", self.name)
            notify(f"Connexion rétablie · {self.name}", "Le bot reprend la lecture du marché.",
                   dedup_key=f"broker-up:{self.name}")
        else:
            log.warning("[%s] Connexion perdue : %s", self.name, reason or "le broker ne répond plus")
            notify(f"Connexion perdue · {self.name}", reason or "Le broker ne répond plus : reconnexion en cours.",
                   Level.CRITICAL, dedup_key=f"broker-down:{self.name}")
