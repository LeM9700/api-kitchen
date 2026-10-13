# Menus composes - mise en production de la phase 2

La phase 2 ouvre l'API tenant d'administration des menus. Elle ne publie encore
aucune route client et ne modifie ni les commandes, ni le calcul du panier, ni le
stock. Les tables requises proviennent de la migration `0080` livree en phase 1 ;
cette phase n'ajoute pas de migration.

## Predeploiement

1. Verifier que l'artefact de production contient la migration `0080` et que
   `uv run alembic current` indique `0080 (head)`.
2. Sauvegarder PostgreSQL et tester la restauration, en incluant les tables
   `menus`, `menu_groups`, `menu_group_items` et `admin_audit_logs` de chaque tenant.
3. Executer les tests menus et catalogue avec la meme revision que l'artefact.
4. Confirmer que les comptes staff destines a gerer les menus possedent
   explicitement `catalog:read` et `catalog:write`. Un staff sans droit reste bloque.
5. Ne pas activer d'interface client : aucune route publique de menus ni commande
   composee n'est disponible avant les phases suivantes.

## Deploiement

1. Executer `uv run alembic upgrade head` avant le deploiement API. Cette commande
   doit etre sans effet si la phase 1 est deja en production.
2. Deployer l'API sur une seule instance, effectuer les controles ci-dessous, puis
   poursuivre le deploiement progressif.
3. Ne pas contourner l'API pour publier des menus directement en base : les
   validations de composition et les traces d'audit seraient perdues.

## Verification fonctionnelle

Sur un tenant de test :

1. Creer un menu brouillon avec `POST /api/v1/catalog/admin/menus`.
2. Ajouter un groupe obligatoire, puis un produit actif.
3. Verifier que `is_publishable` passe a `true` et publier le menu avec `PUT`.
4. Modifier la composition et verifier que le menu repasse automatiquement a
   `is_active=false`.
5. Sur un tenant `connected`, ajouter un produit POS au menu et verifier que les
   donnees du produit source sont inchangees.
6. Confirmer qu'un staff sans `catalog:write` recoit `403`.
7. Controler dans `admin_audit_logs` les actions `menu_created`,
   `menu_composition_changed`, `menu_published`, `menu_price_changed` et
   `menu_auto_unpublished` selon le scenario.

La suppression via `DELETE` retire definitivement le menu, ses groupes et ses
items. Elle doit etre reservee aux brouillons ou aux menus dont la suppression est
confirmee par l'admin. L'action `menu_deleted` reste conservee dans l'audit.

## Surveillance

- Surveiller les taux de `401`, `403`, `404`, `409` et `422` sur le prefixe
  `/api/v1/catalog/admin/menus`.
- Rechercher les erreurs `MENU_NOT_PUBLISHABLE`, `MENU_PRODUCT_INACTIVE` et
  `MENU_PRODUCT_ALREADY_IN_GROUP` anormalement frequentes.
- Verifier que les changements de composition d'un menu actif produisent une
  trace `menu_auto_unpublished`.
- Surveiller les erreurs de contrainte PostgreSQL sur les trois tables de menus.

## Retour arriere

Revenir a la version API precedente masque les routes admin sans supprimer les
donnees. Ne pas downgrader la migration `0080` : ce downgrade detruit toutes les
tables de menus. Si un retour schema est indispensable, exporter et sauvegarder
explicitement les trois tables de chaque tenant avant le downgrade.

Les menus crees pendant la phase 2 ne sont pas consommables par le client et ne
sont references par aucune commande. Un rollback applicatif peut donc les laisser
en base jusqu'au redeploiement corrige.
