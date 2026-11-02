"""
Journal des décisions et mémoire des erreurs (boucle de rétroaction).

Point d'entrée demandé pour ce module ; l'implémentation, déjà utilisée par le moteur, l'API et
le dashboard, est dans learning/trade_memory.py :

  - journal d'entrée   `trade_decisions.json` : instantané de chaque position ouverte par le moteur
                       (`record_entry`) et de chaque setup validé par les stratégies (`record_setup`) :
                       horodatage, actif, prix, SL, TP, indicateurs, raison textuelle
  - post-mortem        `loss_analysis.json` : à la clôture en perte (SL), durée, excursions et cause
                       probable (bruit de marché, spread trop lourd, faux signal en range, mouvement
                       épuisé, sentiment contraire, signal faux dès l'entrée) - `review`
  - mémoire            `mistakes_memory.json` : statistiques par pattern de marché ; `check` bloque
                       un ordre ou un setup qui reproduit un pattern perdant répété
"""

from learning.trade_memory import (  # noqa: F401 - API publique réexportée
    MemoryBlock,
    TradeMemory,
    failure_factors,
    market_features,
    pattern_key,
    pattern_labels,
)

FeedbackLogger = TradeMemory
