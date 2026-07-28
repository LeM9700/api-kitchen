"""HR module: employees, establishments, shifts, time clock, alerts

Revision ID: 0041
Revises: 0040
Create Date: 2026-07-28
"""

import sqlalchemy as sa
from alembic import op

revision = "0041"
down_revision = "0040"
branch_labels = None
depends_on = None


def _get_tenant_slugs(bind) -> list[str]:
    result = bind.execute(sa.text("SELECT slug FROM public.tenants"))
    return [row[0] for row in result]


def upgrade() -> None:
    bind = op.get_bind()
    for slug in _get_tenant_slugs(bind):
        quoted = f'"tenant_{slug}"'

        bind.execute(
            sa.text(
                f"""
                CREATE TABLE IF NOT EXISTS {quoted}.establishments (
                    id SERIAL PRIMARY KEY,
                    name VARCHAR(128) NOT NULL,
                    timezone VARCHAR(64) NOT NULL DEFAULT 'Europe/Paris',
                    is_active BOOLEAN NOT NULL DEFAULT TRUE,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
        )
        bind.execute(
            sa.text(
                f"""
                INSERT INTO {quoted}.establishments (name, timezone)
                SELECT 'Établissement principal', 'Europe/Paris'
                WHERE NOT EXISTS (SELECT 1 FROM {quoted}.establishments)
                """
            )
        )
        bind.execute(
            sa.text(
                f"""
                CREATE TABLE IF NOT EXISTS {quoted}.employee_profiles (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    establishment_id INTEGER NOT NULL REFERENCES {quoted}.establishments(id),
                    hourly_rate_cents INTEGER,
                    weekly_hours_contract INTEGER NOT NULL DEFAULT 35,
                    hire_date DATE,
                    is_active BOOLEAN NOT NULL DEFAULT TRUE,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
        )
        bind.execute(
            sa.text(
                f"CREATE UNIQUE INDEX IF NOT EXISTS ix_employee_profiles_user_id "
                f"ON {quoted}.employee_profiles (user_id)"
            )
        )
        bind.execute(
            sa.text(
                f"""
                CREATE TABLE IF NOT EXISTS {quoted}.shifts (
                    id SERIAL PRIMARY KEY,
                    employee_id INTEGER NOT NULL REFERENCES {quoted}.employee_profiles(id),
                    establishment_id INTEGER NOT NULL REFERENCES {quoted}.establishments(id),
                    starts_at TIMESTAMPTZ NOT NULL,
                    ends_at TIMESTAMPTZ NOT NULL,
                    break_minutes INTEGER NOT NULL DEFAULT 0,
                    status VARCHAR(16) NOT NULL DEFAULT 'scheduled',
                    created_by_user_id INTEGER,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
        )
        bind.execute(
            sa.text(
                f"CREATE INDEX IF NOT EXISTS ix_shifts_employee_starts_at "
                f"ON {quoted}.shifts (employee_id, starts_at)"
            )
        )
        bind.execute(
            sa.text(
                f"""
                CREATE TABLE IF NOT EXISTS {quoted}.time_clock_entries (
                    id SERIAL PRIMARY KEY,
                    employee_id INTEGER NOT NULL REFERENCES {quoted}.employee_profiles(id),
                    shift_id INTEGER REFERENCES {quoted}.shifts(id),
                    establishment_id INTEGER NOT NULL REFERENCES {quoted}.establishments(id),
                    clock_in_at TIMESTAMPTZ NOT NULL,
                    clock_out_at TIMESTAMPTZ,
                    method VARCHAR(16) NOT NULL,
                    status VARCHAR(16) NOT NULL DEFAULT 'open',
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
        )
        bind.execute(
            sa.text(
                f"CREATE INDEX IF NOT EXISTS ix_time_clock_entries_employee_clock_in "
                f"ON {quoted}.time_clock_entries (employee_id, clock_in_at)"
            )
        )
        bind.execute(
            sa.text(
                f"""
                CREATE TABLE IF NOT EXISTS {quoted}.time_clock_corrections (
                    id SERIAL PRIMARY KEY,
                    entry_id INTEGER NOT NULL REFERENCES {quoted}.time_clock_entries(id),
                    corrected_by_user_id INTEGER NOT NULL,
                    old_clock_in_at TIMESTAMPTZ,
                    old_clock_out_at TIMESTAMPTZ,
                    new_clock_in_at TIMESTAMPTZ,
                    new_clock_out_at TIMESTAMPTZ,
                    reason TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
        )
        bind.execute(
            sa.text(
                f"""
                CREATE TABLE IF NOT EXISTS {quoted}.hr_alerts (
                    id SERIAL PRIMARY KEY,
                    employee_id INTEGER NOT NULL REFERENCES {quoted}.employee_profiles(id),
                    type VARCHAR(32) NOT NULL,
                    severity VARCHAR(16) NOT NULL DEFAULT 'warning',
                    payload JSON NOT NULL DEFAULT '{{}}',
                    triggered_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    resolved_at TIMESTAMPTZ,
                    last_alert_sent_at TIMESTAMPTZ
                )
                """
            )
        )
        bind.execute(
            sa.text(
                f"CREATE INDEX IF NOT EXISTS ix_hr_alerts_employee_type "
                f"ON {quoted}.hr_alerts (employee_id, type)"
            )
        )
        bind.execute(
            sa.text(
                f"""
                CREATE TABLE IF NOT EXISTS {quoted}.establishment_hr_config (
                    id SERIAL PRIMARY KEY,
                    establishment_id INTEGER NOT NULL UNIQUE REFERENCES {quoted}.establishments(id),
                    weekly_hours_legal_threshold INTEGER NOT NULL DEFAULT 35,
                    late_tolerance_minutes INTEGER NOT NULL DEFAULT 10,
                    alert_cooldown_hours INTEGER NOT NULL DEFAULT 4,
                    labor_cost_target_ratio NUMERIC(4,3) NOT NULL DEFAULT 0.30
                )
                """
            )
        )
        bind.execute(
            sa.text(
                f"""
                INSERT INTO {quoted}.establishment_hr_config (establishment_id)
                SELECT id FROM {quoted}.establishments
                WHERE id NOT IN (SELECT establishment_id FROM {quoted}.establishment_hr_config)
                """
            )
        )


def downgrade() -> None:
    bind = op.get_bind()
    for slug in _get_tenant_slugs(bind):
        quoted = f'"tenant_{slug}"'
        bind.execute(sa.text(f"DROP TABLE IF EXISTS {quoted}.establishment_hr_config"))
        bind.execute(sa.text(f"DROP TABLE IF EXISTS {quoted}.hr_alerts"))
        bind.execute(sa.text(f"DROP TABLE IF EXISTS {quoted}.time_clock_corrections"))
        bind.execute(sa.text(f"DROP TABLE IF EXISTS {quoted}.time_clock_entries"))
        bind.execute(sa.text(f"DROP TABLE IF EXISTS {quoted}.shifts"))
        bind.execute(sa.text(f"DROP TABLE IF EXISTS {quoted}.employee_profiles"))
        bind.execute(sa.text(f"DROP TABLE IF EXISTS {quoted}.establishments"))
