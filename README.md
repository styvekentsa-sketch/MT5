# MT5 Trading Bot

Bot de trading automatisé multi-marchés — Forex / CFD via MetaTrader 5, crypto via ccxt (Binance, Bybit, OKX) — combinant analyse technique et analyse fondamentale (actualités, calendrier économique, sentiment).

> Projet en cours de développement : les modules marqués « À implémenter » sont des squelettes.

## Structure

```
config/            Paramètres (settings.py), identifiants MT5 (credentials.py), lecture du .env (env.py)
core/              Boucle principale (engine.py) et état du bot (state_manager.py)
brokers/           Interface BaseBroker (base.py), adaptateurs MT5 (mt5_broker.py, mt5_client.py) et ccxt
                   (ccxt_broker.py), modèles communs (models.py), journal des incidents (errors.py)
strategies/        Indicateurs (technical_analysis.py) et décision (strategy_manager.py)
sentiment/         Actualités et calendrier (news_scraper.py), sentiment (sentiment_analyzer.py)
risk/              Lot, SL/TP, drawdown (risk_manager.py)
utils/             Logs (logger.py), alertes (notifier.py), SQLite (database.py)
api/               API REST du dashboard (FastAPI) : main.py, schemas.py, security.py, routes/
dashboard/         Dashboard web (React + Vite + Tailwind)
backtesting/       Simulation sur historique (backtest_engine.py, setup_engine.py)
tests/             Tests unitaires
main.py            Point d'entrée
```

## Installation

