# Livraison des fondations HACCP — phase 1

Périmètre : backend, modèles, migration, diagnostic et contrats documentés.
Le blocage strict des commandes, les nouveaux écrans et les rappels ne sont pas encore activables.
Cette procédure est une directive de livraison ; aucune production n'a été modifiée par ce travail.

## 1. Intégration des branches

Travail isolé dans `codex/haccp-phase1`, créé depuis `7046d46`. Le checkout principal et les
changements livraison/dispatch d'autres sessions n'ont pas été intégrés au commit HACCP.
Faire la revue et l'intégration dans un checkout propre, avec autorisation de livraison.

Révision : `haccp_p1_20261003`, parent `0078`. Une migration concurrente
`0079_establishment_dispatch_settings.py` était présente dans le checkout principal.
Après intégration des branches, exécuter `uv run alembic heads` : **une seule tête est requise**.
Si deux têtes existent, créer une migration Alembic de fusion avec les deux identifiants réels,
puis tester les chemins depuis chacune des têtes ainsi qu'une base neuve. Ne pas renuméroter
ni modifier le parent d'une migration déjà diffusée. Cette fusion appartient à l'intégration,
pas au commit isolé de phase 1.

## 2. Préparation sur staging

1. Sauvegarder PostgreSQL (schéma public et tous les schémas tenants) et vérifier une restauration
   isolée. Inventorier les tenants, la révision actuelle et le nombre d'établissements.
2. Utiliser une copie représentative avec tenants mono-site et multi-site, historiques HACCP,
   lots consommés/écartés, DLC absentes et stocks réels. Aucune donnée sensible dans les logs Git.
3. Installer les dépendances verrouillées, puis exécuter :

   ```bash
   uv sync --frozen --extra dev
   uv run alembic heads
   uv run alembic current
   uv run alembic upgrade head
   uv run python tools/haccp_foundation_report.py --tenant SLUG
   ```

4. Conserver le rapport par tenant : `strict_enabled=false`, `activation_available=false`.
   Les blocages de préparation sont attendus et n'interrompent pas les commandes actuelles.
5. Vérifier absence de sessions strictes inventées et conservation des anciens soldes/contrôles.
   Les restes de lots sont inconnus et les soldes copiés non réconciliés. Avec plusieurs sites,
   aucun stock ni équipement n'est réparti automatiquement.
6. Mesurer la durée et les verrous : les deux nouvelles contraintes uniques portent sur
   `orders` et `ingredient_batches`. Prévoir une fenêtre adaptée aux gros tenants.
   Ne pas lancer plusieurs migrations simultanément ; suspendre le provisionnement de nouveaux
   tenants pendant la migration et le changement de version.

## 3. Vérifications automatisées

Sur une base **jetable dédiée aux tests** : les fixtures recréent leurs tenants de test.

```bash
uv run pytest tests/test_haccp_foundations.py tests/test_haccp.py tests/test_stock_phase2.py tests/test_stock_phase3.py tests/test_stock_phase3_integration.py tests/test_stock_phase4.py tests/test_admin_staff_p1_p2.py -q --tb=short
```

Les tests des fondations utilisent PostgreSQL réel : contraintes, isolement entre sites,
régularisation, déduplication, reprise 0/1/2 établissements, réexécution de migration et
égalité du schéma migré avec un tenant neuf (colonnes, contraintes, index, références).
La fixture historique est construite depuis les modèles hors nouvelles tables ; cette comparaison
ne remplace pas l'essai sur une sauvegarde réelle avant production.

Lors de la validation locale de phase 1, PostgreSQL 16 tourne sur une instance jetable
`127.0.0.1:55439`, base `haccp_phase1_test`. Le limiteur HTTP utilise `REDIS_URL=memory://`
uniquement dans le processus pytest. Ce choix ne valide ni Redis, ni les push, ni ARQ.
Ne pas reprendre ce réglage ou les réglages de performance de cette instance jetable en production.
La configuration métier utilise les secrets et services habituels du déploiement.

Résultats du 2026-10-04 : suite ciblée ci-dessus **77 réussis, 1 ignoré** (test historique
`test_stock_phase2.py:64`, TODO fixture `staff_token`). Après renforcement du test des clés
étrangères de lots, les **24 tests des fondations** ont été rejoués : tous réussis.
Ruff passe sur les 11 fichiers Python concernés ; `alembic heads` retourne la seule tête
`haccp_p1_20261003`. Upgrade depuis une base vide et diagnostic CLI sur tenant de test réussis.
Les tests de notifications et la suite exhaustive sont réservés aux phases concernées.

## 4. Déploiement

- Ne déployer qu'une révision intégrée, relue, avec une tête Alembic et les tests réussis.
- Sauvegarder, appliquer les migrations avec un seul processus, puis démarrer l'API.
  Railway exécute déjà `alembic upgrade head` au démarrage : coordonner cette étape avec
  les réplicas et le provisionnement. Ne pas donner à l'ancien code le rôle de migrer la base.
- Laisser le mode strict désactivé partout. Aucun `UPDATE strict_enabled=true` manuel.
  L'activation attend les services, permissions, écritures stock et parcours UX du sprint.
- Contrôler `/health` et `/health/ready`, connexion admin/staff, catalogue, commande de test,
  stock et HACCP historique. Provisionner un tenant de test, puis lancer le diagnostic dessus.
- Surveiller erreurs SQL, verrous, latence des commandes et inscriptions. Archiver version
  applicative, révision Alembic et rapports de diagnostic ; ouvrir les actions de réconciliation.

## 5. Retour à la version applicative précédente

La migration est **forward-only** : `alembic downgrade` refuse de supprimer les preuves/stock.
En phase 1, revenir à l'ancienne image applicative en conservant les tables additives et le mode
strict désactivé. Son démarrage doit éviter la commande de migration de l'ancien paquet : il ne
connaît pas `haccp_p1_20261003`. Conserver un job de migration utilisant l'artefact courant,
ou utiliser une commande de démarrage API seule pour le rollback applicatif.

Une suppression du schéma exige une restauration de sauvegarde préparée et une décision distincte
sur les données écrites depuis ; pas de DROP de tables ni de modification manuelle du stamp.
Après activation du mode strict dans une phase ultérieure, cette stratégie devra être réévaluée :
revenir à une application ignorant le contrôle strict pourrait réautoriser les commandes.

## 6. Discipline pour les phases suivantes

À chaque phase autorisée : travailler sur une branche/checkout isolé, n'ajouter que les fichiers
du périmètre, exécuter les tests utiles, revoir le diff indexé, mettre à jour le plan `.planning/`,
committer la phase conformément au feu vert utilisateur et fournir ses consignes de production.
Puis s'arrêter avant la phase suivante. Push et déploiement nécessitent une autorisation distincte.
