# Stock

Gestion des ingrédients, approvisionnements, recettes produit, mouvements de stock et vérification de disponibilité. Déduction et restitution atomiques à la confirmation/annulation de commande.

## Endpoints

| Méthode | Path | Auth | Rôles |
|---------|------|------|-------|
| GET | `/api/v1/stock/ingredients` | Bearer JWT | staff, admin — `PaginatedResponse` |
| GET | `/api/v1/stock/movements` | Bearer JWT | staff, admin — `PaginatedResponse` |
| GET | `/api/v1/stock/alerts` | Bearer JWT | staff, admin |
| POST | `/api/v1/stock/ingredients` | Bearer JWT | admin |
| PATCH | `/api/v1/stock/ingredients/{id}` | Bearer JWT | admin |
| POST | `/api/v1/stock/ingredients/{id}/adjust` | Bearer JWT | admin |
| POST | `/api/v1/stock/supply` | Bearer JWT | staff, admin |
| POST | `/api/v1/stock/recipes` | Bearer JWT | admin |
| POST | `/api/v1/stock/recipes/variant` | Bearer JWT | admin |
| POST | `/api/v1/stock/recipes/extra` | Bearer JWT | admin |
| GET | `/api/v1/stock/availability?product_ids=` | Bearer JWT | staff, admin — rate-limit 60/min |

## Modèles de données

