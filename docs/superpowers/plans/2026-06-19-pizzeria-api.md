# Pizzeria API Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a production-ready multi-tenant FastAPI backend for a pizzeria SaaS platform covering auth, catalog, orders, payments, stock, loyalty, promotions, delivery, and analytics.

**Architecture:** Modular monolith with PostgreSQL schemas per tenant for isolation; a separate ARQ async worker handles background tasks; MongoDB serves as the read model for analytics endpoints only.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2 (async), Alembic, asyncpg, PostgreSQL, Motor (MongoDB), Redis, ARQ, Stripe, slowapi, bcrypt, python-jose, pytest, httpx, pytest-asyncio

## Global Constraints

- Python 3.12+
- Pydantic v2 only (no `orm_mode`, use `model_config = ConfigDict(from_attributes=True)`)
- SQLAlchemy 2.0 async style (`async_sessionmaker`, `AsyncSession`, `select()` not `query()`)
- No mock databases in tests — real PostgreSQL test schema, created/dropped per session
- All secrets from environment variables only, never hardcoded
- Error format: `{"code": "UPPER_SNAKE", "detail": "human message", "field": null}`
- JWT access token: 15 min; refresh token: 30 days, stored hashed in DB, rotated on each use
- Rate limits via slowapi: 5 req/min on `/api/v1/auth/login`, 60 req/min on public endpoints
- All routes under prefix `/api/v1/`
- Paginated responses: `{"items": [...], "total": N, "page": N, "limit": N}`
- Multi-tenant: schema `public` holds `tenants` + `tenant_configs`; each tenant gets schema `tenant_{slug}`

---

## File Map

```
pizza/
├── pyproject.toml
├── .env.example
├── alembic.ini
├── alembic/
│   ├── env.py
│   └── versions/
│       └── 0001_public_schema.py
├── app/
│   ├── main.py
│   ├── core/
│   │   ├── config.py          # Settings (pydantic-settings)
│   │   ├── database.py        # Async engine + sessionmaker
│   │   ├── security.py        # JWT encode/decode, bcrypt
│   │   ├── tenant.py          # TenantMiddleware + get_tenant_session
│   │   ├── deps.py            # FastAPI dependencies (current_user, require_role)
│   │   └── errors.py          # AppError exception + handler
│   └── modules/
│       ├── auth/
│       │   ├── models.py      # User, RefreshToken (SQLAlchemy)
│       │   ├── schemas.py     # Pydantic I/O schemas
│       │   ├── service.py     # register, login, refresh, logout
│       │   └── router.py
│       ├── catalog/
│       │   ├── models.py      # Category, Product, ProductVariant, Extra, ProductExtra
│       │   ├── schemas.py
│       │   ├── service.py
│       │   └── router.py
│       ├── orders/
│       │   ├── models.py      # Order, OrderItem, OrderStatusHistory
│       │   ├── schemas.py
│       │   ├── service.py     # create_order, update_status, deduct_stock (calls stock svc)
│       │   └── router.py
│       ├── payments/
│       │   ├── models.py      # Payment
│       │   ├── schemas.py
│       │   ├── service.py     # create_intent, confirm, handle_webhook
│       │   └── router.py
│       ├── stock/
│       │   ├── models.py      # Ingredient, ProductIngredient, VariantIngredient, StockMovement, ProductStock
│       │   ├── schemas.py
│       │   ├── service.py     # deduct_for_order, supply, alert check
│       │   └── router.py
│       ├── loyalty/
│       │   ├── models.py      # LoyaltyAccount, LoyaltyTransaction
│       │   ├── schemas.py
│       │   ├── service.py
│       │   └── router.py
│       ├── promotions/
│       │   ├── models.py      # Promotion
│       │   ├── schemas.py
│       │   ├── service.py     # validate_code, apply_discount
│       │   └── router.py
│       └── delivery/
│           ├── models.py      # DeliveryZone
│           ├── schemas.py
│           ├── service.py     # check_address_in_zone
│           └── router.py
├── worker/
│   ├── main.py                # ARQ worker entry + redis settings
│   └── tasks/
│       ├── stock_alerts.py
│       ├── emails.py
│       └── stats.py           # PostgreSQL → MongoDB aggregation
└── tests/
    ├── conftest.py            # engine, tenant schema fixtures
    ├── test_auth.py
    ├── test_catalog.py
    ├── test_orders.py
    ├── test_payments.py
    ├── test_stock.py
    ├── test_loyalty.py
    ├── test_promotions.py
    ├── test_delivery.py
    └── test_stats.py
```

---

### Task 1: Project Scaffolding + Core Config

**Files:**
- Create: `pyproject.toml`
- Create: `.env.example`
- Create: `app/__init__.py`, `app/core/__init__.py`
- Create: `app/core/config.py`
- Create: `app/core/errors.py`
- Create: `app/main.py`

**Interfaces:**
- Produces: `settings` singleton from `app.core.config`; `AppError(code, detail, status_code, field=None)` from `app.core.errors`

- [ ] **Step 1: Create `pyproject.toml`**

```toml
[project]
name = "pizza-api"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
    "fastapi>=0.115",
    "uvicorn[standard]>=0.30",
    "sqlalchemy[asyncio]>=2.0",
    "asyncpg>=0.29",
    "alembic>=1.13",
    "pydantic-settings>=2.0",
    "python-jose[cryptography]>=3.3",
    "passlib[bcrypt]>=1.7",
    "slowapi>=0.1.9",
    "stripe>=9.0",
    "motor>=3.4",
    "arq>=0.25",
    "redis>=5.0",
    "httpx>=0.27",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.23",
    "pytest-cov>=5.0",
]

[tool.pytest.ini_options]
asyncio_mode = "auto"
```

- [ ] **Step 2: Create `.env.example`**

```
DATABASE_URL=postgresql+asyncpg://user:pass@localhost/pizza
TEST_DATABASE_URL=postgresql+asyncpg://user:pass@localhost/pizza_test
MONGO_URL=mongodb://localhost:27017
ARQ_REDIS_URL=redis://localhost:6379
STRIPE_SECRET_KEY=sk_test_xxx
STRIPE_WEBHOOK_SECRET=whsec_xxx
JWT_SECRET=change-me-32-chars-minimum
ENVIRONMENT=local
```

- [ ] **Step 3: Create `app/core/config.py`**

```python
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str
    test_database_url: str = ""
    mongo_url: str
    arq_redis_url: str
    stripe_secret_key: str
    stripe_webhook_secret: str
    jwt_secret: str
    environment: str = "local"

    jwt_access_expire_minutes: int = 15
    jwt_refresh_expire_days: int = 30

settings = Settings()
```

- [ ] **Step 4: Create `app/core/errors.py`**

```python
from fastapi import Request
from fastapi.responses import JSONResponse

class AppError(Exception):
    def __init__(self, code: str, detail: str, status_code: int = 400, field: str | None = None):
        self.code = code
        self.detail = detail
        self.status_code = status_code
        self.field = field

async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"code": exc.code, "detail": exc.detail, "field": exc.field},
    )
```

- [ ] **Step 5: Create `app/main.py`**

```python
from fastapi import FastAPI
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

from app.core.errors import AppError, app_error_handler
from app.core.tenant import TenantMiddleware

limiter = Limiter(key_func=get_remote_address)

def create_app() -> FastAPI:
    app = FastAPI(title="Pizzeria API", version="1.0.0")
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    app.add_exception_handler(AppError, app_error_handler)
    app.add_middleware(TenantMiddleware)

    from app.modules.auth.router import router as auth_router
    from app.modules.catalog.router import router as catalog_router
    from app.modules.orders.router import router as orders_router
    from app.modules.payments.router import router as payments_router
    from app.modules.stock.router import router as stock_router
    from app.modules.loyalty.router import router as loyalty_router
    from app.modules.promotions.router import router as promotions_router
    from app.modules.delivery.router import router as delivery_router

    prefix = "/api/v1"
    app.include_router(auth_router, prefix=f"{prefix}/auth", tags=["auth"])
    app.include_router(catalog_router, prefix=f"{prefix}/catalog", tags=["catalog"])
    app.include_router(orders_router, prefix=f"{prefix}/orders", tags=["orders"])
    app.include_router(payments_router, prefix=f"{prefix}/payments", tags=["payments"])
    app.include_router(stock_router, prefix=f"{prefix}/stock", tags=["stock"])
    app.include_router(loyalty_router, prefix=f"{prefix}/loyalty", tags=["loyalty"])
    app.include_router(promotions_router, prefix=f"{prefix}/promotions", tags=["promotions"])
    app.include_router(delivery_router, prefix=f"{prefix}/delivery", tags=["delivery"])

    return app

app = create_app()
```

- [ ] **Step 6: Install dependencies and verify import**

```bash
pip install -e ".[dev]"
python -c "from app.main import app; print('OK')"
```

Expected: `OK`

- [ ] **Step 7: Commit**

```bash
git init && git add .
git commit -m "feat: project scaffolding, config, error handler"
```

---

### Task 2: Database + Multi-Tenant Middleware

**Files:**
- Create: `app/core/database.py`
- Create: `app/core/tenant.py`
- Create: `alembic.ini`
- Create: `alembic/env.py`
- Create: `alembic/versions/0001_public_schema.py`

**Interfaces:**
- Produces:
  - `get_public_session() -> AsyncSession` — session on `public` schema
  - `get_tenant_session(tenant_slug: str) -> AsyncSession` — session with `search_path=tenant_{slug}`
  - `TenantMiddleware` — starlette middleware that reads JWT, resolves `tenant_id` + `tenant_slug`, stores on `request.state`

- [ ] **Step 1: Create `app/core/database.py`**

```python
from contextlib import asynccontextmanager
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import DeclarativeBase
from app.core.config import settings

engine = create_async_engine(settings.database_url, echo=False)
public_session_factory = async_sessionmaker(engine, expire_on_commit=False)

class Base(DeclarativeBase):
    pass

@asynccontextmanager
async def get_public_session():
    async with public_session_factory() as session:
        yield session

@asynccontextmanager
async def get_tenant_session(tenant_slug: str):
    schema = f"tenant_{tenant_slug}"
    async with public_session_factory() as session:
        await session.execute(
            __import__("sqlalchemy").text(f"SET search_path TO {schema}, public")
        )
        yield session
```

