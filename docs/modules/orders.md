# Orders

Creation et cycle de vie des commandes : pricing serveur-side, application des promotions, transitions de statut, deduction/restitution de stock atomiques, notifications temps reel, points fidelite, idempotence, livraison et extras.

## Endpoints

| Methode | Path | Auth | Roles | Reponse |
|---------|------|------|-------|---------|
| POST | `/api/v1/orders` | Bearer JWT + `Idempotency-Key` | tous | `OrderOut` |
| POST | `/api/v1/orders/manual` | Bearer JWT + `Idempotency-Key` | staff, admin | `ManualOrderOut` |
| GET | `/api/v1/orders/me` | Bearer JWT | tous | `PaginatedResponse[OrderListOut]` |
| GET | `/api/v1/orders` | Bearer JWT | staff, admin | `PaginatedResponse[OrderListOut]` |
| GET | `/api/v1/orders/export/csv` | Bearer JWT | admin | `text/csv` |
| GET | `/api/v1/orders/{order_id}` | Bearer JWT | proprietaire, staff, admin | `OrderDetailOut` |
| POST | `/api/v1/orders/{order_id}/cancel` | Bearer JWT | proprietaire | `OrderOut` |
| POST | `/api/v1/orders/{order_id}/reorder` | Bearer JWT | proprietaire, staff, admin | `ReorderOut` |
| GET | `/api/v1/orders/{order_id}/receipt` | Bearer JWT | staff, admin | `OrderReceiptOut` |
| PATCH | `/api/v1/orders/{order_id}/status` | Bearer JWT | staff, admin | `OrderOut` |
| PATCH | `/api/v1/orders/{order_id}/items/{item_id}/preparation` | Bearer JWT | staff, admin | `OrderItemOut` |

## Query params

**`GET /orders`** accepte :

- `page`, `page_size`
- `status` : liste separee par virgules, ex. `pending,confirmed`
- `date_from`
- `date_to`

**`GET /orders/me`** accepte :

- `page`, `page_size`

## Modeles de donnees

**`orders`** : `id`, `user_id`, `customer_email`, `customer_name`, `customer_phone`, `order_type`, `status`, `payment_status`, `source`, `created_by_user_id`, `subtotal`, `discount_total`, `delivery_fee`, `total`, `delivery_address`, `delivery_zone_id`, `table_number`, `estimated_delivery_at`, `idempotency_key`, `promo_code`, `created_at`.

**`order_items`** : `id`, `order_id`, `product_id`, `variant_id`, `product_name_snapshot`, `variant_name_snapshot`, `extras_snapshot`, `extras_total`, `quantity`, `unit_price`, `total`, `preparation_status`, `preparation_station`, `prepared_at`, `prepared_by_user_id`.

**`order_status_history`** : `id`, `order_id`, `status`, `note`, `created_at`.

## Migrations

La migration `0023_orders_interfaces_security.py` ajoute :

- `orders.payment_status`
- `orders.delivery_zone_id`
- `orders.estimated_delivery_at`
- `orders.idempotency_key`
- `order_items.product_name_snapshot`
- `order_items.variant_name_snapshot`
- `order_items.extras_snapshot`
- `order_items.extras_total`
- index `orders.user_id`, `orders.status`, `orders.created_at`
- contrainte unique `user_id + idempotency_key`

La migration `0035_admin_staff_api_contracts.py` ajoute :

- `order_type = dine_in`
- `orders.customer_name`, `orders.customer_phone`, `orders.source`, `orders.created_by_user_id`, `orders.table_number`
- `order_items.preparation_status`, `order_items.preparation_station`, `order_items.prepared_at`, `order_items.prepared_by_user_id`
- les contraintes de statut/station de preparation.

## Types de commande

Valeurs supportees :

- `delivery` : `delivery_address` obligatoire, zone/frais de livraison possibles.
- `pickup` : aucune adresse requise, `delivery_fee = 0`, `delivery_zone_id = null`.
- `dine_in` : aucune adresse requise, `delivery_fee = 0`, `delivery_zone_id = null`.

## Transitions de statut

```text
pending -> confirmed | cancelled
confirmed -> preparing | cancelled
queued -> confirmed | cancelled
preparing -> ready | cancelled
ready -> out_for_delivery | delivered
out_for_delivery -> delivered | cancelled
delivered -> terminal
cancelled -> terminal
```

## Cycle paiement / commande

`payment_status` suit le paiement, tandis que `status` suit la production/livraison.

Valeur initiale :

- `status = pending`
- `payment_status = pending`

