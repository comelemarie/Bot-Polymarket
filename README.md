# Bot-Polymarket

Bot de trading Polymarket piloté par l'actualité : il surveille X et des flux RSS, ne sollicite Claude
que lorsqu'une actualité pertinente coïncide avec un mouvement de prix, puis laisse un moteur de risque
déterministe décider de la taille de la position.

> **Le bot démarre en mode papier (`"dry_run": true`).** Aucun ordre réel n'est envoyé tant que tu
> n'as pas passé ce flag à `false`. Laisse-le tourner plusieurs jours en papier et lis
> `logs/decisions.jsonl` avant d'engager de l'argent.

## Architecture

```
            ┌────────────────────┐   ┌──────────────────────┐
  X API ───►│ twitter_stream.py  │   │  polymarket_ws.py    │◄─── WebSocket CLOB
  RSS   ───►│ rss_feed.py        │   │  carnets YES/NO      │     (book, price_change)
            └─────────┬──────────┘   └──────────┬───────────┘
                      │ NewsItem                │ variation de prix
                      ▼                         ▼
                  ┌──────────────────────────────────┐
                  │ news_filter.py (local, gratuit)  │  mot-clé ET variation ≥ seuil
                  │ fenêtre + cooldown + dédoublonnage│  dans la même fenêtre
                  └────────────────┬─────────────────┘
                                   │ Trigger (rare)
                                   ▼
                  ┌──────────────────────────────────┐
                  │ claude_analyst.py                │  JSON strict (structured outputs)
                  │ → P(YES), confiance, déjà pricé ?│  budget d'appels/heure et /jour
                  └────────────────┬─────────────────┘
                                   ▼
                  ┌──────────────────────────────────┐
                  │ risk_manager.py (sans IA)        │  Kelly fractionné + plafonds
                  └────────────────┬─────────────────┘
                                   ▼
                  ┌──────────────────────────────────┐
                  │ polygon_clob.py                  │  ordres FOK via py-clob-client
                  │ + surveillance stop-loss         │  (ou simulation papier)
                  └──────────────────────────────────┘
```

```
Bot-Polymarket/
├── .env.example            # Clés API (Anthropic, X, clé privée Polygon) → copier en .env
├── config.json             # Limites de risque, budget Claude, sources, découverte des marchés
├── main.py                 # Boucle asyncio principale + tâches supervisées
├── core/                   # Config, modèles, état persistant (positions, PnL du jour)
├── ingestion/
│   ├── twitter_stream.py   # X API v2 : polling (recherche récente) ou filtered stream
│   ├── rss_feed.py         # Flux RSS d'agences (gratuit, complément de X)
│   ├── news_filter.py      # Filtre local : mot-clé + variation de prix
│   ├── market_discovery.py # Sélection automatique des marchés + mots-clés générés par Haiku
│   └── polymarket_ws.py    # Métadonnées Gamma + carnet d'ordres temps réel
├── brain/
│   └── claude_analyst.py   # Requête structurée à l'API Anthropic
├── execution/
│   ├── risk_manager.py     # Kelly, plafonds, stop-loss, coupe-circuit
│   └── polygon_clob.py     # Envoi des ordres (py-clob-client) / simulation papier
├── deploy/                 # LaunchDaemon + script de configuration du Mac mini
└── tests/                  # pytest (moteur de risque, filtre, pipeline de bout en bout)
```

## Ce qui a été ajusté par rapport au plan initial

| Plan initial | Choix retenu | Pourquoi |
|---|---|---|
| Claude 3.5 / 3.7 Sonnet | `claude-sonnet-5-5` (modifiable dans `config.json`) | Les modèles 3.x sont retirés. |
| JSON via `tool_choice` forcé | Structured outputs (`messages.parse` + schéma Pydantic) | Le `tool_choice` forcé renvoie une erreur 400 sur les modèles actuels ; les structured outputs garantissent un JSON conforme. |
| Scraping X via Playwright | API officielle X (polling ou stream) + RSS | Le scraping casse souvent et fait bannir les comptes. Le RSS donne une source gratuite et stable. |
| SpaCy / mini-modèle | Regex par marché | Suffisant pour démarrer, sans dépendance lourde. Les mots-clés de `config.json` sont des regex insensibles à la casse. |
| Kelly sur P_Claude brute | Kelly sur une probabilité **ramenée vers le marché** selon la confiance de Claude | Limite l'effet d'une estimation trop sûre d'elle. |
| — | Coupe-circuit journalier, contrôle du spread, plafond sur la profondeur du carnet, ordres FOK, mode papier | Garde-fous supplémentaires indispensables en réel. |

