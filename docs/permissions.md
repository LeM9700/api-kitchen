# Modèle de permissions — rôles tenant

Ce document décrit le modèle d'autorisation appliqué par
`app.core.http.deps.has_permission` / `require_permission` sur les comptes d'un
schéma tenant (`users.role` ∈ `customer`, `staff`, `admin`). Le rôle `super-admin`
(plateforme, `public.super_admins`) est hors périmètre — voir
`app.core.auth.super_admin`.

## Principe : moindre privilège, deny by default

- **`admin`** : accès total à toutes les routes `require_permission(...)`, quel que
  soit le contenu de `users.permissions`. Le champ `permissions` est ignoré pour ce
  rôle (conservé pour compatibilité de schéma, jamais lu par `has_permission`).
- **`staff`** : accès **exactement** à ce qui figure dans `users.permissions`
  (liste de chaînes) ou `["*"]` pour un accès total explicite. **`permissions=None`
  ou `permissions=[]` signifie AUCUN droit fin** — un compte staff sans liste
  explicite ne peut agir sur aucune route `require_permission(...)`.
- **`customer`** : aucune route staff/admin n'est accessible à ce rôle (filtré en
  amont par `require_role`/`require_permission`) ; le champ `permissions` n'est pas
  utilisé.

> ⚠️ Avant la migration `0056_staff_explicit_permissions`, `permissions=None` sur
> un compte staff était traité comme un accès total ("staff légataire non
> restreint"). Ce comportement était une faille de moindre privilège : un compte
> staff créé sans permissions explicites héritait d'un accès permissif. La
> migration normalise tous les comptes staff historiques vers une liste explicite
> et `has_permission` ne fait plus jamais cette exception.

## Catalogue des permissions fines

Permissions actuellement gate-keepées par `require_permission(...)` dans le code
(à tenir à jour à chaque nouvelle route staff) :

| Permission                  | Domaine                                    |
|------------------------------|---------------------------------------------|
| `catalog:read`               | Consultation du catalogue produits          |
| `catalog:write`               | Modification du catalogue produits          |
| `catalog:availability`        | Bascule de disponibilité produit             |
| `haccp:read`                  | Consultation des relevés HACCP               |
| `orders:read`                 | Consultation des commandes                   |
| `orders:write`                | Modification des commandes                   |
| `orders:manual`               | Création de commandes manuelles (guichet)    |
| `orders:preparation`          | Écrans de préparation cuisine (KDS)          |
| `payments:read`               | Consultation des paiements                   |
| `payments:terminal`           | Encaissement par terminal (Stripe Terminal)  |
| `print:read`                  | Accès à la configuration/état d'impression   |
| `stock:read`                  | Consultation des stocks                      |
| `stock:write`                 | Mouvements de stock                          |
| `stock:adjustment:create`     | Création de demandes d'ajustement de stock   |

## Permissions minimales recommandées par rôle métier

Ces ensembles sont des **recommandations opérationnelles** pour l'admin qui
configure un compte staff via `PATCH /admin/users/{id}/permissions` — ils ne sont
pas appliqués automatiquement par le code (le staff démarre à `[]`, l'admin ajoute
explicitement ce dont le poste a besoin) :

- **Caissier / accueil** : `orders:read`, `orders:manual`, `orders:write`,
  `payments:terminal`.
- **Cuisine (KDS)** : `orders:read`, `orders:preparation`.
- **Gestion de stock** : `stock:read`, `stock:write`, `stock:adjustment:create`.
- **Superviseur staff (lecture large, pas d'écriture financière)** : `orders:read`,
  `catalog:read`, `stock:read`, `haccp:read`, `print:read`.

## Effet immédiat d'un retrait de droits

`PATCH /admin/users/{id}/permissions` et `PATCH /admin/users/{id}/deactivate`
révoquent immédiatement les sessions actives (refresh tokens) et publient un
signal de révocation qui ferme les WebSockets ouverts de l'utilisateur (voir
`app.core.auth.token_revocation.publish_session_revoked`). Les requêtes HTTP déjà
en vol avec un ancien access token ne peuvent pas exploiter les anciennes
permissions : `get_current_user` relit `role`/`permissions`/`is_active` en base à
chaque requête (`app.core.tenancy.tenant.get_live_tenant_user_state`) plutôt que
de faire confiance aux claims du JWT.