Le module `payments` marque la commande `payment_status = paid` apres confirmation du paiement.

La transition `pending -> confirmed` est refusee tant que `payment_status != paid`. Cette regle evite de confirmer une commande non payee et garde la deduction de stock dans `orders.update_status`.

## Comportements metier

**Creation (`POST /orders`)**

- Header `Idempotency-Key` obligatoire.
- Rejeu deduplique sur 24h via `user_id + idempotency_key`.
- `items[].unit_price`, `discount_total` et `delivery_fee` client ne sont pas source de verite.
- Les prix sont resolus depuis `Product.base_price`, `ProductVariant.price_delta` et les `Extra` autorises via `ProductExtra`.
- Les extras sont sauvegardes en snapshot JSON dans `order_items.extras_snapshot`.
- `delivery_fee` est calcule cote serveur depuis `delivery_zone_id`.
- Pour `pickup` et `dine_in`, l'API ignore adresse/zone de livraison et force `delivery_fee = 0`.
- `estimated_delivery_at` est calcule depuis `TenantConfig` + `DeliveryZone.estimated_minutes`, puis stocke.
- `total = subtotal - discount_total + delivery_fee`.

**Commande manuelle (`POST /orders/manual`)**

- Reservee staff/admin.
- Header `Idempotency-Key` obligatoire.
- Cree une commande avec `user_id = null`, `source = manual`, `created_by_user_id` et, si fourni, `table_number`.
- Le payload accepte `customer.email`, `customer.full_name`, `customer.phone`, sans compte client obligatoire.
- Les prix restent resolus cote serveur depuis catalogue/variants/extras.
- Le paiement caisse est integre dans le payload via `payment.method`.
- Methodes acceptees : `cash`, `external_terminal`, `cash_register`.
- Pour `external_terminal` et `cash_register`, `external_reference` est obligatoire.
- Si le paiement caisse est valide, la commande passe `payment_status = paid`, puis la transition `pending -> confirmed` reutilise `update_status` pour garder la deduction de stock atomique.
- Reponse : `{ "order": OrderDetailOut, "payment": PaymentOut, "receipt": OrderReceiptOut }`.

**Preparation item par item**

- `PATCH /orders/{order_id}/items/{item_id}/preparation` accepte `pending`, `preparing`, `ready`.
- Les commandes terminales (`delivered`, `cancelled`) refusent les changements avec `ORDER_NOT_ACTIVE`.
- Quand un item passe `ready`, `prepared_at` et `prepared_by_user_id` sont remplis.
- `OrderDetailOut.station_summary` expose un resume par station : `station`, `total_items`, `ready_items`, `all_ready`.
- Le statut global de commande reste pilote par `PATCH /orders/{id}/status`.

**Historique client (`GET /orders/me`)**

- Retourne uniquement les commandes du `user_id` courant.
- Reponse volontairement legere pour les listes.

**Detail commande (`GET /orders/{id}`)**

- Client : seulement ses propres commandes.
- Staff/admin : commandes du tenant courant.
- Retourne les items, snapshots produits/variants, extras et historique de statut.

**Annulation client (`POST /orders/{id}/cancel`)**

- Autorisee uniquement pour le proprietaire.
- Autorisee uniquement si `status == pending`.
- Passe la commande a `cancelled` via le meme service de transition.

**Recommande (`POST /orders/{id}/reorder`)**

- Ne cree pas de commande.
- Retourne un payload panier pre-rempli a partir des `order_items`.
- Verifie que produit et variant sont encore disponibles.
- Signale les items indisponibles dans `unavailable_items`.

**Receipt (`GET /orders/{id}/receipt`)**

- Reserve staff/admin.
- Retourne un JSON structure : items, extras, totaux, adresse, timestamps et metadata.
- Le rendu HTML/PDF/imprimante reste cote interface ou integration caisse.

**Confirmation (`-> confirmed`)**

- Refusee si `payment_status != paid`.
- Deduction de stock atomique dans la meme transaction (`deduct_for_order auto_commit=False`).
- Si la capacite est depassee, la confirmation peut router vers `queued`.
- Apres commit : enqueue ARQ `send_stock_alert` pour chaque ingredient sous seuil.
- Notification WebSocket envoyee au staff.

**Annulation depuis `confirmed` (`-> cancelled`)**

- `restore_for_order` est appele dans la meme transaction.
- Restitue exactement le stock deduit.
- Enqueue ARQ `send_email` d'annulation post-commit.
- Notification client et staff.

**Annulation depuis `pending`**

