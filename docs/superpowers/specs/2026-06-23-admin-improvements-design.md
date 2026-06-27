# Admin Module — Improvements Design

**Date:** 2026-06-23
**Scope:** All improvement axes and missing interface features listed in `docs/modules/admin.md`
**Approach:** Layer-by-layer (migrations → services → schemas/routers → worker)
**Priority order:** A (missing endpoints) → C (business logic) → B (security/robustness)

---

## 1. Context

The admin module covers tenant configuration (hours, closures, prep time, audit), analytics dashboards (MongoDB), and super-admin tenant management. The current implementation is functional but has known gaps documented in `docs/modules/admin.md`.

This spec covers all improvement axes in a single plan, organized by layer to minimize back-and-forth between architectural concerns.

---

## 2. Migrations Alembic

Three new migrations, numbered from `0015` (highest existing: `0014_loyalty_promo_security`).

### `0015_tenant_config_timezone` *(tenant schema)*
```sql
ALTER TABLE tenant_config
  ADD COLUMN timezone VARCHAR(64) NOT NULL DEFAULT 'Europe/Paris';
```
Replaces the hardcoded `_PARIS_TZ = pytz.timezone("Europe/Paris")` constant in `tenant_service.py` with a per-tenant configurable value.

### `0016_audit_user_email` *(tenant schema)*
```sql
ALTER TABLE tenant_config_audit
  ADD COLUMN user_email VARCHAR(255);
```
Nullable — pre-existing audit entries have no email (backward-compatible). Email is denormalized at write time so it remains accurate even if the user later changes their email.

### `0017_tenant_suspension` *(public schema)*
```sql
ALTER TABLE public.tenants
  ADD COLUMN is_suspended BOOLEAN NOT NULL DEFAULT FALSE,
  ADD COLUMN suspended_at TIMESTAMPTZ,
  ADD COLUMN suspension_message TEXT;
```
`is_suspended` is checked by the auth middleware to block all logins for the tenant. `suspension_message` is returned in the 403 response body. Super-admin accounts are exempt from this check.

**No migration for `queued` order status** — `Order.status` is `VARCHAR(32)`, not a PostgreSQL enum. The new status is handled purely in application logic.

---

## 3. Services

### A — Missing endpoints

#### `tenant_service.py` — `get_next_opening()`
New public function wrapping the existing private `_compute_next_opening()`. No new logic — exposes the result already computed internally in `get_tenant_status()`.

```python
async def get_next_opening(session: AsyncSession) -> str | None:
    config = await get_or_create_config(session)
    now_paris = datetime.now(pytz.timezone(config.timezone))
    return await _compute_next_opening(session, now_paris.date(), now_paris.time(), skip_today=False)
```

#### `tenant_service.py` — `_write_audit()` with `user_email`
Add `user_email: str | None` parameter. All callers (`update_config`, `upsert_business_hours`) pass the email extracted from the current JWT payload.

#### `router.py` (admin) — `suspend_tenant()` / `unsuspend_tenant()`
- **Suspend:** writes `is_suspended=True`, `suspended_at=now()`, `suspension_message` to `public.tenants`, then calls `update_config(is_temporarily_closed=True, temporary_closure_message=suspension_message)` on the tenant schema.
- **Unsuspend:** resets `is_suspended=False`, clears `suspended_at` and `suspension_message`, calls `update_config(is_temporarily_closed=False)`.

#### `app/core/deps.py` — suspension check
`get_current_user` (or `require_role`) verifies `public.tenants.is_suspended` for the tenant slug extracted from the JWT. If `True` → 403 with `suspension_message`. Super-admin role is exempt.

---

### C — Advanced business logic

#### `tenant_service.py` — dynamic timezone
Remove module-level `_PARIS_TZ` constant. Replace all `datetime.now(_PARIS_TZ)` calls with `datetime.now(pytz.timezone(config.timezone))`, where `config` is already loaded in context. Affects `get_tenant_status()` and `add_exceptional_closure()`.

