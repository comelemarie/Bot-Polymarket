# Bot-Polymarket

Bot de trading événementiel sur Polymarket. Il détecte des actualités susceptibles de modifier la
probabilité d'un événement, les confronte au carnet d'ordres, fait estimer une probabilité par Claude,
puis vérifie de façon déterministe qu'un avantage net subsiste après frais. Il ne passe un ordre que
dans les limites d'un moteur de risque codé en dur.

> **Mode par défaut : PAPER.** Aucun ordre réel n'est envoyé. Le mode réel exige quatre conditions
> indépendantes (`docs/LIVE.md`).
>
> **Rien ne démontre aujourd'hui que cette stratégie est rentable.** Les tests prouvent que le code
> fait ce qu'il doit faire, pas qu'il gagne de l'argent. La rentabilité se mesure hors échantillon
> (`docs/VALIDATION.md`).

## Architecture

```
 X API / RSS ──► rss_feed / twitter_stream ──► news_normalizer ──┐  (dédoublonnage multi-sources,
                                                                │   publication ≠ réception)
 WebSocket CLOB ──► polymarket_ws ──► market_state ─────────────┤  (carnets synchronisés et validés)
                                                                ▼
                                  news_filter (local, 3 voies, état persistant)
                       actualité seule │ actualité + mouvement │ mouvement seul → anomalie, AUCUN ordre
                                                                ▼
                                  claude_analyst  → probabilité + intervalle + abstention
                                  (JSON validé, budget et coût plafonnés, cache, journal complet)
                                                                ▼  après la latence réelle du modèle
                                  edge_calculator → avantage net (carnet réel, frais, slippage, borne prudente)
                                                                ▼
                                  risk_manager    → Kelly fractionné + plafonds + coupe-circuits
                                                                ▼
                                  execution_engine → intention en base, vérification finale, FOK,
                                  paper_broker | polygon_clob     réconciliation des ordres incertains
                                                                ▼
                       SQLite (state/bot.db) : actualité → événement → analyse → décision → ordre → résultat
                       recorder (data/recordings) ──► backtest/replay ──► analytics/performance_tracker
```

| Module | Rôle |
|---|---|
| `core/config.py` | Configuration validée (clé inconnue = erreur), modes, secrets |
| `core/modes.py` | Déverrouillage du mode réel (4 conditions + bornes de risque) |
| `core/store.py`, `core/state.py` | Persistance SQLite, portefeuille, coupe-circuits persistants |
| `core/clock.py` | Horloge réelle ou simulée (replay sans fuite du futur) |
| `ingestion/news_normalizer.py` | Empreintes, similarité, URL canoniques, fiabilité des sources |
| `ingestion/market_state.py` | Carnets : instantané, deltas, contrôle des meilleurs prix serveur, désynchronisation |
| `ingestion/polymarket_ws.py` | Connexions WebSocket, resynchronisation, métadonnées Gamma |
| `ingestion/clob_rest.py` | Carnet REST avant ordre, taux de frais, résolutions |
| `ingestion/news_filter.py` | Détection locale à 3 voies |
| `ingestion/market_discovery.py` | Sélection automatique des marchés et mots-clés (Haiku) |
| `ingestion/recorder.py` | Enregistrement brut des flux pour le replay |
| `brain/claude_analyst.py` | Analyse structurée, validation, budget, cache |
| `execution/edge_calculator.py` | Avantage net, VWAP, frais, Kelly |
| `execution/risk_manager.py` | Décision d'entrée et de taille, règles de sortie |
| `execution/execution_engine.py` | Cycle de vie des ordres, idempotence, réconciliation, arrêt d'urgence |
| `execution/paper_broker.py`, `execution/polygon_clob.py` | Courtier simulé, courtier réel |
| `bot.py`, `main.py` | Orchestration, CLI |
| `backtest/replay.py` | Replay chronologique |
| `analytics/performance_tracker.py` | PnL, drawdown, Brier, calibration, abstentions |

## Installation

Python 3.10 ou plus récent.

```bash
git clone https://github.com/comelemarie/Bot-Polymarket.git && cd Bot-Polymarket
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env && chmod 600 .env    # renseigner ANTHROPIC_API_KEY
python -m pytest                           # 87 tests
```

## Lancement en simulation (PAPER)

```bash
python main.py
python main.py --status                    # dans un autre terminal
python -m analytics.performance_tracker    # rapport
```

Dans les premières minutes, le bot écrit `Decouverte: … marches retenus`, `Abonnement WebSocket: …`
et `MODE PAPER`. Toutes les décisions, y compris les refus et leurs motifs, sont dans la table
`decisions` :

```bash
sqlite3 state/bot.db "SELECT created_at, market_slug, action, reason FROM decisions ORDER BY created_at DESC LIMIT 20"
```

Sur un Mac mini 24 h/24 : `./deploy/setup_macmini.sh` (service launchd, redémarrage automatique).

### Avec Docker

```bash
docker build -t polybot .
docker run -d --name polybot --restart unless-stopped --env-file .env \
  -v "$PWD/state:/app/state" -v "$PWD/logs:/app/logs" -v "$PWD/data:/app/data" polybot
docker logs -f polybot                                  # suivre les logs
docker exec polybot python main.py --status             # état, coupe-circuits
docker exec polybot python main.py --halt               # arrêt d'urgence
```

- `.env` n'est **jamais** copié dans l'image (`.dockerignore`) : les secrets sont passés au lancement
  avec `--env-file`.
- Les volumes `state/`, `logs/` et `data/` conservent la base (positions, ordres, budget Claude), les
  logs et les enregistrements quand le conteneur est recréé. Sans eux, le bot repartirait sans mémoire
  de ses positions.
- `--restart unless-stopped` relance le conteneur après un crash ou un redémarrage de la machine.
- `docker stop` envoie SIGTERM : le bot s'arrête proprement, mais cela ne ferme aucune position
  (`docs/OPERATIONS.md`).

## Replay et validation

```bash
python -m backtest.replay --from 2026-11-01 --to 2026-11-08 --out state/replay.db
python -m analytics.performance_tracker --db state/replay.db --outcomes-db state/bot.db
```

## Documentation

- `docs/AUDIT.md` : diagnostic de la version précédente et plan suivi.
- `docs/CONFIGURATION.md` : chaque paramètre, sa justification et sa méthode de calibration.
- `docs/VALIDATION.md` : les 4 étapes avant l'argent réel, les biais et les limites statistiques.
- `docs/LIVE.md` : passage au réel et points non vérifiés en production.
- `docs/OPERATIONS.md` : arrêt d'urgence, coupe-circuits, redémarrages, données.

## Risques et limites

- Le modèle peut se tromper avec assurance. Sa confiance n'est pas une probabilité calibrée. Seule la
  comparaison de son score de Brier avec celui du marché, sur des issues réelles, dira s'il apporte de
  l'information.
- Un stop-loss ne garantit pas le prix de sortie quand le carnet est fin ou que le prix saute.
- Le mode papier et le replay ignorent l'impact de nos ordres et la file d'attente : leurs résultats
  sont optimistes.
- Plusieurs formats d'API n'ont pas pu être vérifiés contre la production (liste dans `docs/LIVE.md`).
- Polymarket bloque le trading dans certains pays, dont la France. La lecture des données est publique.
