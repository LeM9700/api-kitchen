# Spec — API Pizzeria (FastAPI)

**Date :** 2026-06-19
**Statut :** Validé
**Scope :** API backend uniquement (Phase 1 sur 4)

---

## 1. Contexte & objectif

Digitaliser n'importe quelle pizzeria via une plateforme multi-tenant composée de :
- **API FastAPI** (Python) — ce document
- **App client** (Flutter) — Phase 2
- **App métier** (Flutter) — Phase 3
- **Site vitrine** (React) — Phase 4

L'API est le cœur du système. Elle expose tous les services consommés par les 3 interfaces.

**Modèle commercial :** plateforme SaaS — plusieurs pizzerias (tenants) indépendantes sur une même infrastructure. Démarrage single-tenant, architecture prête pour le scale multi-tenant dès le premier jour.

---

## 2. Architecture globale

### Approche retenue : Monolithe modulaire + Worker async séparé

```
CLIENTS
  App Client (Flutter) │ App Métier (Flutter) │ Site Vitrine (React)
           │                     │                      │
           └─────────────────────┼──────────────────────┘
                                 │ HTTPS + JWT
                                 ▼
                    API FastAPI  (Render/Railway)
          ┌──────────────────────────────────────────┐
          │  Middleware tenant resolver               │
          │  (JWT → tenant_id → schema PostgreSQL)   │
          ├──────────┬──────────┬────────┬───────────┤
          │ catalog  │ orders   │ users  │ payments  │
          │ stock    │ loyalty  │ promo  │ delivery  │
          └──────────┴──────────┴────────┴───────────┘
                      │                        │
          ┌───────────┘                        └──────────┐
          ▼                                               ▼
   PostgreSQL (schemas séparés)              Worker ARQ
   ┌────────────────────────┐            - Alertes stock
   │ schema: public          │            - Emails
   │   tenants, configs      │            - Notifications push
   ├────────────────────────┤            - Stats batch
   │ schema: pizzeria_abc    │
   │   orders, products…     │                 ▼
   ├────────────────────────┤           MongoDB (read model)
   │ schema: pizzeria_xyz    │           - Stats quotidiennes
   │   orders, products…     │           - Stats mensuelles
   └────────────────────────┘           - Live dashboard
```

### Isolation multi-tenant : PostgreSQL schemas séparés

- Schema `public` : table `tenants` + `tenant_configs` (global)
- Un schema PostgreSQL dédié par pizzeria (`tenant_{slug}`)
- Le middleware FastAPI résout le schema depuis le JWT à chaque requête
- Aucun module n'a besoin de filtrer manuellement par tenant — isolation garantie au niveau connexion DB
- Nouvelle pizzeria → création du schema + migration Alembic automatique

---

## 3. Stack technique

| Composant | Technologie |
|-----------|-------------|
| API | FastAPI (Python 3.12) |
| ORM | SQLAlchemy 2 + Alembic |
| DB transactionnelle | PostgreSQL (managed Render/Railway) |
| DB analytique | MongoDB Atlas (free tier → payant si besoin) |
| Worker async | ARQ (Redis-backed) |
| Cache / Queue | Redis (managed) |
| Paiement | Stripe (carte, Apple Pay, Google Pay, cash/TPE) |
| Auth | JWT (access 15min + refresh 30j) |
| Déploiement | Render ou Railway |
| CI/CD | GitHub Actions |

---

## 4. Modèle de données

### Schema `public` (global, cross-tenant)

```sql
tenants
  id, slug, name, plan, created_at

tenant_configs
  tenant_id, delivery_zones (JSONB), stripe_account_id,
  currency, timezone, logo_url
```

### Schema par tenant

**users**
```sql
users
  id, email, phone, password_hash, role (client|staff|admin),
  first_name, last_name, created_at

refresh_tokens
  id, user_id, token_hash, expires_at
```

**catalog**
```sql
categories
  id, name, display_order, is_active

products
  id, category_id, name, description, base_price,
  image_url, is_active, is_available

product_variants
  id, product_id, name, extra_price

extras
  id, name, price, ingredient_id (nullable), ingredient_qty, is_active

product_extras
  product_id, extra_id
```

**orders**
```sql
orders
  id, user_id, status (pending|confirmed|preparing|ready|
  delivering|delivered|cancelled),
  type (delivery|pickup), address (JSONB),
  delivery_zone_id, delivery_fee,
  subtotal, discount_amount, total,
  payment_status, payment_method,
  notes, created_at, updated_at

order_items
  id, order_id, product_id, variant_id,
  quantity, unit_price, extras (JSONB)

order_status_history
  id, order_id, status, changed_by, changed_at
```

