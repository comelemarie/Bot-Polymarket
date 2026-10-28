# Configuration : valeurs initiales, justification et calibration

Toutes les valeurs de `config.json` sont des **points de départ prudents, pas des valeurs optimales**.
Aucune n'a été calibrée sur des données : il n'en existe pas encore. Une clé inconnue fait échouer le
démarrage, ce qui évite qu'une faute de frappe désactive silencieusement un garde-fou.

## Méthode de calibration (valable pour tous les seuils)

1. **Collecter** en PAPER : flux bruts dans `data/recordings/`, décisions dans `state/bot.db`.
2. **Découper chronologiquement**, sans mélange :
   - développement : ~50 % des données les plus anciennes ;
   - calibration : ~25 % suivantes ;
   - test final : ~25 % les plus récentes, qu'on ne regarde qu'une fois.
3. **Ajuster** les seuils sur la période de calibration avec `python -m backtest.replay`. Une seule
   variable à la fois, avec peu de valeurs essayées : chaque essai supplémentaire augmente le risque de
   sur-ajustement.
4. **Évaluer une fois** sur la période de test. Si le résultat est décevant, ne pas réajuster sur cette
   même période : collecter une nouvelle période de test.

Limite : le replay réutilise les analyses enregistrées. Un seuil de détection plus permissif crée des
déclenchements sans analyse (comptés comme abstentions). Ces seuils-là se calibrent donc en PAPER, sur
de nouvelles périodes.

## Détection (`trigger`)

| Paramètre | Initial | Justification | Calibration |
|---|---|---|---|
| `news_only_enabled` | true | Voie 1 : ne pas exiger que le marché ait déjà réagi, sinon l'avantage est en partie consommé. | Comparer la performance par `voie` (rapport). |
| `price_move_threshold` | 0,03 | 3 points de mid sur 5 min dépassent le bruit habituel d'un marché liquide. | Distribution des mouvements sur 5 min hors actualité (événements `anomaly`). |
| `anomaly_move_threshold` | 0,05 | Plus haut que la voie 2 : sans actualité, on ne journalise que des mouvements nets. | Taux d'anomalies par jour : viser un volume lisible. |
| `market_cooldown_s` | 900 | Évite de payer plusieurs analyses pour un même événement. | Nombre d'analyses rejetées pour cause de cache ou de cooldown. |
| `max_news_age_s` | 3600 | Une actualité d'une heure est probablement déjà intégrée au prix. | Performance par `delai_reaction`. |
| `min_source_reliability` | 0,4 | Écarte les sources jugées peu fiables par défaut. | Performance par `source`. |
| `max_spread_for_analysis` / `min_depth_usdc_for_analysis` | 0,08 / 200 | Pas d'appel payant sur un marché où l'on ne pourrait pas exécuter. | Part des déclenchements filtrés. |

## Analyse (`claude`)

Les tarifs `price_*_per_mtok` correspondent à Claude Sonnet 5.5 (2 $ / 10 $ par million de tokens) et
sont à mettre à jour en cas de changement de modèle. `max_cost_usd_per_day` (10 $) est un plafond
**dur** : le budget est recalculé depuis la base et survit donc aux redémarrages. Le cache réutilise
une analyse pendant 30 min si les mêmes actualités concernent le même marché et que le prix est resté
dans le même intervalle de 2 points.

## Frais (`fees`)

Formule Polymarket pour le taker : `frais = parts × taux × p × (1 − p)`. Les frais sont prélevés en
parts à l'achat et en USDC à la vente. Le taux est lu par marché via `GET /fee-rate`. **[À vérifier]**
L'unité du champ `base_fee` est supposée en points de base, comme dans py-clob-client. Si l'API ne
répond pas, on applique `fallback_taker_fee_rate = 0,07`, le taux le plus élevé relevé dans la
documentation (crypto). Ce repli est volontairement pessimiste.

## Avantage net (`edge`)

| Paramètre | Initial | Justification |
|---|---|---|
| `min_net_edge` | 0,04 | 4 points par part après frais et slippage. Il couvre l'erreur de modèle non mesurée tant que la calibration n'est pas connue. |
| `slippage_buffer` | 0,005 | Mouvement du carnet entre la décision et l'exécution. À remplacer par le slippage observé (rapport, `execution`). |
| `min_half_width` | 0,03 / 0,07 / 0,15 | Demi-largeur minimale de l'intervalle selon l'incertitude déclarée. On n'utilise jamais un intervalle plus étroit que celui-ci. |

**Calibration :** une fois que des issues sont connues, comparer le Brier du modèle à celui du prix de
marché. Si le modèle n'est pas meilleur, aucun `min_net_edge` ne rend la stratégie rentable : le
problème est en amont.

## Risque (`risk`) et sorties (`exits`)

- **Kelly fractionné à 25 %**, appliqué à la borne prudente de la probabilité et au coût tout compris.
  Le Kelly plein suppose une probabilité connue, ce qui n'est pas le cas ici.
- **Réduction selon l'incertitude** (`uncertainty_size_multiplier`) : ×1 (faible), ×0,5 (moyenne),
  0 (forte).
- **Plafonds :**
  - 100 USDC par position ;
  - 5 % du capital par marché ;
  - 8 % par événement (des marchés d'un même événement sont souvent mutuellement exclusifs) ;
  - 20 % par thème (tag Polymarket) ;
  - 40 % au total ;
  - réserve de 100 USDC.
- **Perte journalière : 5 %** (jour UTC). **Drawdown : 15 %**. Ce dernier coupe-circuit est
  persistant et se lève à la main avec `--resume`.
- **Sorties :**
  - stop de prix à −30 % du prix d'entrée ;
  - perte maximale fixée à l'entrée (40 % du coût) ;
  - prise de profit à 0,97 ;
  - invalidation de la thèse par une nouvelle analyse.

Ces niveaux sont arbitraires et à revoir avec l'historique des sorties.

## Données de marché (`market_data`)

`validate_ws_best_prices` compare le carnet reconstruit aux `best_bid`/`best_ask` annoncés par chaque
`price_change`. C'est la seule détection des messages perdus disponible : le protocole n'a pas de
numéro de séquence. **[À vérifier]** Si les logs montrent des désynchronisations en boucle juste après
chaque instantané, la sémantique de ces champs diffère de celle supposée. Il faut alors désactiver ce
contrôle et le signaler.
