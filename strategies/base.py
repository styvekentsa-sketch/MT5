"""
Classe de base des stratégies « à setups » : chaque stratégie évalue le marché selon le régime
détecté et retourne un `TradingSetup` (models/schemas.py) ou None.

Cycle d'un setup :
  1. `find_setup()` (propre à la stratégie) : contexte, déclencheur, niveaux d'entrée / SL / TP,
     statut SURVEILLANCE (contexte favorable, pas encore de déclencheur), PRÊT ou INVALIDÉ
  2. `analyze()` : pour un setup PRÊT, taille calculée par le calculateur de risque
     (`calculate_position_size`) puis contrôle indépendant de toutes les règles
     (`validate_trade_risk`) ; refus -> INVALIDÉ avec le motif

Formules communes (identiques pour toutes les stratégies) :
  - marge de protection   b = max(0.10 x ATR, 2 x spread, 1 tick)
  - coût estimé           c = spread actuel (sortie au prix défavorable / glissement)
  - ratio net estimé      (|TP - entrée| - c) / (|entrée - SL| + c)
  - objectif pour un ratio net R : TP = entrée ± (R x (|entrée - SL| + c) + c)
  - entrée                premier prix exécutable après confirmation (Ask à l'achat, Bid à la vente),
                          refusée s'il s'est éloigné de plus de 0.20 ATR de la clôture de confirmation

Symétrie : les ventes sont calculées par le même code que les achats, sur la série miroir
(prix -> K - prix) ; les niveaux sont ensuite reconvertis. Aucune règle ne peut ainsi diverger
entre achat et vente.

Une stratégie ne passe jamais d'ordre : le moteur (ou l'utilisateur, en mode analyste) décide.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pandas as pd

from brokers.base import BaseBroker
from models.schemas import MarketRegime, SetupStatus, Side, TradingSetup, TradingStyle
from risk.calculator import net_reward_risk, protection_margin, target_for_ratio  # noqa: F401 - réexportés
from risk.risk_manager import calculate_position_size, validate_trade_risk
from risk.trailing import TrailingStop
from risk.verifier import verify_order
from utils import alerts
from utils.logger import get_logger

log = get_logger(__name__)

TIMEFRAME_SECONDS = {"M1": 60, "M5": 300, "M15": 900, "M30": 1800, "H1": 3600, "H4": 14_400, "D1": 86_400,
                     "W1": 604_800}

MIN_NET_REWARD_RISK = 3.0            # gain net / perte prévue minimal d'un setup (cf. RISK_MIN_NET_REWARD_RISK)
ENTRY_MAX_DRIFT_ATR = 0.20           # entrée refusée si le prix a fui de plus de 0.20 ATR
NEWS_NOTE = "fenêtre de sécurité"    # note d'un setup bloqué par une annonce majeure


# =============================================================================
#  Cotation et formules communes
# =============================================================================

@dataclass(frozen=True)
class Quote:
    """Dernier prix exécutable et pas de cotation du symbole."""
    bid: float
    ask: float
    tick_size: float

    @property
    def spread(self) -> float:
        return max(0.0, self.ask - self.bid)

    def entry(self, side: Side) -> float:
        return self.ask if side == Side.BUY else self.bid


# =============================================================================
#  Série miroir (ventes calculées comme des achats)
# =============================================================================

def mirror_frame(df: pd.DataFrame, k: float) -> pd.DataFrame:
    """Prix -> k - prix : une baisse devient une hausse, plus hauts et plus bas s'échangent."""
    out = pd.DataFrame({"open": k - df["open"], "high": k - df["low"], "low": k - df["high"], "close": k - df["close"]},
                       index=df.index)
    for column in df.columns.difference(out.columns):        # spread, volume... inchangés
        out[column] = df[column]
    return out


def mirror_quote(quote: Quote, k: float) -> Quote:
    return Quote(bid=k - quote.ask, ask=k - quote.bid, tick_size=quote.tick_size)


