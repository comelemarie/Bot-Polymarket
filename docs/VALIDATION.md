# Validation : du test unitaire à l'argent réel

La réussite des tests prouve que le code fait ce qu'il prétend faire. Elle ne prouve **pas** que la
stratégie gagne de l'argent. La rentabilité se démontre séparément, sur des données hors échantillon
et en nombre suffisant.

## Étape 1 — Tests unitaires et replay

```bash
python -m pytest
python -m backtest.replay --from 2026-11-01 --to 2026-11-08 --out state/replay-dev.db
python -m analytics.performance_tracker --db state/replay-dev.db --outcomes-db state/bot.db
```

Il faut d'abord des enregistrements, qu'on obtient en faisant tourner le bot en PAPER.

## Étape 2 — Simulation réaliste

Le replay applique déjà :
- les dates réelles de réception ;
- la latence du modèle (mesurée en direct) et les latences de traitement et d'exécution
  (configurables) ;
- le carnet **à l'instant de décision** ;
- les frais (taux enregistré, ou taux de repli pessimiste) et le slippage ;
- les rejets FOK quand la profondeur manque.

Il ne simule **pas** l'impact de nos propres ordres ni la file d'attente : ses résultats sont donc un
majorant.

## Étape 3 — Paper trading en direct

Mode par défaut. Au minimum plusieurs semaines, et surtout **au moins 30 trades soldés, idéalement
plus de 100**, avant toute conclusion.

À regarder dans le rapport :
- `brier.modele` comparé à `brier.prix_du_marche_au_declenchement` : sans avantage probabiliste, il n'y
  a rien à exploiter ;
- `calibration` : la fréquence observée de chaque tranche doit tomber dans son intervalle à 95 % ;
- `pnl.realise_net_usdc`, avec l'intervalle de confiance du rendement par trade ;
- `decisions_rejetees_gain_hypothetique_par_part` : si les refus auraient été gagnants en moyenne, les
  seuils sont trop stricts ; dans le cas inverse, les garde-fous jouent leur rôle.

## Étape 4 — Réel, capital limité

Voir `docs/LIVE.md`. Ne commencer qu'avec un capital dont la perte totale est acceptable, et des
plafonds plus bas qu'en papier.

## Biais à surveiller

- **Sélection :** les marchés découverts sont les plus actifs *à l'époque*. Le replay utilise la liste
  enregistrée à chaque instant, jamais la liste actuelle.
- **Fuite du futur :** ne jamais rappeler Claude sur des événements passés pour un backtest. Le modèle
  peut connaître l'issue. Le replay n'utilise que les analyses produites en direct.
- **Sur-ajustement :** chaque seuil ajusté sur une période la rend inutilisable comme test.
- **Période favorable :** une semaine d'actualité très chargée ne représente pas un mois normal. Il
  faut comparer plusieurs périodes.
- **Petit échantillon :** avec 20 trades, un taux de gain de 60 % a un intervalle à 95 % d'environ
  [39 %, 78 %]. Ce n'est pas distinguable du hasard.