#### `tenant_service.py` — cooldown on `is_temporarily_closed`
In `update_config()`, if `is_temporarily_closed` is in the payload: query `tenant_config_audit` for the most recent entry with `field_name='is_temporarily_closed'`. If `changed_at > now - 2min` → raise `HTTPException(429, "Trop de changements de statut. Attendez 2 minutes.")`.

No new DB column needed — reuses the existing audit trail.

#### `tenant_service.py` — config change notifications
After committing in `update_config()`, if `is_temporarily_closed` changed value → enqueue ARQ task `notify_config_change(tenant_slug, new_value)`. The task sends:
- Email to all `admin` users of the tenant.
- Push notification to all active device tokens of `staff` and `admin` users.

#### `orders/service.py` — `queued` status
At order confirmation (payment confirmed → `update_status("confirmed")`), call `get_active_orders_count()` (already exists in `tenant_service.py`). If `active_count >= config.peak_orders_threshold` → set order status to `queued` instead of `confirmed`. The `estimated_prep_time` returned to the client uses the extended formula: `prep_time_peak + (active_count - threshold) * overhead_per_order_minutes`.

---

### B — Security & robustness

#### Rate-limit on `is_temporarily_closed`
The cooldown (2 min) is enforced in the service layer. An additional SlowAPI rate-limit of `5/minute` is applied via a separate `@limiter.limit` decorator on `PATCH /tenant/config` only when `is_temporarily_closed` is present in the request body. Implementation: the router checks for the field presence before calling the service and applies the stricter limit via a dedicated route or a request-time check.

**Practical implementation:** add `PATCH /tenant/toggle-closure` as a dedicated endpoint with `@limiter.limit("5/minute")` for toggling `is_temporarily_closed`, keeping the existing `PATCH /tenant/config` at `30/minute` for all other fields.

---

## 4. Schemas & Routers

### New Pydantic schemas

**MongoDB stats — strict schemas** (inferred from `worker/tasks/stats.py`):

```python
class DailyStatsResponse(BaseModel):
    date: str
    revenue: float
    order_count: int
    avg_basket: float
    tenant_slug: str

class MonthlyStatsResponse(BaseModel):
    tenant_slug: str
    year: str
    month: str
    total_orders: int
    total_revenue: float
    avg_order_value: float
    updated_at: str

class LiveStatsResponse(BaseModel):
    tenant_slug: str
    orders_last_24h: int
    revenue_last_24h: float
    avg_order_value_24h: float
    pending_orders: int
    computed_at: str

class StockAlertItem(BaseModel):
    ingredient_id: int
    name: str
    current_qty: float
    alert_threshold: float
    unit: str

class StockSnapshotResponse(BaseModel):
    tenant_slug: str
    computed_at: str
    alerts: list[StockAlertItem]

class StatsSummaryResponse(BaseModel):
    live: LiveStatsResponse
    last_day: DailyStatsResponse | None
```

**Updated existing schemas:**
- `TenantConfigAuditResponse` — add `user_email: str | None`
- `TenantConfigResponse` — add `timezone: str`
- `TenantConfigUpdate` — add `timezone: str | None` with pytz validation
- `NextOpeningResponse` — `next_opening: str | None`
- `TenantSuspendRequest` — `suspension_message: str` (1–500 chars)
- `TenantResponse` (super-admin) — add `is_suspended: bool`, `suspended_at: datetime | None`, `suspension_message: str | None`

### Router changes

**`tenant_router.py`:**
| Method | Path | Auth | New/Changed |
|--------|------|------|-------------|
| GET | `/tenant/next-opening` | Public | New — `NextOpeningResponse` |
| PATCH | `/tenant/toggle-closure` | Bearer JWT / admin | New — `5/min` rate-limit, `TenantConfigUpdate` subset |

**`router.py` (admin stats):**
| Method | Path | Change |
|--------|------|--------|
| GET | `/stats/daily` | `response_model=list[DailyStatsResponse]` |
| GET | `/stats/monthly` | `response_model=list[MonthlyStatsResponse]` |
| GET | `/stats/live` | `response_model=LiveStatsResponse` |
| GET | `/stats/stock` | `response_model=StockSnapshotResponse` |
| GET | `/stats/summary` | New — `response_model=StatsSummaryResponse`, role `admin` |