- Pas de restitution, car le stock n'a pas encore ete deduit.

**Livraison (`-> delivered`)**

- `credit_points_for_order` appele post-commit.
- Les erreurs fidelite sont capturees et loguees.
- La transition n'est pas rollbackee si la fidelite echoue.
- Notification client.

## Securite implementee

- `unit_price`, `discount_total` et `delivery_fee` client ne sont pas fiables.
- Pricing produit, variant, extras et livraison calcule exclusivement cote serveur.
- `Idempotency-Key` obligatoire pour limiter les doubles commandes.
- Rate-limit de creation commande base sur `user_id` authentifie, fallback IP si absent.
- `apply_promo` masque les erreurs derriere `INVALID_PROMO`.
- `PATCH /orders/{id}/status` reserve staff/admin.
- `PATCH /orders/{id}/items/{item_id}/preparation` reserve staff/admin.
- `GET /orders/{id}` protege contre l'IDOR client : un client ne voit que ses commandes.
- Isolation tenant via `get_tenant_session(current_user["tenant_slug"])`.
- Atomicite stock/statut : `deduct_for_order` s'execute dans la transaction de `update_status`.
- Confirmation impossible avant paiement (`payment_status == paid` requis).

Important production :

- Ne jamais appeler `session.commit()` avant `update_status` dans le meme contexte de session, sinon l'atomicite stock/statut peut etre brisee.
- Appliquer `alembic upgrade head` avant de deployer ces contrats API.

## Schemas de reponse

**`OrderListOut`**

Resume de commande pour listes client/staff :

- `id`
- `customer_email`
- `customer_name`
- `customer_phone`
- `source`
- `created_by_user_id`
- `status`
- `payment_status`
- `subtotal`
- `discount_total`
- `delivery_fee`
- `total`
- `delivery_address`
- `delivery_zone_id`
- `table_number`
- `estimated_delivery_at`
- `created_at`

**`OrderDetailOut`**

Etend `OrderListOut` avec :

- `user_id`
- `promo_code`
- `items`
- `station_summary`
- `status_history`

**`OrderItemOut`**

- `product_id`
- `variant_id`
- `product_name`
- `variant_name`
- `quantity`
- `unit_price`
- `extras_total`
- `total`
- `extras`
- `preparation_status`
- `preparation_station`
- `prepared_at`
- `prepared_by_user_id`

**`OrderReceiptOut`**

- `order_id`
- `status`
- `payment_status`
- `customer_email`
- `delivery_address`
- `created_at`
- `estimated_delivery_at`
- `items`
- `totals`
- `meta`

## Ce qui reste a traiter hors module orders

### Interface client

- Panier persiste serveur : a traiter plutot dans un module `cart`.
- Connexion WebSocket client : infrastructure notifications presente, integration interface a faire.
- Page recapitulatif post-paiement : consomme `GET /orders/{id}`.

### Interface staff

- Board cuisine temps reel : consomme `GET /orders?status=...` et WebSocket staff.
- Progression rapide en un clic : consomme `PATCH /orders/{id}/status`.
- Impression ticket : consomme `GET /orders/{id}/receipt`.
- Alerte sonore : cote interface, declenchee depuis les events WebSocket staff.

### Interface admin tenant

- Historique avance avec filtres montant/client/recherche : filtres de base statut/date deja presents, filtres avances a ajouter si necessaire.
- Rapprochement revenus : lien avec `GET /admin/stats/daily`.
- Remboursement : gere par le module `payments`.

### Super-admin

- Vue agreggee cross-tenant sans donnees personnelles : hors scope `orders` tenant courant.

## Verification

Tests executes sur le perimetre :

```text
PYTHONPATH=. pytest tests/test_order_type.py tests/test_orders.py tests/test_catalog.py::test_availability_map_batches_single_query tests/test_catalog.py::test_create_product_requires_admin tests/test_catalog.py::test_catalog_paginated_response_exposes_total_count tests/test_catalog.py::test_catalog_csv_validation_accepts_core_rows tests/test_catalog.py::test_product_inherits_preparation_station_from_category tests/test_catalog.py::test_product_preparation_station_overrides_category tests/test_catalog.py::test_availability_override_requires_reason_when_unavailable tests/test_catalog.py::test_availability_map_applies_latest_unavailable_override tests/test_payments_interfaces.py -q
36 passed
```

Note locale : les tests d'integration PostgreSQL doivent etre rejoues apres `alembic upgrade head`, car les modeles ORM incluent les colonnes ajoutees par `0035_admin_staff_api_contracts.py`.