def mirror_constant(frames: list[pd.DataFrame]) -> float:
    """Constante de miroir gardant tous les prix positifs."""
    return 2 * max(float(df["high"].max()) for df in frames if len(df))


def closed_candles(df: pd.DataFrame | None) -> pd.DataFrame:
    """Bougies clôturées (la dernière, en cours, est retirée), colonnes numériques."""
    if df is None or df.empty or not {"open", "high", "low", "close"} <= set(df.columns):
        return pd.DataFrame(columns=["open", "high", "low", "close"])
    columns = [c for c in ("open", "high", "low", "close", "spread") if c in df.columns]
    return df[columns].apply(pd.to_numeric, errors="coerce").dropna().iloc[:-1]


@dataclass(frozen=True)
class BuyPlan:
    """Plan d'achat (calculé sur la série réelle ou sur la série miroir pour une vente)."""
    status: SetupStatus
    entry: float
    stop_loss: float
    take_profit: float
    net_reward_risk: float
    regime: MarketRegime
    reason: str
    notes: tuple[str, ...] = ()
    levels: tuple[tuple[str, float], ...] = ()       # niveaux de prix (reconvertis pour une vente)
    indicators: tuple[tuple[str, float], ...] = ()   # valeurs sans unité de prix (ADX, ratios...)


_STATUS_RANK = {SetupStatus.READY: 0, SetupStatus.WATCHING: 1, SetupStatus.INVALIDATED: 2, SetupStatus.EXPIRED: 3}
_MIRRORED_REGIME = {MarketRegime.TREND_UP: MarketRegime.TREND_DOWN, MarketRegime.TREND_DOWN: MarketRegime.TREND_UP}

# Les plans d'achat décrivent leurs motifs avec ces mots ; une vente les remplace par leur symétrique
WORDS = {
    Side.BUY: {"tendance": "haussière", "sommet": "sommet", "creux": "creux", "bas": "bas", "haut": "haut",
               "bougie": "haussière", "tiers": "supérieur", "sous": "sous", "au_dessus": "au-dessus", "borne": "basse",
               "support": "support", "resistance": "résistance", "demande": "demande", "direction": "haussière"},
    Side.SELL: {"tendance": "baissière", "sommet": "creux", "creux": "sommet", "bas": "haut", "haut": "bas",
                "bougie": "baissière", "tiers": "inférieur", "sous": "au-dessus", "au_dessus": "sous", "borne": "haute",
                "support": "résistance", "resistance": "support", "demande": "offre", "direction": "baissière"},
}


# =============================================================================
#  Classe de base
# =============================================================================

