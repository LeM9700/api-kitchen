# Promotions

Gestion des codes promo : CRUD admin, validation, application serveur-side dans le calcul de commande, quotas atomiques, usage par utilisateur, restrictions first-order.

## Endpoints

| Méthode | Path | Auth | Rôles |
|---------|------|------|-------|
| GET | `/api/v1/promotions` | Public | header `X-Tenant-Slug`, offres affichables uniquement |
| POST | `/api/v1/promotions/validate` | Bearer JWT | tous, rate-limit strict |
| GET | `/api/v1/promotions/admin` | Bearer JWT | staff, admin |
| GET | `/api/v1/promotions/{promo_id}` | Bearer JWT | staff, admin |
| GET | `/api/v1/promotions/{promo_id}/usages` | Bearer JWT | staff, admin |
| POST | `/api/v1/promotions` | Bearer JWT | admin |
| PATCH | `/api/v1/promotions/{promo_id}` | Bearer JWT | admin |
| POST | `/api/v1/promotions/{promo_id}/toggle` | Bearer JWT | admin |
| DELETE | `/api/v1/promotions/{promo_id}` | Bearer JWT | admin |
| POST | `/api/v1/promotions/bulk-generate` | Bearer JWT | admin |
| POST | `/api/v1/promotions/cleanup-expired` | Bearer JWT | admin |
| GET | `/api/v1/promotions/super-admin/stats` | Bearer JWT | super-admin |

## Modèles de données

**`promotions`** (migration `0004` + `0009` + `0014` + `0029`) : `id`, `code` (unique, majuscules), `description`, `discount_type` (`fixed` | `percent`), `discount_value`, `min_order_amount`, `starts_at`, `ends_at`, `is_active`, `max_uses` (nullable = illimité), `max_uses_per_user` (nullable), `current_uses`, `first_order_only`, `campaign_id`, `user_id`, `is_public`, `is_stackable`, `email_verified_required`.

**`promo_code_usages`** (migration `0014`) : `id`, `promo_code_id`, `user_id`, `order_id`, `used_at`. Contrainte unique `(promo_code_id, order_id)` — prévient les doublons sur retry.

**`promotion_campaigns`** (migration `0029`) : `id`, `name`, `prefix`, `description`, `created_by_user_id`, `created_at`. Supporte la génération en masse de codes de campagne.

**`promotion_target_categories`** (migration `0029`) : `id`, `promotion_id`, `category_id`. Cible une ou plusieurs catégories catalogue.

**`promotion_target_products`** (migration `0029`) : `id`, `promotion_id`, `product_id`. Cible un ou plusieurs produits catalogue.

## Comportements métier

**Prévisualisation (`POST /validate`)** : pipeline sans consommation de quota :
1. Vérification existence.
2. Non expirée (`starts_at`/`ends_at`).
3. `is_active`.
4. Quota global : `current_uses < max_uses` (si `max_uses` défini).
5. Quota par utilisateur : comptage des `promo_code_usages` pour l'utilisateur.
6. Restriction `first_order_only` : vérifie qu'aucune commande `delivered` n'existe pour cet utilisateur.
7. `min_order_amount`.
8. Promo nominative : `user_id` doit correspondre à l'utilisateur authentifié.
9. Email vérifié si `email_verified_required=True` via lecture de `auth.User.email_verified_at`.
10. Ciblage panier : seuls les items correspondant aux catégories/produits ciblés sont éligibles.

Toutes les erreurs masquées derrière `INVALID_PROMO` (422) — pas d'information sur la raison du refus.

**Application dans les commandes** : `promotions_service.apply_promo` est appelé par `orders/service.create_order`. `discount_total` est calculé côté serveur uniquement depuis les lignes panier résolues par le catalogue.

**Prévisualisation vs application** : `preview_promos()` calcule la remise sans modifier `current_uses`. `apply_promo()` consomme le quota atomiquement pendant la création de commande.

**Quota atomique** : mise à jour de `current_uses` via `UPDATE ... SET current_uses = current_uses + 1 WHERE id = X AND (max_uses IS NULL OR current_uses < max_uses)` — CAS (compare-and-swap) SQL pour éviter les race conditions.

**Ciblage et cumul** : une promotion peut cibler toute la commande, des catégories, ou des produits. Plusieurs codes sont refusés si l'un d'eux n'est pas `is_stackable=True`. En cumul autorisé, les pourcentages sont appliqués avant les montants fixes et chaque remise est plafonnée au montant éligible.

