# Menus composes - mise en production de la phase 3

La phase 3 expose les menus en lecture publique pour l'application cliente. Elle
ne modifie ni les commandes, ni les paiements, ni le stock. Aucune migration n'est
ajoutee : la revision Alembic `0080` de la phase 1 reste le seul prerequis schema.

## Predeploiement

1. Verifier `uv run alembic current` et `uv run alembic heads` : les deux doivent
   indiquer une tete unique `0080`.
2. Executer les tests des phases menus et la regression catalogue avec la revision
   exacte de l'artefact.
3. Verifier qu'au moins un tenant de staging possede un menu publie complet, un
   menu brouillon, un menu incomplet et un produit temporairement indisponible.
4. Confirmer que le proxy transmet le header public `X-Tenant-Slug` et conserve
   l'adresse client utilisee par le rate limit.
5. Ne pas activer l'ajout au panier d'un menu : cette logique appartient a la
   phase 4 et n'est pas encore acceptee par l'API de commande.

## Deploiement

1. Executer `uv run alembic upgrade head` avant l'API. La commande doit etre sans
   effet lorsque `0080` est deja appliquee.
2. Deployer d'abord une instance API, effectuer les controles ci-dessous, puis
   poursuivre le deploiement progressif.
3. Activer la consommation cote client seulement apres validation des payloads sur
   plusieurs tenants. L'API admin de phase 2 reste compatible.

## Smoke tests

Avec un tenant de staging :

```text
GET /api/v1/catalog/menus?page=1&page_size=20
X-Tenant-Slug: <tenant>

GET /api/v1/catalog/menus/<menu_id>
X-Tenant-Slug: <tenant>
```

Verifier les points suivants :

- aucune authentification n'est demandee, mais l'absence de `X-Tenant-Slug`
  retourne `400` ;
- seuls les menus actifs, complets et dans leur fenetre sont listes ;
- `total`, `total_count`, `page`, `page_size` et `pages` sont coherents ;
- chaque groupe expose ses limites de choix et ses produits dans l'ordre admin ;
- variantes, extras, galerie, allergenes, tags et disponibilite sont presents ;
- une rupture operationnelle produit `is_available=false` sans faire apparaitre un
  brouillon ni une erreur de validation admin ;
- un menu incomplet ou hors fenetre retourne `404` sur le detail ;
- la 61e requete dans une minute pour une meme cle de limitation retourne `429`.

## Surveillance

- Suivre latence p95, volume et taux de `400`, `404`, `429` et `5xx` sur
  `/api/v1/catalog/menus`.
- Surveiller les erreurs PostgreSQL et les temps des requetes catalogue enrichies.
- Comparer ponctuellement `is_available` avec les indisponibilites produit et le
  stock du tenant.
- Une hausse de `404` peut signaler des liens clients vers des menus depublies ou
  devenus incomplets apres des changements catalogue.

## Retour arriere

Revenir au commit API de phase 2 retire uniquement les deux routes publiques. Les
menus, leur composition et les audits restent en base et l'administration continue
de fonctionner. Ne pas downgrader `0080`, car cela supprimerait toutes les donnees
de menus. Desactiver la section Menus cote client avant le rollback API evite des
erreurs visibles pendant la propagation du deploiement.
