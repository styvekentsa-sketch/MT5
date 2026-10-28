# MT5 Trading Bot

Bot de trading automatisé pour MetaTrader 5 combinant analyse technique et analyse fondamentale (actualités, calendrier économique, sentiment).

> Projet en cours de développement : les modules marqués « À implémenter » sont des squelettes.

## Structure

```
config/            Paramètres (settings.py), identifiants MT5 (credentials.py), lecture du .env (env.py)
core/              Boucle principale (engine.py) et état du bot (state_manager.py)
brokers/           Wrapper MetaTrader 5 (mt5_client.py)
strategies/        Indicateurs (technical_analysis.py) et décision (strategy_manager.py)
sentiment/         Actualités et calendrier (news_scraper.py), sentiment (sentiment_analyzer.py)
risk/              Lot, SL/TP, drawdown (risk_manager.py)
utils/             Logs (logger.py), alertes (notifier.py), SQLite (database.py)
api/               API REST du dashboard (FastAPI) : main.py, schemas.py, security.py, routes/
dashboard/         Dashboard web (React + Vite + Tailwind)
backtesting/       Simulation sur historique (backtest_engine.py)
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
