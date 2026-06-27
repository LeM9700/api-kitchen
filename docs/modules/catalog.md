# Catalog

Exposition du catalogue produits d'un tenant : catégories, produits, variantes, extras, galerie d'images, allergènes réglementaires (UE 1169/2011) et étiquettes diététiques.

## Endpoints

### Catégories
| Méthode | Path | Auth | Rôles |
|---------|------|------|-------|
| GET | `/api/v1/catalog/categories` | Public | — rate-limit 60/min |
| POST | `/api/v1/catalog/categories` | Bearer JWT | admin |

### Produits
| Méthode | Path | Auth | Rôles |
|---------|------|------|-------|
| GET | `/api/v1/catalog/products` | Public | — rate-limit 60/min — FTS + filtres + payload enrichi |
| GET | `/api/v1/catalog/products/suggestions?q=` | Public | — rate-limit 60/min — autocomplete |
| GET | `/api/v1/catalog/products/{product_id}` | Public | — rate-limit 60/min — fiche détaillée |
| POST | `/api/v1/catalog/products` | Bearer JWT | admin |
| PUT | `/api/v1/catalog/products/{product_id}` | Bearer JWT | admin |
| DELETE | `/api/v1/catalog/products/{product_id}` | Bearer JWT | admin |

### Variantes
| Méthode | Path | Auth | Rôles |
|---------|------|------|-------|
| POST | `/api/v1/catalog/products/{product_id}/variants` | Bearer JWT | staff, admin |
| GET | `/api/v1/catalog/products/{product_id}/variants` | Bearer JWT | staff, admin |
| PUT | `/api/v1/catalog/products/{product_id}/variants/{variant_id}` | Bearer JWT | staff, admin |
| DELETE | `/api/v1/catalog/products/{product_id}/variants/{variant_id}` | Bearer JWT | staff, admin |

### Extras
| Méthode | Path | Auth | Rôles |
|---------|------|------|-------|
| GET | `/api/v1/catalog/extras` | Public | — rate-limit 60/min |
| POST | `/api/v1/catalog/extras` | Bearer JWT | admin |
| PUT | `/api/v1/catalog/extras/{extra_id}` | Bearer JWT | admin |
| POST | `/api/v1/catalog/products/{product_id}/extras/{extra_id}` | Bearer JWT | staff, admin |
| DELETE | `/api/v1/catalog/products/{product_id}/extras/{extra_id}` | Bearer JWT | staff, admin |
| GET | `/api/v1/catalog/products/{product_id}/extras` | Bearer JWT | staff, admin |

### Images (galerie — migration `0006`)
| Méthode | Path | Auth | Rôles |
|---------|------|------|-------|
| POST | `/api/v1/catalog/{entity_type}/{entity_id}/images` | Bearer JWT | staff, admin — rate-limit 20/min |
| GET | `/api/v1/catalog/{entity_type}/{entity_id}/images` | Public | — |
| DELETE | `/api/v1/catalog/images/{image_id}` | Bearer JWT | staff, admin |
| PATCH | `/api/v1/catalog/images/{image_id}/primary` | Bearer JWT | staff, admin |
| PATCH | `/api/v1/catalog/{entity_type}/{entity_id}/images/reorder` | Bearer JWT | staff, admin |

`entity_type` accepte : `products`, `categories`, `extras`, `variants`.

### Allergènes & étiquettes diététiques (migrations `0009`, `0013`)
| Méthode | Path | Auth | Rôles |
|---------|------|------|-------|
| GET | `/api/v1/catalog/allergens` | Public | — |
| POST | `/api/v1/catalog/allergens` | Bearer JWT | admin |
| GET | `/api/v1/catalog/products/{product_id}/allergens` | Public | — |
| PATCH | `/api/v1/catalog/products/{product_id}/allergens/{allergen_id}` | Bearer JWT | admin |
| POST | `/api/v1/catalog/products/{product_id}/allergens/recompute` | Bearer JWT | staff, admin — rate-limit 10/min |
| GET | `/api/v1/catalog/products/{product_id}/allergens/audit` | Bearer JWT | admin |
| GET | `/api/v1/catalog/dietary-tags` | Public | — |
| PUT | `/api/v1/catalog/products/{product_id}/dietary-tags` | Bearer JWT | admin |

### Recommandations / bundles simples (migration `0022`)
| Méthode | Path | Auth | Rôles |
|---------|------|------|-------|
| GET | `/api/v1/catalog/products/{product_id}/recommendations` | Bearer JWT | staff, admin |
| POST | `/api/v1/catalog/products/{product_id}/recommendations` | Bearer JWT | admin |
| PUT | `/api/v1/catalog/products/{product_id}/recommendations/{recommendation_id}` | Bearer JWT | admin |
| DELETE | `/api/v1/catalog/products/{product_id}/recommendations/{recommendation_id}` | Bearer JWT | admin |