**Campagnes** : `POST /bulk-generate` crée une `promotion_campaign` puis génère N codes uniques au format `PREFIXE-XXXXXX`, héritant des règles de la promotion modèle.

**Stats et audit** : les vues admin agrègent `promo_code_usages` et `orders` pour exposer `usage_count`, utilisateurs uniques, CA brut, CA net et remise totale.

**Enregistrement d'usage** : `record_promo_usage()` crée une `PromoCodeUsage` post-commit, idempotent via la contrainte unique `(promo_code_id, order_id)`.

**Soft-delete** : `DELETE /{promo_id}` positionne `is_active = False`. Code non réutilisable.

**Normalisation** : `code` converti en majuscules à la création et à la mise à jour.

## Sécurité implémentée

- [🔒] `apply_promo` masque toutes les erreurs derrière `INVALID_PROMO` — pas d'énumération des codes valides/expirés.
- [🔒] CAS atomique sur `current_uses` — prévient l'overshooting du quota en cas de requêtes concurrentes.
- [🔒] `PromoCodeUsage` avec contrainte unique `(promo_code_id, order_id)` — idempotence sur retry.
- [🔒] `max_uses_per_user` : limite par utilisateur, vérifiée avant application.
- [🔒] `POST /validate` est rate-limité par IP et par utilisateur.
- [🔒] `POST /validate` ajoute un délai minimal uniforme pour réduire le timing oracle.
- [🔒] La réponse publique de `GET /promotions` exclut les promos privées, nominatives, futures et expirées.

---

## Axes d'amélioration

### Logique métier
- [✅] **Promo par catégorie/produit** : supportée via `promotion_target_categories` et `promotion_target_products`.
- [✅] **Promo combinée** : supportée avec `is_stackable`; refus par défaut.
- [✅] **Codes générés en masse** : supportés via `POST /bulk-generate` et `promotion_campaigns`.
- [✅] **Promo liée à un client spécifique** : supportée via `promotions.user_id`.
- [✅] **Expiration automatique** : filtrage public et endpoint de cleanup `POST /cleanup-expired`.
- [✅] **Statistiques d'usage** : endpoints admin et super-admin exposent usage, CA et remise.

### Sécurité & contre-intrusion
- [✅] **Brute-force de codes** : `POST /validate` limite les tentatives à 5/min par utilisateur authentifié et 3/min par IP.
- [✅] **Timing oracle résiduel** : `POST /validate` impose un délai minimal uniforme.
- **Enumération via first_order_only** : la restriction first_order first vérifie les livraisons passées — un attaquant qui teste des codes sur plusieurs comptes peut inférer des patterns (même réponse pour les comptes sans historique). Acceptable dans l'absolu mais à surveiller.
- **Race condition first_order** : la validation est revérifiée à l'application, mais le verrouillage strict par utilisateur/first-order reste à surveiller côté cycle de commande.
- [✅] **Abus de `max_uses_per_user`** : mitigation supportée via `email_verified_required`. Cette vérification dépend du module `auth` : `promotions` ne crée pas la donnée `email_verified`, mais consomme `auth.User.email_verified_at`.

### Accessibilité API
- [✅] Exposer `usage_count` et `remaining_uses` dans la réponse admin de `GET /promotions/admin` pour le suivi.
- [✅] Ajouter `GET /promotions/{promo_id}` pour le détail d'un code.
- [✅] Exposer `GET /promotions/{promo_id}/usages` pour l'audit d'usage.

---

## Ce qui manque pour les interfaces

### Interface client
- **Affichage des promos actives** : `GET /promotions` renvoie les promos `is_active=True` — afficher une bannière ou une section "Offres du moment" si `discount_type` visible.
- **Champ code promo au checkout** : input texte + bouton "Appliquer" appelant `POST /validate` avant de soumettre la commande.
- **Prévisualisation de la remise** : afficher le montant économisé après validation du code (retour `{valid, discount}`).
- **Feedback d'erreur** : afficher "Code invalide ou expiré" (sans préciser pourquoi — c'est intentionnel).

### Interface staff
- **Vue des promos actives** : liste en lecture seule pour répondre aux questions clients ("ce code est-il encore valable ?").

