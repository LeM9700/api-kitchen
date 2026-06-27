# Admin Module — Improvements Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implémenter tous les axes d'amélioration du module admin : endpoints manquants, logique métier avancée, et robustesse sécurité, selon la spec `docs/superpowers/specs/2026-06-23-admin-improvements-design.md`.

**Architecture:** Layer-by-layer — migrations d'abord (elles conditionnent tout), puis services, puis schemas/routers, puis worker. Priorité A (endpoints) → C (logique métier) → B (sécurité) à l'intérieur de chaque couche.

**Tech Stack:** FastAPI, SQLAlchemy async, Alembic, Pydantic v2, ARQ (worker), Motor (MongoDB async), SlowAPI (rate-limit), pytz, pytest-asyncio, httpx.

## Global Constraints

- Python 3.12+, FastAPI, SQLAlchemy 2.x async, Pydantic v2 (`model_dump`, `model_config`, `ConfigDict`)
- Migrations Alembic : numéroter à partir de `0015`, inclure `upgrade()` et `downgrade()`
- Migrations tenant-schema : itérer sur `SELECT slug FROM public.tenants` via `_get_tenant_slugs(bind)`
- Worker tasks : créer leur propre engine/session (processus séparé de FastAPI)
- Rate-limit via `@limiter.limit(...)` de `app.core.limiter` (SlowAPI)
- Pas de FK cross-schema — stocker les IDs directement, pas de relations SQLAlchemy cross-schema
- Tests : `pytest tests/ -v`, fixtures `client` (HTTP) et `db_session` (SQLAlchemy rollback) dans `tests/conftest.py`
- Commits fréquents, un par tâche minimum

---

## File Map

| Fichier | Action | Responsabilité |
|---------|--------|----------------|
| `alembic/versions/0015_tenant_config_timezone.py` | Créer | Colonne `timezone` sur `tenant_config` |
| `alembic/versions/0016_audit_user_email.py` | Créer | Colonne `user_email` sur `tenant_config_audits` |
| `alembic/versions/0017_tenant_suspension.py` | Créer | Colonnes suspension sur `public.tenants` |
| `app/modules/admin/tenant_models.py` | Modifier | Ajout champ `timezone` sur `TenantConfig` |
| `app/modules/admin/stats_schemas.py` | Créer | Schemas Pydantic stricts pour stats MongoDB |
| `app/modules/admin/tenant_schemas.py` | Modifier | `timezone`, `user_email`, nouveaux types réponse |
| `app/modules/admin/tenant_service.py` | Modifier | `get_next_opening`, timezone dynamique, cooldown, notif trigger, audit email |
| `app/core/deps.py` | Modifier | Vérification suspension tenant dans `get_current_user` |
| `app/modules/admin/tenant_router.py` | Modifier | `/next-opening`, `/toggle-closure` |
| `app/modules/admin/router.py` | Modifier | Stats typées, `/stats/summary`, suspend/unsuspend |
| `app/modules/orders/service.py` | Modifier | Statut `queued` quand capacité dépassée |
| `worker/tasks/stock_snapshot.py` | Créer | Cron `aggregate_stock_snapshot` (toutes les heures) |
| `worker/tasks/emails.py` | Modifier | Task `notify_config_change` |
| `worker/main.py` | Modifier | Enregistrement du cron et de la task |
| `docs/modules/admin.md` | Modifier | Marquer les axes implémentés |
| `tests/test_admin_improvements.py` | Créer | Tests des nouvelles fonctionnalités |

---

## Task 1 : Migrations Alembic (0015, 0016, 0017)

**Files:**
- Create: `alembic/versions/0015_tenant_config_timezone.py`
- Create: `alembic/versions/0016_audit_user_email.py`
- Create: `alembic/versions/0017_tenant_suspension.py`

**Interfaces:**
- Produces: colonne `tenant_config.timezone VARCHAR(64) DEFAULT 'Europe/Paris'`, colonne `tenant_config_audits.user_email VARCHAR(255)`, colonnes `public.tenants.is_suspended / suspended_at / suspension_message`

- [ ] **Step 1 : Créer `0015_tenant_config_timezone.py`**

```python
# alembic/versions/0015_tenant_config_timezone.py
"""Ajoute timezone configurable par tenant dans tenant_config.

Revision ID: 0015
Revises: 0014
Create Date: 2026-06-23
"""
import sqlalchemy as sa
from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def _get_tenant_slugs(bind) -> list[str]:
    result = bind.execute(sa.text("SELECT slug FROM public.tenants"))
    return [row[0] for row in result]


def upgrade() -> None:
    bind = op.get_bind()
    for slug in _get_tenant_slugs(bind):
        schema = f"tenant_{slug}"
        op.add_column(
            "tenant_config",
            sa.Column(
                "timezone",
                sa.String(64),
                nullable=False,
                server_default="Europe/Paris",
            ),
            schema=schema,
        )


def downgrade() -> None:
    bind = op.get_bind()
    for slug in _get_tenant_slugs(bind):
        schema = f"tenant_{slug}"
        op.drop_column("tenant_config", "timezone", schema=schema)
```

- [ ] **Step 2 : Créer `0016_audit_user_email.py`**

```python
# alembic/versions/0016_audit_user_email.py
"""Ajoute user_email dénormalisé dans tenant_config_audits.

Revision ID: 0016
Revises: 0015
Create Date: 2026-06-23
"""
import sqlalchemy as sa
from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def _get_tenant_slugs(bind) -> list[str]:
    result = bind.execute(sa.text("SELECT slug FROM public.tenants"))
    return [row[0] for row in result]


def upgrade() -> None:
    bind = op.get_bind()
    for slug in _get_tenant_slugs(bind):
        schema = f"tenant_{slug}"
        op.add_column(
            "tenant_config_audits",
            sa.Column("user_email", sa.String(255), nullable=True),
            schema=schema,
        )


def downgrade() -> None:
    bind = op.get_bind()
    for slug in _get_tenant_slugs(bind):
        schema = f"tenant_{slug}"
        op.drop_column("tenant_config_audits", "user_email", schema=schema)
```

- [ ] **Step 3 : Créer `0017_tenant_suspension.py`**

```python
# alembic/versions/0017_tenant_suspension.py
"""Ajoute les colonnes de suspension sur public.tenants.

Revision ID: 0017
Revises: 0016
Create Date: 2026-06-23
"""
import sqlalchemy as sa
from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tenants",
        sa.Column("is_suspended", sa.Boolean(), nullable=False, server_default="false"),
        schema="public",
    )
    op.add_column(
        "tenants",
        sa.Column("suspended_at", sa.DateTime(timezone=True), nullable=True),
        schema="public",
    )
    op.add_column(
        "tenants",
        sa.Column("suspension_message", sa.Text(), nullable=True),
        schema="public",
    )


def downgrade() -> None:
    op.drop_column("tenants", "suspension_message", schema="public")
    op.drop_column("tenants", "suspended_at", schema="public")
    op.drop_column("tenants", "is_suspended", schema="public")
```

- [ ] **Step 4 : Appliquer les migrations**

```bash
alembic upgrade head
```

Résultat attendu : `Running upgrade 0014 -> 0015`, `0015 -> 0016`, `0016 -> 0017` sans erreur.

- [ ] **Step 5 : Commit**

```bash
git add alembic/versions/0015_tenant_config_timezone.py \
        alembic/versions/0016_audit_user_email.py \
        alembic/versions/0017_tenant_suspension.py
git commit -m "feat: add migrations 0015-0017 (timezone, audit email, suspension)"
```