### Import / export CSV (migration `0022`)
| Méthode | Path | Auth | Rôles |
|---------|------|------|-------|
| POST | `/api/v1/catalog/imports/csv/dry-run` | Bearer JWT | admin |
| POST | `/api/v1/catalog/imports/csv/{token}/confirm` | Bearer JWT | admin |
| GET | `/api/v1/catalog/exports/csv` | Bearer JWT | admin |

### Audit prix & complétude admin (migration `0022`)
| Méthode | Path | Auth | Rôles |
|---------|------|------|-------|
| GET | `/api/v1/catalog/admin/completeness` | Bearer JWT | staff, admin |
| GET | `/api/v1/catalog/price-audit/{entity_type}/{entity_id}` | Bearer JWT | admin |

### Super-admin lecture seule
| Méthode | Path | Auth | Rôles |
|---------|------|------|-------|
| GET | `/api/v1/catalog/super-admin/tenants/{tenant_slug}/products` | Bearer JWT | super-admin |
| GET | `/api/v1/catalog/super-admin/tenants/{tenant_slug}/products/{product_id}` | Bearer JWT | super-admin |

## Modèles de données

**`categories`** : `id`, `name`, `display_order`, `is_active`.

**`products`** : `id`, `category_id`, `name`, `description`, `base_price`, `image_url`, `is_active`.

**`product_variants`** : `id`, `product_id`, `name`, `price_delta` (delta sur `base_price`), `is_active`.

**`extras`** : `id`, `name`, `price`, `is_active`.

**`product_extras`** : table de liaison `(product_id, extra_id)` — PK composite.

**`extra_ingredients`** (migration `0022`) : `id`, `extra_id`, `ingredient_id`, `quantity`. Permet de calculer les allergènes d'une sélection réelle incluant les extras.

**`media_images`** (migration `0006`) : `id`, `entity_type`, `entity_id`, `cloudinary_public_id` (unique), `url`, `url_thumbnail` (300×300 `c_fill`), `url_medium` (800px), `format`, `size_bytes`, `width`, `height`, `is_primary`, `display_order`, `alt_text`, `created_at`.

**`allergen_definitions`** (migration `0009`) : `id`, `name`, `slug` (unique), `is_regulatory` (14 allergènes UE + customs), `description`, `created_at`. Seedé automatiquement avec les 14 allergènes UE.

**`product_allergens`** : `(product_id, allergen_id)` PK composite, `level` (present/traces/absent), `source` (ingredient/manual).

**`dietary_tags`** : `id`, `name`, `slug` (unique). Seedé avec 8 étiquettes.

**`product_dietary_tags`** : `(product_id, dietary_tag_id)` PK composite.

**`allergen_change_audit`** (migration `0013`) : `id`, `product_id`, `allergen_id`, `changed_by_user_id`, `changed_at`, `old_level`, `new_level`, `old_source`, `new_source`, `ip_address`, `reason`. Table immuable pour conformité HACCP.

**`product_recommendations`** (migration `0022`) : `id`, `product_id`, `recommended_product_id`, `display_order`, `label`, `is_active`. Associations manuelles pour produits suggérés / bundles simples.

**`catalog_price_audits`** (migration `0022`) : `id`, `entity_type` (`product`, `variant`, `extra`), `entity_id`, `old_price`, `new_price`, `changed_by_user_id`, `source` (`admin`, `import`), `reason`, `changed_at`.

**`catalog_import_batches`** (migration `0022`) : `id`, `token`, `filename`, `csv_text`, `status`, `validation_report`, `created_by_user_id`, `created_at`. Stocke les dry-runs CSV jusqu'à confirmation.

## Comportements métier

**Variantes** : `price_delta` ajouté à `base_price` pour le prix final. Résolu côté serveur à la création de commande.

**Soft-delete produits/variantes** : `DELETE` positionne `is_active = False`, pas de suppression physique.

**Payload public enrichi** : `GET /products` retourne désormais un résumé prêt interface client : catégorie, image primaire, allergènes, dietary tags, disponibilité stock de base et `regulatory_complete`. Les réponses paginées catalog exposent aussi `total_count`.

**Fiche produit détaillée** : `GET /products/{id}` agrège produit, catégorie, variants, extras, allergènes, dietary tags, galerie, recommandations et disponibilité stock de base.

**Images** : upload multipart vers Cloudinary, 3 URLs générées (original / thumbnail 300×300 / medium 800px). Validation magic bytes + extension. Sanitisation SVG via defusedxml (XSS). Première image auto-promue `is_primary`. Suppression Cloudinary physique à `DELETE /images/{id}` avec auto-promotion de la suivante. Depuis `0022`, un quota tenant + entité est appliqué et le `cloudinary_public_id` retourné doit commencer par le préfixe tenant attendu.

**Allergènes** : `PATCH /products/{product_id}/allergens/{allergen_id}` associe manuellement un allergène à un produit avec le niveau (present/traces/absent). Chaque modification crée une entrée dans `allergen_change_audit` (immuable). La publication d'un produit (`is_active = True`) nécessite que les 14 allergènes réglementaires soient déclarés — `validate_product_for_publication()` rejette la mise à jour sinon.