### Interface admin (tenant)
- **CRUD promos** : formulaire de création avec : code, type (fixe/%), valeur, date début/fin, quota global, quota par client, first-order only, montant minimum.
- **Statistiques d'usage** : combien de fois chaque code a été utilisé, CA généré/perdu.
- **Désactivation en un clic** : toggle `is_active` directement depuis la liste.
- **Génération de codes en masse** : formulaire pour créer N codes uniques d'un coup (campagnes promotionnelles).

### Super-admin
- Vue cross-tenant du volume de remises accordées (impact financier).

---

## Plan d'amélioration ciblé `app/modules/promotions`

Statut : implémenté dans `app/modules/promotions` avec la migration `0029_promotions_business_interfaces.py`, les contrats API étendus, les endpoints métiers, les tests promotions ciblés et le branchement checkout pour transmettre les lignes panier résolues à `apply_promo()`.

Note provisioning : le DDL de création de tenant dans `auth/service.py` est aligné sur ce modèle pour que les nouveaux tenants créés après cette évolution disposent aussi des tables et colonnes promotions.

Correspondance avec les 8 phases demandées :
1. ✅ Refondre les contrats API.
2. ✅ Ajouter le modèle métier.
3. ✅ Sécuriser `/promotions/validate`.
4. ✅ Implémenter ciblage et cumul.
5. ✅ Ajouter les endpoints métiers.
6. ✅ Ajouter stats et audit.
7. ✅ Ajouter expiration et nettoyage.
8. ✅ Ajouter les tests ciblés.

Objectif : combler les manques listés ci-dessus en améliorant uniquement le module `app/modules/promotions`, ses migrations associées, ses schémas API et ses services. Les autres modules peuvent être lus ou appelés comme dépendances métier existantes (`orders`, `catalog`, `auth`), mais les évolutions fonctionnelles restent concentrées côté promotions.

### Décisions de cadrage

- Les priorités **API interfaces**, **sécurité** et **business avancé** sont au même niveau.
- Les migrations sont autorisées.
- `GET /api/v1/promotions` public doit exposer seulement les offres affichables : actives, non expirées, déjà commencées, non nominatives, et sûres pour une bannière client.
- Le ciblage promo doit supporter : commande entière, catégories, produits.
- Les promotions ne sont pas cumulables par défaut. Une promo doit être explicitement `is_stackable=True` pour pouvoir être combinée.
- En cas de cumul autorisé, l'ordre de calcul est stable : remises en pourcentage d'abord, remises fixes ensuite, avec plafonnement au montant éligible.
- Les codes générés en masse sont rattachés à une campagne et prennent un format lisible du type `PREFIXE-8K2Q9Z`.
- Les promos nominatives ciblent un `user_id` interne.
- `email_verified` dépend du module `auth`. Le module promotions doit prévoir le contrôle, mais ne doit pas porter la création ou la maintenance de cette donnée.
- `POST /promotions/validate` doit rester générique en cas d'échec : `INVALID_PROMO` sans raison détaillée côté client.
- La prévisualisation de remise doit accepter un panier détaillé pour calculer correctement les remises ciblées.
- Staff : lecture et audit. Admin tenant : création, modification, désactivation, génération, statistiques. Super-admin : statistiques cross-tenant.

### Phase 1 — Fondations modèles, migrations et contrats API ✅

**But** : préparer la structure de données nécessaire aux campagnes, promos nominatives, ciblage catégorie/produit, cumul et affichage public.

À faire :
- Ajouter une migration pour étendre `promotions` :
  - `user_id` nullable pour les codes nominatifs.
  - `campaign_id` nullable.
  - `is_public` booléen pour contrôler l'exposition dans `GET /promotions`.
  - `is_stackable` booléen, `False` par défaut.
  - `email_verified_required` booléen, `False` par défaut.
- Ajouter une table `promotion_campaigns` :
  - `id`, `name`, `prefix`, `description`, `created_at`, `created_by_user_id`.
- Ajouter les tables de ciblage :
  - `promotion_target_categories(promotion_id, category_id)`.
  - `promotion_target_products(promotion_id, product_id)`.
- Ajouter les index nécessaires :
  - `promotions(code)`, `promotions(campaign_id)`, `promotions(user_id)`, `promotions(is_active)`, `promotions(starts_at)`, `promotions(ends_at)`.
