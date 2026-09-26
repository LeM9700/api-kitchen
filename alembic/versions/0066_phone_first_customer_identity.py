"""Phone-first customer identity

Revision ID: 0066
Revises: 0065
Create Date: 2026-09-25
"""

from alembic import op
import sqlalchemy as sa

revision = "0066"
down_revision = "0065"
branch_labels = None
depends_on = None


def _get_tenant_slugs(bind) -> list[str]:
    result = bind.execute(sa.text("SELECT slug FROM public.tenants"))
    return [row[0] for row in result]


def upgrade() -> None:
    bind = op.get_bind()
    for slug in _get_tenant_slugs(bind):
        schema = f"tenant_{slug}"
        op.alter_column("users", "email", existing_type=sa.String(255), nullable=True, schema=schema)
        op.add_column("users", sa.Column("phone_e164", sa.String(20), nullable=True), schema=schema)
        op.add_column("users", sa.Column("phone_verified_at", sa.DateTime(timezone=True), nullable=True), schema=schema)
        op.add_column("users", sa.Column("phone_otp_hash", sa.String(255), nullable=True), schema=schema)
        op.add_column("users", sa.Column("phone_otp_expires_at", sa.DateTime(timezone=True), nullable=True), schema=schema)
        op.add_column(
            "users",
            sa.Column("phone_otp_attempts", sa.Integer(), server_default="0", nullable=False),
            schema=schema,
        )
        op.add_column(
            "users",
            sa.Column("pending_profile_completion", sa.Boolean(), server_default="false", nullable=False),
            schema=schema,
        )
        op.create_index("ix_users_phone_e164", "users", ["phone_e164"], unique=True, schema=schema)


def downgrade() -> None:
    bind = op.get_bind()
    for slug in _get_tenant_slugs(bind):
        schema = f"tenant_{slug}"
        op.drop_index("ix_users_phone_e164", table_name="users", schema=schema)
        op.drop_column("users", "pending_profile_completion", schema=schema)
        op.drop_column("users", "phone_otp_attempts", schema=schema)
        op.drop_column("users", "phone_otp_expires_at", schema=schema)
        op.drop_column("users", "phone_otp_hash", schema=schema)
        op.drop_column("users", "phone_verified_at", schema=schema)
        op.drop_column("users", "phone_e164", schema=schema)
        op.alter_column("users", "email", existing_type=sa.String(255), nullable=False, schema=schema)