**`router.py` (super-admin tenants):**
| Method | Path | Auth | New/Changed |
|--------|------|------|-------------|
| GET | `/tenants` | super-admin | Response enriched with `is_suspended`, `suspended_at` |
| PATCH | `/tenants/{tenant_id}/suspend` | super-admin | New — `TenantSuspendRequest` → `TenantResponse` |
| PATCH | `/tenants/{tenant_id}/unsuspend` | super-admin | New — `TenantResponse` |

---

## 5. Worker

### New cron `aggregate_stock_snapshot`

**File:** `worker/tasks/stock_snapshot.py`
**Schedule:** every hour at `:00`

**Logic:**
1. For each tenant slug (reuses `_get_all_tenant_slugs()`):
2. Query `ingredients` where `current_qty <= alert_threshold`
3. Upsert into `stock_snapshots_{slug}` MongoDB:
```python
{
    "tenant_slug": slug,
    "computed_at": now.isoformat(),
    "alerts": [
        {"ingredient_id": r.id, "name": r.name,
         "current_qty": float(r.current_qty),
         "alert_threshold": float(r.alert_threshold),
         "unit": r.unit}
        for r in under_threshold
    ]
}
```
4. For each ingredient under threshold: if `last_alert_sent_at` is `None` or `> 4h` → enqueue existing `send_stock_alert` task.

### New task `notify_config_change`

**File:** `worker/tasks/emails.py` (added alongside existing tasks)

**Trigger:** enqueued from `tenant_service.update_config()` when `is_temporarily_closed` changes.

**Logic:**
1. Fetch all `admin` users of the tenant from PostgreSQL.
2. Send email (via existing `send_email` task) to each admin: subject `"[{tenant_name}] Statut restaurant modifié"`, body indicating new open/closed state.
3. Fetch all active device tokens for `staff`/`admin` users of the tenant.
4. Send push notification (via existing `send_push_notification`) to each token: title `"Statut restaurant"`, body `"Le restaurant est maintenant OUVERT"` or `"Le restaurant est maintenant FERMÉ"`.

### `worker/main.py` changes
```python
from worker.tasks.stock_snapshot import aggregate_stock_snapshot
from worker.tasks.emails import notify_config_change

# In cron_jobs list:
cron(aggregate_stock_snapshot, hour=set(range(24)), minute={0}),

# In functions list:
notify_config_change,
```

---

## 6. MFA Super-Admin (out of scope)

TOTP or email-based MFA for super-admin accounts is **not implemented** in this plan. The risk is documented: a compromised super-admin JWT exposes the full tenant list. Mitigation to consider in a future iteration: TOTP (RFC 6238) on `/auth/login` for accounts with `role=super-admin`, with a separate `mfa_secret` column on `public.users`.

---

## 7. Files Changed Summary

| Layer | Files modified | Files created |
|-------|---------------|---------------|
| Migrations | — | `0015_tenant_config_timezone.py`, `0016_audit_user_email.py`, `0017_tenant_suspension.py` |
| Services | `app/modules/admin/tenant_service.py`, `app/modules/orders/service.py`, `app/core/deps.py` | — |
| Schemas | `app/modules/admin/tenant_schemas.py` | `app/modules/admin/stats_schemas.py` |
| Routers | `app/modules/admin/tenant_router.py`, `app/modules/admin/router.py` | — |
| Worker | `worker/main.py`, `worker/tasks/emails.py` | `worker/tasks/stock_snapshot.py` |
| Docs | `docs/modules/admin.md` | — |

---

## 8. Out of Scope

- MFA super-admin (documented above, not implemented)
- Worker monitoring / heartbeat (separate worker improvement plan)
- Cross-tenant analytics for super-admin dashboard
- Dead-letter console interface
- Prometheus metrics on worker tasks