**À savoir sur le déclencheur « mot-clé + variation de prix »** : il économise beaucoup d'appels, mais
quand le prix a déjà bougé, une partie de l'edge a disparu. Mets `"require_price_move": false` pour
appeler Claude dès qu'un mot-clé apparaît, ce qui sera plus rapide mais plus cher.

## Sélection automatique des marchés

Avec `discovery.enabled: true` (réglage par défaut), le bot choisit seul les marchés à surveiller.
Il refait cette sélection toutes les heures (`refresh_interval_s`).

1. **Lecture** des events actifs sur l'API Polymarket (Gamma), triés par volume 24 h
   (jusqu'à `max_pages` × 100 events).
2. **Filtres**. Un marché est retenu s'il :
   - est un marché YES/NO ouvert aux ordres ;
   - a une liquidité ≥ `min_liquidity_usdc` et un volume 24 h ≥ `min_volume_24h_usdc` ;
   - se termine entre `min_hours_to_end` heures et `max_days_to_end` jours ;
   - n'est pas exclu par `exclude_tags`, `exclude_question_patterns` ou `exclude_slugs`.

   Si `include_tags` n'est pas vide, seules ces catégories sont gardées (ex. `["politics", "crypto"]`).
   On garde ensuite les `max_markets` plus actifs.
3. **Mots-clés** : pour chaque nouveau marché, Claude Haiku propose 3 à 8 expressions qu'on
   retrouverait dans un titre d'actualité (noms, organisations, termes précis). Les marchés sont
   traités par lots de 25 et le résultat est mis en cache dans `state/keywords_cache.json` : on ne
   paie qu'une fois par marché, quelques centimes pour 150 marchés. Si l'appel échoue, le bot utilise
   les noms propres de la question et retente au rafraîchissement suivant.

Par défaut, deux types de marchés sont exclus :
- **le sport** (`exclude_tags: ["sports"]`) : pendant un match, le prix bouge en continu, ce qui
  satisferait sans cesse la condition « variation de prix » ;
- **les marchés crypto « Up or Down »** à très courte échéance, qui réagissent au cours et non à
  l'actualité.

Retire ces exclusions si tu veux quand même ces marchés.

Un marché où tu as une position ouverte reste toujours surveillé (pour le stop-loss), même s'il ne
passe plus les filtres. La liste manuelle `markets` reste disponible : ses marchés s'ajoutent à la
sélection automatique avec tes propres mots-clés et notes.

Pour vérifier la sélection et les mots-clés générés, regarde les lignes `Decouverte:` et
`Marches surveilles:` dans `logs/bot.log`, ainsi que le fichier `state/keywords_cache.json`.

## Moteur de risque

Pour un contrat acheté au prix `p` (meilleur ask) avec une probabilité estimée `q` :

```
q_ajustée = mid_marché + confiance × (P_Claude − mid_marché)
edge      = q_ajustée − p                  (calculé pour YES et pour NO, on garde le meilleur)
Kelly     = (q_ajustée − p) / (1 − p)
mise      = min( kelly_fraction × Kelly × capital,
                 5 % du capital − exposition déjà prise sur ce marché,
                 40 % du capital − exposition totale,
                 USDC disponibles − réserve minimale,
                 50 % de la profondeur du carnet jusqu'au prix limite )
prix limite = min(ask + slippage max, q_ajustée − edge minimum), arrondi au tick
```

Aucun trade n'est passé si l'une de ces conditions est vraie : actualité non pertinente, information
déjà intégrée au prix, confiance < `min_confidence`, edge < `min_edge`, spread > `max_spread`, ordre
< `min_order_usdc`, ou coupe-circuit actif (perte du jour > `max_daily_loss_pct`).

Sortie : stop-loss si le meilleur bid tombe à `prix_entrée × (1 − stop_loss_pct)`, take-profit
si le bid dépasse `take_profit_price`. La vérification a lieu toutes les `position_check_interval_s`.

Tout est journalisé dans `logs/decisions.jsonl` : actualités, réponse de Claude, verdict du risque,
exécutions et sorties.

## Installation

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env && chmod 600 .env   # puis remplir
python -m pytest                          # 20 tests
```

1. **Marchés** : la sélection automatique est active par défaut (voir plus haut). Tu peux aussi
   ajouter des marchés à la main dans `config.json → markets`, avec leur `slug` et leurs mots-clés.
   Le slug est le dernier segment de l'URL : `polymarket.com/event/<event>/<slug-du-marché>`, ou
   `polymarket.com/event/<slug>` si l'event ne contient qu'un marché.
2. **Sources** : le RSS est actif par défaut (Google News, BBC, NYT, CNBC, Al Jazeera, CoinDesk).
   Plus tu surveilles de marchés, plus la couverture d'actualité compte. Pour X, mets `news.x.enabled` à `true`, renseigne
   `X_BEARER_TOKEN` et choisis le mode `poll` (tiers d'API d'entrée de gamme) ou `stream` (filtered
   stream, qui demande un accès supérieur).
3. **Lancer** : `python main.py`.

### Passer en réel

1. Wallet Polygon approvisionné en USDC, avec les allowances déjà accordées sur Polymarket.
   Le plus simple est d'avoir déjà tradé une fois depuis l'interface web.
2. Dans `.env` : `POLYGON_PRIVATE_KEY`, `POLYMARKET_SIGNATURE_TYPE` (0 = EOA/MetaMask,
   1 = compte email/Magic, 2 = proxy navigateur) et `POLYMARKET_FUNDER_ADDRESS` (l'adresse du proxy
   wallet affichée sur Polymarket).
3. Commence avec un capital réduit et `max_exposure_per_market_pct` bas, puis `"dry_run": false`.
4. Vérifie la première exécution dans `logs/bot.log` et sur polymarket.com.

## Mac mini 24/7

```bash
./deploy/setup_macmini.sh
```

Le script :
- désactive la veille et active le redémarrage automatique après une coupure de courant
  (`pmset autorestart 1`) ou un gel du système ;
- crée le venv et installe les dépendances ;
- installe un **LaunchDaemon** (`/Library/LaunchDaemons/com.polybot.plist`). Le bot démarre au boot
  sans qu'une session soit ouverte, et launchd le relance s'il s'arrête (`KeepAlive`, au plus une
  relance toutes les 30 s).

Le bot a aussi sa propre résilience : chaque composant (WebSocket, X, RSS, surveillance des
positions) est supervisé et relancé seul en cas d'erreur. Le WebSocket se reconnecte avec un backoff
exponentiel. L'état (positions, PnL du jour, solde papier) est écrit de façon atomique dans
`state/portfolio.json` et survit aux redémarrages. `state/heartbeat` contient le timestamp du dernier
battement (toutes les 5 min) et peut servir à une supervision externe.

**Points d'attention sur le Mac :**
- **FileVault** : s'il est actif, le Mac reste bloqué sur l'écran de déverrouillage après une coupure
  de courant, et le bot ne redémarre pas. Désactive-le, ou accepte de devoir intervenir à la main.
- **Mises à jour macOS** : désactive l'installation automatique, qui peut redémarrer la machine.
- Un onduleur (UPS) et une connexion Ethernet valent mieux que le Wi-Fi.

## Sécurité et limites

- **Injection via les tweets** : un tweet peut contenir des instructions destinées à Claude. Le prompt
  traite les actualités comme des données non fiables, et surtout les plafonds de risque codés en dur
  bornent les dégâts quelle que soit la réponse de Claude.
- **Clé privée** : `.env` est exclu de git et passé en `chmod 600`. Utilise un wallet dédié au bot,
  avec seulement le capital alloué.
- **Coût Claude** : plafonné par `max_calls_per_hour` / `max_calls_per_day` (30 / 300 par défaut).
  Un appel d'analyse coûte de l'ordre de 1 à 5 centimes de dollar, soit au pire quelques dollars à
  une quinzaine de dollars par jour avec ces plafonds. La génération des mots-clés (Haiku) est
  négligeable.
- **Accès à Polymarket** : la plateforme bloque certains pays (dont la France) pour le trading. Vérifie
  ta situation avant de passer en réel.
- Le mode papier simule les exécutions contre le carnet réel (FOK), mais pas l'impact de tes propres
  ordres ni la latence d'exécution.