- Étendre les schémas Pydantic :
  - `PromotionCreate`, `PromotionUpdate`, `PromotionOut`.
  - `PromotionAdminOut`.
  - `PromotionTargetOut`.
  - `PromotionCampaignOut`.
- Préparer `PromotionUpdate` en PATCH partiel plutôt que réutiliser `PromotionCreate` pour `PUT`.

Critères d'acceptation :
- Les migrations créent les nouvelles colonnes/tables sans casser les promos existantes.
- Une promo existante reste valide avec les valeurs par défaut.
- Les schémas exposent les nouveaux champs sans fuite de données sensibles côté public.

### Phase 2 — Sécurisation et correction de `/promotions/validate` ✅

**But** : rendre la validation exploitable par l'interface checkout sans fuite d'information ni consommation prématurée des quotas.

À faire :
- Remplacer l'usage public de `validate_promo()` par un service de prévisualisation sécurisé.
- Séparer clairement :
  - `preview_promo()` : valide et calcule une remise sans incrémenter `current_uses`.
  - `apply_promo()` : applique réellement la promo pendant la création de commande et consomme le quota atomiquement.
- Ajouter un payload de validation basé sur panier détaillé :
  - `codes` ou `code`.
  - `items[]` avec `product_id`, `category_id`, `quantity`, `unit_price`, `line_total`.
  - `order_total`.
- Masquer toutes les erreurs derrière `INVALID_PROMO` pour le client.
- Ajouter un rate-limit strict sur `/validate` :
  - par utilisateur authentifié.
  - par IP.
- Ajouter une mitigation de timing oracle par délai minimal uniforme.
- Vérifier `email_verified_required` en consommant l'information exposée par `auth`.
- Si l'information `email_verified` n'est pas disponible, retourner une erreur générique et documenter le prérequis côté auth.

Critères d'acceptation :
- Un appel `/validate` ne modifie jamais `current_uses`.
- Les codes inexistants, expirés, désactivés, non éligibles, hors quota ou réservés retournent la même erreur client.
- Les quotas ne sont consommés que pendant `apply_promo()`.
- Le rate-limit protège contre le brute-force de codes.

### Phase 3 — Règles métier avancées ✅

**But** : supporter les remises ciblées, les codes nominatifs, le cumul contrôlé et les campagnes de codes.

À faire :
- Implémenter le calcul de montant éligible :
  - commande entière si aucune cible n'est définie.
  - lignes dont `category_id` correspond à une cible catégorie.
  - lignes dont `product_id` correspond à une cible produit.
- Implémenter le calcul multi-code :
  - refuser plusieurs codes si au moins un code n'est pas cumulable.
  - appliquer les remises en pourcentage avant les remises fixes.
  - plafonner chaque remise au montant encore éligible.
- Implémenter les promos nominatives :
  - `user_id` défini => seul cet utilisateur peut valider/appliquer la promo.
- Implémenter les campagnes de génération :
  - endpoint admin `POST /promotions/bulk-generate`.
  - création d'une `promotion_campaign`.
  - génération de N promotions uniques au format `PREFIXE-XXXXXX`.
  - héritage des règles de remise, dates, quotas, ciblage et contraintes.
- Prévoir les collisions de code par retry de génération jusqu'à unicité.

Critères d'acceptation :
- Une promo ciblée ne réduit que les lignes éligibles.
- Plusieurs codes non cumulables sont refusés.
- Les codes générés sont uniques, rattachés à une campagne et auditables.
- Une promo nominative échoue pour tout autre utilisateur.

### Phase 4 — Endpoints pour interfaces client, staff et admin ✅

**But** : exposer les données nécessaires aux interfaces sans ajouter l'interface elle-même.

Endpoints cibles :

| Méthode | Path | Auth | Rôles | Usage |
|---------|------|------|-------|-------|
| GET | `/api/v1/promotions` | Public | header `X-Tenant-Slug` | Offres affichables client |
| POST | `/api/v1/promotions/validate` | Bearer JWT | tous | Prévisualisation checkout |
| GET | `/api/v1/promotions/admin` | Bearer JWT | staff, admin | Liste métier avec filtres et stats |
| GET | `/api/v1/promotions/{promo_id}` | Bearer JWT | staff, admin | Détail d'une promo |
| GET | `/api/v1/promotions/{promo_id}/usages` | Bearer JWT | staff, admin | Audit paginé des usages |
| POST | `/api/v1/promotions` | Bearer JWT | admin | Création promo |
| PATCH | `/api/v1/promotions/{promo_id}` | Bearer JWT | admin | Modification partielle |
| POST | `/api/v1/promotions/{promo_id}/toggle` | Bearer JWT | admin | Activation/désactivation rapide |
| DELETE | `/api/v1/promotions/{promo_id}` | Bearer JWT | admin | Soft-delete |
| POST | `/api/v1/promotions/bulk-generate` | Bearer JWT | admin | Génération de campagne |
| GET | `/api/v1/promotions/super-admin/stats` | Bearer JWT | super-admin | Impact financier cross-tenant |

