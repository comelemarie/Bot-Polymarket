# Passage au mode réel (LIVE)

**Ne pas passer en réel avant d'avoir terminé les étapes 1 à 3 de `docs/VALIDATION.md`.** Le bot ne
passe jamais en réel de lui-même.

## Les quatre conditions, toutes obligatoires

| # | Où | Valeur |
|---|---|---|
| 1 | `config.json` | `"mode": "live"` |
| 2 | `config.json` | `"live": {"confirmation": "I-ACCEPT-REAL-MONEY-RISK"}` |
| 3 | `.env` | `POLYBOT_LIVE_UNLOCK=I-ACCEPT-REAL-MONEY-RISK` |
| 4 | ligne de commande | `python main.py --confirm-live` |

Sont exigés en plus :
- `POLYGON_PRIVATE_KEY`, `POLYMARKET_SIGNATURE_TYPE` et `POLYMARKET_FUNDER_ADDRESS` (si type 1 ou 2),
  plus `ANTHROPIC_API_KEY` ;
- des limites dans les bornes `live.max_allowed_*`.

Si une seule condition manque, le démarrage échoue en listant ce qui manque. `--mode paper` force
toujours le mode papier, quelle que soit la configuration.

## Vérifications au démarrage et avant chaque ordre

- **Au démarrage :**
  - les intentions d'ordre interrompues passent en état incertain ;
  - les positions sont réconciliées avec la Data API ;
  - en cas d'écart, un coupe-circuit `reconciliation` bloque toute entrée.
- **Avant chaque ordre :**
  - le carnet WebSocket est synchronisé et cohérent avec le carnet REST ;
  - le risque est réévalué avec le solde réel ;
  - le prix limite est respecté ;
  - aucun ordre n'est en état incertain sur ce marché.

## Procédure recommandée

1. Créer un wallet dédié, approvisionné avec le seul capital alloué. Faire un premier trade manuel sur
   polymarket.com pour accorder les allowances USDC.
2. Réduire les plafonds : par exemple `max_position_usdc: 10`, `max_total_exposure_pct: 0.10`.
3. Remplir les quatre conditions, puis lancer `python main.py --confirm-live`.
4. Vérifier le premier ordre dans `logs/bot.jsonl`, dans `python main.py --status` et sur
   polymarket.com.
5. Relancer le rapport chaque semaine et comparer avec la période papier.

## Points non vérifiés en conditions réelles

Les points suivants n'ont pas pu être testés contre l'API de production pendant le développement :
- le format exact de la réponse `post_order` (`makingAmount`/`takingAmount`) et le montant des frais
  pour un FOK ;
- les champs de `get_order` (`status`, `size_matched`) utilisés par la réconciliation ;
- l'unité de `base_fee` dans `/fee-rate` ;
- les champs de la Data API `/positions` (`asset`, `size`). La documentation signale une v2 :
  vérifier que la v1 répond toujours.

Le premier ordre réel doit être minuscule et contrôlé à la main.