Windows uniquement (le package `MetaTrader5` n'existe que sous Windows), Python 3.10 à 3.12.

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env   # puis renseigner les valeurs
```

## Utilisation

Toujours lancer depuis la racine du projet :

```powershell
python main.py                        # bot
python -m brokers.mt5_client          # test de connexion MT5
python -m sentiment.news_scraper      # test calendrier + flux RSS
python -m sentiment.sentiment_analyzer  # test du sentiment sur les actualités réelles
python -m strategies.strategy_manager   # évalue chaque symbole (aucun ordre envoyé)
```

`DRY_RUN=true` (défaut) = mode Paper : les signaux sont calculés et logués, aucun ordre réel n'est envoyé.

## Conventions

- Configuration : `from config import settings`
- Logs : `from utils.logger import get_logger` puis `log = get_logger(__name__)`
- Aucun secret dans le code : tout passe par le `.env` (jamais commité)

## API du dashboard

Activée par `API_ENABLED=true` (avec une `API_KEY` d'au moins 16 caractères), elle démarre avec `python main.py` dans un thread séparé du moteur.

- Documentation interactive : http://127.0.0.1:8000/docs
- Authentification : en-tête `X-API-Key` sur toutes les routes `/api`

| Méthode | Route | Rôle |
|---|---|---|
| GET | `/api/status` | État de la boucle, mode PAPER/LIVE, connexion MT5, dernier tour |
| GET | `/api/account` | Solde, equity, marge, P/L du jour |
| GET | `/api/positions` | Positions ouvertes (`?bot_only=true` pour celles du bot) |
| GET | `/api/sentiment` | Actualités, score par article, sentiment global et par symbole |
| GET | `/api/data-feeds` | Traçabilité : articles (mots-clés, score, symboles), calendrier (statut de blocage), état des sources |
| GET | `/api/webhooks/events` | 20 derniers webhooks reçus (acceptés et refusés), secrets masqués |
| POST | `/api/webhooks/{source}` | Réception TradingView… — auth `?token=` ou `passphrase` (`WEBHOOK_SECRET`), **jamais exécuté** |
| POST | `/api/control/start` | Reprendre le moteur |
| POST | `/api/control/stop` | Mettre en pause (aucun nouveau trade, positions conservées) |
| POST | `/api/control/positions/{ticket}/close` | Clôture **réelle** d'une position |
| POST | `/api/control/kill-switch` | Pause + clôture **réelle** des positions du bot (`include_manual` pour toutes) |
| POST | `/api/control/mode` | `{"dry_run": false, "confirm": "LIVE"}` pour passer en LIVE |

## Dashboard web

Prérequis : Node.js 20+ et l'API activée (`API_ENABLED=true`).

```powershell
cd dashboard
npm install
npm run dev        # http://localhost:5173
```

Au premier lancement, la fenêtre **Connexion au bot** demande l'URL de l'API et la clé (`API_KEY`), mémorisées sur l'appareil. URL par défaut : `VITE_API_BASE_URL` (fichier `dashboard/.env.local`), sinon le même hôte que la page sur le port 8000. Statut, compte et positions sont rafraîchis toutes les 2,5 s, le sentiment toutes les 60 s ; rien n'est interrogé quand l'app est en arrière-plan ou hors ligne.

Vues :
- **Trading** : compte, connexion MT5 / simulation, **limites de risque** du jour (perte max `RISK_MAX_DAILY_LOSS_PCT`, pertes consécutives), graphique, positions, journal des décisions, actualités. Le bouton **Indicateurs** du graphique superpose les pivots confirmés, les supports / résistances de la séance précédente (S2 → R2), les bornes du range validé, et ajoute les volets ADX (+DI / −DI) et ATR, calculés sur les bougies clôturées. Les setups proposés sont tracés en pointillés (entrée, SL, TP).
- **Analyse** (mode analyste, aucun ordre) : propositions des stratégies à setups avec stratégie, régime, entrée, SL, TP, ratio net, taille, risque et **arguments pour / contre** ; performances par stratégie (espérance nette en R, taux de réussite, drawdown) des trades réels clos et des rapports de backtest (`reports/backtests/`), segmentables par actif, horizon, régime et sens, avec courbe d'équité.
- **Données** : sources, webhooks, journal des trades et mémoire des erreurs.

Routes correspondantes : `GET /api/analysis/risk`, `/setups` (analyse mise en cache 15 s, `?refresh=true` pour forcer), `/indicators?symbol=&timeframe=`, `/performance`.

### Depuis un téléphone (réseau local)

1. `.env` du bot : `API_HOST=0.0.0.0` et ajouter l'origine du dashboard, ex. `API_CORS_ORIGINS=http://localhost:5173,http://192.168.1.20:5173`
2. `npm run dev -- --host` (ou `npm run build && npm run preview -- --host`)
3. Sur le téléphone : `http://192.168.1.20:5173` (IP du PC) ; l'URL de l'API est déduite automatiquement (`http://192.168.1.20:8000`)

Sur le réseau local en HTTP, la clé API circule en clair : à réserver à un réseau de confiance.

### Application installable (PWA)

`npm run build` génère le manifest et le service worker (seule l'interface est mise en cache, jamais les données de trading). L'installation exige une page servie en **HTTPS** (ou `localhost`) :

- Android / PC / Mac (Chrome, Edge, Samsung Internet) : bouton **Installer** dans l'en-tête ou les paramètres
- iPhone / iPad (Safari) : Partager → « Sur l'écran d'accueil »

Pour un accès distant (VPS ou téléphone hors du réseau local), servir dashboard **et** API en HTTPS, par exemple derrière un reverse proxy (Caddy, Nginx) ou via Tailscale : une page HTTPS ne peut pas appeler une API en HTTP.

Icônes : modifier `dashboard/public/logo.svg` puis `npm run generate-pwa-assets`.

## Tests

Les tests simulent MT5 et le réseau : ils tournent sans terminal MetaTrader ni connexion internet.

```powershell
pip install -r requirements-dev.txt
python -m pytest
```

Couverture : configuration, calendrier économique et flux RSS, sentiment (orientation par paire, robustesse), indicateurs techniques, décision hybride et SL/TP, taille de position et règles de risque, moteur (séquence, cooldown, isolation des erreurs, arrêt), API (authentification, CORS, monitoring, contrôle).

## Notifications (console / Telegram / Discord)

Renseigner dans `.env` `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` et/ou `DISCORD_WEBHOOK_URL` (voir `.env.example`), et/ou `NOTIFY_CONSOLE=true` pour les afficher dans la console du bot, puis redémarrer le bot. Alertes envoyées : démarrage et arrêt du bot, webhook reçu ou rejeté, trade bloqué par le calendrier économique ou par une règle de risque, kill switch. Alertes formatées de [utils/alerts.py](utils/alerts.py) : **setup validé** (niveaux, ratio net, risque, arguments pour / contre), **trade fermé en perte** (conclusion du post-mortem, excursions, contexte), **blocage** par une annonce majeure. L'envoi est non bloquant (file + thread dédié) : une panne réseau ne ralentit ni n'interrompt jamais le moteur. Une même alerte répétée n'est pas renvoyée avant `NOTIFY_COOLDOWN_SECONDS` (15 min par défaut).

## Historique (SQLite)

Base locale `data/trading_bot.db` (créée automatiquement, exclue de git) : webhooks reçus (`webhooks_log`), articles analysés (`market_sentiment`), décision du moteur pour chaque symbole à chaque tour (`trade_decisions`). L'historique plus ancien que `DB_RETENTION_DAYS` (90 jours) est purgé au démarrage. Consultation via l'API, paginée et filtrable :

| Route | Filtres |
|---|---|
| `GET /api/history/webhooks` | `source`, `accepted`, `limit`, `offset` |
| `GET /api/history/trades` | `symbol`, `status` (répétable), `trades_only`, `since`, `limit`, `offset` |
| `GET /api/history/sentiment` | `symbol`, `label`, `limit`, `offset` |

## Backtest

Rejoue la stratégie bougie par bougie avec le **même code** que le bot : signal technique, `decide()` (poids, seuil, confirmation technique), SL/TP, puis `calculate_position_size` et `validate_trade_risk` exécutés contre un broker simulé (pertes journalières et consécutives comprises). Décision à la clôture d'une bougie, entrée à l'ouverture de la suivante (aucune donnée future).

```powershell
python -m backtesting.runner --symbol EURUSD --mt5 --bars 5000                 # historique du terminal MT5
python -m backtesting.runner --symbol EURUSD --csv data\EURUSD_M15.csv         # CSV standard ou export MT5
python -m backtesting.runner --symbol XAUUSD --mt5 --calendar annonces.csv --out reports\xau
```

Métriques : rendement total, drawdown maximal, taux de réussite, profit factor, gain/perte moyens, espérance par trade, et motifs de refus du risque. `--out` écrit `trades.csv`, `equity.csv` et `report.json`.

Limites : un symbole par backtest ; sentiment neutre par défaut (`--sentiment database` rejoue les articles historisés) ; calendrier uniquement via `--calendar` ; profits calculés dans la devise de cotation (exact pour les paires en USD sur un compte USD) ; swaps et glissement hors gaps non simulés.

### Backtest des stratégies à setups

[backtesting/setup_engine.py](backtesting/setup_engine.py) rejoue les stratégies à setups (`SETUP_STRATEGIES`, ou `--strategies momentum_breakout`) avec la **même séquence de décision qu'en direct** (`BaseStrategy.evaluate` : détection, fenêtre de sécurité des annonces, taille, règles de risque, vérificateur indépendant).

```powershell
python -m backtesting.setup_engine --symbol EURUSD --mt5 --bars 6000 --slippage 1     # M15 + H1 du terminal
```

Garanties anti-anticipation (vérifiées par `tests/test_backtest.py`) :
- une stratégie ne voit que les bougies **clôturées** de chaque timeframe (une H1 n'est visible qu'à sa fermeture) ; empoisonner le futur ne change aucune décision passée ;
- les pivots ne sont utilisés qu'après leurs 2 bougies de confirmation (détecteur de régime causal) ;
- le calendrier rejoué ne garde que l'horaire et le consensus des annonces : le chiffre publié (`actual`, souvent révisé) est retiré ;
- décision à la clôture de la bougie, exécution à l'ouverture de la suivante : achat à l'Ask (+ glissement), vente au Bid (- glissement) ; un setup expiré avant l'exécution est abandonné.

Coûts : spread historique (ou `--spread`), glissement défavorable sur l'entrée et les stops, TP exécuté à son prix, gaps exécutés à l'ouverture. SL et TP touchés dans la même bougie : le SL est retenu et le trade est marqué « ambigu » (compté dans le rapport).

Métriques : espérance nette (en devise et en R), taux de réussite, gain/perte moyens, profit factor, drawdown maximal, plus longue série de pertes, segmentées par stratégie, actif, horizon, régime et sens.

## Simulation, mode analyste et setups

- **Simulation** (`BROKER_TYPE=sim`, [brokers/sim_broker.py](brokers/sim_broker.py)) : le bot, l'API et le dashboard tournent sans terminal MT5 ni internet. Bougies fictives reproductibles (`SIM_SEED`) ou CSV locaux (`SIM_DATA_DIR`), compte virtuel (`SIM_BALANCE`) dont les positions se ferment à leur SL / TP. Tout est marqué SIMULATION (logs, compte, dashboard).
- **Mode analyste** : `python main.py --analyst` (ou `--analyst --watch 60`) affiche les setups détectés, avec taille de position et contrôle du risque, sans jamais passer d'ordre.
- **Setups** : les stratégies héritent de `BaseStrategy` ([strategies/base.py](strategies/base.py)), décrivent leur plan d'achat (la vente est calculée sur la série miroir, symétrie exacte) et produisent des `TradingSetup` ([models/schemas.py](models/schemas.py)) au statut surveillance / prêt / expiré / invalidé. Formules communes : marge b = max(0,10 ATR, 2 spreads, 1 tick), ratio net = (gain - spread) / (perte + spread) >= 3, entrée refusée à plus de 0,20 ATR de la clôture de confirmation.
  - [Pullback de tendance](strategies/pullback.py) : tendance confirmée (H1), retour sur le dernier sommet cassé (± 0,20 ATR), creux structurel intact, cassure du sommet de correction en M15 ; SL creux de correction - b, TP pour un ratio net de 3.
  - [Rejet de range](strategies/range_rejection.py) : range validé (ADX <= 20, 2 contacts par borne), balayage de la borne basse, réintégration en 3 bougies max, bougie haussière clôturant dans son tiers supérieur ; SL extrême du balayage - b, TP borne haute - b, rejet si ratio net < 3.
  - [Breakout momentum](strategies/momentum_breakout.py) : contexte H1 sans choc ni tendance opposée ; en M15, clôture au-dessus du plus haut des 40 bougies précédentes (+ 0,10 ATR) avec un corps >= 1,2 ATR(14) ; jamais d'entrée sur la bougie d'impulsion : micro-respiration de 1 à 3 bougies sans clôture sous le niveau cassé, puis relance (bougie haussière clôturant au-dessus du plus haut précédent ; filtre de force facultatif : corps >= `STRATEGY_MOMENTUM_RESTART_BODY_ATR` x ATR(5), 0 = désactivé, le défaut) sur la dernière bougie clôturée ; SL plus bas de la respiration - m x b, m = multiplicateur de l'actif (`STRATEGY_MARGIN_MULTIPLIERS`, 1.0 par défaut, 1.5 pour l'or et l'argent), TP recalculé sur ce stop pour le ratio net `STRATEGY_MOMENTUM_TARGET_RR` (3 par défaut ; en dessous de `RISK_MIN_NET_REWARD_RISK`, le démarrage est refusé) ; trailing stop progressif facultatif en backtest (`STRATEGY_MOMENTUM_TRAILING_ACTIVATION_R` / `_DISTANCE_R`, sorties marquées « trailing ») ; respiration de plus de 3 bougies : setup annulé ; validité une bougie M15 ; **sessions de liquidité** uniquement (Londres 07:00-10:00 UTC, New York 12:30-16:00 UTC) : décision ou bougie de cassure hors de ces fenêtres -> setup invalidé « Hors session de liquidité », avant toute taille de position.
  - Sélecteur : [strategies/registry.py](strategies/registry.py), stratégies actives choisies par `SETUP_STRATEGIES` (toutes par défaut), utilisé par le mode analyste, le dashboard et le backtest.
- **Heure du serveur** : MT5 horodate les bougies à l'heure de son serveur. [utils/server_time.py](utils/server_time.py) les convertit en UTC réel avec `MT5_SERVER_TIMEZONE` (défaut `Europe/Athens` : UTC+2 l'hiver, UTC+3 l'été, vérifié sur MetaQuotes-Demo), pour les sessions de liquidité et pour rejouer le calendrier économique en backtest.
- **Régime** : [core/regime_detector.py](core/regime_detector.py), strictement causal : pivots confirmés 2-2 (disponibles à la clôture de la 2e bougie suivante, jamais redessinés), structure de marché sur les 3 derniers sommets / creux, ATR(14) et ADX(14) de Wilder (100 bougies de préchauffage, ATR de référence = celui d'avant la bougie jugée). Régimes : tendance haussière / baissière (structure + ADX >= 25), range (ADX <= 20 et bornes validées), choc (bougie >= 3 ATR, volatilité >= 1,5 x sa médiane, spread >= 2 x sa médiane, annonce majeure), transition sinon. Pivots classiques de la séance précédente pour les objectifs.

## Profils de stratégie

`STRATEGY_PROFILE` fixe les valeurs par défaut de la stratégie ; toute variable renseignée dans le `.env` reste prioritaire.

| | `standard` (défaut) | `scalping` | `micro` |
|---|---|---|---|
| Timeframe / boucle | M15 / 60 s | M1 / 10 s | M1 / 10 s |
| Indicateurs | EMA 20/50, RSI 14, MACD 12/26/9, ATR 14 | EMA 5/13, RSI 7, MACD 5/13/4, ATR 7 | comme scalping |
| Seuils (hybride / technique) | 0.6 / 0.3 | 0.2 / 0.2 | aucun : tout score non nul |
| Pondération technique / sentiment | 0.7 / 0.3 | 0.85 / 0.15 | 1.0 / 0 (sentiment ignoré) |
| SL / TP | 1.5 / 3.0 ATR, élargi au dernier plus bas / haut | 1.0 / 1.2 ATR, au moins 3 spreads | 5 / 5 pips, au moins 3 spreads |

**Profil `adaptive`** ([strategies/regime.py](strategies/regime.py)) : à chaque tour et pour chaque symbole, le bot choisit son style :

| Mode | Quand | Signal | Objectifs et gestion |
|---|---|---|---|
| Swing | EMA20/50 D1 et H1 alignées (écart ≥ 0,5 ATR), sentiment pas opposé | signal H1 dans le sens D1, RSI D1 non épuisé | SL 2 ATR H1, TP 4 ATR H1, trailing stop à 2 ATR dès 1 ATR de gain |
| Day | ouvertures de sessions (`ADAPTIVE_DAY_WINDOWS`, UTC) | croisement EMA9/21 M15 dans la tendance H1, pas de support / résistance avant le TP | SL 10-20 pips, TP 15-30 pips, fermeture à `ADAPTIVE_DAY_CLOSE` |
| Scalping | heures liquides, ATR M5 ≤ 1,2 × sa médiane | tout micro-signal M1 (EMA 5/13, RSI 7, MACD 5/13/4) | SL / TP 5 pips, jamais coupé à la main |

Le mode figure dans le commentaire de l'ordre et dans le journal des décisions. Quelle que soit la stratégie, une perte journalière de `RISK_MAX_DAILY_LOSS_PCT` déclenche le **kill switch** : fermeture de toutes les positions du bot, pause du moteur et journée verrouillée. Le profil adaptive n'a pas de backtest : le simulateur ne rejoue qu'un seul timeframe.

Les règles de risque (1 % par trade, perte journalière, pertes consécutives, risque cumulé) s'appliquent à l'identique. Backtest sur l'historique MT5 réel (EURUSD, GBPUSD, XAUUSD, M1 et M5, 3 jours à 4 semaines) : le profil scalping est perdant dans toutes les variantes testées (taux de réussite 20-44 %, profit factor 0.48-0.96), le spread représentant une part importante de chaque objectif. Le profil micro ouvre un signal sur presque chaque bougie M1 et perd lui aussi (taux de réussite 28-39 % pour un objectif symétrique de 5 pips, -19 à -36 % sans la limite de pertes consécutives, -0.5 à -6 % avec).

## Calendrier économique et fenêtre de sécurité

[core/news_pipeline.py](core/news_pipeline.py) :

- **Sources interchangeables** : Forex Factory (semaine en cours : heure, devise, importance, consensus, précédent) et fournisseur simulé (code ou `NEWS_CALENDAR_FILE`, résultat réel compris). Les événements sont fusionnés et dédoublonnés ; une source en panne n'arrête pas les autres.
- **Fenêtre de sécurité** : aucune nouvelle proposition (moteur et setups des stratégies) de 30 min avant à 15 min après une annonce à fort impact sur une devise de l'actif. Calendrier indisponible : propositions suspendues par précaution. Une seule alerte par annonce.
- **Analyse structurée** (`NewsAnalysis`, Pydantic) : fait, source et fiabilité, actifs concernés, surprise (réel vs consensus, indicateurs inversés comme le chômage), effet par actif et horizon, conséquence sur les setups en cours (validé / invalidé / neutre). Analyse par règles ; un analyseur LLM peut implémenter la même interface.

## Taille de position et vérificateur indépendant

- **Calculateur** ([risk/calculator.py](risk/calculator.py)) : volume = budget / perte pour 1 lot (taille du contrat, valeur du tick), budget = min(1 % du capital, 2 % - risque déjà ouvert), arrondi **vers le bas** au pas du broker ; refus si le volume minimal dépasse le budget ou si une position ouverte n'a pas de SL.
- **Vérificateur indépendant** ([risk/verifier.py](risk/verifier.py)), recalculé à part et bloquant avant tout ordre (moteur) ou setup (stratégies) : ratio net >= `RISK_MIN_NET_REWARD_RISK` (3), spread <= 10 % de la distance entrée-stop, <= 2 x sa médiane 60 min et <= plafond de l'actif (30 points), perte du jour < 3 %, moins de 2 pertes consécutives.
- **Journal et mémoire des setups** : chaque setup validé est vérifié par la mémoire des erreurs puis journalisé dans `trade_decisions.json` ([core/feedback_logger.py](core/feedback_logger.py)).

## Connexion à un compte MT5 depuis le dashboard

Le bouton **Changer de compte** (bandeau du compte) ouvre un formulaire numéro de compte / mot de passe de trading / serveur, envoyé à `POST /api/auth/login`. Le bot appelle `mt5.login()` sur son terminal, sans toucher au `.env` ; `POST /api/auth/logout` revient au compte du `.env`, `GET /api/auth/session` indique le compte actif.

- Le terminal MT5 n'a qu'un compte actif : la connexion change le compte **du bot** (vu par tous les écrans). Pour plusieurs comptes simultanés, utiliser le mode multi-comptes.
- Sécurité : chaque changement de compte met le moteur **en pause et en PAPER** ; reprendre et passer en LIVE reste une action explicite. Un échec (403, message clair) rétablit le compte précédent et son état de trading. 5 tentatives échouées au plus par tranche de 5 minutes (429).
- Le mot de passe n'est ni journalisé, ni renvoyé, ni écrit sur disque ou dans le navigateur ; il reste en mémoire du bot pour les reconnexions automatiques (perdu au redémarrage : retour au compte du `.env`).
- Dans MT5 (Outils → Options → Expert Advisors), l'option « Disable algorithmic trading when the account has been changed » coupe Algo Trading à chaque changement de compte : le dashboard le signale, réactivez-le avant de passer en LIVE.
- Hors de l'ordinateur du bot (téléphone, réseau local), le mot de passe transite en clair si l'API est en HTTP : préférer HTTPS.

## Mémoire des erreurs

[learning/trade_memory.py](learning/trade_memory.py) ferme la boucle entre les trades et leurs résultats (`data/learning/`) :

1. **Journal d'entrée** (`trade_decisions.json`) : à chaque ordre exécuté, instantané complet (prix, SL / TP, mode, RSI, EMA, MACD, ATR, volatilité relative, spread en points et rapporté au SL, sentiment, raison du signal).
2. **Post-mortem** (`loss_analysis.json`) : à la clôture d'une position perdante (historique MT5), durée, excursions défavorable / favorable en pips et facteurs d'échec probables : spread trop lourd, bruit / impulsion (stop touché en ≤ 2 bougies), faux signal en range (EMA collées), mouvement épuisé (RSI), sentiment contraire, prix jamais allé dans le bon sens.
3. **Mémoire** (`mistakes_memory.json`) : chaque trade clos alimente les statistiques de son pattern (symbole, mode, sens, zone RSI, tendance, volatilité, spread, sentiment, session). Un pattern perdant `MISTAKE_MIN_LOSSES` fois avec au moins `MISTAKE_MIN_LOSS_RATE` de pertes bloque les ordres identiques : « Ordre rejeté par la mémoire des erreurs - pattern similaire au trade perdant #… ». Les gains sur le même pattern lèvent le blocage ; un pattern est oublié après `MISTAKE_MEMORY_DAYS` jours sans perte.

Consultation : `GET /api/learning`, panneau « Mémoire des erreurs » de la vue Données, étapes « Mémoire » et « Post-mortem » du journal des décisions. Les positions simulées (PAPER) sont journalisées mais pas analysées.

## Plateformes (MT5 / crypto)

Le moteur, le risque et l'API ne parlent qu'à `BaseBroker` (`connect`, `get_balance`, `get_positions`, `place_order`, `close_order`, données de marché). `BROKER_TYPE` choisit l'adaptateur :

| | `mt5` (défaut) | `ccxt` |
|---|---|---|
| Marchés | Forex, métaux, indices, CFD | Contrats perpétuels linéaires USDT |
| Plateformes | Tout broker MT5 (MetaQuotes, Exness avec `MT5_SYMBOL_SUFFIX=m`...) | `binanceusdm`, `bybit`, `okx`... |
| Volume | Lots, bornés par `RISK_MIN_LOT` / `RISK_MAX_LOT` | Contrats, bornés par les limites de l'exchange (minimum, pas, valeur minimale) |
| Spread max | `RISK_MAX_SPREAD_POINTS` | `RISK_MAX_SPREAD_PCT` (% du prix) |
| SL / TP | Attachés à la position | Ordres conditionnels *reduce-only* ; SL refusé = position refermée aussitôt |

La taille de position suit la même règle partout (perte au SL = `RISK_PER_TRADE_PCT` du capital), calculée avec la taille de contrat du marché. En PAPER sans clé API, l'adaptateur ccxt lit les données publiques réelles et utilise un capital fictif (`CCXT_PAPER_BALANCE`). Pour un vrai compte, commencer par `CCXT_ENVIRONMENT=demo` avec une clé limitée au trading (jamais de droit de retrait).

Chaque incident broker (réseau, authentification, limite de requêtes, ordre refusé...) est classé et consigné dans `logs/broker_errors.log` ; `GET /api/broker` renvoie la plateforme, l'état de la connexion et les derniers incidents. Une perte de connexion déclenche une seule alerte, puis le bot se reconnecte au tour suivant.

## Multi-comptes

Avec `MT5_ACCOUNTS=main,test` (voir `.env.example`), `python main.py` démarre un **superviseur** : un processus bot par compte, chacun relié à **son propre terminal MetaTrader 5** (une copie du dossier d'installation par compte) ou, avec `ACCOUNT_<ID>_BROKER=ccxt`, à son exchange crypto, avec ses propres règles de risque (`ACCOUNT_<ID>_RISK_PER_TRADE_PCT`...), sa base SQLite (`data/trading_bot_<id>.db`) et ses logs (`logs/<id>/`). Un compte arrêté est redémarré automatiquement (5 s, 10 s... 2 min) avec une alerte. Chaque compte étant un processus distinct, la panne d'un exchange ou d'un terminal n'interrompt jamais les autres comptes.

La passerelle (port `API_PORT`) expose `GET /api/accounts` (synthèse de tous les comptes) et relaie toutes les routes de chaque compte via `/api/accounts/{id}/api/...`. Le dashboard affiche alors une barre de comptes : le compte sélectionné pilote tout l'écran (positions, graphique, kill switch). Chaque alerte Telegram / Discord indique le compte concerné.