À faire :
- Ajouter pagination et filtres admin :
  - `is_active`, `campaign_id`, `code`, `starts_at`, `ends_at`, `user_id`, `is_public`.
- Retourner `usage_count` et `remaining_uses` dans les vues admin.
- Exposer une vue staff en lecture seule pour répondre aux questions client.
- Garder la réponse publique minimale : pas de quota interne, pas de `user_id`, pas de détails d'audit.

Critères d'acceptation :
- Le client voit seulement les promotions affichables.
- Le staff peut consulter sans modifier.
- L'admin peut gérer le cycle de vie complet d'une promotion.
- Le super-admin peut lire les statistiques globales sans modifier les tenants.

### Phase 5 — Statistiques, audit et impact financier ✅

**But** : rendre les promotions pilotables par les interfaces métier.

À faire :
- Ajouter des fonctions d'agrégation sur `promo_code_usages` et `orders` :
  - nombre d'utilisations.
  - nombre d'utilisateurs uniques.
  - remise totale accordée.
  - CA brut associé.
  - CA net après remise.
  - dernière utilisation.
- Ajouter `GET /promotions/{promo_id}/usages` paginé :
  - `user_id`, `order_id`, `used_at`, statut commande, `subtotal`, `discount_total`, `total`.
- Ajouter la vue `GET /promotions/admin` avec stats inline.
- Ajouter la vue super-admin cross-tenant :
  - agrégation par tenant.
  - volume total des remises.
  - nombre d'utilisations.
  - CA net associé.

Critères d'acceptation :
- Les stats d'une promo correspondent aux commandes liées à ses usages.
- Les usages sont paginés et filtrables.
- Les données financières sont lisibles par admin et super-admin selon leurs rôles.

### Phase 6 — Expiration automatique et règles de cycle de vie ✅

**But** : éviter que les promotions expirées restent visibles ou exploitables.

À faire :
- Filtrer `GET /promotions` public sur :
  - `is_active=True`.
  - `is_public=True`.
  - `starts_at IS NULL OR starts_at <= now`.
  - `ends_at IS NULL OR ends_at >= now`.
  - `user_id IS NULL`.
- Ajouter un service de cleanup :
  - désactiver ou marquer les promos expirées selon la stratégie retenue.
  - ne jamais supprimer les données nécessaires à l'audit.
- Prévoir une tâche planifiée compatible avec l'infrastructure worker existante.
- Garder `DELETE` comme soft-delete (`is_active=False`).

Critères d'acceptation :
- Une promo expirée ne sort jamais dans la liste publique.
- Une promo expirée ne peut pas être validée ni appliquée.
- L'historique d'usage reste consultable.

### Phase 7 — Tests et validation métier ✅

**But** : sécuriser les comportements sensibles avant branchement des interfaces.

Tests à couvrir :
- Création, modification partielle, désactivation et soft-delete.
- Liste publique filtrée : pas de promo expirée, privée, nominative ou future.
- Prévisualisation sans consommation de quota.
- Application réelle avec incrément atomique.
- Erreurs génériques sur `/validate`.
- Rate-limit sur `/validate`.
- Calcul catégorie/produit.
- Calcul multi-code non cumulable et cumulable.
- Promo nominative.
- Promo avec `email_verified_required`.
- Génération de campagne avec unicité des codes.
- Audit paginé des usages.
- Statistiques admin.
- Statistiques super-admin cross-tenant.

Critères d'acceptation :
- Les tests prouvent que le client ne peut pas forger une remise.
- Les quotas restent cohérents en concurrence.
- Les endpoints exposent les données nécessaires aux interfaces client, staff, admin et super-admin.
- Aucune évolution fonctionnelle hors `app/modules/promotions` n'est requise, sauf prérequis explicite `auth.email_verified`.