**payments**
```sql
payments
  id, order_id, stripe_payment_intent_id,
  method (card|cash|tpe|apple_pay|google_pay),
  amount, status, paid_at
```

**stock**
```sql
ingredients
  id, name, unit (kg|L|pcs), current_qty,
  alert_threshold, cost_per_unit

product_ingredients
  product_id, ingredient_id, qty_required

variant_ingredients
  variant_id, ingredient_id, qty_required
  -- surcharge les product_ingredients si défini

stock_movements
  id, ingredient_id, order_id (nullable),
  type (in|out|adjustment), qty, reason,
  created_by, created_at

product_stock
  id, product_id, current_qty, alert_threshold
```

**loyalty & promotions**
```sql
loyalty_accounts
  id, user_id, points_balance, total_earned

loyalty_transactions
  id, loyalty_account_id, order_id,
  points_delta, reason, created_at

promotions
  id, code, type (percent|fixed|free_item),
  value, min_order_amount, max_uses,
  uses_count, valid_from, valid_until, is_active
```

**delivery**
```sql
delivery_zones
  id, name, polygon (JSONB), fee, min_order_amount,
  estimated_minutes, is_active
```

### Flux de déduction de stock automatique

À chaque commande confirmée, le service orders déclenche :
1. Pour chaque `order_item` → récupérer `variant_ingredients` (fallback `product_ingredients`)
2. Récupérer les extras liés à des ingrédients
3. Déduire `qty_required × quantity` de `ingredients.current_qty`
4. Déduire 1 unité de `product_stock.current_qty`
5. Insérer `stock_movements` (type=out, order_id lié)
6. Si `current_qty < alert_threshold` → publier événement ARQ → alerte admin

---

## 5. Endpoints API

Préfixe global : `/api/v1/`
Convention : réponses paginées `?page=&limit=`, erreurs standardisées.

### Auth
```
POST   /auth/register
POST   /auth/login
POST   /auth/refresh
POST   /auth/logout
```

### Catalog
```
GET    /catalog/categories
GET    /catalog/products
GET    /catalog/products/{id}
POST   /catalog/products                    [admin]
PUT    /catalog/products/{id}               [admin]
DELETE /catalog/products/{id}               [admin]
POST   /catalog/products/{id}/variants      [admin]
POST   /catalog/extras                      [admin]
```

### Orders
```
POST   /orders                              [client]
GET    /orders/{id}                         [client|staff]
GET    /orders/{id}/status                  [client]        # polling
GET    /orders/my                           [client]
GET    /orders                              [staff|admin]
PATCH  /orders/{id}/status                  [staff|admin]
DELETE /orders/{id}                         [client]        # si pending
```

### Payments
```
POST   /payments/intent                     [client]
POST   /payments/confirm                    [client]
POST   /payments/webhook                    [public]        # Stripe webhook
GET    /payments/{order_id}                 [admin]
```

### Stock
```
GET    /stock/ingredients                   [staff|admin]
POST   /stock/ingredients                   [admin]
PUT    /stock/ingredients/{id}              [admin]
POST   /stock/ingredients/{id}/supply       [staff|admin]
GET    /stock/ingredients/alerts            [admin]
GET    /stock/products                      [staff|admin]
POST   /stock/movements                     [admin]
GET    /stock/movements                     [admin]
```

### Loyalty
```
GET    /loyalty/me                          [client]
GET    /loyalty/transactions                [client]
POST   /loyalty/redeem                      [client]
GET    /loyalty/{user_id}                   [admin]
```

### Promotions
```
GET    /promotions                          [public]
POST   /promotions/validate                 [client]
POST   /promotions                          [admin]
PUT    /promotions/{id}                     [admin]
DELETE /promotions/{id}                     [admin]
```

### Delivery
```
GET    /delivery/zones                      [public]
POST   /delivery/zones                      [admin]
PUT    /delivery/zones/{id}                 [admin]
POST   /delivery/check                      [client]
```

### Stats (lecture MongoDB)
```
GET    /admin/stats/daily                   [admin]
GET    /admin/stats/monthly                 [admin]
GET    /admin/stats/live                    [staff|admin]
GET    /admin/stats/stock                   [admin]
GET    /super-admin/stats                   [super-admin]   # cross-tenant
GET    /admin/orders/live                   [staff]         # polling commandes
```

### Super-admin
```
GET    /admin/tenants                       [super-admin]
POST   /admin/tenants                       [super-admin]
```

---

## 6. Pipeline Stats (CQRS)

