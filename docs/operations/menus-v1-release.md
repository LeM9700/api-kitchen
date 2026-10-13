# Release menus V1

Ce runbook consolide la mise en production du parcours menus tenant. Il couvre
l'API, l'application admin staff et l'application client. Il ne cree aucun tag et
ne pousse aucun depot : ces actions restent des decisions explicites de release.

## Versions candidates

| Composant | Version | Prerequis |
|-----------|---------|-----------|
| API | `0.2.0` | PostgreSQL migre jusqu'a Alembic `0081` |
| Admin staff | `0.2.0+2` | API `0.2.0` disponible |
| Client | `1.1.0+2` | API `0.2.0` et au moins un menu pilote publie |

Les migrations `0080` et `0081` forment un ensemble : `0080` cree le catalogue
des menus dans chaque schema tenant et `0081` ajoute la hierarchie parent / composants
aux commandes. Alembic doit conserver une seule tete a `0081` avant de produire les
artefacts frontend.

## Contrat de la V1

- Un menu appartient a un seul tenant et reste local, y compris avec un POS en
  mode `connected`.
- Un menu a un prix fixe ; variantes et extras sont inclus sans supplement.
- Chaque groupe exige au moins un choix et respecte ses bornes minimum / maximum.
- Le serveur revalide publication, disponibilite, composition, variantes, extras
  et permissions au checkout.
- La commande stocke une ligne parent facturee et des composants non refactures,
  avec les snapshots necessaires a l'historique, au recu et a la cuisine.
- Les mouvements de stock et la preparation portent sur les composants reels.

## Limites volontaires

La V1 ne comprend pas le prix dynamique, les options payantes par groupe, les
horaires recurrents complexes, l'import CSV de menus, la synchronisation des menus
depuis le POS, les recommandations automatiques ni les analytics avances.

## Avant de deployer

1. Sauvegarder la base et verifier une restauration sur l'environnement prevu.
2. Confirmer que les trois artefacts proviennent des versions candidates ci-dessus.
3. Executer la barriere de tests de `menus-phase7-production.md`.
4. Verifier `alembic heads`, puis appliquer `alembic upgrade head` sur une copie de
   production avant la fenetre de deploiement.
5. Choisir un tenant pilote avec produits actifs, variantes, extras et, si utile,
   un produit synchronise POS.
6. Confirmer les permissions `catalog:read` et `catalog:write` des roles pilotes.

## Ordre de deploiement

1. Deployer l'API `0.2.0`.
2. Appliquer les migrations `0080` puis `0081` et confirmer `alembic current`.
3. Tester les routes admin et publiques avec le header tenant attendu.
4. Deployer l'admin `0.2.0+2`, creer un brouillon, verifier le blocage de publication,
   puis publier un menu complet.
5. Deployer le client `1.1.0+2` sur un canal interne.
6. Tester un panier produit + menu en retrait et en livraison, puis verifier paiement,
   cuisine, stock, recu et historique.
7. Ouvrir progressivement aux autres tenants apres observation du tenant pilote.

Les anciens clients restent compatibles avec les commandes produits classiques.
Le nouveau client exige cependant l'API `0.2.0` pour `GET /catalog/menus` et le
payload `menu_items`.

## Smoke tests

- Un brouillon ou un menu incomplet n'est jamais visible publiquement.
- Un admin peut publier ; un staff en lecture seule ne peut pas modifier.
- Une modification de composition depublie le menu.
- Un produit POS peut etre selectionne sans que ses donnees soient modifiees.
- Le client respecte les minimums et maximums, et conserve son panier apres un
  refus de revalidation.
- Le montant facture correspond au prix fixe serveur, sans double facturation des
  composants ni supplement de variante ou d'extra.
- Le recu et l'historique affichent le parent puis les composants.
- La cuisine et le stock traitent uniquement les composants reels.

## Surveillance

Surveiller les `403`, `404`, `409`, `422` et `5xx` sur `/catalog/admin/menus`,
`/catalog/menus` et `/orders`. Suivre en particulier `MENU_NOT_AVAILABLE`,
`INVALID_MENU_SELECTION`, `MENU_PRODUCT_UNAVAILABLE` et
`MENU_PARENT_NOT_PREPARABLE`.

Une hausse des `403` suggere un probleme de permissions. Une hausse des erreurs de
selection peut signaler un client non aligne, un menu modifie pendant la commande
ou des produits devenus indisponibles. Comparer les montants commandes au prix fixe
serveur et verifier l'absence de composants factures separement.

## Retour arriere

1. Depublier les menus du tenant pilote pour masquer immediatement le parcours.
2. Revenir aux binaires frontend precedents si l'incident est limite a une interface.
3. Conserver l'API et les tables menus afin de preserver les commandes et snapshots.
4. Ne pas downgrader `0081` apres la premiere commande menu. Une restauration de base
   est requise si un retour schema devient indispensable.
5. Documenter les menus depublies et les commandes affectees avant toute correction.

Le retrait des interfaces n'efface aucune donnee. Les commandes existantes restent
consultables grace a leurs snapshots, meme si le catalogue change ensuite.
