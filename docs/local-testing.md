# Local Testing

## 1. Activate the project environment

```powershell
.\.venv312\Scripts\Activate.ps1
```

If the editable install must be redone in this environment:

```powershell
python -m pip install --no-build-isolation -e ".[dev]"
```

## 2. Start PostgreSQL for local development

With Docker:

```powershell
docker run --name pizza-postgres -e POSTGRES_USER=postgres -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=pizza -p 55432:5432 -d postgres:16
docker exec pizza-postgres createdb -U postgres pizza_test
```

If the container already exists:

```powershell
docker start pizza-postgres
docker exec pizza-postgres createdb -U postgres pizza_test
```

The second command can fail with "already exists"; that is fine.

## 3. Configure `.env`

Use real PostgreSQL credentials, not the placeholder `user:pass` values:

```env
DATABASE_URL=postgresql+asyncpg://postgres:postgres@127.0.0.1:55432/pizza
TEST_DATABASE_URL=postgresql+asyncpg://postgres:postgres@127.0.0.1:55432/pizza_test
MONGO_URL=mongodb://localhost:27017
ARQ_REDIS_URL=redis://localhost:6379
STRIPE_SECRET_KEY=sk_test_xxx
STRIPE_WEBHOOK_SECRET=whsec_xxx
JWT_SECRET=change-me-32-chars-minimum
SUPER_ADMIN_MFA_ENCRYPTION_KEY=<fernet key, see below>
TENANT_MFA_ENCRYPTION_KEY=<fernet key, see below>
ENVIRONMENT=local
```

`SUPER_ADMIN_MFA_ENCRYPTION_KEY` and `TENANT_MFA_ENCRYPTION_KEY` are required for the super-admin
MFA endpoints (`/super-admin/mfa/setup`, `/mfa/confirm`, login with `mfa_code`) and tenant-user MFA
to work — without them, `encrypt_super_admin_mfa_secret`/`encrypt_tenant_mfa_secret` raise
`CryptoNotConfigured` and the related tests fail. Generate two **distinct** Fernet keys (never reuse
one for both):

```powershell
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

**Important**: `DATABASE_URL` is the URL the running app actually connects through — pytest fixtures
that verify DB state directly (`db_engine` in `tests/conftest.py`) use `TEST_DATABASE_URL` if set,
*falling back to* `DATABASE_URL` otherwise. If you set both to different databases, the app under
test writes to one while fixtures assert against the other, and integration tests will fail with
misleading "missing table"/"tenant not found" errors. Point both at the same database (`pizza_test`)
unless you specifically need to isolate them.

## 4. Run migrations

```powershell
alembic upgrade head
```

## 5. Run tests

All tests:

```powershell
pytest -v --tb=short
```

HTTP route tests without the DB connectivity test:

```powershell
pytest -v --tb=short tests/test_auth.py tests/test_catalog.py tests/test_delivery.py tests/test_loyalty.py tests/test_orders.py tests/test_payments.py tests/test_promotions.py tests/test_stats.py tests/test_stock.py
```

Only the DB connectivity test:

```powershell
pytest -v --tb=short tests/test_db.py
```

## 6. Run the API and open Swagger

```powershell
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

Swagger UI:

```text
http://127.0.0.1:8000/docs
```

OpenAPI JSON:

```text
http://127.0.0.1:8000/openapi.json
```
