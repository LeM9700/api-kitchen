# Mise en production - Menus commandes phase 4

## Perimetre

Cette livraison ajoute la commande de menus composes avec prix fixe, ligne parent et composants.
Elle introduit la migration Alembic `0081` sur `order_items` dans chaque schema tenant.

## Pre-deploiement

1. Sauvegarder PostgreSQL ou disposer d'un point de restauration verifie.
2. Verifier que la revision courante est `0080` ou une revision anterieure compatible.
3. Verifier que l'API et les workers utilisent le meme commit applicatif.
4. Ne pas activer l'UX client des menus avant que la migration et l'API soient deployees.

Commandes de controle :

```text
uv run alembic current
uv run alembic heads
uv run alembic upgrade head
```

La revision attendue apres migration est `0081 (head)`.

## Ordre de deploiement

1. Mettre l'API en maintenance courte si plusieurs instances peuvent demarrer les migrations en parallele.
2. Appliquer `uv run alembic upgrade head` une seule fois.
3. Deployer l'API et les workers sur le meme commit.
4. Executer les controles de fumee ci-dessous.
5. Activer ensuite les clients qui envoient `menu_items`.

Les anciens clients restent compatibles : `items` continue de fonctionner sans changement.

## Controles de fumee

- Creer une commande avec un menu actif et tous ses groupes obligatoires.
- Verifier que le total utilise `menus.fixed_price`, pas la somme des composants.
- Verifier en base une ligne `item_type=menu` et ses lignes `item_type=menu_component`.
- Verifier le detail et le recu : parent menu, puis noms des produits/variantes enfants.
- Verifier le KDS : seuls les composants sont preparables ; le parent ne bloque pas le passage a `ready`.
- Confirmer une commande payee et verifier la deduction des recettes produit, variante et extras.
- Tester une selection incomplete, un produit hors groupe et un extra non autorise : chaque requete doit etre refusee.
- Tester une promotion generique, puis une promotion ciblee produit, sur un panier menu.

## Surveillance

Surveiller en priorite :

- taux de `INVALID_MENU_SELECTION`, `MENU_NOT_AVAILABLE` et `MENU_PRODUCT_UNAVAILABLE` ;
- erreurs de contrainte `ck_order_items_type_shape` ;
- ecarts entre total commande, paiement et recu ;
- commandes KDS qui resteraient en preparation apres tous les composants prets ;
- erreurs de stock ou HACCP a la confirmation.

## Retour arriere

Le code applicatif peut revenir a la version precedente tant qu'aucun client n'envoie de menu.
Conserver alors les colonnes `0081` est sans impact sur les anciennes commandes.

Le downgrade `0081 -> 0080` est autorise uniquement si aucun `order_items.item_type` different de
`product` n'existe. Il est volontairement bloque des qu'un historique menu est present, car supprimer
les colonnes detruirait la composition, les snapshots et la lisibilite des recus.

En presence de commandes menus :

1. revenir seulement au code applicatif compatible avec `0081` ;
2. desactiver l'exposition/creation des menus si necessaire ;
3. conserver la migration et analyser l'incident ;
4. restaurer une sauvegarde uniquement dans le cadre d'une decision operationnelle explicite.

## Verification automatisee executee

- upgrade reel PostgreSQL `0080 -> 0081` ;
- cycle downgrade/upgrade sur schemas sans historique menu ;
- tests de prix fixe, validations serveur, snapshots, recu, recommande, promotions, stock et KDS ;
- non-regression commandes, paiements, promotions, catalogue menus et stock.