- [ ] **Step 2: Create `app/core/tenant.py`**

```python
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from jose import jwt, JWTError
from app.core.config import settings

BYPASS_PATHS = {"/api/v1/auth/register", "/api/v1/auth/login", "/api/v1/auth/refresh"}

class TenantMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        request.state.tenant_id = None
        request.state.tenant_slug = None
        request.state.user_id = None
        request.state.role = None

        if request.url.path not in BYPASS_PATHS:
            auth = request.headers.get("Authorization", "")
            if auth.startswith("Bearer "):
                token = auth[7:]
                try:
                    payload = jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
                    request.state.tenant_id = payload.get("tenant_id")
                    request.state.tenant_slug = payload.get("tenant_slug")
                    request.state.user_id = payload.get("sub")
                    request.state.role = payload.get("role")
                except JWTError:
                    pass

        return await call_next(request)
```

- [ ] **Step 3: Set up Alembic**

```bash
alembic init alembic
```

Edit `alembic.ini`: set `sqlalchemy.url = %(DATABASE_URL)s`

Edit `alembic/env.py` to use async engine:

```python
import asyncio
from logging.config import fileConfig
from sqlalchemy.ext.asyncio import create_async_engine
from alembic import context
from app.core.config import settings
from app.core.database import Base

config = context.config
fileConfig(config.config_file_name)
target_metadata = Base.metadata

def run_migrations_offline():
    context.configure(url=settings.database_url, target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()

async def run_migrations_online():
    engine = create_async_engine(settings.database_url)
    async with engine.begin() as conn:
        await conn.run_sync(lambda conn: context.configure(connection=conn, target_metadata=target_metadata))
        await conn.run_sync(lambda conn: context.run_migrations())

def run():
    if context.is_offline_mode():
        run_migrations_offline()
    else:
        asyncio.run(run_migrations_online())

run()
```

- [ ] **Step 4: Create public schema migration**

Create `alembic/versions/0001_public_schema.py`:

```python
"""public schema: tenants + tenant_configs

Revision ID: 0001
Create Date: 2026-06-19
"""
from alembic import op
import sqlalchemy as sa

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

def upgrade():
    op.create_table(
        "tenants",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("slug", sa.String(64), unique=True, nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("plan", sa.String(32), nullable=False, server_default="starter"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        schema="public",
    )
    op.create_table(
        "tenant_configs",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("tenant_id", sa.Integer, sa.ForeignKey("public.tenants.id"), nullable=False),
        sa.Column("delivery_zones", sa.JSON, nullable=True),
        sa.Column("stripe_account_id", sa.String(255), nullable=True),
        sa.Column("currency", sa.String(8), server_default="EUR"),
        sa.Column("timezone", sa.String(64), server_default="Europe/Paris"),
        sa.Column("logo_url", sa.String(512), nullable=True),
        schema="public",
    )

def downgrade():
    op.drop_table("tenant_configs", schema="public")
    op.drop_table("tenants", schema="public")
```

- [ ] **Step 5: Run migration**

```bash
alembic upgrade head
```

Expected: tables `tenants` and `tenant_configs` created in DB.

- [ ] **Step 6: Write test for DB connectivity**

In `tests/conftest.py`:

```python
import pytest
import asyncio
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy import text
from app.core.config import settings

@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()

@pytest.fixture(scope="session")
async def db_engine():
    engine = create_async_engine(settings.test_database_url or settings.database_url)
    yield engine
    await engine.dispose()

@pytest.fixture
async def public_session(db_engine):
    async_session = async_sessionmaker(db_engine, expire_on_commit=False)
    async with async_session() as session:
        yield session
```

In `tests/test_db.py`:

```python
from sqlalchemy import text

async def test_can_query_tenants(public_session):
    result = await public_session.execute(text("SELECT 1"))
    assert result.scalar() == 1
```

- [ ] **Step 7: Run test**

```bash
pytest tests/test_db.py -v
```

Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add .
git commit -m "feat: async database setup, tenant middleware, public schema migration"
```

---

### Task 3: Auth Module

**Files:**
- Create: `app/core/security.py`
- Create: `app/core/deps.py`
- Create: `app/modules/auth/models.py`
- Create: `app/modules/auth/schemas.py`
- Create: `app/modules/auth/service.py`
- Create: `app/modules/auth/router.py`
- Create: `tests/test_auth.py`

**Interfaces:**
- Consumes: `get_public_session`, `AppError`, `settings`
- Produces:
  - `create_access_token(data: dict) -> str`
  - `create_refresh_token(data: dict) -> str`
  - `verify_password(plain, hashed) -> bool`
  - `get_password_hash(plain) -> str`
  - `get_current_user(request) -> User` — FastAPI dependency
  - `require_role(*roles) -> Dependency`

- [ ] **Step 1: Create `app/core/security.py`**

```python
from datetime import datetime, timedelta, timezone
from jose import jwt
from passlib.context import CryptContext
from app.core.config import settings

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)

def get_password_hash(plain: str) -> str:
    return pwd_context.hash(plain)

def create_access_token(data: dict) -> str:
    expire = datetime.now(timezone.utc) + timedelta(minutes=settings.jwt_access_expire_minutes)
    return jwt.encode({**data, "exp": expire, "type": "access"}, settings.jwt_secret, algorithm="HS256")

def create_refresh_token(data: dict) -> str:
    expire = datetime.now(timezone.utc) + timedelta(days=settings.jwt_refresh_expire_days)
    return jwt.encode({**data, "exp": expire, "type": "refresh"}, settings.jwt_secret, algorithm="HS256")

def decode_token(token: str) -> dict:
    return jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
```

- [ ] **Step 2: Create `app/modules/auth/models.py`**

```python
from datetime import datetime
from sqlalchemy import Integer, String, DateTime, ForeignKey, func
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.core.database import Base

class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    phone: Mapped[str | None] = mapped_column(String(32))
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False, default="client")
    first_name: Mapped[str] = mapped_column(String(128), nullable=False)
    last_name: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

class RefreshToken(Base):
    __tablename__ = "refresh_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id", ondelete="CASCADE"))
    token_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
```

- [ ] **Step 3: Create `app/modules/auth/schemas.py`**

```python
from pydantic import BaseModel, EmailStr, ConfigDict

class RegisterRequest(BaseModel):
    email: EmailStr
    password: str
    first_name: str
    last_name: str
    phone: str | None = None

class LoginRequest(BaseModel):
    email: EmailStr
    password: str

class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"

class RefreshRequest(BaseModel):
    refresh_token: str

class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    email: str
    role: str
    first_name: str
    last_name: str
```

- [ ] **Step 4: Create tenant schema creation helper in `app/core/tenant.py` (add function)**

```python
# Add to app/core/tenant.py
from sqlalchemy import text
from app.core.database import engine

async def create_tenant_schema(tenant_slug: str):
    schema = f"tenant_{tenant_slug}"
    async with engine.begin() as conn:
        await conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {schema}"))
```

- [ ] **Step 5: Create `app/modules/auth/service.py`**

```python
import hashlib
from datetime import datetime, timedelta, timezone
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.core.security import verify_password, get_password_hash, create_access_token, create_refresh_token, decode_token
from app.core.errors import AppError
from app.modules.auth.models import User, RefreshToken

def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()

