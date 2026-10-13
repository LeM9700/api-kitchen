# Menus composes - mise en production de la phase 1

La phase 1 ajoute uniquement le stockage tenant des menus. Elle n'expose aucune route et ne modifie ni les commandes, ni le prix calcule, ni le stock. Les menus restent donc inaccessibles aux clients et aux admins jusqu'aux phases suivantes.

## Predeploiement

1. Sauvegarder PostgreSQL et verifier la restauration sur un environnement de staging representatif.
2. Confirmer que la revision Alembic courante est `haccp_merge_0079_p3_20261012` et qu'une seule tete `0080` est visible.
3. Verifier que tous les schemas declares dans `public.tenants` possedent la table `products`.
4. Executer les tests cibles et le controle Alembic avec la meme version de code que l'artefact deploye.

## Deploiement

1. Arreter les taches susceptibles de provisionner un tenant pendant la migration.
2. Executer `uv run alembic upgrade head` une seule fois.
3. Deployer l'API apres la migration. Le nouveau provisioning utilise deja les modeles SQLAlchemy et cree les trois tables pour les futurs tenants.
4. Reprendre le provisioning puis surveiller les erreurs SQL et les temps de verrouillage.

La migration ne transforme aucune donnee et ne prend pas de verrou prolonge sur les tables catalogue existantes. Elle cree trois tables vides par tenant avec leurs contraintes et index.

## Verification

Pour chaque schema `tenant_*`, verifier la presence de `menus`, `menu_groups` et `menu_group_items`. Controler aussi les contraintes de prix, de limites de choix, d'unicite produit/groupe et les cles etrangeres vers `menus`, `menu_groups` et `products`.

Creer ensuite un tenant de test et confirmer que son schema contient les memes tables et contraintes qu'un tenant migre. Aucun menu actif ne doit exister apres cette phase.

## Retour arriere

Le downgrade `0080 -> haccp_merge_0079_p3_20261012` supprime les trois tables et toutes les donnees de menus qu'elles contiennent. Tant qu'aucune phase ulterieure n'ecrit de menus, ce retour arriere est sans perte metier. Apres ouverture de l'API de gestion, sauvegarder explicitement les tables avant tout downgrade.