class BaseStrategy(ABC):
    """Stratégie produisant des `TradingSetup` à partir de bougies multi-timeframes et du dernier prix."""

    #: Identifiant court (journal, setups)
    name: str = "strategy"
    #: Style de trading des setups produits
    style: TradingStyle = TradingStyle.DAY
    #: Timeframes nécessaires ; le premier est celui du contexte (régime)
    timeframes: tuple[str, ...] = ("H1",)
    #: Bougies demandées par timeframe
    bars: int = 300
    #: Durée de validité d'un setup, en bougies du premier timeframe
    validity_bars: int = 3
    #: Fuseau des étiquettes de bougies (None : UTC réel) : fixé par le broker (analyze) ou le backtest
    bar_timezone = None
    #: Ratio net minimal exigé par le vérificateur indépendant (None : RISK_MIN_NET_REWARD_RISK)
    min_net_reward_risk: float | None = None
    #: Moteur (trades d'opportunité) : nom affiché, étiquette du commentaire d'ordre, indicateur identifiant
    #: un signal (un seul trade par valeur)
    label: str = "Setup"
    role: str = ""                   # rôle affiché dans les logs et le dashboard (stratégies d'opportunité)
    order_tag: str = "SETUP"
    anchor: str = "entry"
    #: Gestion de la position ouverte (backtest) : breakeven / trailing stop (risk/trailing.py), ou None
    trailing: TrailingStop | None = None

    @abstractmethod
    def plan_buy(self, data: dict[str, pd.DataFrame], quote: Quote | None) -> BuyPlan | None:
        """Plan d'ACHAT sur les bougies clôturées `data` (la base l'applique aussi à la série miroir pour vendre)."""

    def find_setup(self, symbol: str, data: dict[str, pd.DataFrame], now: datetime,
                   quote: Quote | None = None) -> TradingSetup | None:
        """Setup détecté (achat ou vente) sur les bougies CLÔTURÉES `data` et le prix `quote`, ou None."""
        candidates: list[TradingSetup] = []
        buy = self.plan_buy(data, quote)
        if buy is not None:
            candidates.append(self._to_setup(symbol, Side.BUY, buy, None, now))
        frames = [df for df in data.values() if len(df)]
        if frames:
            k = mirror_constant(frames)
            sell = self.plan_buy({tf: mirror_frame(df, k) if len(df) else df for tf, df in data.items()},
                                 mirror_quote(quote, k) if quote else None)
            if sell is not None:
                candidates.append(self._to_setup(symbol, Side.SELL, sell, k, now))
        return min(candidates, key=lambda s: _STATUS_RANK[s.status], default=None)

    def _to_setup(self, symbol: str, side: Side, plan: BuyPlan, k: float | None, now: datetime) -> TradingSetup:
        price = (lambda p: p) if k is None else (lambda p: k - p)
        regime = plan.regime if k is None else _MIRRORED_REGIME.get(plan.regime, plan.regime)
        return TradingSetup(
            symbol=symbol, side=side, style=self.style, strategy=self.name, regime=regime,
            entry_price=price(plan.entry), stop_loss=price(plan.stop_loss), take_profit=price(plan.take_profit),
            net_reward_risk=round(plan.net_reward_risk, 3), status=plan.status, created_at=now,
            expires_at=self.expiry(now), reason=plan.reason.format(**WORDS[side]),
            notes=[note.format(**WORDS[side]) for note in plan.notes],
            indicators={**{name: round(price(value), 8) for name, value in plan.levels},
                        **{name: round(value, 8) for name, value in plan.indicators}},
        )

    def expiry(self, now: datetime) -> datetime:
        return now + timedelta(seconds=TIMEFRAME_SECONDS.get(self.timeframes[0], 3600) * self.validity_bars)

    def load(self, broker: BaseBroker, symbol: str) -> dict[str, pd.DataFrame]:
        return {tf: closed_candles(broker.get_historical_data(symbol, count=self.bars, timeframe=tf))
                for tf in dict.fromkeys(self.timeframes)}

    @staticmethod
    def quote(broker: BaseBroker, symbol: str) -> Quote | None:
        tick, specs = broker.get_current_tick(symbol), broker.get_symbol_specs(symbol)
        if tick is None or specs is None:
            return None
        return Quote(bid=tick.bid, ask=tick.ask, tick_size=specs.tick_size)

    def analyze(self, broker: BaseBroker, symbol: str, now: datetime | None = None,
                memory=None) -> TradingSetup | None:
        """
        Détection puis, pour un setup prêt : taille de position, contrôle des règles de risque,
        vérificateur indépendant et mémoire des erreurs (`memory` : TradeMemory) ; un setup qui
        reste prêt est journalisé (trade_decisions.json) et alerté (utils/alerts.py). Le backtest
        appelle `evaluate` : aucune alerte.
        """
        now = now or datetime.now(timezone.utc)
        self.bar_timezone = getattr(broker, "bar_timezone", None)
        try:
            data, quote = self.load(broker, symbol), self.quote(broker, symbol)
        except Exception:  # noqa: BLE001 - broker indisponible : aucune proposition
            log.exception("[%s] Données indisponibles pour la stratégie %s", symbol, self.name)
            return None
        setup = self.evaluate(symbol, data, now, quote, broker, memory=memory)
        if setup is not None and setup.status == SetupStatus.READY:
            alerts.setup_validated(setup)
        elif setup is not None and setup.notes and setup.notes[-1].startswith(NEWS_NOTE):
            alerts.security_block("annonce majeure", setup.notes[-1], symbol)
        return setup

    def evaluate(self, symbol: str, data: dict[str, pd.DataFrame], now: datetime, quote: Quote | None,
                 broker, memory=None, news=None) -> TradingSetup | None:
        """
        Séquence de décision complète, IDENTIQUE en direct et en backtest : détection sur les
        bougies clôturées `data`, fenêtre de sécurité des annonces (`news` : pipeline, celui du
        processus par défaut), taille, règles de risque, vérificateur indépendant, mémoire des erreurs.
        """
        try:
            setup = self.find_setup(symbol, data, now, quote)
        except Exception:  # noqa: BLE001 - une stratégie en erreur ne doit pas arrêter l'analyse des autres
            log.exception("[%s] Stratégie %s en erreur", symbol, self.name)
            return None
        if setup is None or setup.status != SetupStatus.READY:
            return setup
        block = self.news_block(symbol, now, news)
        if block is not None:                              # fenêtre de sécurité : aucune nouvelle proposition
            return setup.model_copy(update={"status": SetupStatus.INVALIDATED,
                                            "notes": [*setup.notes, f"{NEWS_NOTE} : {block.reason}"]})
        setup = self.size(setup, broker)
        if setup.status == SetupStatus.READY and memory is not None:
            setup = self.remember(setup, broker, memory)
        return setup

    @staticmethod
    def news_block(symbol: str, now: datetime, news=None):
        """Annonce majeure dans la fenêtre de sécurité de l'actif (core/news_pipeline.py), ou None."""
        from core.news_pipeline import get_pipeline

        return (news or get_pipeline()).safety_check(symbol, now)

    @staticmethod
    def remember(setup: TradingSetup, broker: BaseBroker, memory) -> TradingSetup:
        """Bloque le setup s'il ressemble à un pattern perdant répété ; sinon le journalise."""
        from learning.trade_memory import market_features

        features = market_features(broker, setup.symbol, {
            "mode": setup.style.value, "entry_price": setup.entry_price, "suggested_sl": setup.stop_loss,
            "sentiment_score": 0.0, "technical_score": None})
        block = memory.check(setup.symbol, setup.side.value, setup.style.value, features)
        if block is not None:
            log.warning("[%s] %s", setup.symbol, block.message)
            return setup.model_copy(update={"status": SetupStatus.INVALIDATED, "notes": [*setup.notes, block.message]})
        memory.record_setup(setup, features)
        return setup

    def size(self, setup: TradingSetup, broker: BaseBroker) -> TradingSetup:
        """Taille (calculateur de risque) puis vérification indépendante de toutes les règles."""
        volume = calculate_position_size(setup.symbol, setup.entry_price, setup.stop_loss, broker=broker)
        if volume <= 0:
            return setup.model_copy(update={"status": SetupStatus.INVALIDATED,
                                            "notes": [*setup.notes, "taille nulle : le volume minimal dépasserait "
                                                                    "le risque autorisé par position"]})
        check = validate_trade_risk(setup.symbol, setup.side.value, volume, setup.stop_loss, setup.take_profit,
                                    broker=broker)
        update = {"volume": volume, "risk_amount": check.get("risk_amount") or 0.0,
                  "risk_pct": check.get("risk_pct") or 0.0}
        notes = list(setup.notes)
        if not check.get("is_approved"):
            notes.append(f"risque : {check.get('reason')}")
        verdict = verify_order(setup.symbol, setup.side.value, setup.entry_price, setup.stop_loss, setup.take_profit,
                               broker, min_ratio=self.min_net_reward_risk)
        if not verdict.approved:
            notes.append(f"vérificateur indépendant : {verdict.reason}")
        if len(notes) > len(setup.notes):
            update.update(status=SetupStatus.INVALIDATED, notes=notes)
        return setup.model_copy(update=update)


Strategy = BaseStrategy                # nom historique
