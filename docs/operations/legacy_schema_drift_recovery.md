# Legacy schema drift recovery

This procedure is for a local PostgreSQL database whose tenant schemas have
drifted from Alembic history. It is not a production migration path.

## Why drift can exist

The app provisions new tenant schemas from SQLAlchemy metadata, while historical
Alembic migrations update tenants that already exist. In a local database used
across feature branches, some tenants can therefore contain objects from newer
metadata even though `public.alembic_version` still points to an older revision.

## How to identify it

Confirm the target database first and mask secrets in any output:

```bash
uv run alembic current
uv run alembic heads
```

Then audit every tenant from `public.tenants` using `information_schema` and
`pg_catalog`. For the 0047 recovery, verify:

- `products.external_product_id varchar(255) NULL`
- `products.tax_rate numeric(5,4) NULL`
- `uq_products_external_product_id`
- `product_overrides.product_id`
- unique constraint on `product_id`
- FK `product_id -> products.id ON DELETE CASCADE`
- old `connection_id` and `external_product_id` columns removed from
  `product_overrides`

Do not rely on a single tenant as representative.

## Why 0047 is not modified

Migration `0047_products_external_id_tax_rate_overrides_refactor.py` is
historical and has already run in deployed environments. Editing it would change
history for databases that have already applied it. Local drift must be repaired
with a one-off recovery procedure, then Alembic can be reconciled only after the
actual schemas match the historical migration.

## Before repair

Create and validate a full custom-format dump:

```bash
mkdir -p backups
pg_dump -Fc "$DATABASE_URL" > backups/pizza-before-0047-recovery-YYYYMMDD-HHMMSS.dump
pg_restore --list backups/pizza-before-0047-recovery-YYYYMMDD-HHMMSS.dump
```

If `pg_restore --list` cannot read the dump, stop.

Stop local app or worker processes that can write to the local DB during the
repair. Do not stop PostgreSQL.

## Tenant-aware repair

For each tenant, recheck the expected pre-repair state immediately before any
DDL. If a tenant no longer matches the expected profile, classify it as
`CHANGED_SINCE_AUDIT` and skip it.

Use one transaction per tenant. Before replacing `product_overrides`, lock the
table and run `SELECT COUNT(*)`. If it is not zero, roll back that tenant and
perform a data migration review instead of dropping the table.

Never run a generic destructive script across tenants with mixed states.

## Disposable local tenants

If test tenants are incomplete, verify that they contain no useful business data
before cleanup. Check tenant users, orders, payments, customer data, employee
data, stock, catalog data, POS connections, and tenant config rows. Seed-only
rows such as regulatory allergens, dietary tags, and the default establishment
are not useful business data by themselves.

If no project deprovisioning service exists, remove a disposable tenant in a
transaction after dependency checks:

```sql
BEGIN;
DROP SCHEMA IF EXISTS "tenant_slug_here" CASCADE;
DELETE FROM public.tenants WHERE slug = 'slug_here';
COMMIT;
```

## Conditions before stamp

Only run `alembic stamp` when every remaining tenant has been reaudited and is
equivalent to the target migration.

For 0047:

```bash
uv run alembic stamp 0047
uv run alembic current
```

If later migrations are already partially present because of local metadata
provisioning, audit those migrations the same way. Create only missing objects,
leave equivalent existing objects untouched, and stamp the final revision only
after every remaining tenant matches the target state.

## Validation commands

```bash
uv run alembic current
uv run alembic heads
uv run python -c "from app.main import app; print(app.openapi()['openapi'])"
uv run python -c "import worker.main; import worker.tasks.order_hub"
uv run pytest -v --tb=short tests/test_order_hub.py tests/test_pos_order_webhook.py
uv run pytest -v --tb=short tests/test_worker_catalog_sync.py tests/test_catalog_hub_sync_integration.py
uv run ruff check app tests
```

Finally restart the local API if it was stopped and verify:

```bash
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/health/ready
```
