# Mise en production - Menus phase 7

Version candidate validee par cette barriere : API `0.2.0`, admin `0.2.0+2`,
client `1.1.0+2`. Le runbook consolide de release se trouve dans
`docs/operations/menus-v1-release.md`.

Cette phase verrouille les parcours menus par des tests transversaux. Elle ne
modifie ni le schema de donnees, ni les contrats API, ni les variables
d'environnement.

## Prerequis

- API menus phases 1 a 4 deployee.
- Alembic a la revision `0081`.
- Application admin incluant la phase 5.
- Application client incluant la phase 6.
- Sauvegarde recente et restauration verifiee selon le runbook habituel.

## Barriere de validation

Depuis `api-pizza/` :

```powershell
uv run python -m py_compile app/modules/catalog/menu_service.py app/modules/catalog/menu_schemas.py app/modules/catalog/router.py app/modules/orders/schemas.py app/modules/orders/service.py
uv run pytest tests/test_catalog_menus_phase1.py tests/test_catalog_menus_phase2.py tests/test_catalog_menus_phase3.py tests/test_order_menus_phase4.py -q --tb=short
uv run alembic current
uv run alembic heads
```

Depuis `app-admin-staff/` :

```powershell
C:\Users\melbo\flutter\bin\flutter.bat analyze --no-pub
C:\Users\melbo\flutter\bin\flutter.bat test --no-pub test\menu_management_phase5_test.dart
```

Depuis `app-client/` :

```powershell
C:\Users\melbo\flutter\bin\flutter.bat analyze --no-pub
C:\Users\melbo\flutter\bin\flutter.bat test --no-pub test\features\catalog\menu_phase6_test.dart test\features\checkout\menu_checkout_phase6_test.dart
```

## Ordre de livraison

1. Executer la barriere de validation sur les revisions candidates.
2. Deployer l'API et verifier `/health/ready`.
3. Confirmer la revision Alembic `0081` sans lancer de nouvelle migration.
4. Effectuer un smoke test admin : brouillon, groupe, produit, publication.
5. Effectuer un smoke test client : composition, panier, retrait et livraison.
6. Verifier la commande, le stock, la cuisine, le recu et l'historique.
7. Promouvoir les applications admin et client deja validees.

## Points de controle

- Un customer et un staff sans permission ne peuvent pas acceder aux routes
  admin des menus.
- Un prix envoye ou falsifie par un client n'est jamais utilise.
- Une composition incomplete ou forgeable est refusee par l'API.
- Le stock est deduit depuis les composants sans double comptage du parent.
- La commande conserve les snapshots du menu et de ses composants.
- Un menu modifie ou indisponible bloque le checkout sans vider le panier.
- Les tenants `CONNECTED` peuvent creer un menu local sans modifier les
  produits synchronises.

## Surveillance et retour arriere

Surveiller les `403`, `409`, `422` et `5xx` sur les routes de menus et de
commandes, ainsi que les codes `MENU_NOT_AVAILABLE`, `INVALID_MENU_SELECTION`
et `MENU_PRODUCT_UNAVAILABLE`.

La phase 7 n'ajoute aucun changement d'execution a restaurer. En cas d'incident
sur une version applicative precedente, desactiver les menus publies, restaurer
les binaires concernes et conserver l'API ainsi que les donnees. Ne pas
downgrader `0081` apres la creation de commandes menus.
