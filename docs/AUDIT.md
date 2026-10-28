# Audit du bot (état au commit 919f3a9)

Périmètre : les 13 modules Python (~2 200 lignes), la configuration, les 20 tests et la documentation.
Méthode : relecture du code, comparaison avec le code source de `py-clob-client 0.34.6` installé, et avec
la documentation Polymarket trouvée par recherche web. docs.polymarket.com n'était pas joignable
directement depuis l'environnement de développement (domaine non autorisé), donc les points marqués
**[à vérifier]** reposent sur des extraits de documentation, pas sur une lecture complète.

## Ce qui fonctionne et sera conservé

| Élément | État |
|---|---|
| Découpage ingestion / brain / execution | Sain, conservé |
| Découverte automatique des marchés (Gamma + mots-clés Haiku en cache) | Conservée |
| Sorties structurées Claude (Pydantic + `messages.parse`) et repli en cas de refus | Conservés |
| Plafonds Kelly fractionné / exposition par marché / réserve USDC | Conservés et étendus |
| Supervision des tâches asyncio, reconnexion WebSocket avec backoff | Conservées |
| Déploiement launchd + pmset | Conservé |

## Problèmes identifiés, par gravité

### Critiques (argent réel ou intégrité des données)

1. **Passage en réel par un seul booléen.** `dry_run: false` suffit à envoyer des ordres réels. Aucune
   confirmation, aucun contrôle des limites ni de la synchronisation des flux.
2. **Réponse d'ordre ambiguë traitée comme un échec.** `py-clob-client` transforme une erreur réseau ou
   un délai dépassé *après envoi* en `PolyApiException(status_code=None)`. Le code actuel considère
   l'ordre comme non exécuté, ce qui peut provoquer un double ordre. Il n'existe ni identifiant
   d'idempotence, ni réconciliation avec le compte réel.
3. **Frais ignorés.** Polymarket applique désormais des frais de taker `C × feeRate × p × (1 − p)`
   (taux par marché via `GET /fee-rate`), prélevés en parts à l'achat. L'avantage est donc surestimé
   d'environ un point de probabilité à p = 0,5 pour un taux de 0,04.
4. **Carnet « périmé » mal défini.** `is_fresh` exige une mise à jour du carnet dans les 120 dernières
   secondes. Un marché calme mais parfaitement synchronisé est donc jugé périmé : le bot n'y entre
   jamais et, surtout, **le stop-loss ne s'y déclenche jamais**. À l'inverse, après une déconnexion,
   l'ancien carnet reste utilisable jusqu'à 120 s.
5. **Aucun contrôle de cohérence du carnet.** Les champs `best_bid`/`best_ask` fournis par chaque
   `price_change` ne sont pas comparés au carnet reconstruit. Les carnets croisés ou les prix hors
   ]0,1[ ne sont pas détectés, `tick_size_change` est ignoré et il n'y a aucune resynchronisation.
6. **Horodatage inventé.** Un item RSS sans date reçoit `datetime.now()` comme date de publication.
   La date de réception n'est pas enregistrée.

### Majeurs (qualité des décisions)

7. **La confiance de Claude est utilisée comme multiplicateur** (`shrink_to_market`). La confiance
   subjective devient ainsi un paramètre de rentabilité, sans aucune calibration.
8. **Pas d'incertitude sur la probabilité** : seule l'estimation ponctuelle est utilisée, et il n'existe
   pas de voie d'abstention explicite.
9. **Une seule voie de détection** (actualité + mouvement de prix). Les opportunités antérieures à la
   réaction du marché sont exclues par construction, et les mouvements sans actualité ne sont pas
   tracés.
10. **Pas de dédoublonnage entre sources.** Google News republie les dépêches de BBC, NYT, etc. avec un
    identifiant différent.
11. **État du filtre en mémoire** (actualités vues, cooldowns) : il est perdu au redémarrage, ce qui
    entraîne de nouvelles analyses et de nouveaux coûts.
12. **Risque incomplet** : pas de plafond par événement, par thème ni par position, pas de drawdown
    maximal. Le jour du coupe-circuit est calculé en heure locale et non en UTC.
13. **Claude sans plafond de coût, sans cache, sans déduplication des requêtes**, et sans journal
    complet des prompts et réponses. Les URL citées ne sont pas vérifiées.
14. **Une seule raison de sortie** (prix). L'invalidation de la thèse et le plafond de perte défini à
    l'entrée manquent.

### Manques fonctionnels

15. Pas de persistance structurée : un JSON et un JSONL, sans identifiants reliant actualité →
    analyse → décision → ordre.
16. Pas de mode replay, pas d'enregistrement des données de marché, pas de suivi de performance
    (Brier, calibration, PnL net).
17. Pas d'arrêt d'urgence pilotable.
18. Les tests ne couvrent ni les coupures WebSocket, ni les réponses ambiguës, ni les redémarrages, ni
    le blocage du mode réel.

## Plan d'implémentation (par priorité)

1. **Sécurité et cohérence des données**
   - modes PAPER / LIVE / REPLAY avec déverrouillage du réel en trois clés ;
   - horloge injectable ;
   - persistance SQLite ;
   - `NewsItem` complet et dédoublonnage multi-sources ;
   - `market_state.py` (synchronisation, validation, resynchronisation) ;
   - masquage des secrets dans les logs.
2. **Fiabilité de l'exécution**
   - `execution_engine.py` : intention écrite avant l'envoi, idempotence, état UNKNOWN,
     réconciliation ;
   - séparation courtier papier / courtier réel ;
   - vérification REST du carnet avant chaque ordre ;
   - arrêt d'urgence.
3. **Avantage net** : `edge_calculator.py` (VWAP sur le carnet, frais réels, slippage, intervalle de
   probabilité prudent).
4. **Risque**
   - plafonds par position, marché, événement et thème ;
   - drawdown et perte journalière en UTC ;
   - réduction de la mise selon l'incertitude ;
   - refus si le solde ou l'exposition sont inconnus.
5. **Tests et replay** : enregistreur des flux bruts, `backtest/replay.py` chronologique (latences,
   carnet à l'instant de décision, analyses enregistrées), tests des cas listés dans la mission.
6. **Performance** : `analytics/performance_tracker.py` (PnL, drawdown, Brier, calibration,
   abstentions), métriques et latences.

Hors périmètre volontaire : bus de messages, base distribuée, interface web. SQLite et asyncio suffisent
à cette échelle.