**`ingredients`** : `id`, `name`, `unit` (kg, L, pièce…), `current_qty`, `alert_threshold`, `last_alert_sent_at` (migration `0005`, nullable — cooldown d'alerte configurable par tenant).

**`product_ingredients`** : `id`, `product_id`, `ingredient_id`, `quantity` (quantité requise par unité produit).

**`extra_ingredients`** : `id`, `extra_id`, `ingredient_id`, `quantity` (quantité requise par extra commandé).

**`variant_ingredients`** : `id`, `variant_id`, `ingredient_id`, `quantity` (existant en base mais non lu par `deduct_for_order`).

**`stock_movements`** : `id`, `ingredient_id`, `quantity_delta` (positif = entrée, négatif = sortie), `reason` (`supply`, `inventory`, `waste`, `correction`, `order:{id}`, `cancel:{id}`), `user_id`, `created_at`.

**`product_stock`** : `id`, `product_id`, `available_qty` (table complémentaire, non couplée au système de déduction par recette).

## Comportements métier

**Approvisionnement (`POST /supply`)** : incrémente `current_qty` + crée un `StockMovement(reason="supply")`.

**Liste ingrédients (`GET /stock/ingredients`)** : supporte `below_threshold`, `unit`, `search`; chaque item expose `is_below_threshold` pour l'UI.

**Historique (`GET /stock/movements`)** : paginé, filtrable par `ingredient_id`, `date_from`, `date_to`.

**Alertes (`GET /stock/alerts`)** : retourne uniquement les ingrédients dont `current_qty < alert_threshold`.

**Patch ingrédient (`PATCH /stock/ingredients/{id}`)** : met à jour indépendamment `name`, `unit`, `alert_threshold`; les champs inconnus sont ignorés.

**Ajustement manuel (`POST /stock/ingredients/{id}/adjust`)** :
- `reason="inventory"` remplace `current_qty` par `new_qty` puis enregistre le delta calculé dans `stock_movements`.
- `reason="waste"` ou `reason="correction"` applique directement `quantity` comme delta signé.

**Recettes variantes/extras** :
- `POST /stock/recipes/variant` lie un `variant_id` à un `ingredient_id` avec `quantity`.
- `POST /stock/recipes/extra` lie un `extra_id` à un `ingredient_id` avec `quantity`.

**Déduction à la confirmation** : `deduct_for_order(auto_commit=False)` appelée par `orders/service.update_status` dans la même transaction. Pour chaque `OrderItem` :
1. Lit les `ProductIngredient` (recette).
2. Ajoute les `VariantIngredient` si `variant_id` est renseigné.
3. Ajoute les `ExtraIngredient` résolus via `extras_snapshot[].extra_id`.
2. Vérifie `current_qty >= qty_required × item.quantity`.
3. Si insuffisant, lève `INSUFFICIENT_STOCK` (409).
4. Décrémente `current_qty`, crée `StockMovement(reason="order:{order_id}")`.
5. Après commit (dans le caller), enqueue ARQ `send_stock_alert` si sous `alert_threshold`.

**Restitution à l'annulation depuis `confirmed`** : `restore_for_order` lit les `OrderItems`, leurs recettes produit, variantes et extras, crée des `StockMovement` positifs (`reason="cancel:{order_id}"`), incrémente `current_qty`. Dans la même transaction que `update_status`.

**Disponibilité (`GET /availability`)** : pour chaque `product_id`, compare les quantités requises (recette) au `current_qty` des ingrédients. Retourne `{"product_id": int, "available": bool, "limiting_ingredient": str | None}`. Si aucune recette définie : `available = True` par défaut.

**Alertes stock** : worker ARQ `send_stock_alert` — lit `tenant_config.stock_alert_cooldown_hours` au runtime, évalue `last_alert_sent_at`, puis met à jour `last_alert_sent_at` avant d'enqueue `send_stock_alert_email` (évite les doublons en cas de retry ARQ).

## Sécurité implémentée

- [🔒] Atomicité deduct/restore : s'exécutent dans la transaction `update_status` — pas de stock partiel.
- [🔒] Vérification de stock avant déduction — `INSUFFICIENT_STOCK` (409) si manque.
- [⚠️ PROD] `deduct_for_order` doit être appelé avec `auto_commit=False` depuis `update_status` — appel direct avec `auto_commit=True` brise l'atomicité.

---

## Axes d'amélioration

### Logique métier
- **Prévision de rupture** : pas de logique de prévision basée sur la vélocité de consommation.
- **`product_stock.available_qty`** : table `product_stock` existe mais découplée du système de déduction par recette — source de confusion potentielle.

### Sécurité & contre-intrusion
- **Stock négatif** : si `deduct_for_order` est appelée en dehors de la transaction protégée (bug de refactoring), `current_qty` peut devenir négatif. Ajouter une contrainte `CHECK (current_qty >= 0)` en base.
- **Approvisionnement frauduleux** : `POST /supply` est accessible staff — un staff malveillant peut gonfler le stock pour masquer une perte. Ajouter une validation (quantité max par approvisionnement) et une entrée dans `stock_movements` avec `changed_by_user_id`.
- **DoS par indisponibilité** : si un attaquant passe de nombreuses commandes simultanées pour épuiser un ingrédient clé, les commandes légitimes seront rejetées (409). Mitigation : réserver (soft-lock) le stock lors du `pending` et non au `confirmed`.
- **Injection de `product_id`** : `GET /availability?product_ids=1,2,...` — valider que les IDs sont bien des entiers et qu'ils appartiennent au tenant courant.
- **Alerte flooding** : le cooldown configurable `last_alert_sent_at` limite le spam email, mais un ingrédient qui oscille autour du seuil peut toujours déclencher une alerte à chaque nouvelle fenêtre.

### Accessibilité API
- Ajouter `GET /stock/movements?ingredient_id=X&limit=50` pour l'historique.
- Ajouter `POST /stock/ingredients/{id}/adjust` pour les ajustements manuels (inventaire, pertes).
- Inclure `alert_threshold` et `is_below_threshold` dans `GET /ingredients` pour la mise en évidence UI.
- Ajouter `GET /stock/alerts` pour lister les ingrédients actuellement sous seuil.

---

## Ce qui manque pour les interfaces

### Interface staff
- **Liste des ingrédients en alerte** : `GET /stock/ingredients` + filtre `below_threshold=true` — pour gérer les réapprovisionnements urgents.
- **Formulaire d'approvisionnement** : `POST /supply` avec sélection d'ingrédient et quantité.
- **Disponibilité produits** : `GET /stock/availability?product_ids=...` pour afficher quels produits sont en rupture avant ouverture.

### Interface admin (tenant)
- **Gestion des ingrédients** : CRUD ingrédients avec unités et seuils d'alerte.
- **Gestion des recettes** : lier produits/variantes/extras à leurs ingrédients avec les quantités (`POST /stock/recipes`).
- **Tableau de stock en temps réel** : liste des ingrédients avec `current_qty` vs `alert_threshold`, code couleur rouge/orange/vert.
- **Historique des mouvements** : graph de consommation par ingrédient sur 7/30 jours.
- **Seuils d'alerte configurables** : modifier `alert_threshold` par ingrédient depuis l'interface.
- **Snapshot stock** : alimenter `stock_snapshots_{slug}` MongoDB (non implémenté) pour `GET /admin/stats/stock`.

### Super-admin
- Accès aux tableaux de stock en lecture (pour détecter des tenants en situation de rupture critique).
