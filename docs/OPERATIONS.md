# Exploitation

## Commandes

```bash
python main.py                         # PAPER
python main.py --status                # positions, coupe-circuits, ordres incertains
python main.py --halt                  # arrêt d'urgence : plus aucune nouvelle entrée
python main.py --halt --flatten        # idem + sortie de toutes les positions (bot en cours d'exécution)
python main.py --resume                # lève les coupe-circuits manuel / drawdown / réconciliation
python -m analytics.performance_tracker
python -m backtest.replay --from ... --to ...
```

## Arrêt d'urgence

`--halt` écrit un coupe-circuit persistant en base. Le bot en cours d'exécution le lit en moins de
5 secondes. Aucune nouvelle entrée n'est alors possible, même après un redémarrage, jusqu'à
`--resume`. Les sorties (stop-loss) continuent de fonctionner.

`--halt --flatten` demande en plus au bot de :
- annuler tous les ordres au carnet (LIVE) ;
- tenter de vendre chaque position au meilleur bid moins `exit_slippage`.

Une vente peut échouer si le carnet est trop fin. Il faut alors vérifier avec `--status` et terminer à
la main sur polymarket.com. Si le bot n'est pas lancé, la demande de sortie sera exécutée à son prochain
démarrage.

**Arrêter le processus n'annule rien sur le marché.** Les positions restent ouvertes et ne sont plus
protégées par le stop-loss tant que le bot est arrêté. Le bot n'utilise que des ordres FOK, donc aucun
ordre ne devrait rester au carnet. Après un incident, il faut tout de même vérifier sur polymarket.com.

## Coupe-circuits

| Nom | Déclencheur | Levée |
|---|---|---|
| perte journalière | equity < début de journée (UTC) − `max_daily_loss_pct` | automatique le lendemain (UTC) |
| `drawdown` | equity < plus haut − `max_drawdown_pct` | `--resume` |
| `reconciliation` | écart entre positions locales et compte, ou positions indisponibles | `--resume`, après vérification |
| `manuel` | `--halt` | `--resume` |
| ordre incertain | réponse d'ordre perdue | automatique après réconciliation (marché concerné uniquement) |

## Redémarrages

L'état complet est en base (`state/bot.db`) : positions, intentions d'ordre, cooldowns, actualités vues,
budget Claude. Au redémarrage :
- une intention d'ordre sans réponse passe en **incertaine** en LIVE (elle sera vérifiée, jamais
  supposée non exécutée) et en **annulée** en simulation ;
- une même décision ne peut pas produire un second ordre (clé d'idempotence).

## Résolution des marchés

Chaque heure, le bot enregistre les marchés résolus (table `outcomes`), utilisés par le rapport.
- En PAPER, les positions sont soldées à 1 ou 0.
- En LIVE, la position passe en `resolved_pending_redeem` : le rachat (redeem) se fait sur
  polymarket.com.

## Données et journaux

- `state/bot.db` : base SQLite (mode WAL). Sauvegarde : `sqlite3 state/bot.db ".backup state/backup.db"`.
- `data/recordings/` : flux bruts gzip, par heure. Compter quelques centaines de Mo par jour pour 150
  marchés. Désactivable avec `storage.record_market_data`.
- `logs/bot.jsonl` : logs structurés. Les secrets (clé Anthropic, clés privées) y sont masqués.
