# Livraison HACCP phase 2

Cette phase ajoute les routes et services de contrôle strict par établissement. Elle ne bloque
pas encore les commandes : vérifier `ordering_enforced=false` et laisser `strict_enabled=false`
jusqu'à la fin des phases stock/commandes.

## Avant déploiement

1. Intégrer `c7defc7` puis le commit de phase 2 dans un checkout propre. Garder toute migration
   concurrente de livraison hors du commit HACCP ; vérifier `uv run alembic heads` et résoudre
   les têtes par une migration de fusion si nécessaire.
2. Sauvegarder PostgreSQL et tester la restauration. La phase 2 ne change pas le schéma : aucune
   migration supplémentaire n'est attendue. Vérifier que le code et les 17 tables de phase 1
   sont bien présents dans chaque tenant.
3. Sur staging, configurer un établissement mono-site puis un tenant multi-site : fuseau valide,
   créneaux, équipements avec limites, tâches quotidiennes ou ancrées, stock réconcilié et lots
   DLC traçables. Affecter un staff avec `haccp:read`, `haccp:write`, `haccp:validate`, puis
   l'affectation site dans l'interface admin.

## Vérifications staging

```bash
uv run pytest tests/test_haccp_strict.py tests/test_haccp.py tests/test_haccp_foundations.py -q
uv run ruff check app/modules/haccp/strict_contracts.py app/modules/haccp/strict_checklist.py app/modules/haccp/strict_service.py app/modules/haccp/strict_router.py app/modules/haccp/router.py app/modules/haccp/service.py tests/test_haccp_strict.py tests/test_haccp.py
```

Tester manuellement :

- checklist ouverture et fermeture avec température hors limites, tâche manquante et trois DLC ;
- expiration à la seconde, lot écarté et remplacement traçable ;
- changement d'une DLC après saisie : la révision doit invalider les preuves précédentes ;
- double clic/rejeu de clé identique puis même clé avec contenu différent ;
- staff affecté au site A refusé sur B, admin capable d'affecter/révoquer ;
- fermeture oubliée, régularisation maintenant, puis ouverture du jour courant toujours requise ;
- jour sans créneau et contrôle avant minuit/juste après minuit ;
- deux tenants avec mêmes identifiants locaux sans partage de session ou de preuve.

## Mise en production

Déployer l'API et le worker avec le même artefact, après vérification du schéma phase 1. Ne pas
activer manuellement `strict_enabled`, ne pas appeler les routes de phase 2 depuis le client avant
que les permissions et l'écran staff soient disponibles, et ne pas annoncer un blocage de commande
avant la phase 4. Surveiller les réponses `HACCP_*`, `HACCP_RETRY_REQUIRED`, les conflits de clé,
les contrôles obsolètes et les erreurs de fuseau/horaires.

Le rollback consiste à revenir à l'image précédente en conservant les tables additives et les
audits ; ne pas supprimer les données de contrôles et ne pas restaurer un ancien code après une
activation stricte future. Aucun push ni déploiement n'a été exécuté pour cette phase.