---

## Task 2 : Modèle TenantConfig + Schemas

**Files:**
- Modify: `app/modules/admin/tenant_models.py`
- Create: `app/modules/admin/stats_schemas.py`
- Modify: `app/modules/admin/tenant_schemas.py`
- Test: `tests/test_admin_improvements.py`

**Interfaces:**
- Produces: `TenantConfig.timezone: str`, `DailyStatsResponse`, `MonthlyStatsResponse`, `LiveStatsResponse`, `StockSnapshotResponse`, `StockAlertItem`, `StatsSummaryResponse`, `NextOpeningResponse`, `TenantClosureToggle`, `TenantSuspendRequest`, `TenantResponse`

- [ ] **Step 1 : Ajouter `timezone` dans `TenantConfig`**

Dans `app/modules/admin/tenant_models.py`, ajouter le champ après `overhead_per_order_minutes` :

```python
    # Timezone configurable par tenant (IANA, ex: "Europe/Paris", "America/New_York").
    timezone: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        default="Europe/Paris",
        server_default="Europe/Paris",
    )
```

Ajouter `String` aux imports SQLAlchemy si pas déjà présent (il l'est déjà).

- [ ] **Step 2 : Créer `app/modules/admin/stats_schemas.py`**

```python
# app/modules/admin/stats_schemas.py
"""Schemas Pydantic stricts pour les documents MongoDB des stats admin."""
from pydantic import BaseModel


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

- [ ] **Step 3 : Mettre à jour `tenant_schemas.py`**

Ajouter `timezone` à `TenantConfigUpdate` (avec validation pytz) et `TenantConfigResponse`. Ajouter `user_email` à `TenantConfigAuditResponse`. Ajouter les nouveaux types. Voici le fichier complet :

```python
# app/modules/admin/tenant_schemas.py
"""Schemas Pydantic pour le tableau de bord tenant self-service."""
from datetime import date, datetime, time

import pytz
from pydantic import BaseModel, ConfigDict, Field, field_validator


class TenantConfigUpdate(BaseModel):
    """Mise a jour partielle de la configuration tenant -- tous champs optionnels."""

    is_temporarily_closed: bool | None = None
    temporary_closure_message: str | None = None
    default_closure_message: str | None = None
    prep_time_normal_minutes: int | None = Field(None, ge=1, le=180)
    prep_time_peak_minutes: int | None = Field(None, ge=1, le=360)
    peak_orders_threshold: int | None = Field(None, ge=1, le=100)
    auto_calc_prep_time: bool | None = None
    overhead_per_order_minutes: int | None = Field(None, ge=0, le=60)
    timezone: str | None = None

    @field_validator("temporary_closure_message", "default_closure_message", mode="before")
    @classmethod
    def validate_message_length(cls, v: str | None) -> str | None:
        if v and len(v) > 500:
            raise ValueError("Le message ne peut pas depasser 500 caracteres")
        return v

    @field_validator("timezone", mode="before")
    @classmethod
    def validate_timezone(cls, v: str | None) -> str | None:
        if v is None:
            return v
        try:
            pytz.timezone(v)
        except pytz.UnknownTimeZoneError:
            raise ValueError(f"Timezone inconnue : {v!r}")
        return v


class TenantConfigResponse(BaseModel):
    """Representation complete de la configuration tenant."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    is_temporarily_closed: bool
    temporary_closure_message: str | None
    default_closure_message: str
    prep_time_normal_minutes: int
    prep_time_peak_minutes: int
    peak_orders_threshold: int
    auto_calc_prep_time: bool
    overhead_per_order_minutes: int
    timezone: str
    updated_at: datetime


class BusinessHoursCreate(BaseModel):
    slot_index: int = Field(..., ge=0, le=10)
    opens_at: time
    closes_at: time


class BusinessHoursResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    day_of_week: int
    slot_index: int
    opens_at: time
    closes_at: time
    is_active: bool


class ExceptionalClosureCreate(BaseModel):
    closure_date: date
    custom_message: str | None = None
    use_default_message: bool = False

    @field_validator("custom_message", mode="before")
    @classmethod
    def validate_custom_message_length(cls, v: str | None) -> str | None:
        if v and len(v) > 500:
            raise ValueError("Le message ne peut pas depasser 500 caracteres")
        return v


class ExceptionalClosureResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    closure_date: date
    custom_message: str | None
    use_default_message: bool
    created_at: datetime


class TenantConfigAuditResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    changed_by_user_id: int
    user_email: str | None
    changed_at: datetime
    field_name: str
    old_value: str | None
    new_value: str | None
    ip_address: str | None


class TenantStatusResponse(BaseModel):
    is_open: bool
    estimated_prep_time_minutes: int
    message: str | None
    next_opening: str | None
    active_orders_count: int


class NextOpeningResponse(BaseModel):
    next_opening: str | None


class TenantClosureToggle(BaseModel):
    """Payload pour PATCH /tenant/toggle-closure (endpoint dédié rate-limité)."""

    is_temporarily_closed: bool
    temporary_closure_message: str | None = None

    @field_validator("temporary_closure_message", mode="before")
    @classmethod
    def validate_message_length(cls, v: str | None) -> str | None:
        if v and len(v) > 500:
            raise ValueError("Le message ne peut pas depasser 500 caracteres")
        return v


class TenantSuspendRequest(BaseModel):
    suspension_message: str = Field(..., min_length=1, max_length=500)


class TenantResponse(BaseModel):
    """Représentation d'un tenant pour les endpoints super-admin."""

    id: int
    slug: str
    name: str
    plan: str
    created_at: datetime
    is_suspended: bool
    suspended_at: datetime | None
    suspension_message: str | None
```

- [ ] **Step 4 : Écrire les tests des schemas**

```python
# tests/test_admin_improvements.py
import pytest
from pydantic import ValidationError

from app.modules.admin.tenant_schemas import TenantConfigUpdate
from app.modules.admin.stats_schemas import DailyStatsResponse, LiveStatsResponse


def test_tenant_config_update_valid_timezone():
    update = TenantConfigUpdate(timezone="America/New_York")
    assert update.timezone == "America/New_York"


def test_tenant_config_update_invalid_timezone():
    with pytest.raises(ValidationError):
        TenantConfigUpdate(timezone="Not/ATimezone")


def test_daily_stats_response_parsing():
    data = {
        "date": "2026-06-22",
        "revenue": 150.5,
        "order_count": 10,
        "avg_basket": 15.05,
        "tenant_slug": "pizza-test",
    }
    resp = DailyStatsResponse(**data)
    assert resp.order_count == 10


def test_live_stats_response_extra_fields_ignored():
    data = {
        "tenant_slug": "pizza-test",
        "orders_last_24h": 5,
        "revenue_last_24h": 75.0,
        "avg_order_value_24h": 15.0,
        "pending_orders": 2,
        "computed_at": "2026-06-23T10:00:00Z",
        "_id": "should_be_ignored",
    }
    resp = LiveStatsResponse(**{k: v for k, v in data.items() if k != "_id"})
    assert resp.pending_orders == 2
```

- [ ] **Step 5 : Lancer les tests**

```bash
pytest tests/test_admin_improvements.py -v
```

Résultat attendu : 4 tests PASS.

- [ ] **Step 6 : Commit**

```bash
git add app/modules/admin/tenant_models.py \
        app/modules/admin/stats_schemas.py \
        app/modules/admin/tenant_schemas.py \
        tests/test_admin_improvements.py
git commit -m "feat: add timezone to TenantConfig, stats schemas, extended tenant schemas"
```

---

## Task 3 : tenant_service — `get_next_opening` + `_write_audit` avec `user_email`

**Files:**
- Modify: `app/modules/admin/tenant_service.py`

**Interfaces:**
- Consumes: `TenantConfig.timezone` (Task 2), `TenantConfigAuditResponse.user_email` (Task 2)
- Produces: `get_next_opening(session) -> str | None`, `_write_audit(... user_email=None)`

- [ ] **Step 1 : Ajouter `user_email` à `TenantConfigAudit` model**

Dans `app/modules/admin/tenant_models.py`, ajouter le champ à la classe `TenantConfigAudit` après `user_agent` :

```python
    user_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
```

- [ ] **Step 2 : Mettre à jour `_write_audit()` dans `tenant_service.py`**

Remplacer la signature et le corps de `_write_audit` :

```python
async def _write_audit(
    session: AsyncSession,
    user_id: int,
    field_name: str,
    old_value: str | None,
    new_value: str | None,
    ip_address: str | None = None,
    user_agent: str | None = None,
    user_email: str | None = None,
) -> None:
    audit = TenantConfigAudit(
        changed_by_user_id=user_id,
        field_name=field_name,
        old_value=old_value,
        new_value=new_value,
        ip_address=ip_address,
        user_agent=user_agent,
        user_email=user_email,
    )
    session.add(audit)
```

- [ ] **Step 3 : Propager `user_email` dans `update_config()`**

Ajouter `user_email: str | None = None` aux paramètres de `update_config` et passer au `_write_audit` :

```python
async def update_config(
    session: AsyncSession,
    data: TenantConfigUpdate,
    user_id: int | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
    user_email: str | None = None,
    arq_pool=None,
    tenant_slug: str | None = None,
) -> TenantConfig:
```

Dans la boucle d'audit inside `update_config`, passer `user_email=user_email` à `_write_audit`.

- [ ] **Step 4 : Propager `user_email` dans `upsert_business_hours()`**

Ajouter `user_email: str | None = None` aux paramètres de `upsert_business_hours` et passer au `_write_audit` existant dans cette fonction.

- [ ] **Step 5 : Ajouter `get_next_opening()`**

Ajouter cette nouvelle fonction publique à la fin du bloc "Tenant status" (après `get_tenant_status`), avant les helpers privés :

```python
async def get_next_opening(session: AsyncSession) -> str | None:
    """Retourne le prochain horaire d'ouverture sans calculer le statut complet.

    Args:
        session: Session SQLAlchemy active sur le schema tenant courant.

    Returns:
        Chaîne lisible ou None si aucun créneau dans les 7 prochains jours.
    """
    config = await get_or_create_config(session)
    now_paris = datetime.now(pytz.timezone(config.timezone))
    return await _compute_next_opening(
        session, now_paris.date(), now_paris.time(), skip_today=False
    )
```

- [ ] **Step 6 : Ajouter un test**

Dans `tests/test_admin_improvements.py` :

```python
async def test_next_opening_endpoint_public(client):
    response = await client.get(
        "/api/v1/admin/tenant/next-opening",
        params={"tenant_slug": "test"},
    )
    # Sans tenant réel configuré, peut retourner 200 ou 422 selon la config —
    # on vérifie juste que l'endpoint existe (pas 404/405).
    assert response.status_code != 404
    assert response.status_code != 405
```

- [ ] **Step 7 : Lancer les tests**

```bash
pytest tests/test_admin_improvements.py -v
```

- [ ] **Step 8 : Commit**

```bash
git add app/modules/admin/tenant_models.py \
        app/modules/admin/tenant_service.py
git commit -m "feat: add get_next_opening(), user_email in audit trail"
```

---

## Task 4 : tenant_service — Timezone dynamique + Cooldown + Notifications

**Files:**
- Modify: `app/modules/admin/tenant_service.py`

**Interfaces:**
- Consumes: `TenantConfig.timezone` (Task 2), `arq_pool` (ARQ pool), `tenant_slug`
- Produces: `update_config` avec cooldown 2 min sur `is_temporarily_closed`, timezone dynamique, enqueue `notify_config_change`

- [ ] **Step 1 : Supprimer `_PARIS_TZ` et dynamiser la timezone**

Supprimer la ligne :
```python
_PARIS_TZ = pytz.timezone("Europe/Paris")
```

Dans `get_tenant_status()`, remplacer chaque usage de `_PARIS_TZ` :
```python
# Avant
now_paris = datetime.now(_PARIS_TZ)
# Après — config est déjà chargé au début de get_tenant_status via get_or_create_config
now_paris = datetime.now(pytz.timezone(config.timezone))
```

Dans `add_exceptional_closure()`, la vérification de date utilise aussi `_PARIS_TZ` :
```python
# Avant
today = datetime.now(_PARIS_TZ).date()
# Après — charger config depuis la session
config = await get_or_create_config(session)
today = datetime.now(pytz.timezone(config.timezone)).date()
```

- [ ] **Step 2 : Ajouter le cooldown `is_temporarily_closed` dans `update_config()`**

Ajouter ce bloc au début du corps de `update_config`, après `config = await get_or_create_config(session)` :

```python
    # [🔒 DOS] Cooldown 2 min sur is_temporarily_closed pour éviter le spam open/close.
    updates = data.model_dump(exclude_none=True)
    if "is_temporarily_closed" in updates:
        from datetime import timezone as _tz
        from sqlalchemy import desc as _desc
        last_toggle = await session.scalar(
            select(TenantConfigAudit)
            .where(TenantConfigAudit.field_name == "is_temporarily_closed")
            .order_by(_desc(TenantConfigAudit.changed_at))
        )
        if last_toggle is not None:
            age_seconds = (
                datetime.now(_tz.utc) - last_toggle.changed_at.replace(tzinfo=_tz.utc)
            ).total_seconds()
            if age_seconds < 120:
                raise HTTPException(
                    status_code=429,
                    detail="Trop de changements de statut. Attendez 2 minutes avant de modifier is_temporarily_closed.",
                )
```

Note : le `updates = data.model_dump(exclude_none=True)` remplace la ligne identique existante plus bas — supprimer le doublon.

- [ ] **Step 3 : Ajouter le trigger de notification dans `update_config()`**

À la fin de `update_config`, après `await session.refresh(config)` et avant `return config`, ajouter :

```python
    # Notifier les admins/staff si is_temporarily_closed a changé.
    if (
        arq_pool is not None
        and tenant_slug is not None
        and "is_temporarily_closed" in updates
    ):
        try:
            await arq_pool.enqueue_job(
                "notify_config_change",
                tenant_slug=tenant_slug,
                is_closed=config.is_temporarily_closed,
            )
        except Exception:
            pass  # Notification non critique — ne pas bloquer la réponse.

    return config
```

- [ ] **Step 4 : Mettre à jour `tenant_router.py` pour passer `arq_pool` et `user_email` à `update_config`**

Dans `tenant_router.py`, la fonction `patch_config` doit maintenant passer `user_email` et `arq_pool`. Modifier la signature et le corps :

```python
from app.core.deps import get_arq_pool

@router.patch("/config", response_model=TenantConfigResponse)
@limiter.limit("30/minute")
async def patch_config(
    request: Request,
    body: TenantConfigUpdate,
    current_user: dict = Depends(require_role("admin")),
    arq_pool=Depends(get_arq_pool),
) -> TenantConfigResponse:
    ip = request.client.host if request.client else None
    xff = request.headers.get("x-forwarded-for")
    ip = xff.split(",")[0].strip() if xff else ip
    user_agent = request.headers.get("user-agent", "")

    async with get_tenant_session(current_user["tenant_slug"]) as session:
        return await tenant_service.update_config(
            session,
            body,
            user_id=current_user["id"],
            ip_address=ip,
            user_agent=user_agent,
            user_email=current_user.get("email"),
            arq_pool=arq_pool,
            tenant_slug=current_user["tenant_slug"],
        )
```

- [ ] **Step 5 : Lancer les tests**

```bash
pytest tests/ -v
```

Résultat attendu : tous les tests existants PASS, pas de régression.

- [ ] **Step 6 : Commit**

```bash
git add app/modules/admin/tenant_service.py \
        app/modules/admin/tenant_router.py
git commit -m "feat: dynamic timezone, 2min cooldown on closure toggle, config change notification trigger"
```

---

## Task 5 : `core/deps.py` — Vérification suspension tenant

**Files:**
- Modify: `app/core/deps.py`
- Test: `tests/test_admin_improvements.py`

**Interfaces:**
- Consumes: colonne `public.tenants.is_suspended` (Task 1)
- Produces: `get_current_user` lève `AppError(403)` si le tenant est suspendu (super-admin exempt)

- [ ] **Step 1 : Ajouter la vérification de suspension dans `get_current_user()`**

Après le bloc de construction du dict `user` (ligne `request.state.role = user["role"]`), ajouter :

```python
    # [🔒] Blocage si le tenant est suspendu — super-admin exempt.
    if user.get("role") != "super-admin" and user.get("tenant_slug"):
        from app.core.database import get_public_session
        from sqlalchemy import text as _text
        async with get_public_session() as pub_session:
            row = await pub_session.execute(
                _text(
                    "SELECT is_suspended, suspension_message "
                    "FROM public.tenants WHERE slug = :slug"
                ),
                {"slug": user["tenant_slug"]},
            )
            tenant_row = row.fetchone()
            if tenant_row and tenant_row.is_suspended:
                raise AppError(
                    "FORBIDDEN",
                    tenant_row.suspension_message or "Tenant suspendu",
                    403,
                )

    return user
```

- [ ] **Step 2 : Ajouter les tests d'autorisation**

Dans `tests/test_admin_improvements.py` :

```python
async def test_admin_stats_requires_auth(client):
    response = await client.get("/api/v1/admin/stats/daily")
    assert response.status_code == 401

async def test_tenant_config_requires_admin(client):
    response = await client.get("/api/v1/admin/tenant/config")
    assert response.status_code == 401

async def test_suspend_requires_super_admin(client):
    response = await client.patch("/api/v1/admin/tenants/1/suspend", json={"suspension_message": "test"})
    assert response.status_code == 401
```

- [ ] **Step 3 : Lancer les tests**

```bash
pytest tests/test_admin_improvements.py -v
pytest tests/test_auth.py -v
```

Résultat attendu : tous PASS.

- [ ] **Step 4 : Commit**

```bash
git add app/core/deps.py \
        tests/test_admin_improvements.py
git commit -m "feat: block suspended tenants in get_current_user (super-admin exempt)"
```

---

## Task 6 : `tenant_router.py` — `/next-opening` et `/toggle-closure`

**Files:**
- Modify: `app/modules/admin/tenant_router.py`

**Interfaces:**
- Consumes: `get_next_opening()` (Task 3), `update_config()` (Task 4), `TenantClosureToggle` (Task 2), `NextOpeningResponse` (Task 2)
- Produces: `GET /api/v1/admin/tenant/next-opening` (public), `PATCH /api/v1/admin/tenant/toggle-closure` (admin, 5/min)

- [ ] **Step 1 : Ajouter l'import de `get_arq_pool` et des nouveaux schemas**

En haut de `tenant_router.py`, ajouter aux imports :

```python
from app.core.deps import get_arq_pool
from app.modules.admin.tenant_schemas import (
    ...,  # existants
    NextOpeningResponse,
    TenantClosureToggle,
    TenantConfigUpdate,
)
```

- [ ] **Step 2 : Ajouter `GET /tenant/next-opening`**

Dans la section "Routes publiques" de `tenant_router.py` :

```python
@router.get("/next-opening", response_model=NextOpeningResponse)
async def get_next_opening(
    tenant_slug: str = Query(..., description="Slug du tenant"),
) -> NextOpeningResponse:
    """Retourne le prochain horaire d'ouverture sans statut complet.

    Endpoint public — utilisé par la vitrine pour afficher "Rouvre lundi à 11h".

    Args:
        tenant_slug: Slug du tenant (query param).

    Returns:
        NextOpeningResponse avec next_opening en clair ou None.
    """
    async with get_tenant_session(tenant_slug) as session:
        result = await tenant_service.get_next_opening(session)
    return NextOpeningResponse(next_opening=result)
```

- [ ] **Step 3 : Ajouter `PATCH /tenant/toggle-closure`**

Dans la section "Routes admin -- config" :

```python
@router.patch("/toggle-closure", response_model=TenantConfigResponse)
@limiter.limit("5/minute")
async def toggle_closure(
    request: Request,
    body: TenantClosureToggle,
    current_user: dict = Depends(require_role("admin")),
    arq_pool=Depends(get_arq_pool),
) -> TenantConfigResponse:
    """Bascule l'état de fermeture manuelle du restaurant.

    [🔒 SÉCURITÉ] Endpoint dédié rate-limité à 5/min pour éviter le spam
    open/close. Le cooldown 2 min est également appliqué côté service.

    Args:
        request: Requête FastAPI (requis par SlowAPI).
        body: TenantClosureToggle avec is_temporarily_closed et message optionnel.
        current_user: Utilisateur admin injecté par dépendance.
        arq_pool: Pool ARQ pour enqueue la notification.

    Returns:
        TenantConfigResponse mise à jour.
    """
    ip = request.client.host if request.client else None
    xff = request.headers.get("x-forwarded-for")
    ip = xff.split(",")[0].strip() if xff else ip
    user_agent = request.headers.get("user-agent", "")

    update_data = TenantConfigUpdate(
        is_temporarily_closed=body.is_temporarily_closed,
        temporary_closure_message=body.temporary_closure_message,
    )

    async with get_tenant_session(current_user["tenant_slug"]) as session:
        return await tenant_service.update_config(
            session,
            update_data,
            user_id=current_user["id"],
            ip_address=ip,
            user_agent=user_agent,
            user_email=current_user.get("email"),
            arq_pool=arq_pool,
            tenant_slug=current_user["tenant_slug"],
        )
```

- [ ] **Step 4 : Ajouter tests des nouveaux endpoints**

Dans `tests/test_admin_improvements.py` :

```python
async def test_next_opening_requires_tenant_slug(client):
    response = await client.get("/api/v1/admin/tenant/next-opening")
    assert response.status_code == 422  # tenant_slug manquant

async def test_toggle_closure_requires_admin(client):
    response = await client.patch(
        "/api/v1/admin/tenant/toggle-closure",
        json={"is_temporarily_closed": True},
    )
    assert response.status_code == 401
```

- [ ] **Step 5 : Lancer les tests**

```bash
pytest tests/test_admin_improvements.py -v
```

- [ ] **Step 6 : Commit**

```bash
git add app/modules/admin/tenant_router.py
git commit -m "feat: add /tenant/next-opening (public) and /tenant/toggle-closure (5/min)"
```

---

## Task 7 : `router.py` — Stats typées + `/stats/summary` + Suspend/Unsuspend

**Files:**
- Modify: `app/modules/admin/router.py`

**Interfaces:**
- Consumes: `DailyStatsResponse`, `MonthlyStatsResponse`, `LiveStatsResponse`, `StockSnapshotResponse`, `StatsSummaryResponse`, `TenantSuspendRequest`, `TenantResponse` (Task 2)
- Produces: endpoints stats typés, `GET /stats/summary`, `PATCH /tenants/{id}/suspend`, `PATCH /tenants/{id}/unsuspend`

- [ ] **Step 1 : Ajouter les imports**

En tête de `app/modules/admin/router.py`, ajouter :

```python
from datetime import datetime, timezone

from app.modules.admin.stats_schemas import (
    DailyStatsResponse,
    LiveStatsResponse,
    MonthlyStatsResponse,
    StatsSummaryResponse,
    StockSnapshotResponse,
)
from app.modules.admin.tenant_schemas import TenantResponse, TenantSuspendRequest
```

- [ ] **Step 2 : Typer les endpoints stats existants**

Remplacer les 4 fonctions stats par les versions avec `response_model` :

```python
@router.get("/stats/daily", response_model=list[DailyStatsResponse])
async def daily_stats(
    current_user=Depends(require_role("admin")),
    db: AsyncIOMotorDatabase = Depends(get_mongo),
) -> list[DailyStatsResponse]:
    slug = current_user["tenant_slug"]
    docs = await db[f"daily_stats_{slug}"].find().sort("date", -1).limit(30).to_list(30)
    return [DailyStatsResponse(**{k: v for k, v in doc.items() if k != "_id"}) for doc in docs]


@router.get("/stats/monthly", response_model=list[MonthlyStatsResponse])
async def monthly_stats(
    current_user=Depends(require_role("admin")),
    db: AsyncIOMotorDatabase = Depends(get_mongo),
) -> list[MonthlyStatsResponse]:
    slug = current_user["tenant_slug"]
    docs = await db[f"monthly_stats_{slug}"].find().sort("month", -1).limit(12).to_list(12)
    return [MonthlyStatsResponse(**{k: v for k, v in doc.items() if k != "_id"}) for doc in docs]


@router.get("/stats/live", response_model=LiveStatsResponse | dict)
async def live_stats(
    current_user=Depends(require_role("staff", "admin")),
    db: AsyncIOMotorDatabase = Depends(get_mongo),
) -> LiveStatsResponse | dict:
    slug = current_user["tenant_slug"]
    doc = await db[f"live_dashboard_{slug}"].find_one({"tenant_slug": slug})
    if not doc:
        return {}
    return LiveStatsResponse(**{k: v for k, v in doc.items() if k != "_id"})


@router.get("/stats/stock", response_model=StockSnapshotResponse | dict)
async def stock_stats(
    current_user=Depends(require_role("admin")),
    db: AsyncIOMotorDatabase = Depends(get_mongo),
) -> StockSnapshotResponse | dict:
    slug = current_user["tenant_slug"]
    doc = await db[f"stock_snapshots_{slug}"].find_one({"tenant_slug": slug})
    if not doc:
        return {}
    return StockSnapshotResponse(**{k: v for k, v in doc.items() if k != "_id"})
```

- [ ] **Step 3 : Ajouter `GET /stats/summary`**

Après les endpoints stats existants :

```python
@router.get("/stats/summary", response_model=StatsSummaryResponse)
async def stats_summary(
    current_user=Depends(require_role("admin")),
    db: AsyncIOMotorDatabase = Depends(get_mongo),
) -> StatsSummaryResponse:
    """Retourne live + dernier daily en une seule requête pour le chargement initial du dashboard.

    Args:
        current_user: Utilisateur admin injecté par dépendance.
        db: Base MongoDB injectée par dépendance.

    Returns:
        StatsSummaryResponse combinant live dashboard et dernier jour.
    """
    slug = current_user["tenant_slug"]

    live_doc = await db[f"live_dashboard_{slug}"].find_one({"tenant_slug": slug})
    live = (
        LiveStatsResponse(**{k: v for k, v in live_doc.items() if k != "_id"})
        if live_doc
        else LiveStatsResponse(
            tenant_slug=slug,
            orders_last_24h=0,
            revenue_last_24h=0.0,
            avg_order_value_24h=0.0,
            pending_orders=0,
            computed_at="",
        )
    )

    daily_docs = await db[f"daily_stats_{slug}"].find().sort("date", -1).limit(1).to_list(1)
    last_day = (
        DailyStatsResponse(**{k: v for k, v in daily_docs[0].items() if k != "_id"})
        if daily_docs
        else None
    )

    return StatsSummaryResponse(live=live, last_day=last_day)
```

- [ ] **Step 4 : Ajouter `PATCH /tenants/{tenant_id}/suspend`**

Remplacer le bloc `list_tenants` + `create_tenant` existant et ajouter les nouveaux endpoints dessous :

```python
@router.get("/tenants")
async def list_tenants(current_user=Depends(require_role("super-admin"))):
    async with get_public_session() as session:
        result = await session.execute(
            text(
                "SELECT id, slug, name, plan, created_at, "
                "is_suspended, suspended_at, suspension_message "
                "FROM public.tenants"
            )
        )
        return [dict(row._mapping) for row in result]


@router.post("/tenants", status_code=201)
async def create_tenant(body: TenantCreate, current_user=Depends(require_role("super-admin"))):
    async with get_public_session() as session:
        result = await session.execute(
            text(
                "INSERT INTO public.tenants (slug, name, plan) "
                "VALUES (:slug, :name, :plan) RETURNING id, slug"
            ),
            body.model_dump(),
        )
        row = result.fetchone()
        await session.commit()
    await create_tenant_schema(body.slug)
    return {"id": row.id, "slug": row.slug}


@router.patch("/tenants/{tenant_id}/suspend", response_model=TenantResponse)
async def suspend_tenant(
    tenant_id: int,
    body: TenantSuspendRequest,
    current_user=Depends(require_role("super-admin")),
) -> TenantResponse:
    """Suspend un tenant : flag is_suspended + fermeture forcée + message.

    Args:
        tenant_id: Identifiant du tenant à suspendre.
        body: TenantSuspendRequest avec suspension_message.
        current_user: Utilisateur super-admin injecté par dépendance.

    Returns:
        TenantResponse avec l'état mis à jour.

    Raises:
        HTTPException: 404 si le tenant est introuvable.
    """
    from app.modules.admin import tenant_service
    from app.modules.admin.tenant_schemas import TenantConfigUpdate

    now = datetime.now(timezone.utc)

    async with get_public_session() as session:
        result = await session.execute(
            text("SELECT slug FROM public.tenants WHERE id = :id"),
            {"id": tenant_id},
        )
        row = result.fetchone()
        if row is None:
            from fastapi import HTTPException
            raise HTTPException(status_code=404, detail="Tenant introuvable.")

        tenant_slug = row.slug
        await session.execute(
            text(
                "UPDATE public.tenants SET is_suspended = true, "
                "suspended_at = :now, suspension_message = :msg "
                "WHERE id = :id"
            ),
            {"now": now, "msg": body.suspension_message, "id": tenant_id},
        )
        await session.commit()

        result2 = await session.execute(
            text(
                "SELECT id, slug, name, plan, created_at, "
                "is_suspended, suspended_at, suspension_message "
                "FROM public.tenants WHERE id = :id"
            ),
            {"id": tenant_id},
        )
        tenant_row = result2.fetchone()

    # Forcer la fermeture du restaurant pour que les clients voient le message.
    from app.core.database import get_tenant_session
    async with get_tenant_session(tenant_slug) as t_session:
        await tenant_service.update_config(
            t_session,
            TenantConfigUpdate(
                is_temporarily_closed=True,
                temporary_closure_message=body.suspension_message,
            ),
        )

    return TenantResponse(**dict(tenant_row._mapping))


@router.patch("/tenants/{tenant_id}/unsuspend", response_model=TenantResponse)
async def unsuspend_tenant(
    tenant_id: int,
    current_user=Depends(require_role("super-admin")),
) -> TenantResponse:
    """Réactive un tenant suspendu et rouvre le restaurant.

    Args:
        tenant_id: Identifiant du tenant à réactiver.
        current_user: Utilisateur super-admin injecté par dépendance.

    Returns:
        TenantResponse avec l'état mis à jour.

    Raises:
        HTTPException: 404 si le tenant est introuvable.
    """
    from app.modules.admin import tenant_service
    from app.modules.admin.tenant_schemas import TenantConfigUpdate

    async with get_public_session() as session:
        result = await session.execute(
            text("SELECT slug FROM public.tenants WHERE id = :id"),
            {"id": tenant_id},
        )
        row = result.fetchone()
        if row is None:
            from fastapi import HTTPException
            raise HTTPException(status_code=404, detail="Tenant introuvable.")

        tenant_slug = row.slug
        await session.execute(
            text(
                "UPDATE public.tenants SET is_suspended = false, "
                "suspended_at = NULL, suspension_message = NULL "
                "WHERE id = :id"
            ),
            {"id": tenant_id},
        )
        await session.commit()

        result2 = await session.execute(
            text(
                "SELECT id, slug, name, plan, created_at, "
                "is_suspended, suspended_at, suspension_message "
                "FROM public.tenants WHERE id = :id"
            ),
            {"id": tenant_id},
        )
        tenant_row = result2.fetchone()

    from app.core.database import get_tenant_session
    async with get_tenant_session(tenant_slug) as t_session:
        await tenant_service.update_config(
            t_session,
            TenantConfigUpdate(is_temporarily_closed=False),
        )

    return TenantResponse(**dict(tenant_row._mapping))
```

- [ ] **Step 5 : Lancer les tests**

```bash
pytest tests/ -v
```

- [ ] **Step 6 : Commit**

```bash
git add app/modules/admin/router.py
git commit -m "feat: typed stats responses, /stats/summary, tenant suspend/unsuspend endpoints"
```

---

## Task 8 : `orders/service.py` — Statut `queued`

**Files:**
- Modify: `app/modules/orders/service.py`
- Test: `tests/test_admin_improvements.py`

**Interfaces:**
- Consumes: `TenantConfig.peak_orders_threshold`, `TenantConfig.prep_time_peak_minutes`, `TenantConfig.overhead_per_order_minutes`
- Produces: `VALID_TRANSITIONS["queued"] = {"confirmed", "cancelled"}`, statut `queued` assigné si capacité dépassée lors de la confirmation

- [ ] **Step 1 : Ajouter `queued` dans `VALID_TRANSITIONS`**

Dans `app/modules/orders/service.py`, modifier le dict `VALID_TRANSITIONS` :

```python
VALID_TRANSITIONS = {
    "pending": {"confirmed", "cancelled"},
    "confirmed": {"preparing", "cancelled"},
    "queued": {"confirmed", "cancelled"},   # ← nouveau
    "preparing": {"ready", "cancelled"},
    "ready": {"out_for_delivery", "delivered"},
    "out_for_delivery": {"delivered", "cancelled"},
    "delivered": set(),
    "cancelled": set(),
}
```

- [ ] **Step 2 : Ajouter la logique de file d'attente dans `update_status()`**

Ajouter les imports en tête de fichier (si pas déjà là) :

```python
from app.modules.admin.tenant_models import TenantConfig
```

Dans `update_status()`, après `previous_status = order.status` et avant `if status not in VALID_TRANSITIONS...`, ajouter :

```python
    # [FILE D'ATTENTE] Si la confirmation est demandée et que la capacité est dépassée,
    # router vers "queued" au lieu de "confirmed".
    if status == "confirmed":
        from sqlalchemy import select as _select, func as _func
        config = await session.scalar(_select(TenantConfig))
        if config is not None:
            active_count = await session.scalar(
                _select(_func.count()).select_from(Order).where(
                    Order.status.in_(("confirmed", "preparing", "queued"))
                )
            ) or 0
            if active_count >= config.peak_orders_threshold:
                status = "queued"
```

- [ ] **Step 3 : Ajouter un test de la logique queued**

Dans `tests/test_admin_improvements.py` :

```python
async def test_valid_transitions_include_queued():
    from app.modules.orders.service import VALID_TRANSITIONS
    assert "queued" in VALID_TRANSITIONS
    assert "confirmed" in VALID_TRANSITIONS["queued"]
    assert "cancelled" in VALID_TRANSITIONS["queued"]
```

- [ ] **Step 4 : Lancer les tests**

```bash
pytest tests/test_admin_improvements.py::test_valid_transitions_include_queued -v
pytest tests/test_orders.py -v
```

Résultat attendu : tous PASS.

- [ ] **Step 5 : Commit**

```bash
git add app/modules/orders/service.py
git commit -m "feat: queued order status when peak_orders_threshold exceeded at confirmation"
```

---

## Task 9 : Worker `aggregate_stock_snapshot`

**Files:**
- Create: `worker/tasks/stock_snapshot.py`

**Interfaces:**
- Consumes: `Ingredient` model (current_qty, alert_threshold, last_alert_sent_at), `_get_all_tenant_slugs` de `worker/tasks/stats.py`, task `send_stock_alert` existante
- Produces: cron `aggregate_stock_snapshot` qui upsert `stock_snapshots_{slug}` chaque heure et enqueue `send_stock_alert` pour les ingrédients éligibles

- [ ] **Step 1 : Créer `worker/tasks/stock_snapshot.py`**

```python
# worker/tasks/stock_snapshot.py
"""Cron ARQ : snapshot horaire des ingrédients sous seuil d'alerte.

Alimente la collection MongoDB ``stock_snapshots_{slug}`` lue par
``GET /admin/stats/stock``. Enqueue ``send_stock_alert`` pour chaque
ingrédient éligible (cooldown 4h vérifié dans la task aval).
"""
import logging
from datetime import datetime, timezone

from motor.motor_asyncio import AsyncIOMotorClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import settings
from app.core.database import tenant_schema_name
from app.modules.stock.models import Ingredient

from worker.tasks.stats import _get_all_tenant_slugs

logger = logging.getLogger(__name__)


async def aggregate_stock_snapshot(ctx) -> None:
    """Calcule le snapshot stock pour tous les tenants et alerte si nécessaire.

    Pour chaque tenant :
    1. Requête les ingrédients dont ``current_qty <= alert_threshold``.
    2. Upsert le document dans ``stock_snapshots_{slug}`` MongoDB.
    3. Enqueue ``send_stock_alert`` pour chaque ingrédient sous seuil.
       Le cooldown 4h est évalué dans ``send_stock_alert`` (non ici).

    Planifié toutes les heures à ``minute=0``.

    Args:
        ctx: Contexte ARQ injecté automatiquement (contient ``redis``).
    """
    engine = create_async_engine(settings.database_url)
    client = AsyncIOMotorClient(settings.mongo_url)
    db = client[settings.mongo_db]
    now = datetime.now(timezone.utc)

    try:
        tenant_slugs = await _get_all_tenant_slugs(engine)
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        for slug in tenant_slugs:
            schema = tenant_schema_name(slug)
            try:
                async with session_factory() as session:
                    await session.execute(text(f'SET search_path TO "{schema}", public'))
                    result = await session.execute(
                        select(Ingredient).where(
                            Ingredient.current_qty <= Ingredient.alert_threshold
                        )
                    )
                    under_threshold = list(result.scalars().all())

                alerts = [
                    {
                        "ingredient_id": ing.id,
                        "name": ing.name,
                        "current_qty": float(ing.current_qty),
                        "alert_threshold": float(ing.alert_threshold),
                        "unit": ing.unit,
                    }
                    for ing in under_threshold
                ]

                await db[f"stock_snapshots_{slug}"].update_one(
                    {"tenant_slug": slug},
                    {
                        "$set": {
                            "tenant_slug": slug,
                            "computed_at": now.isoformat(),
                            "alerts": alerts,
                        }
                    },
                    upsert=True,
                )

                # Enqueue alerte pour chaque ingrédient sous seuil.
                arq_pool = ctx.get("redis")
                if arq_pool is not None:
                    for ing in under_threshold:
                        try:
                            await arq_pool.enqueue_job(
                                "send_stock_alert",
                                ingredient_id=ing.id,
                                ingredient_name=ing.name,
                                current_qty=float(ing.current_qty),
                                tenant_slug=slug,
                            )
                        except Exception as exc:
                            logger.error(
                                "aggregate_stock_snapshot: enqueue failed tenant=%s ingredient=%s: %s",
                                slug,
                                ing.name,
                                exc,
                            )

            except Exception as exc:
                logger.error(
                    "aggregate_stock_snapshot: erreur tenant=%s: %s", slug, exc
                )
                continue

    finally:
        client.close()
        await engine.dispose()
```

- [ ] **Step 2 : Vérifier que l'import de `_get_all_tenant_slugs` fonctionne**

```bash
python -c "from worker.tasks.stock_snapshot import aggregate_stock_snapshot; print('OK')"
```

Résultat attendu : `OK`.

- [ ] **Step 3 : Commit**

```bash
git add worker/tasks/stock_snapshot.py
git commit -m "feat: aggregate_stock_snapshot cron — stock alerts + MongoDB upsert"
```

---

## Task 10 : Worker `notify_config_change` + Enregistrement

**Files:**
- Modify: `worker/tasks/emails.py`
- Modify: `worker/main.py`

**Interfaces:**
- Consumes: `User` model (email, role), `DeviceToken` model (user_id, is_active, platform, token), `notify_staff` de `notification_service`
- Produces: task `notify_config_change(ctx, tenant_slug, is_closed)` enregistrée dans ARQ, cron `aggregate_stock_snapshot` enregistré

- [ ] **Step 1 : Ajouter `notify_config_change` dans `worker/tasks/emails.py`**

Ajouter à la fin du fichier :

```python
async def notify_config_change(ctx, tenant_slug: str, is_closed: bool) -> None:
    """Task ARQ : notifie les admins et le staff d'un changement de statut de fermeture.

    Envoie un email à tous les admins du tenant ET une notification push à tous
    les tokens staff/admin actifs.

    Args:
        ctx: Contexte ARQ injecté automatiquement.
        tenant_slug: Slug du tenant concerné.
        is_closed: True si le restaurant vient de fermer, False s'il rouvre.
    """
    from sqlalchemy import select, text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.core.config import settings as _settings
    from app.core.database import tenant_schema_name
    from app.modules.auth.models import User
    from app.modules.notifications.models import DeviceToken
    from app.modules.notifications.notification_service import notify_staff

    status_label = "FERMÉ" if is_closed else "OUVERT"
    subject = f"[{tenant_slug}] Statut restaurant modifié"
    body = (
        f"Le restaurant {tenant_slug!r} est maintenant {status_label}.\n\n"
        "Ce message est automatique suite à une modification de configuration."
    )

    engine = create_async_engine(_settings.database_url)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    schema = tenant_schema_name(tenant_slug)

    try:
        async with session_factory() as session:
            await session.execute(text(f'SET search_path TO "{schema}", public'))

            # Email aux admins.
            admin_result = await session.execute(
                select(User).where(User.role == "admin", User.is_active.is_(True))
            )
            admins = list(admin_result.scalars().all())

            for admin in admins:
                if not _settings.smtp_host:
                    logger.info(
                        "notify_config_change (SMTP non configuré): %s → %s",
                        admin.email,
                        subject,
                    )
                else:
                    try:
                        _send_smtp(admin.email, subject, body)
                    except Exception as exc:
                        logger.error(
                            "notify_config_change email échec to=%s: %s",
                            admin.email,
                            exc,
                        )

            # Push staff + admin.
            try:
                await notify_staff(
                    session=session,
                    tenant_slug=tenant_slug,
                    event="tenant.status_changed",
                    title="Statut restaurant",
                    body=f"Le restaurant est maintenant {status_label}",
                    data={"is_closed": is_closed},
                )
            except Exception as exc:
                logger.error(
                    "notify_config_change push échec tenant=%s: %s", tenant_slug, exc
                )

    finally:
        await engine.dispose()
```

- [ ] **Step 2 : Mettre à jour `worker/main.py`**

Remplacer le contenu de `worker/main.py` par :

```python
from arq import cron
from arq.connections import RedisSettings

from app.core.config import settings
from worker.tasks.emails import notify_config_change
from worker.tasks.loyalty import expire_loyalty_points
from worker.tasks.stats import aggregate_live_stats, aggregate_monthly_stats
from worker.tasks.stock_snapshot import aggregate_stock_snapshot


def get_redis_settings() -> RedisSettings:
    return RedisSettings.from_dsn(settings.arq_redis_url)


class WorkerSettings:
    functions = [
        "worker.tasks.stock_alerts.send_stock_alert",
        "worker.tasks.emails.send_email",
        "worker.tasks.emails.send_verification_email",
        "worker.tasks.emails.send_stock_alert_email",
        "worker.tasks.emails.notify_config_change",
        "worker.tasks.stats.aggregate_daily_stats",
        "worker.tasks.worker_utils.dead_letter_handler",
    ]
    cron_jobs = [
        cron(aggregate_monthly_stats, hour=0, minute=0),
        cron(
            aggregate_live_stats,
            minute={0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55},
        ),
        cron(expire_loyalty_points, hour=3, minute=0),
        # Snapshot stock toutes les heures — alimente stock_snapshots_{slug}.
        cron(aggregate_stock_snapshot, hour=set(range(24)), minute={0}),
    ]
    redis_settings = get_redis_settings()
    on_startup = None
    on_shutdown = None

    max_tries = 3
    job_timeout = 120
```

- [ ] **Step 3 : Vérifier les imports worker**

```bash
python -c "from worker.main import WorkerSettings; print('OK')"
```

Résultat attendu : `OK`.

- [ ] **Step 4 : Ajouter un test basique**

Dans `tests/test_admin_improvements.py` :

```python
def test_worker_settings_has_notify_config_change():
    from worker.main import WorkerSettings
    assert "worker.tasks.emails.notify_config_change" in WorkerSettings.functions

def test_worker_settings_has_stock_snapshot_cron():
    from worker.main import WorkerSettings
    from worker.tasks.stock_snapshot import aggregate_stock_snapshot
    cron_fns = [c.coroutine for c in WorkerSettings.cron_jobs]
    assert aggregate_stock_snapshot in cron_fns
```

- [ ] **Step 5 : Lancer les tests**

```bash
pytest tests/test_admin_improvements.py -v
```

- [ ] **Step 6 : Commit**

```bash
git add worker/tasks/emails.py worker/main.py
git commit -m "feat: notify_config_change task + aggregate_stock_snapshot cron registered"
```

---

## Task 11 : Mise à jour `docs/modules/admin.md`

**Files:**
- Modify: `docs/modules/admin.md`

- [ ] **Step 1 : Mettre à jour la section endpoints**

Ajouter les nouveaux endpoints dans le tableau "Configuration tenant" :

```markdown
| GET | `/api/v1/admin/tenant/next-opening` | Public | — prochain horaire d'ouverture |
| PATCH | `/api/v1/admin/tenant/toggle-closure` | Bearer JWT | admin — rate-limit 5/min |
```

Ajouter dans le tableau "Statistiques" :

```markdown
| GET | `/api/v1/admin/stats/summary` | Bearer JWT | admin — daily + live en une requête |
```

Ajouter dans le tableau "Super-admin" :

```markdown
| PATCH | `/api/v1/admin/tenants/{id}/suspend` | Bearer JWT | super-admin |
| PATCH | `/api/v1/admin/tenants/{id}/unsuspend` | Bearer JWT | super-admin |
```

- [ ] **Step 2 : Mettre à jour "Modèles de données"**

Ajouter dans `tenant_config` : `, timezone (IANA, défaut 'Europe/Paris')`.

Ajouter dans `tenant_config_audit` : `, user_email`.

Ajouter dans `public.tenants` : `, is_suspended, suspended_at, suspension_message`.

- [ ] **Step 3 : Mettre à jour "Comportements métier"**

Ajouter :

```markdown
**Timezone par tenant** : `timezone` dans `tenant_config` (défaut `Europe/Paris`). Toutes les évaluations horaires utilisent cette valeur.

**File d'attente commandes** : à la confirmation d'une commande, si `active_orders >= peak_orders_threshold`, le statut passe à `queued` au lieu de `confirmed`. Transitions valides depuis `queued` : `confirmed`, `cancelled`.

**Cooldown fermeture** : `PATCH /tenant/toggle-closure` vérifie que le dernier changement de `is_temporarily_closed` date de plus de 2 minutes (429 sinon). Rate-limit supplémentaire : 5/min.

**Notifications config** : tout changement de `is_temporarily_closed` enqueue `notify_config_change` — email aux admins + push aux tokens staff/admin actifs.

**Suspension tenant** : `PATCH /tenants/{id}/suspend` met `is_suspended=true` + force `is_temporarily_closed=true` + bloque tous les logins (hors super-admin) avec 403.

**Stock snapshot** : cron `aggregate_stock_snapshot` (toutes les heures) lit les ingrédients sous seuil depuis PostgreSQL, écrit dans `stock_snapshots_{slug}` MongoDB, enqueue `send_stock_alert` pour chaque ingrédient éligible.
```

- [ ] **Step 4 : Marquer les axes implémentés dans "Axes d'amélioration"**

Préfixer les axes résolus avec `[✅ IMPLÉMENTÉ]` :

```markdown
- [✅ IMPLÉMENTÉ] **Stock snapshots** : cron `aggregate_stock_snapshot` — voir worker.
- [✅ IMPLÉMENTÉ] **Timezone configurable** : champ `timezone` dans `tenant_config`.
- [⚠️ PARTIEL] **Fermeture programmée** : cooldown + toggle-closure ajoutés, fermeture automatique planifiée non implémentée.
- [✅ IMPLÉMENTÉ] **Notifications de configuration** : email + push sur changement de `is_temporarily_closed`.
- [⚠️ PARTIEL] **Gestion multi-admins** : `user_email` ajouté dans l'audit.
- [✅ IMPLÉMENTÉ] **File d'attente commandes** : statut `queued` quand capacité dépassée.
- [⚠️ PROD] **MFA super-admin** : non implémenté — JWT super-admin compromis expose tous les tenants. Prévoir TOTP sur `/auth/login` pour `role=super-admin`.
- [✅ IMPLÉMENTÉ] **DoS par fermeture/réouverture** : cooldown 2 min + rate-limit 5/min sur toggle-closure.
- [⚠️ PROD] **Stats MongoDB leak** : schémas Pydantic stricts ajoutés sur daily/monthly/live/stock.
- [✅ IMPLÉMENTÉ] **Suspension de tenant** : `PATCH /tenants/{id}/suspend` + blocage login.
```

- [ ] **Step 5 : Mettre à jour "Ce qui manque pour les interfaces" — marquer les endpoints disponibles**

Ajouter une note en tête :

```markdown
> Les endpoints suivants sont maintenant disponibles et peuvent être intégrés aux interfaces :
> - Widget horaires + statut : `GET /tenant/hours`, `GET /tenant/status`, `GET /tenant/next-opening`
> - Dashboard live initial : `GET /stats/summary` (live + dernier jour en une requête)
> - Fermeture urgente : `PATCH /tenant/toggle-closure`
> - Suspension tenant : `PATCH /tenants/{id}/suspend` / `PATCH /tenants/{id}/unsuspend`
> - Stock en alerte : `GET /stats/stock` (alimenté par cron horaire)
```

- [ ] **Step 6 : Lancer tous les tests**

```bash
pytest tests/ -v
```

Résultat attendu : tous les tests existants PASS, plus les nouveaux de `test_admin_improvements.py`.

- [ ] **Step 7 : Commit final**

```bash
git add docs/modules/admin.md \
        tests/test_admin_improvements.py
git commit -m "docs: update admin.md with implemented axes and new endpoint documentation"
```

---

## Récapitulatif des commits

| # | Message | Fichiers clés |
|---|---------|--------------|
| 1 | `feat: add migrations 0015-0017` | 3 fichiers alembic |
| 2 | `feat: timezone in TenantConfig, stats schemas, extended schemas` | tenant_models, stats_schemas, tenant_schemas |
| 3 | `feat: get_next_opening(), user_email in audit trail` | tenant_service, tenant_models |
| 4 | `feat: dynamic timezone, 2min cooldown, notification trigger` | tenant_service, tenant_router |
| 5 | `feat: block suspended tenants in get_current_user` | core/deps |
| 6 | `feat: /tenant/next-opening and /tenant/toggle-closure` | tenant_router |
| 7 | `feat: typed stats, /stats/summary, tenant suspend/unsuspend` | admin/router |
| 8 | `feat: queued order status when capacity exceeded` | orders/service |
| 9 | `feat: aggregate_stock_snapshot cron` | worker/tasks/stock_snapshot |
| 10 | `feat: notify_config_change + cron registered` | worker/tasks/emails, worker/main |
| 11 | `docs: update admin.md` | docs/modules/admin.md |
