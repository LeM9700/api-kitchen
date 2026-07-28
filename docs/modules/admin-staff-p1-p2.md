# Admin/Staff P1-P2 API Contracts

Ce document complete les contrats P0 pour l'app Flutter admin/staff.

## P1 - Stock, DLC et exports

### Lots ingredients / DLC

Endpoints :

| Methode | Path | Auth |
|---------|------|------|
| GET | `/api/v1/stock/ingredients/{id}/batches` | staff/admin avec `stock:read` |
| POST | `/api/v1/stock/ingredients/{id}/batches` | staff/admin avec `stock:write` |
| PATCH | `/api/v1/stock/batches/{batch_id}` | admin |
| POST | `/api/v1/stock/batches/{batch_id}/open` | staff/admin avec `stock:write` |
| POST | `/api/v1/stock/batches/{batch_id}/discard` | staff/admin avec `stock:write` |

Modele `ingredient_batches` :

- `ingredient_id`
- `quantity`
- `received_at`
- `expires_at`
- `opened_at`
- `use_within_hours_after_opening`
- `status`: `sealed`, `opened`, `expired`, `consumed`, `discarded`
- `created_by_user_id`

La reponse expose `effective_expires_at`, calcule comme la date la plus proche entre `expires_at` et `opened_at + use_within_hours_after_opening`.

### Demandes d'ajustement stock

Endpoints :

| Methode | Path | Auth |
|---------|------|------|
| POST | `/api/v1/stock/adjustment-requests` | staff/admin avec `stock:adjustment:create` |
| GET | `/api/v1/stock/adjustment-requests` | admin |
| POST | `/api/v1/stock/adjustment-requests/{id}/approve` | admin |
| POST | `/api/v1/stock/adjustment-requests/{id}/reject` | admin |

Le staff cree une demande sans modifier le stock. L'admin approuve ou rejette. A l'approbation seulement, l'API applique `quantity_delta`, cree un `StockMovement(reason="request:{reason}")`, audite `reviewed_by_user_id/reviewed_at` et refuse tout stock negatif.

`tenant_config.large_stock_adjustment_threshold` configure le seuil de gros ajustement. Les reponses exposent `is_large_adjustment` pour permettre une confirmation UI.

### Exports CSV

Endpoints :

| Methode | Path | Auth |
|---------|------|------|
| GET | `/api/v1/orders/export/csv` | admin |
| GET | `/api/v1/payments/export/csv` | admin |

Filtres supportes : `date_from`, `date_to`, `status`, `payment_status`, `order_type`. L'export paiements supporte aussi `provider`.

## P2 - Permissions, impression et Stripe Terminal

### Permissions fines staff

Les JWT exposent `permissions`. Admin et super-admin ont acces complet. Un staff avec `permissions=null` garde les acces historiques pendant la migration. Un staff avec une liste explicite est limite a cette liste.

Endpoint admin :

```http
PATCH /api/v1/admin/users/{user_id}/permissions
```

Payload :

```json
{
  "permissions": ["orders:read", "orders:preparation", "payments:terminal"]
}
```

Permissions branchees dans cette phase :

- `orders:read`
- `orders:manual`
- `orders:write`
- `orders:preparation`
- `payments:read`
- `payments:terminal`
- `stock:read`
- `stock:write`
- `stock:adjustment:create`
- `catalog:read`
- `catalog:write`
- `catalog:availability`
- `print:read`

### Impression partagee

Endpoints :

| Methode | Path | Auth |
|---------|------|------|
| GET | `/api/v1/tenant/print-config` | staff/admin avec `print:read` |
| PATCH | `/api/v1/tenant/print-config` | admin |

Champs :

- `print_enabled`
- `print_config`

Les modifications sont tracees dans `tenant_config_audits`.

### Stripe Terminal / Tap to Pay

Endpoints :

| Methode | Path | Auth |
|---------|------|------|
| POST | `/api/v1/payments/terminal/connection-token` | staff/admin avec `payments:terminal` |
| POST | `/api/v1/payments/terminal/intent` | staff/admin avec `payments:terminal` |
| GET | `/api/v1/payments/terminal/readers` | staff/admin avec `payments:terminal` |
| POST | `/api/v1/payments/terminal/readers/{reader_id}/process` | staff/admin avec `payments:terminal` |
| POST | `/api/v1/payments/terminal/readers/{reader_id}/cancel` | staff/admin avec `payments:terminal` |

`/terminal/intent` cree un paiement local `provider="stripe_terminal"` et un PaymentIntent Stripe `payment_method_types=["card_present"]`. Si `reader_id` et `process_on_reader=true` sont fournis, l'API declenche l'action lecteur. En local/dev/test, un fallback `local_terminal_{payment_id}` permet de tester le flux sans Stripe.

## Migration et provisioning

La migration `0036_p1_p2_admin_staff_contracts.py` ajoute :

- `users.permissions`
- `tenant_config.large_stock_adjustment_threshold`
- `tenant_config.print_enabled`
- `tenant_config.print_config`
- `ingredient_batches`
- `stock_adjustment_requests`

Le provisioning des nouveaux tenants dans `app/modules/auth/service.py` est aligne avec ces structures.