**Composition réelle** : `validate_catalog_selection_for_order()` calcule les allergènes d'une sélection produit + variant + extras via `product_ingredients`, `variant_ingredients` et `extra_ingredients`. Ce service interne est prêt à être appelé par panier/commande.

**Recherche produits** : `GET /products?q=` utilise un index GIN PostgreSQL (langue française) sur `name` et `description`. Filtres additionnels : `category_id`, `allergen_slug`, `dietary_tag_slug`.

**Autocomplete** : `GET /products/suggestions?q=` retourne des suggestions légères (`id`, `name`, `category_id`, image primaire).

**Import/export CSV** : import en deux étapes : `dry-run` valide et stocke un lot, `confirm` applique les upserts/liens si le lot est valide. L'export CSV couvre catalogue textuel/métier : catégories, produits, variants, extras, associations, allergènes/tags, recommandations et composition des extras.

**Historique de prix** : toute création ou modification de prix produit/variant/extra via admin ou import CSV crée une entrée `catalog_price_audits`.

**Recommandations** : associations manuelles produit → produits recommandés, avec `display_order`, `label` et soft-disable via `is_active`.

**Disponibilité stock** : le résumé et le détail produit exposent une disponibilité de base via lecture du service stock existant (`get_product_availability`) sans modifier le module stock.

**Super-admin** : routes lecture seule par `tenant_slug` explicite pour consulter le catalogue d'un tenant sans manipulation manuelle du `search_path`.

## Sécurité implémentée

- [🔒] `entity_type` validé en whitelist — 422 si invalide (prévient les injections de path).
- [🔒] SVG sanitisé via defusedxml (XSS) avec blocage de tags dangereux : `<script>`, `<use>`, `<foreignObject>`, `<iframe>`, `<object>`, `<embed>`.
- [🔒] Magic bytes + extension validés avant upload Cloudinary (limite 8 Mo).
- [🔒] Vérification du préfixe Cloudinary retourné : `pizza/{tenant_slug}/{entity_type}/{entity_id}/`.
- [🔒] Quotas images : 500 images par tenant, 10 images par entité catalogue.
- [🔒] `primary` et `reorder` vérifient que les images appartiennent bien à l'entité ciblée.
- [🔒] `validate_product_for_publication()` : bloque la mise à jour `is_active=True` si un allergène réglementaire n'est pas déclaré — conformité UE 1169/2011.
- [🔒] Audit trail immuable `allergen_change_audit` pour toutes les modifications allergènes (IP + user).
- [🔒] Audit prix `catalog_price_audits` pour les changements de prix produits, variants et extras.
- [🔒] Recherche FTS : `q=` reste bindé via paramètres SQLAlchemy (`plainto_tsquery('french', :q)`), pas d'interpolation f-string.

---

## Axes d'amélioration — statut après phases 0 → 8

### Réalisé
- **Variants / extras d'allergènes** : la composition réelle est calculable via produit + variant + extras (`validate_catalog_selection_for_order()`).
- **Import/export CSV** : dry-run, confirmation et export CSV disponibles.
- **Historique de prix** : audit complet des prix produits, variants et extras.
- **Produits liés / recommandations** : associations manuelles disponibles.
- **Disponibilité stock inline** : résumé et détail produit exposent `availability` via lecture stock.
- **Path traversal Cloudinary** : préfixe tenant vérifié après upload.
- **Upload DoS** : quotas tenant et entité ajoutés en plus du rate-limit IP.
- **GIN SQL injection** : requêtes FTS paramétrées.
- **Allergen bypass** : service interne catalog disponible pour validation panier/commande.
- **SVG malgré defusedxml** : tags SVG dangereux supplémentaires bloqués.
- **Accessibilité API** : `GET /products`, `GET /products/{id}`, filtres `allergen_slug` / `dietary_tag_slug`, `total_count` et suggestions sont exposés.
- **Super-admin** : accès lecture seule dédié par `tenant_slug`.

### À intégrer côté interfaces
- **Interface client** : consommer `ProductSummaryOut`, `ProductDetailOut`, `products/suggestions`, filtres visuels allergènes/tags et galerie.
- **Interface staff** : brancher drag-and-drop upload image sur les endpoints existants ; afficher `availability`.
- **Interface admin tenant** : construire l'UI de CRUD produits/variants/extras, module allergènes, complétude catalogue, import/export CSV, historique prix et recommandations.
- **Super-admin UI** : ajouter les écrans lecture seule multi-tenant.

### Reste technique / vigilance
- Brancher explicitement `validate_catalog_selection_for_order()` dans le module panier/commande quand ce flux sera travaillé. Le service est prêt côté `catalog`, mais `orders` n'a pas été modifié dans cette phase.
- Rejouer les tests d'intégration DB/images quand PostgreSQL local est disponible. Les tests unitaires catalog et Cloudinary passent, mais la suite DB a été bloquée par une connexion PostgreSQL refusée (`WinError 1225`).
- Évaluer plus tard une conversion SVG → PNG côté Cloudinary si la politique produit veut supprimer complètement les SVG servis aux clients.