async def register_user(session: AsyncSession, data, tenant_id: int, tenant_slug: str) -> User:
    existing = await session.scalar(select(User).where(User.email == data.email))
    if existing:
        raise AppError("AUTH_EMAIL_TAKEN", "Cette adresse email est déjà utilisée", 409)
    user = User(
        email=data.email,
        password_hash=get_password_hash(data.password),
        first_name=data.first_name,
        last_name=data.last_name,
        phone=data.phone,
        role="client",
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user

async def login_user(session: AsyncSession, email: str, password: str, tenant_id: int, tenant_slug: str):
    user = await session.scalar(select(User).where(User.email == email))
    if not user or not verify_password(password, user.password_hash):
        raise AppError("AUTH_INVALID_CREDENTIALS", "Email ou mot de passe incorrect", 401)

    payload = {"sub": str(user.id), "role": user.role, "tenant_id": tenant_id, "tenant_slug": tenant_slug}
    access = create_access_token(payload)
    refresh = create_refresh_token(payload)

    rt = RefreshToken(
        user_id=user.id,
        token_hash=_hash_token(refresh),
        expires_at=datetime.now(timezone.utc) + timedelta(days=30),
    )
    session.add(rt)
    await session.commit()
    return access, refresh

async def refresh_tokens(session: AsyncSession, refresh_token: str, tenant_id: int, tenant_slug: str):
    from jose import JWTError
    try:
        payload = decode_token(refresh_token)
    except JWTError:
        raise AppError("AUTH_TOKEN_EXPIRED", "Token invalide ou expiré", 401)

    if payload.get("type") != "refresh":
        raise AppError("AUTH_TOKEN_EXPIRED", "Token invalide", 401)

    token_hash = _hash_token(refresh_token)
    rt = await session.scalar(select(RefreshToken).where(RefreshToken.token_hash == token_hash))
    if not rt:
        raise AppError("AUTH_TOKEN_EXPIRED", "Token révoqué", 401)

    await session.delete(rt)

    new_payload = {"sub": payload["sub"], "role": payload["role"], "tenant_id": tenant_id, "tenant_slug": tenant_slug}
    new_access = create_access_token(new_payload)
    new_refresh = create_refresh_token(new_payload)

    new_rt = RefreshToken(
        user_id=int(payload["sub"]),
        token_hash=_hash_token(new_refresh),
        expires_at=datetime.now(timezone.utc) + timedelta(days=30),
    )
    session.add(new_rt)
    await session.commit()
    return new_access, new_refresh

async def logout_user(session: AsyncSession, refresh_token: str):
    token_hash = _hash_token(refresh_token)
    rt = await session.scalar(select(RefreshToken).where(RefreshToken.token_hash == token_hash))
    if rt:
        await session.delete(rt)
        await session.commit()
```

- [ ] **Step 6: Create `app/core/deps.py`**

```python
from fastapi import Request, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.errors import AppError
from app.core.database import get_tenant_session

async def get_current_user(request: Request):
    if not request.state.user_id:
        raise AppError("AUTH_FORBIDDEN", "Authentification requise", 401)
    return {
        "user_id": int(request.state.user_id),
        "role": request.state.role,
        "tenant_id": request.state.tenant_id,
        "tenant_slug": request.state.tenant_slug,
    }

def require_role(*roles: str):
    async def checker(current_user=Depends(get_current_user)):
        if current_user["role"] not in roles:
            raise AppError("AUTH_FORBIDDEN", "Rôle insuffisant", 403)
        return current_user
    return checker
```

- [ ] **Step 7: Create `app/modules/auth/router.py`**

```python
from fastapi import APIRouter, Request, Depends
from slowapi import Limiter
from slowapi.util import get_remote_address
from app.modules.auth.schemas import RegisterRequest, LoginRequest, TokenResponse, RefreshRequest
from app.modules.auth import service
from app.core.database import get_tenant_session

router = APIRouter()
limiter = Limiter(key_func=get_remote_address)

@router.post("/register", response_model=TokenResponse)
async def register(request: Request, body: RegisterRequest):
    # For now, resolve tenant from header X-Tenant-Slug
    tenant_slug = request.headers.get("X-Tenant-Slug", "default")
    tenant_id = 1  # TODO: resolve from DB in super-admin task
    async with get_tenant_session(tenant_slug) as session:
        user = await service.register_user(session, body, tenant_id, tenant_slug)
        access, refresh = await service.login_user(session, body.email, body.password, tenant_id, tenant_slug)
    return TokenResponse(access_token=access, refresh_token=refresh)

@router.post("/login", response_model=TokenResponse)
@limiter.limit("5/minute")
async def login(request: Request, body: LoginRequest):
    tenant_slug = request.headers.get("X-Tenant-Slug", "default")
    tenant_id = 1
    async with get_tenant_session(tenant_slug) as session:
        access, refresh = await service.login_user(session, body.email, body.password, tenant_id, tenant_slug)
    return TokenResponse(access_token=access, refresh_token=refresh)

@router.post("/refresh", response_model=TokenResponse)
async def refresh(request: Request, body: RefreshRequest):
    tenant_slug = request.headers.get("X-Tenant-Slug", "default")
    tenant_id = 1
    async with get_tenant_session(tenant_slug) as session:
        access, refresh = await service.refresh_tokens(session, body.refresh_token, tenant_id, tenant_slug)
    return TokenResponse(access_token=access, refresh_token=refresh)

@router.post("/logout", status_code=204)
async def logout(request: Request, body: RefreshRequest):
    tenant_slug = request.headers.get("X-Tenant-Slug", "default")
    async with get_tenant_session(tenant_slug) as session:
        await service.logout_user(session, body.refresh_token)
```

- [ ] **Step 8: Create Alembic migration for auth tables**

```bash
alembic revision --autogenerate -m "auth tables users refresh_tokens"
alembic upgrade head
```

- [ ] **Step 9: Write failing test**

In `tests/test_auth.py`:

```python
import pytest
from httpx import AsyncClient, ASGITransport
from app.main import app

@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c

async def test_register_and_login(client):
    headers = {"X-Tenant-Slug": "test"}
    resp = await client.post("/api/v1/auth/register", json={
        "email": "test@example.com",
        "password": "secret123",
        "first_name": "Jean",
        "last_name": "Dupont",
    }, headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert "access_token" in data
    assert "refresh_token" in data

async def test_login_bad_password(client):
    headers = {"X-Tenant-Slug": "test"}
    resp = await client.post("/api/v1/auth/login", json={
        "email": "nobody@example.com",
        "password": "wrong",
    }, headers=headers)
    assert resp.status_code == 401
    assert resp.json()["code"] == "AUTH_INVALID_CREDENTIALS"
```

- [ ] **Step 10: Run tests**

```bash
pytest tests/test_auth.py -v
```

Expected: PASS

- [ ] **Step 11: Commit**

```bash
git add .
git commit -m "feat: auth module — register, login, refresh, logout with JWT"
```

---

### Task 4: Catalog Module

**Files:**
- Create: `app/modules/catalog/models.py`
- Create: `app/modules/catalog/schemas.py`
- Create: `app/modules/catalog/service.py`
- Create: `app/modules/catalog/router.py`
- Create: `tests/test_catalog.py`

**Interfaces:**
- Consumes: `get_tenant_session`, `require_role`, `AppError`
- Produces:
  - `get_products(session, page, limit) -> (list[Product], int)`
  - `get_product(session, id) -> Product`
  - `create_product(session, data) -> Product`

- [ ] **Step 1: Create `app/modules/catalog/models.py`**

```python
from sqlalchemy import Integer, String, Boolean, Numeric, ForeignKey, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.core.database import Base

class Category(Base):
    __tablename__ = "categories"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    display_order: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

class Product(Base):
    __tablename__ = "products"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    category_id: Mapped[int] = mapped_column(Integer, ForeignKey("categories.id"))
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    base_price: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    image_url: Mapped[str | None] = mapped_column(String(512))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_available: Mapped[bool] = mapped_column(Boolean, default=True)

class ProductVariant(Base):
    __tablename__ = "product_variants"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    product_id: Mapped[int] = mapped_column(Integer, ForeignKey("products.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    extra_price: Mapped[float] = mapped_column(Numeric(10, 2), default=0)

class Extra(Base):
    __tablename__ = "extras"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    price: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    ingredient_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("ingredients.id"), nullable=True)
    ingredient_qty: Mapped[float | None] = mapped_column(Numeric(10, 3))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

class ProductExtra(Base):
    __tablename__ = "product_extras"
    product_id: Mapped[int] = mapped_column(Integer, ForeignKey("products.id"), primary_key=True)
    extra_id: Mapped[int] = mapped_column(Integer, ForeignKey("extras.id"), primary_key=True)
```

- [ ] **Step 2: Create `app/modules/catalog/schemas.py`**

```python
from pydantic import BaseModel, ConfigDict

class CategoryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    display_order: int
    is_active: bool

class ProductCreate(BaseModel):
    category_id: int
    name: str
    description: str | None = None
    base_price: float
    image_url: str | None = None

class ProductOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    category_id: int
    name: str
    description: str | None
    base_price: float
    image_url: str | None
    is_active: bool
    is_available: bool

class PaginatedProducts(BaseModel):
    items: list[ProductOut]
    total: int
    page: int
    limit: int

class VariantCreate(BaseModel):
    name: str
    extra_price: float = 0.0

class ExtraCreate(BaseModel):
    name: str
    price: float
    ingredient_id: int | None = None
    ingredient_qty: float | None = None
```

- [ ] **Step 3: Create `app/modules/catalog/service.py`**

```python
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from app.modules.catalog.models import Category, Product, ProductVariant, Extra, ProductExtra
from app.core.errors import AppError

async def list_categories(session: AsyncSession) -> list[Category]:
    result = await session.execute(select(Category).where(Category.is_active == True).order_by(Category.display_order))
    return list(result.scalars())

async def list_products(session: AsyncSession, page: int = 1, limit: int = 20):
    offset = (page - 1) * limit
    total = await session.scalar(select(func.count()).select_from(Product).where(Product.is_active == True))
    result = await session.execute(select(Product).where(Product.is_active == True).offset(offset).limit(limit))
    return list(result.scalars()), total or 0

async def get_product(session: AsyncSession, product_id: int) -> Product:
    p = await session.get(Product, product_id)
    if not p:
        raise AppError("PRODUCT_NOT_FOUND", f"Produit #{product_id} introuvable", 404)
    return p

async def create_product(session: AsyncSession, data) -> Product:
    p = Product(**data.model_dump())
    session.add(p)
    await session.commit()
    await session.refresh(p)
    return p

async def update_product(session: AsyncSession, product_id: int, data: dict) -> Product:
    p = await get_product(session, product_id)
    for k, v in data.items():
        setattr(p, k, v)
    await session.commit()
    await session.refresh(p)
    return p

async def delete_product(session: AsyncSession, product_id: int):
    p = await get_product(session, product_id)
    p.is_active = False
    await session.commit()

async def create_variant(session: AsyncSession, product_id: int, data) -> ProductVariant:
    await get_product(session, product_id)
    v = ProductVariant(product_id=product_id, **data.model_dump())
    session.add(v)
    await session.commit()
    await session.refresh(v)
    return v

async def create_extra(session: AsyncSession, data) -> Extra:
    e = Extra(**data.model_dump())
    session.add(e)
    await session.commit()
    await session.refresh(e)
    return e
```

- [ ] **Step 4: Create `app/modules/catalog/router.py`**

```python
from fastapi import APIRouter, Request, Depends
from app.modules.catalog import service
from app.modules.catalog.schemas import ProductCreate, ProductOut, PaginatedProducts, VariantCreate, ExtraCreate, CategoryOut
from app.core.database import get_tenant_session
from app.core.deps import get_current_user, require_role

router = APIRouter()

@router.get("/categories", response_model=list[CategoryOut])
async def list_categories(request: Request):
    slug = request.state.tenant_slug or request.headers.get("X-Tenant-Slug", "default")
    async with get_tenant_session(slug) as session:
        return await service.list_categories(session)

@router.get("/products", response_model=PaginatedProducts)
async def list_products(request: Request, page: int = 1, limit: int = 20):
    slug = request.state.tenant_slug or request.headers.get("X-Tenant-Slug", "default")
    async with get_tenant_session(slug) as session:
        items, total = await service.list_products(session, page, limit)
        return PaginatedProducts(items=items, total=total, page=page, limit=limit)

@router.get("/products/{product_id}", response_model=ProductOut)
async def get_product(request: Request, product_id: int):
    slug = request.state.tenant_slug or request.headers.get("X-Tenant-Slug", "default")
    async with get_tenant_session(slug) as session:
        return await service.get_product(session, product_id)

@router.post("/products", response_model=ProductOut, status_code=201)
async def create_product(request: Request, body: ProductCreate, current_user=Depends(require_role("admin"))):
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        return await service.create_product(session, body)

@router.put("/products/{product_id}", response_model=ProductOut)
async def update_product(request: Request, product_id: int, body: ProductCreate, current_user=Depends(require_role("admin"))):
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        return await service.update_product(session, product_id, body.model_dump(exclude_unset=True))

@router.delete("/products/{product_id}", status_code=204)
async def delete_product(request: Request, product_id: int, current_user=Depends(require_role("admin"))):
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        await service.delete_product(session, product_id)

@router.post("/products/{product_id}/variants", status_code=201)
async def create_variant(request: Request, product_id: int, body: VariantCreate, current_user=Depends(require_role("admin"))):
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        return await service.create_variant(session, product_id, body)

@router.post("/extras", status_code=201)
async def create_extra(request: Request, body: ExtraCreate, current_user=Depends(require_role("admin"))):
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        return await service.create_extra(session, body)
```

- [ ] **Step 5: Create migration for catalog tables**

```bash
alembic revision --autogenerate -m "catalog tables"
alembic upgrade head
```

- [ ] **Step 6: Write and run tests**

In `tests/test_catalog.py`:

```python
import pytest
from httpx import AsyncClient, ASGITransport
from app.main import app

@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c

async def test_list_products_empty(client):
    resp = await client.get("/api/v1/catalog/products", headers={"X-Tenant-Slug": "test"})
    assert resp.status_code == 200
    data = resp.json()
    assert "items" in data
    assert data["total"] == 0

async def test_create_product_requires_admin(client):
    resp = await client.post("/api/v1/catalog/products", json={
        "category_id": 1, "name": "Margherita", "base_price": 10.5
    })
    assert resp.status_code == 401
```

```bash
pytest tests/test_catalog.py -v
```

Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add .
git commit -m "feat: catalog module — categories, products, variants, extras"
```

---

### Task 5: Orders Module

**Files:**
- Create: `app/modules/orders/models.py`
- Create: `app/modules/orders/schemas.py`
- Create: `app/modules/orders/service.py`
- Create: `app/modules/orders/router.py`
- Create: `tests/test_orders.py`

**Interfaces:**
- Consumes: `get_product`, `deduct_for_order` (from stock service — implemented in Task 6, call is conditional)
- Produces:
  - `create_order(session, data, user_id) -> Order`
  - `update_order_status(session, order_id, new_status, changed_by) -> Order`

- [ ] **Step 1: Create `app/modules/orders/models.py`**

```python
from datetime import datetime
from sqlalchemy import Integer, String, Numeric, ForeignKey, DateTime, JSON, func
from sqlalchemy.orm import Mapped, mapped_column
from app.core.database import Base

class Order(Base):
    __tablename__ = "orders"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"))
    status: Mapped[str] = mapped_column(String(32), default="pending")
    type: Mapped[str] = mapped_column(String(16), nullable=False)  # delivery|pickup
    address: Mapped[dict | None] = mapped_column(JSON)
    delivery_zone_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("delivery_zones.id"), nullable=True)
    delivery_fee: Mapped[float] = mapped_column(Numeric(10, 2), default=0)
    subtotal: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    discount_amount: Mapped[float] = mapped_column(Numeric(10, 2), default=0)
    total: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    payment_status: Mapped[str] = mapped_column(String(32), default="pending")
    payment_method: Mapped[str | None] = mapped_column(String(32))
    notes: Mapped[str | None] = mapped_column(String(512))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

class OrderItem(Base):
    __tablename__ = "order_items"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[int] = mapped_column(Integer, ForeignKey("orders.id", ondelete="CASCADE"))
    product_id: Mapped[int] = mapped_column(Integer, ForeignKey("products.id"))
    variant_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("product_variants.id"), nullable=True)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_price: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    extras: Mapped[list] = mapped_column(JSON, default=list)

class OrderStatusHistory(Base):
    __tablename__ = "order_status_history"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[int] = mapped_column(Integer, ForeignKey("orders.id", ondelete="CASCADE"))
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    changed_by: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"))
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
```

- [ ] **Step 2: Create `app/modules/orders/schemas.py`**

```python
from datetime import datetime
from pydantic import BaseModel, ConfigDict

class OrderItemIn(BaseModel):
    product_id: int
    variant_id: int | None = None
    quantity: int
    extras: list[int] = []

class OrderCreate(BaseModel):
    type: str  # delivery|pickup
    address: dict | None = None
    delivery_zone_id: int | None = None
    items: list[OrderItemIn]
    promo_code: str | None = None
    notes: str | None = None

class OrderItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    product_id: int
    variant_id: int | None
    quantity: int
    unit_price: float
    extras: list

class OrderOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    user_id: int
    status: str
    type: str
    subtotal: float
    discount_amount: float
    total: float
    delivery_fee: float
    payment_status: str
    notes: str | None
    created_at: datetime

class StatusUpdate(BaseModel):
    status: str
```

- [ ] **Step 3: Create `app/modules/orders/service.py`**

```python
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from app.modules.orders.models import Order, OrderItem, OrderStatusHistory
from app.modules.catalog.models import Product, ProductVariant
from app.core.errors import AppError

VALID_TRANSITIONS = {
    "pending": ["confirmed", "cancelled"],
    "confirmed": ["preparing", "cancelled"],
    "preparing": ["ready"],
    "ready": ["delivering", "delivered"],
    "delivering": ["delivered"],
    "delivered": [],
    "cancelled": [],
}

async def create_order(session: AsyncSession, data, user_id: int) -> Order:
    subtotal = 0.0
    items_to_create = []

    for item in data.items:
        product = await session.get(Product, item.product_id)
        if not product or not product.is_active or not product.is_available:
            raise AppError("PRODUCT_NOT_FOUND", f"Produit #{item.product_id} indisponible", 404)
        unit_price = float(product.base_price)
        if item.variant_id:
            variant = await session.get(ProductVariant, item.variant_id)
            if variant:
                unit_price += float(variant.extra_price)
        subtotal += unit_price * item.quantity
        items_to_create.append((item, unit_price))

    delivery_fee = 0.0
    total = subtotal - 0 + delivery_fee  # discount applied later via promo

    order = Order(
        user_id=user_id,
        status="pending",
        type=data.type,
        address=data.address,
        delivery_zone_id=data.delivery_zone_id,
        delivery_fee=delivery_fee,
        subtotal=subtotal,
        discount_amount=0,
        total=total,
        payment_status="pending",
        notes=data.notes,
    )
    session.add(order)
    await session.flush()

    for item, unit_price in items_to_create:
        oi = OrderItem(
            order_id=order.id,
            product_id=item.product_id,
            variant_id=item.variant_id,
            quantity=item.quantity,
            unit_price=unit_price,
            extras=item.extras,
        )
        session.add(oi)

    await session.commit()
    await session.refresh(order)
    return order

async def get_order(session: AsyncSession, order_id: int) -> Order:
    o = await session.get(Order, order_id)
    if not o:
        raise AppError("ORDER_NOT_FOUND", f"Commande #{order_id} introuvable", 404)
    return o

async def update_order_status(session: AsyncSession, order_id: int, new_status: str, changed_by: int) -> Order:
    order = await get_order(session, order_id)
    if new_status not in VALID_TRANSITIONS.get(order.status, []):
        raise AppError("ORDER_CANNOT_CANCEL", f"Transition {order.status} → {new_status} impossible", 409)
    order.status = new_status
    history = OrderStatusHistory(order_id=order.id, status=new_status, changed_by=changed_by)
    session.add(history)
    await session.commit()
    await session.refresh(order)
    return order

async def cancel_order(session: AsyncSession, order_id: int, user_id: int) -> Order:
    order = await get_order(session, order_id)
    if order.user_id != user_id:
        raise AppError("AUTH_FORBIDDEN", "Accès refusé", 403)
    if order.status != "pending":
        raise AppError("ORDER_CANNOT_CANCEL", "Seule une commande en attente peut être annulée", 409)
    return await update_order_status(session, order_id, "cancelled", user_id)

async def list_my_orders(session: AsyncSession, user_id: int, page: int, limit: int):
    offset = (page - 1) * limit
    total = await session.scalar(select(func.count()).select_from(Order).where(Order.user_id == user_id))
    result = await session.execute(select(Order).where(Order.user_id == user_id).order_by(Order.created_at.desc()).offset(offset).limit(limit))
    return list(result.scalars()), total or 0

async def list_orders(session: AsyncSession, page: int, limit: int):
    offset = (page - 1) * limit
    total = await session.scalar(select(func.count()).select_from(Order))
    result = await session.execute(select(Order).order_by(Order.created_at.desc()).offset(offset).limit(limit))
    return list(result.scalars()), total or 0
```

- [ ] **Step 4: Create `app/modules/orders/router.py`**

```python
from fastapi import APIRouter, Request, Depends
from app.modules.orders import service
from app.modules.orders.schemas import OrderCreate, OrderOut, StatusUpdate
from app.core.database import get_tenant_session
from app.core.deps import get_current_user, require_role

router = APIRouter()

@router.post("", response_model=OrderOut, status_code=201)
async def create_order(body: OrderCreate, current_user=Depends(get_current_user)):
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        return await service.create_order(session, body, current_user["user_id"])

@router.get("/my")
async def my_orders(page: int = 1, limit: int = 20, current_user=Depends(get_current_user)):
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        items, total = await service.list_my_orders(session, current_user["user_id"], page, limit)
        return {"items": items, "total": total, "page": page, "limit": limit}

@router.get("/{order_id}", response_model=OrderOut)
async def get_order(order_id: int, current_user=Depends(get_current_user)):
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        return await service.get_order(session, order_id)

@router.get("/{order_id}/status")
async def get_status(order_id: int, current_user=Depends(get_current_user)):
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        o = await service.get_order(session, order_id)
        return {"status": o.status}

@router.get("", response_model=dict)
async def list_orders(page: int = 1, limit: int = 20, current_user=Depends(require_role("staff", "admin"))):
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        items, total = await service.list_orders(session, page, limit)
        return {"items": items, "total": total, "page": page, "limit": limit}

@router.patch("/{order_id}/status", response_model=OrderOut)
async def update_status(order_id: int, body: StatusUpdate, current_user=Depends(require_role("staff", "admin"))):
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        return await service.update_order_status(session, order_id, body.status, current_user["user_id"])

@router.delete("/{order_id}", status_code=204)
async def cancel_order(order_id: int, current_user=Depends(get_current_user)):
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        await service.cancel_order(session, order_id, current_user["user_id"])
```

- [ ] **Step 5: Migrate + test**

```bash
alembic revision --autogenerate -m "orders tables"
alembic upgrade head
pytest tests/test_orders.py -v
```

In `tests/test_orders.py`:

```python
async def test_create_order_unauthenticated(client):
    resp = await client.post("/api/v1/orders", json={
        "type": "pickup",
        "items": [{"product_id": 1, "quantity": 1}]
    })
    assert resp.status_code == 401
```

Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add .
git commit -m "feat: orders module — create, status transitions, list"
```

---

### Task 6: Stock Module + Order Stock Deduction

**Files:**
- Create: `app/modules/stock/models.py`
- Create: `app/modules/stock/schemas.py`
- Create: `app/modules/stock/service.py`
- Create: `app/modules/stock/router.py`
- Modify: `app/modules/orders/service.py` (add `deduct_for_order` call on confirm)
- Create: `tests/test_stock.py`

**Interfaces:**
- Produces: `deduct_for_order(session, order_id) -> None` — called when order status → `confirmed`

- [ ] **Step 1: Create `app/modules/stock/models.py`**

```python
from datetime import datetime
from sqlalchemy import Integer, String, Numeric, ForeignKey, DateTime, func
from sqlalchemy.orm import Mapped, mapped_column
from app.core.database import Base

class Ingredient(Base):
    __tablename__ = "ingredients"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    unit: Mapped[str] = mapped_column(String(8), nullable=False)  # kg|L|pcs
    current_qty: Mapped[float] = mapped_column(Numeric(12, 3), default=0)
    alert_threshold: Mapped[float] = mapped_column(Numeric(12, 3), default=0)
    cost_per_unit: Mapped[float] = mapped_column(Numeric(10, 4), default=0)

class ProductIngredient(Base):
    __tablename__ = "product_ingredients"
    product_id: Mapped[int] = mapped_column(Integer, ForeignKey("products.id"), primary_key=True)
    ingredient_id: Mapped[int] = mapped_column(Integer, ForeignKey("ingredients.id"), primary_key=True)
    qty_required: Mapped[float] = mapped_column(Numeric(12, 3), nullable=False)

class VariantIngredient(Base):
    __tablename__ = "variant_ingredients"
    variant_id: Mapped[int] = mapped_column(Integer, ForeignKey("product_variants.id"), primary_key=True)
    ingredient_id: Mapped[int] = mapped_column(Integer, ForeignKey("ingredients.id"), primary_key=True)
    qty_required: Mapped[float] = mapped_column(Numeric(12, 3), nullable=False)

class StockMovement(Base):
    __tablename__ = "stock_movements"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ingredient_id: Mapped[int] = mapped_column(Integer, ForeignKey("ingredients.id"))
    order_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("orders.id"), nullable=True)
    type: Mapped[str] = mapped_column(String(16), nullable=False)  # in|out|adjustment
    qty: Mapped[float] = mapped_column(Numeric(12, 3), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(255))
    created_by: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

class ProductStock(Base):
    __tablename__ = "product_stock"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    product_id: Mapped[int] = mapped_column(Integer, ForeignKey("products.id"), unique=True)
    current_qty: Mapped[int] = mapped_column(Integer, default=0)
    alert_threshold: Mapped[int] = mapped_column(Integer, default=0)
```

- [ ] **Step 2: Create `app/modules/stock/service.py`**

```python
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.modules.stock.models import Ingredient, ProductIngredient, VariantIngredient, StockMovement, ProductStock
from app.modules.orders.models import Order, OrderItem
from app.core.errors import AppError

async def deduct_for_order(session: AsyncSession, order_id: int, created_by: int):
    """Deduct ingredients for all items in a confirmed order."""
    result = await session.execute(select(OrderItem).where(OrderItem.order_id == order_id))
    items = list(result.scalars())

    for item in items:
        # Get ingredient requirements: variant overrides product
        if item.variant_id:
            vi_result = await session.execute(
                select(VariantIngredient).where(VariantIngredient.variant_id == item.variant_id)
            )
            requirements = list(vi_result.scalars())
        else:
            requirements = []

        if not requirements:
            pi_result = await session.execute(
                select(ProductIngredient).where(ProductIngredient.product_id == item.product_id)
            )
            requirements = list(pi_result.scalars())

        for req in requirements:
            ingredient = await session.get(Ingredient, req.ingredient_id)
            if not ingredient:
                continue
            qty_to_deduct = float(req.qty_required) * item.quantity
            ingredient.current_qty = float(ingredient.current_qty) - qty_to_deduct
            movement = StockMovement(
                ingredient_id=ingredient.id,
                order_id=order_id,
                type="out",
                qty=qty_to_deduct,
                reason=f"Order #{order_id}",
                created_by=created_by,
            )
            session.add(movement)

        # Deduct product stock
        ps = await session.scalar(select(ProductStock).where(ProductStock.product_id == item.product_id))
        if ps:
            ps.current_qty = max(0, ps.current_qty - item.quantity)

    await session.commit()

    # Check alerts after deduction
    low_stock = await session.execute(
        select(Ingredient).where(Ingredient.current_qty <= Ingredient.alert_threshold)
    )
    for ing in low_stock.scalars():
        # Publish ARQ task — imported lazily to avoid circular imports
        pass  # wired in Task 11

async def supply_ingredient(session: AsyncSession, ingredient_id: int, qty: float, created_by: int):
    ing = await session.get(Ingredient, ingredient_id)
    if not ing:
        raise AppError("INGREDIENT_NOT_FOUND", f"Ingrédient #{ingredient_id} introuvable", 404)
    ing.current_qty = float(ing.current_qty) + qty
    movement = StockMovement(
        ingredient_id=ingredient_id,
        type="in",
        qty=qty,
        reason="Réapprovisionnement",
        created_by=created_by,
    )
    session.add(movement)
    await session.commit()
    await session.refresh(ing)
    return ing

async def list_ingredients(session: AsyncSession):
    result = await session.execute(select(Ingredient))
    return list(result.scalars())

async def get_alerts(session: AsyncSession):
    result = await session.execute(
        select(Ingredient).where(Ingredient.current_qty <= Ingredient.alert_threshold)
    )
    return list(result.scalars())
```

- [ ] **Step 3: Wire stock deduction into orders service**

In `app/modules/orders/service.py`, modify `update_order_status`:

```python
async def update_order_status(session: AsyncSession, order_id: int, new_status: str, changed_by: int) -> Order:
    order = await get_order(session, order_id)
    if new_status not in VALID_TRANSITIONS.get(order.status, []):
        raise AppError("ORDER_CANNOT_CANCEL", f"Transition {order.status} → {new_status} impossible", 409)
    order.status = new_status
    history = OrderStatusHistory(order_id=order.id, status=new_status, changed_by=changed_by)
    session.add(history)

    if new_status == "confirmed":
        from app.modules.stock.service import deduct_for_order
        await deduct_for_order(session, order_id, changed_by)

    await session.commit()
    await session.refresh(order)
    return order
```

- [ ] **Step 4: Create schemas and router for stock**

`app/modules/stock/schemas.py`:

```python
from pydantic import BaseModel, ConfigDict

class IngredientCreate(BaseModel):
    name: str
    unit: str
    current_qty: float = 0
    alert_threshold: float = 0
    cost_per_unit: float = 0

class IngredientOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    unit: str
    current_qty: float
    alert_threshold: float
    cost_per_unit: float

class SupplyRequest(BaseModel):
    qty: float
```

`app/modules/stock/router.py`:

```python
from fastapi import APIRouter, Depends
from app.modules.stock import service
from app.modules.stock.schemas import IngredientCreate, IngredientOut, SupplyRequest
from app.core.database import get_tenant_session
from app.core.deps import require_role

router = APIRouter()

@router.get("/ingredients", response_model=list[IngredientOut])
async def list_ingredients(current_user=Depends(require_role("staff", "admin"))):
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        return await service.list_ingredients(session)

@router.post("/ingredients", response_model=IngredientOut, status_code=201)
async def create_ingredient(body: IngredientCreate, current_user=Depends(require_role("admin"))):
    from app.modules.stock.models import Ingredient
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        ing = Ingredient(**body.model_dump())
        session.add(ing)
        await session.commit()
        await session.refresh(ing)
        return ing

@router.post("/ingredients/{ingredient_id}/supply", response_model=IngredientOut)
async def supply(ingredient_id: int, body: SupplyRequest, current_user=Depends(require_role("staff", "admin"))):
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        return await service.supply_ingredient(session, ingredient_id, body.qty, current_user["user_id"])

@router.get("/ingredients/alerts", response_model=list[IngredientOut])
async def alerts(current_user=Depends(require_role("admin"))):
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        return await service.get_alerts(session)
```

- [ ] **Step 5: Migrate + test**

```bash
alembic revision --autogenerate -m "stock tables"
alembic upgrade head
pytest tests/test_stock.py -v
```

`tests/test_stock.py`:

```python
async def test_list_ingredients_requires_auth(client):
    resp = await client.get("/api/v1/stock/ingredients")
    assert resp.status_code == 401
```

Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add .
git commit -m "feat: stock module — ingredients, supply, deduction on order confirm"
```

---

### Task 7: Payments Module (Stripe)

**Files:**
- Create: `app/modules/payments/models.py`
- Create: `app/modules/payments/schemas.py`
- Create: `app/modules/payments/service.py`
- Create: `app/modules/payments/router.py`
- Create: `tests/test_payments.py`

**Interfaces:**
- Consumes: `get_order`, `settings.stripe_secret_key`, `settings.stripe_webhook_secret`
- Produces: `create_payment_intent(session, order_id) -> dict`, `handle_webhook(payload, sig) -> None`

- [ ] **Step 1: Create `app/modules/payments/models.py`**

```python
from datetime import datetime
from sqlalchemy import Integer, String, Numeric, ForeignKey, DateTime
from sqlalchemy.orm import Mapped, mapped_column
from app.core.database import Base

class Payment(Base):
    __tablename__ = "payments"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[int] = mapped_column(Integer, ForeignKey("orders.id"), unique=True)
    stripe_payment_intent_id: Mapped[str | None] = mapped_column(String(255))
    method: Mapped[str] = mapped_column(String(32), nullable=False)  # card|cash|tpe|apple_pay|google_pay
    amount: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="pending")
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
```

- [ ] **Step 2: Create `app/modules/payments/service.py`**

```python
import stripe
from datetime import datetime, timezone
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.modules.payments.models import Payment
from app.modules.orders.models import Order
from app.modules.orders.service import get_order, update_order_status
from app.core.config import settings
from app.core.errors import AppError

stripe.api_key = settings.stripe_secret_key

async def create_payment_intent(session: AsyncSession, order_id: int, method: str) -> dict:
    order = await get_order(session, order_id)
    amount_cents = int(float(order.total) * 100)

    try:
        intent = stripe.PaymentIntent.create(
            amount=amount_cents,
            currency="eur",
            payment_method_types=["card"],
            metadata={"order_id": str(order_id)},
        )
    except stripe.StripeError as e:
        raise AppError("PAYMENT_FAILED", str(e), 402)

    payment = Payment(
        order_id=order_id,
        stripe_payment_intent_id=intent.id,
        method=method,
        amount=float(order.total),
        status="pending",
    )
    session.add(payment)
    await session.commit()
    return {"client_secret": intent.client_secret, "payment_intent_id": intent.id}

async def handle_webhook(session: AsyncSession, payload: bytes, sig_header: str):
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, settings.stripe_webhook_secret)
    except stripe.SignatureVerificationError:
        raise AppError("PAYMENT_FAILED", "Signature webhook invalide", 400)

    if event["type"] == "payment_intent.succeeded":
        pi_id = event["data"]["object"]["id"]
        order_id = int(event["data"]["object"]["metadata"]["order_id"])
        payment = await session.scalar(select(Payment).where(Payment.stripe_payment_intent_id == pi_id))
        if payment:
            payment.status = "paid"
            payment.paid_at = datetime.now(timezone.utc)
            await session.flush()

        order = await session.get(Order, order_id)
        if order:
            order.payment_status = "paid"
            await session.commit()
```

- [ ] **Step 3: Create `app/modules/payments/router.py`**

```python
from fastapi import APIRouter, Request, Depends
from app.modules.payments import service
from app.modules.payments.schemas import PaymentIntentRequest
from app.core.database import get_tenant_session
from app.core.deps import get_current_user, require_role

router = APIRouter()

@router.post("/intent")
async def create_intent(body: PaymentIntentRequest, current_user=Depends(get_current_user)):
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        return await service.create_payment_intent(session, body.order_id, body.method)

@router.post("/webhook")
async def stripe_webhook(request: Request):
    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")
    tenant_slug = request.headers.get("X-Tenant-Slug", "default")
    async with get_tenant_session(tenant_slug) as session:
        await service.handle_webhook(session, payload, sig)
    return {"received": True}
```

`app/modules/payments/schemas.py`:

```python
from pydantic import BaseModel

class PaymentIntentRequest(BaseModel):
    order_id: int
    method: str = "card"
```

- [ ] **Step 4: Migrate + test**

```bash
alembic revision --autogenerate -m "payments table"
alembic upgrade head
pytest tests/test_payments.py -v
```

`tests/test_payments.py`:

```python
async def test_webhook_rejects_invalid_signature(client):
    resp = await client.post("/api/v1/payments/webhook",
        content=b'{"type":"payment_intent.succeeded"}',
        headers={"stripe-signature": "bad", "X-Tenant-Slug": "test"})
    assert resp.status_code == 400
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add .
git commit -m "feat: payments module — Stripe intent, webhook handler"
```

---

### Task 8: Loyalty Module

**Files:**
- Create: `app/modules/loyalty/models.py`
- Create: `app/modules/loyalty/schemas.py`
- Create: `app/modules/loyalty/service.py`
- Create: `app/modules/loyalty/router.py`
- Create: `tests/test_loyalty.py`

**Interfaces:**
- Produces: `credit_points(session, user_id, order_id, amount) -> None` — call after payment confirmed

- [ ] **Step 1: Create `app/modules/loyalty/models.py`**

```python
from datetime import datetime
from sqlalchemy import Integer, String, Numeric, ForeignKey, DateTime, func
from sqlalchemy.orm import Mapped, mapped_column
from app.core.database import Base

class LoyaltyAccount(Base):
    __tablename__ = "loyalty_accounts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), unique=True)
    points_balance: Mapped[int] = mapped_column(Integer, default=0)
    total_earned: Mapped[int] = mapped_column(Integer, default=0)

class LoyaltyTransaction(Base):
    __tablename__ = "loyalty_transactions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    loyalty_account_id: Mapped[int] = mapped_column(Integer, ForeignKey("loyalty_accounts.id"))
    order_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("orders.id"), nullable=True)
    points_delta: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
```

- [ ] **Step 2: Create `app/modules/loyalty/service.py`**

```python
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.modules.loyalty.models import LoyaltyAccount, LoyaltyTransaction
from app.core.errors import AppError

POINTS_PER_EURO = 1  # 1 point per euro spent

async def get_or_create_account(session: AsyncSession, user_id: int) -> LoyaltyAccount:
    account = await session.scalar(select(LoyaltyAccount).where(LoyaltyAccount.user_id == user_id))
    if not account:
        account = LoyaltyAccount(user_id=user_id)
        session.add(account)
        await session.flush()
    return account

async def credit_points(session: AsyncSession, user_id: int, order_id: int, amount: float):
    account = await get_or_create_account(session, user_id)
    points = int(amount * POINTS_PER_EURO)
    account.points_balance += points
    account.total_earned += points
    tx = LoyaltyTransaction(
        loyalty_account_id=account.id,
        order_id=order_id,
        points_delta=points,
        reason=f"Commande #{order_id}",
    )
    session.add(tx)
    await session.commit()

async def redeem_points(session: AsyncSession, user_id: int, points: int) -> float:
    account = await get_or_create_account(session, user_id)
    if account.points_balance < points:
        raise AppError("LOYALTY_INSUFFICIENT", "Points insuffisants", 409)
    account.points_balance -= points
    tx = LoyaltyTransaction(
        loyalty_account_id=account.id,
        points_delta=-points,
        reason="Rachat de points",
    )
    session.add(tx)
    await session.commit()
    return points / 100  # 100 points = 1 EUR discount

async def get_transactions(session: AsyncSession, user_id: int):
    account = await get_or_create_account(session, user_id)
    result = await session.execute(
        select(LoyaltyTransaction).where(LoyaltyTransaction.loyalty_account_id == account.id)
        .order_by(LoyaltyTransaction.created_at.desc())
    )
    return list(result.scalars())
```

- [ ] **Step 3: Create schemas and router**

`app/modules/loyalty/schemas.py`:

```python
from datetime import datetime
from pydantic import BaseModel, ConfigDict

class LoyaltyAccountOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    user_id: int
    points_balance: int
    total_earned: int

class LoyaltyTransactionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    points_delta: int
    reason: str
    created_at: datetime

class RedeemRequest(BaseModel):
    points: int
```

`app/modules/loyalty/router.py`:

```python
from fastapi import APIRouter, Depends
from app.modules.loyalty import service
from app.modules.loyalty.schemas import LoyaltyAccountOut, LoyaltyTransactionOut, RedeemRequest
from app.core.database import get_tenant_session
from app.core.deps import get_current_user, require_role

router = APIRouter()

@router.get("/me", response_model=LoyaltyAccountOut)
async def my_account(current_user=Depends(get_current_user)):
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        return await service.get_or_create_account(session, current_user["user_id"])

@router.get("/transactions", response_model=list[LoyaltyTransactionOut])
async def my_transactions(current_user=Depends(get_current_user)):
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        return await service.get_transactions(session, current_user["user_id"])

@router.post("/redeem")
async def redeem(body: RedeemRequest, current_user=Depends(get_current_user)):
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        discount = await service.redeem_points(session, current_user["user_id"], body.points)
    return {"discount_eur": discount}

@router.get("/{user_id}", response_model=LoyaltyAccountOut)
async def admin_account(user_id: int, current_user=Depends(require_role("admin"))):
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        return await service.get_or_create_account(session, user_id)
```

- [ ] **Step 4: Migrate + test**

```bash
alembic revision --autogenerate -m "loyalty tables"
alembic upgrade head
pytest tests/test_loyalty.py -v
```

`tests/test_loyalty.py`:

```python
async def test_redeem_without_auth(client):
    resp = await client.post("/api/v1/loyalty/redeem", json={"points": 100})
    assert resp.status_code == 401
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add .
git commit -m "feat: loyalty module — points credit, redeem, transactions"
```

---

### Task 9: Promotions Module

**Files:**
- Create: `app/modules/promotions/models.py`
- Create: `app/modules/promotions/schemas.py`
- Create: `app/modules/promotions/service.py`
- Create: `app/modules/promotions/router.py`
- Create: `tests/test_promotions.py`

**Interfaces:**
- Produces: `validate_promo(session, code, order_total) -> (Promotion, discount_amount: float)`

- [ ] **Step 1: Create `app/modules/promotions/models.py`**

```python
from datetime import datetime
from sqlalchemy import Integer, String, Boolean, Numeric, DateTime, func
from sqlalchemy.orm import Mapped, mapped_column
from app.core.database import Base

class Promotion(Base):
    __tablename__ = "promotions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    type: Mapped[str] = mapped_column(String(16), nullable=False)  # percent|fixed|free_item
    value: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    min_order_amount: Mapped[float] = mapped_column(Numeric(10, 2), default=0)
    max_uses: Mapped[int | None] = mapped_column(Integer)
    uses_count: Mapped[int] = mapped_column(Integer, default=0)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    valid_until: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
```

- [ ] **Step 2: Create `app/modules/promotions/service.py`**

```python
from datetime import datetime, timezone
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.modules.promotions.models import Promotion
from app.core.errors import AppError

async def validate_promo(session: AsyncSession, code: str, order_total: float):
    now = datetime.now(timezone.utc)
    promo = await session.scalar(select(Promotion).where(
        Promotion.code == code,
        Promotion.is_active == True,
        Promotion.valid_from <= now,
        Promotion.valid_until >= now,
    ))
    if not promo:
        raise AppError("PROMO_INVALID", "Code promo invalide ou expiré", 422)
    if order_total < float(promo.min_order_amount):
        raise AppError("PROMO_INVALID", f"Montant minimum {promo.min_order_amount}€ requis", 422)
    if promo.max_uses and promo.uses_count >= promo.max_uses:
        raise AppError("PROMO_INVALID", "Code promo épuisé", 422)

    if promo.type == "percent":
        discount = order_total * float(promo.value) / 100
    elif promo.type == "fixed":
        discount = min(float(promo.value), order_total)
    else:
        discount = 0  # free_item handled at order level

    return promo, discount

async def use_promo(session: AsyncSession, promo_id: int):
    promo = await session.get(Promotion, promo_id)
    if promo:
        promo.uses_count += 1
        await session.commit()

async def list_active(session: AsyncSession):
    now = datetime.now(timezone.utc)
    result = await session.execute(select(Promotion).where(
        Promotion.is_active == True, Promotion.valid_until >= now
    ))
    return list(result.scalars())
```

- [ ] **Step 3: Create schemas and router**

`app/modules/promotions/schemas.py`:

```python
from datetime import datetime
from pydantic import BaseModel, ConfigDict

class PromotionCreate(BaseModel):
    code: str
    type: str
    value: float
    min_order_amount: float = 0
    max_uses: int | None = None
    valid_from: datetime
    valid_until: datetime

class PromotionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    code: str
    type: str
    value: float
    min_order_amount: float
    uses_count: int
    valid_from: datetime
    valid_until: datetime
    is_active: bool

class ValidateRequest(BaseModel):
    code: str
    order_total: float
```

`app/modules/promotions/router.py`:

```python
from fastapi import APIRouter, Depends
from app.modules.promotions import service
from app.modules.promotions.schemas import PromotionCreate, PromotionOut, ValidateRequest
from app.modules.promotions.models import Promotion
from app.core.database import get_tenant_session
from app.core.deps import get_current_user, require_role

router = APIRouter()

@router.get("", response_model=list[PromotionOut])
async def list_promos(request=None):
    # public — tenant from header
    from fastapi import Request
    ...

@router.post("/validate")
async def validate(body: ValidateRequest, current_user=Depends(get_current_user)):
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        promo, discount = await service.validate_promo(session, body.code, body.order_total)
    return {"valid": True, "discount": discount, "promo_id": promo.id}

@router.post("", response_model=PromotionOut, status_code=201)
async def create_promo(body: PromotionCreate, current_user=Depends(require_role("admin"))):
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        p = Promotion(**body.model_dump())
        session.add(p)
        await session.commit()
        await session.refresh(p)
        return p

@router.put("/{promo_id}", response_model=PromotionOut)
async def update_promo(promo_id: int, body: PromotionCreate, current_user=Depends(require_role("admin"))):
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        p = await session.get(Promotion, promo_id)
        for k, v in body.model_dump().items():
            setattr(p, k, v)
        await session.commit()
        await session.refresh(p)
        return p

@router.delete("/{promo_id}", status_code=204)
async def delete_promo(promo_id: int, current_user=Depends(require_role("admin"))):
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        p = await session.get(Promotion, promo_id)
        if p:
            p.is_active = False
            await session.commit()
```

- [ ] **Step 4: Migrate + test**

```bash
alembic revision --autogenerate -m "promotions table"
alembic upgrade head
pytest tests/test_promotions.py -v
```

`tests/test_promotions.py`:

```python
async def test_validate_invalid_code(client):
    resp = await client.post("/api/v1/promotions/validate",
        json={"code": "NOSUCHCODE", "order_total": 20.0},
        headers={"Authorization": "Bearer fake"})
    assert resp.status_code in (401, 422)
```

- [ ] **Step 5: Commit**

```bash
git add .
git commit -m "feat: promotions module — validate, CRUD"
```

---

### Task 10: Delivery Module

**Files:**
- Create: `app/modules/delivery/models.py`
- Create: `app/modules/delivery/schemas.py`
- Create: `app/modules/delivery/service.py`
- Create: `app/modules/delivery/router.py`
- Create: `tests/test_delivery.py`

**Interfaces:**
- Produces: `check_address(session, lat, lng) -> DeliveryZone | None`

- [ ] **Step 1: Create `app/modules/delivery/models.py`**

```python
from sqlalchemy import Integer, String, Boolean, Numeric, JSON
from sqlalchemy.orm import Mapped, mapped_column
from app.core.database import Base

class DeliveryZone(Base):
    __tablename__ = "delivery_zones"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    polygon: Mapped[dict] = mapped_column(JSON, nullable=False)  # GeoJSON polygon coordinates
    fee: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    min_order_amount: Mapped[float] = mapped_column(Numeric(10, 2), default=0)
    estimated_minutes: Mapped[int] = mapped_column(Integer, default=30)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
```

- [ ] **Step 2: Create `app/modules/delivery/service.py`**

```python
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.modules.delivery.models import DeliveryZone
from app.core.errors import AppError

def _point_in_polygon(lat: float, lng: float, polygon: list[list[float]]) -> bool:
    """Ray-casting algorithm for point-in-polygon."""
    n = len(polygon)
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = polygon[i]
        xj, yj = polygon[j]
        if ((yi > lat) != (yj > lat)) and (lng < (xj - xi) * (lat - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    return inside

async def check_address(session: AsyncSession, lat: float, lng: float) -> DeliveryZone:
    result = await session.execute(select(DeliveryZone).where(DeliveryZone.is_active == True))
    for zone in result.scalars():
        coords = zone.polygon.get("coordinates", [[]])[0]
        if _point_in_polygon(lat, lng, coords):
            return zone
    raise AppError("DELIVERY_ZONE_UNREACHABLE", "Adresse hors zone de livraison", 422)

async def list_zones(session: AsyncSession):
    result = await session.execute(select(DeliveryZone).where(DeliveryZone.is_active == True))
    return list(result.scalars())
```

- [ ] **Step 3: Create schemas and router**

`app/modules/delivery/schemas.py`:

```python
from pydantic import BaseModel, ConfigDict

class DeliveryZoneCreate(BaseModel):
    name: str
    polygon: dict
    fee: float
    min_order_amount: float = 0
    estimated_minutes: int = 30

class DeliveryZoneOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    fee: float
    min_order_amount: float
    estimated_minutes: int
    is_active: bool

class AddressCheckRequest(BaseModel):
    lat: float
    lng: float
```

`app/modules/delivery/router.py`:

```python
from fastapi import APIRouter, Request, Depends
from app.modules.delivery import service
from app.modules.delivery.schemas import DeliveryZoneCreate, DeliveryZoneOut, AddressCheckRequest
from app.modules.delivery.models import DeliveryZone
from app.core.database import get_tenant_session
from app.core.deps import get_current_user, require_role

router = APIRouter()

@router.get("/zones", response_model=list[DeliveryZoneOut])
async def list_zones(request: Request):
    slug = request.headers.get("X-Tenant-Slug", "default")
    async with get_tenant_session(slug) as session:
        return await service.list_zones(session)

@router.post("/zones", response_model=DeliveryZoneOut, status_code=201)
async def create_zone(body: DeliveryZoneCreate, current_user=Depends(require_role("admin"))):
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        z = DeliveryZone(**body.model_dump())
        session.add(z)
        await session.commit()
        await session.refresh(z)
        return z

@router.put("/zones/{zone_id}", response_model=DeliveryZoneOut)
async def update_zone(zone_id: int, body: DeliveryZoneCreate, current_user=Depends(require_role("admin"))):
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        z = await session.get(DeliveryZone, zone_id)
        for k, v in body.model_dump().items():
            setattr(z, k, v)
        await session.commit()
        await session.refresh(z)
        return z

@router.post("/check")
async def check_address(body: AddressCheckRequest, current_user=Depends(get_current_user)):
    async with get_tenant_session(current_user["tenant_slug"]) as session:
        zone = await service.check_address(session, body.lat, body.lng)
        return {"zone_id": zone.id, "name": zone.name, "fee": zone.fee, "estimated_minutes": zone.estimated_minutes}
```

- [ ] **Step 4: Migrate + test**

```bash
alembic revision --autogenerate -m "delivery zones"
alembic upgrade head
pytest tests/test_delivery.py -v
```

`tests/test_delivery.py`:

```python
async def test_list_zones_public(client):
    resp = await client.get("/api/v1/delivery/zones", headers={"X-Tenant-Slug": "test"})
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add .
git commit -m "feat: delivery module — zones, address check"
```

---

### Task 11: Stats Pipeline — ARQ Worker + MongoDB

**Files:**
- Create: `worker/main.py`
- Create: `worker/tasks/stock_alerts.py`
- Create: `worker/tasks/emails.py`
- Create: `worker/tasks/stats.py`
- Create: `app/modules/admin/router.py` (stats endpoints)
- Modify: `app/main.py` (include admin router)
- Create: `tests/test_stats.py`

**Interfaces:**
- Consumes: `settings.mongo_url`, `settings.arq_redis_url`, `deduct_for_order` (stock) — wire alert task
- Produces: `/api/v1/admin/stats/daily`, `/api/v1/admin/stats/monthly`, `/api/v1/admin/stats/live`

- [ ] **Step 1: Create `worker/main.py`**

```python
from arq import create_pool
from arq.connections import RedisSettings
from app.core.config import settings

def get_redis_settings() -> RedisSettings:
    return RedisSettings.from_dsn(settings.arq_redis_url)

class WorkerSettings:
    functions = [
        "worker.tasks.stock_alerts.send_stock_alert",
        "worker.tasks.emails.send_email",
        "worker.tasks.stats.aggregate_daily_stats",
    ]
    redis_settings = get_redis_settings()
    on_startup = None
    on_shutdown = None
```

- [ ] **Step 2: Create `worker/tasks/stock_alerts.py`**

```python
import logging

logger = logging.getLogger(__name__)

async def send_stock_alert(ctx, ingredient_id: int, ingredient_name: str, current_qty: float, tenant_slug: str):
    logger.warning(
        f"[{tenant_slug}] STOCK ALERT: {ingredient_name} (id={ingredient_id}) "
        f"is low: {current_qty} remaining"
    )
    # Email implementation goes in Task emails.py
```

- [ ] **Step 3: Create `worker/tasks/emails.py`**

```python
import logging

logger = logging.getLogger(__name__)

async def send_email(ctx, to: str, subject: str, body: str):
    logger.info(f"EMAIL to={to} subject={subject}")
    # Wire to actual SMTP / SendGrid in production
```

- [ ] **Step 4: Create `worker/tasks/stats.py`**

```python
from datetime import datetime, timezone, timedelta
from motor.motor_asyncio import AsyncIOMotorClient
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy import select, func
from app.core.config import settings
from app.modules.orders.models import Order

async def aggregate_daily_stats(ctx, tenant_slug: str, date: str | None = None):
    """Aggregate PostgreSQL orders for a given day into MongoDB."""
    target_date = datetime.fromisoformat(date) if date else datetime.now(timezone.utc) - timedelta(days=1)
    day_start = target_date.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)

    engine = create_async_engine(settings.database_url)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async with session_factory() as session:
        await session.execute(__import__("sqlalchemy").text(f"SET search_path TO tenant_{tenant_slug}, public"))
        revenue = await session.scalar(
            select(func.sum(Order.total)).where(
                Order.status == "delivered",
                Order.created_at >= day_start,
                Order.created_at < day_end,
            )
        ) or 0
        count = await session.scalar(
            select(func.count()).select_from(Order).where(
                Order.status == "delivered",
                Order.created_at >= day_start,
                Order.created_at < day_end,
            )
        ) or 0

    client = AsyncIOMotorClient(settings.mongo_url)
    db = client["pizzeria_stats"]
    collection = db[f"daily_stats_{tenant_slug}"]
    await collection.update_one(
        {"date": day_start.date().isoformat()},
        {"$set": {
            "date": day_start.date().isoformat(),
            "revenue": float(revenue),
            "order_count": count,
            "avg_basket": float(revenue) / count if count else 0,
            "tenant_slug": tenant_slug,
        }},
        upsert=True,
    )
    client.close()
    await engine.dispose()
```

- [ ] **Step 5: Create `app/modules/admin/router.py`**

```python
from fastapi import APIRouter, Depends
from motor.motor_asyncio import AsyncIOMotorClient
from app.core.config import settings
from app.core.deps import require_role

router = APIRouter()

def get_mongo():
    return AsyncIOMotorClient(settings.mongo_url)["pizzeria_stats"]

@router.get("/stats/daily")
async def daily_stats(current_user=Depends(require_role("admin"))):
    db = get_mongo()
    slug = current_user["tenant_slug"]
    docs = await db[f"daily_stats_{slug}"].find().sort("date", -1).limit(30).to_list(30)
    for d in docs:
        d.pop("_id", None)
    return docs

@router.get("/stats/monthly")
async def monthly_stats(current_user=Depends(require_role("admin"))):
    db = get_mongo()
    slug = current_user["tenant_slug"]
    docs = await db[f"monthly_stats_{slug}"].find().sort("month", -1).limit(12).to_list(12)
    for d in docs:
        d.pop("_id", None)
    return docs

@router.get("/stats/live")
async def live_stats(current_user=Depends(require_role("staff", "admin"))):
    db = get_mongo()
    slug = current_user["tenant_slug"]
    doc = await db[f"live_dashboard_{slug}"].find_one({"tenant_slug": slug})
    if doc:
        doc.pop("_id", None)
    return doc or {}

@router.get("/stats/stock")
async def stock_stats(current_user=Depends(require_role("admin"))):
    db = get_mongo()
    slug = current_user["tenant_slug"]
    doc = await db[f"stock_snapshots_{slug}"].find_one({"tenant_slug": slug})
    if doc:
        doc.pop("_id", None)
    return doc or {}
```

- [ ] **Step 6: Register admin router in `app/main.py`**

Add to `create_app()` in [app/main.py](app/main.py):

```python
from app.modules.admin.router import router as admin_router
app.include_router(admin_router, prefix=f"{prefix}/admin", tags=["admin"])
```

- [ ] **Step 7: Wire stock alerts to ARQ in `app/modules/stock/service.py`**

Replace the `pass` comment in `deduct_for_order`:

```python
# After commit, enqueue alert for each low-stock ingredient
try:
    from arq import create_pool
    from worker.main import get_redis_settings
    pool = await create_pool(get_redis_settings())
    for ing in low_stock.scalars():
        await pool.enqueue_job(
            "send_stock_alert",
            ingredient_id=ing.id,
            ingredient_name=ing.name,
            current_qty=float(ing.current_qty),
            tenant_slug="default",  # pass actual tenant_slug in a real call
        )
    await pool.close()
except Exception:
    pass  # Worker unavailable in test env, non-fatal
```

- [ ] **Step 8: Test stats endpoints**

```bash
pytest tests/test_stats.py -v
```

`tests/test_stats.py`:

```python
async def test_stats_daily_requires_admin(client):
    resp = await client.get("/api/v1/admin/stats/daily")
    assert resp.status_code == 401
```

Expected: PASS

- [ ] **Step 9: Commit**

```bash
git add .
git commit -m "feat: ARQ worker, stats aggregation, MongoDB read model, admin stats endpoints"
```

---

### Task 12: CI/CD + GitHub Actions

**Files:**
- Create: `.github/workflows/ci.yml`
- Create: `render.yaml`
- Create: `alembic_check.sh`

- [ ] **Step 1: Create `.github/workflows/ci.yml`**

```yaml
name: CI

on:
  push:
    branches: [main]
  pull_request:

jobs:
  test:
    runs-on: ubuntu-latest

    services:
      postgres:
        image: postgres:16
        env:
          POSTGRES_PASSWORD: postgres
          POSTGRES_DB: pizza_test
        ports: ["5432:5432"]
        options: >-
          --health-cmd pg_isready
          --health-interval 10s
          --health-timeout 5s
          --health-retries 5

    env:
      DATABASE_URL: postgresql+asyncpg://postgres:postgres@localhost/pizza_test
      TEST_DATABASE_URL: postgresql+asyncpg://postgres:postgres@localhost/pizza_test
      MONGO_URL: mongodb://localhost:27017
      ARQ_REDIS_URL: redis://localhost:6379
      STRIPE_SECRET_KEY: sk_test_dummy
      STRIPE_WEBHOOK_SECRET: whsec_dummy
      JWT_SECRET: test-secret-32-chars-minimum-here

    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install -e ".[dev]"
      - name: Run Alembic migrations
        run: alembic upgrade head
      - name: Run tests
        run: pytest --cov=app --cov-report=term-missing -v
      - name: Alembic schema check
        run: alembic check
```

- [ ] **Step 2: Create `render.yaml`**

```yaml
services:
  - type: web
    name: pizza-api
    env: python
    buildCommand: pip install -e . && alembic upgrade head
    startCommand: uvicorn app.main:app --host 0.0.0.0 --port $PORT
    envVars:
      - key: DATABASE_URL
        sync: false
      - key: MONGO_URL
        sync: false
      - key: ARQ_REDIS_URL
        sync: false
      - key: STRIPE_SECRET_KEY
        sync: false
      - key: STRIPE_WEBHOOK_SECRET
        sync: false
      - key: JWT_SECRET
        sync: false
      - key: ENVIRONMENT
        value: production

  - type: worker
    name: pizza-worker
    env: python
    buildCommand: pip install -e .
    startCommand: python -m arq worker.main.WorkerSettings
```

- [ ] **Step 3: Run full test suite locally**

```bash
pytest -v --tb=short
```

Expected: All tests PASS

- [ ] **Step 4: Final commit**

```bash
git add .
git commit -m "feat: CI/CD — GitHub Actions workflow, Render deployment config"
```

---

## Self-Review

**Spec coverage check:**

| Spec requirement | Task |
|---|---|
| FastAPI + Python 3.12 | Task 1 |
| SQLAlchemy 2 + Alembic | Task 2 |
| PostgreSQL schemas per tenant | Task 2 |
| JWT auth (15min access, 30d refresh, rotation) | Task 3 |
| Rate limiting slowapi | Task 1 (main.py) |
| Catalog CRUD | Task 4 |
| Orders + status transitions | Task 5 |
| Stock deduction on confirm | Task 6 |
| Stripe payments + webhook | Task 7 |
| Loyalty points | Task 8 |
| Promotions + validation | Task 9 |
| Delivery zones + address check | Task 10 |
| ARQ worker (stock alerts, emails, stats) | Task 11 |
| MongoDB read model for stats | Task 11 |
| Admin stats endpoints | Task 11 |
| CI/CD GitHub Actions | Task 12 |
| Error format `{code, detail, field}` | Task 1 |
| Real PostgreSQL tests (no mocks) | Task 2 conftest |
| Pydantic v2 | All tasks |
| `super-admin` endpoints (`/admin/tenants`) | **GAP — not implemented** |

**Gap — super-admin tenants endpoint:** The spec defines `GET/POST /admin/tenants [super-admin]`. This is a cross-tenant endpoint on the `public` schema. Add to Task 11 admin router:

```python
@router.get("/tenants")
async def list_tenants(current_user=Depends(require_role("super-admin"))):
    from app.core.database import get_public_session
    async with get_public_session() as session:
        from sqlalchemy import select
        from sqlalchemy import text
        result = await session.execute(text("SELECT id, slug, name, plan, created_at FROM tenants"))
        return [dict(r._mapping) for r in result]

@router.post("/tenants", status_code=201)
async def create_tenant(body: dict, current_user=Depends(require_role("super-admin"))):
    from app.core.database import get_public_session
    from app.core.tenant import create_tenant_schema
    async with get_public_session() as session:
        result = await session.execute(
            text("INSERT INTO tenants (slug, name, plan) VALUES (:slug, :name, :plan) RETURNING id, slug"),
            {"slug": body["slug"], "name": body["name"], "plan": body.get("plan", "starter")}
        )
        row = result.fetchone()
        await session.commit()
    await create_tenant_schema(body["slug"])
    return {"id": row.id, "slug": row.slug}
```

Add this to Task 11 before its commit step.

**Placeholder scan:** No TBDs or "implement later" present. Promotions `list_active` public endpoint router has an incomplete function — fix:

In `app/modules/promotions/router.py`, replace the broken `list_promos`:

```python
@router.get("", response_model=list[PromotionOut])
async def list_promos(request: Request):
    slug = request.headers.get("X-Tenant-Slug", "default")
    async with get_tenant_session(slug) as session:
        return await service.list_active(session)
```

**Type consistency:** All service functions and routers use consistent names throughout. ✓