```
PostgreSQL (source de vérité)
      │
      │  Event-driven (commande livrée, paiement confirmé)
      │  + Cron batch (nuit, 2h00)
      ▼
Stats Service (worker ARQ)
  → agrège les données PostgreSQL
  → upsert dans MongoDB
      │
      ▼
MongoDB (read model)
  - daily_stats_{tenant}    : CA, nb commandes, panier moyen, top produits
  - monthly_stats_{tenant}  : CA, nouveaux clients, fidélité, promos
  - stock_snapshots_{tenant}: état ingrédients, nb alertes
  - live_dashboard_{tenant} : commandes en cours, CA du jour (rafraîchi /2min)
      │
      ▼
API /admin/stats/* → lit uniquement MongoDB (zéro charge sur PostgreSQL)
```

---

## 7. Sécurité

### JWT
- Access token : 15 min, embarque `user_id`, `role`, `tenant_id`
- Refresh token : 30 jours, stocké en DB (révocable), rotation à chaque usage

### Couches de protection
- HTTPS obligatoire (natif Render/Railway)
- Rate limiting (slowapi) : 5 req/min sur `/auth/login`, 60 req/min sur endpoints publics
- CORS : origines whitelist par tenant
- Validation stricte : Pydantic v2 sur tous les inputs
- Webhook Stripe : vérification de signature obligatoire
- Secrets : variables d'environnement uniquement, jamais dans le code

### Format d'erreurs standardisé
```json
{
  "code": "ORDER_NOT_FOUND",
  "detail": "La commande #42 n'existe pas",
  "field": null
}
```

**Codes métier principaux :**

| Code | HTTP | Situation |
|------|------|-----------|
| `AUTH_INVALID_CREDENTIALS` | 401 | Login raté |
| `AUTH_TOKEN_EXPIRED` | 401 | Token expiré |
| `AUTH_FORBIDDEN` | 403 | Rôle insuffisant |
| `TENANT_NOT_FOUND` | 404 | Pizzeria inconnue |
| `ORDER_NOT_FOUND` | 404 | Commande inexistante |
| `ORDER_CANNOT_CANCEL` | 409 | Annulation impossible |
| `STOCK_INSUFFICIENT` | 409 | Stock insuffisant |
| `PROMO_INVALID` | 422 | Code promo invalide |
| `PAYMENT_FAILED` | 402 | Échec Stripe |
| `DELIVERY_ZONE_UNREACHABLE` | 422 | Hors zone |

---

## 8. Tests

| Niveau | Outil | Priorité |
|--------|-------|----------|
| Intégration | pytest + PostgreSQL réelle | Haute |
| Unitaire (logique métier) | pytest | Moyenne |
| End-to-end API | httpx + TestClient | Basse |

- Pas de mock de base de données — tests sur schema PostgreSQL dédié créé/détruit par test
- Flows critiques couverts : commande complète, déduction stock, paiement Stripe, calcul promo

---

## 9. Déploiement

### Services Render/Railway

| Service | Commande de démarrage |
|---------|----------------------|
| API | `uvicorn app.main:app --host 0.0.0.0 --port $PORT` |
| Worker | `python -m app.worker.main` |

### Infrastructure managée
- PostgreSQL : Render managed (backups automatiques)
- MongoDB : Atlas free tier
- Redis : Render managed ou Railway

### Variables d'environnement
```
DATABASE_URL
MONGO_URL
ARQ_REDIS_URL
STRIPE_SECRET_KEY
STRIPE_WEBHOOK_SECRET
JWT_SECRET
ENVIRONMENT (local|staging|production)
```

### CI/CD (GitHub Actions)
```
Push sur main
  → pytest (intégration + unitaires)
  → alembic check
  → ✅ deploy auto staging
  → ❌ bloque le deploy + notif email
```

### Structure des dossiers
```
app/
├── core/
│   ├── config.py
│   ├── database.py
│   ├── security.py
│   └── tenant.py
├── modules/
│   ├── auth/
│   ├── catalog/
│   ├── orders/
│   ├── payments/
│   ├── stock/
│   ├── loyalty/
│   ├── promotions/
│   └── delivery/
├── worker/
│   ├── tasks/
│   │   ├── stock_alerts.py
│   │   ├── notifications.py
│   │   ├── emails.py
│   │   └── stats.py
│   └── main.py
└── main.py
```

---

## 10. Périmètre hors-scope (Phase 1)

Les éléments suivants sont exclus de l'API Phase 1 et feront l'objet de specs séparées :
- App client Flutter (Phase 2)
- App métier Flutter (Phase 3)
- Site vitrine React (Phase 4)
- Notifications push (infrastructure prête, implémentation Phase 3)
- Système de réservation de table
- Gestion multi-établissements par pizzeria (plusieurs adresses)
