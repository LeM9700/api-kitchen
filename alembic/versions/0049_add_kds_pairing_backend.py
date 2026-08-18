"""Add persistent KDS screen pairing backend

Revision ID: 0049
Revises: 0048
Create Date: 2026-08-18
"""

import sqlalchemy as sa
from alembic import op

revision = "0049"
down_revision = "0048"
branch_labels = None
depends_on = None


def _get_tenant_slugs(bind) -> list[str]:
    result = bind.execute(sa.text("SELECT slug FROM public.tenants"))
    return [row[0] for row in result]


def _table_exists(bind, schema: str, table_name: str) -> bool:
    return bool(
        bind.execute(
            sa.text(
                """SELECT EXISTS (
                   SELECT 1 FROM information_schema.tables
                   WHERE table_schema = :schema AND table_name = :table_name
                )"""
            ),
            {"schema": schema, "table_name": table_name},
        ).scalar()
    )


def _index_exists(bind, schema: str, index_name: str) -> bool:
    return bool(
        bind.execute(
            sa.text(
                """SELECT EXISTS (
                   SELECT 1 FROM pg_indexes
                   WHERE schemaname = :schema AND indexname = :index_name
                )"""
            ),
            {"schema": schema, "index_name": index_name},
        ).scalar()
    )


def upgrade() -> None:
    bind = op.get_bind()
    for slug in _get_tenant_slugs(bind):
        schema = f"tenant_{slug}"

        if not _table_exists(bind, schema, "kds_screens"):
            op.create_table(
                "kds_screens",
                sa.Column("id", sa.Integer, primary_key=True),
                sa.Column("name", sa.String(120), nullable=False),
                sa.Column("screen_key", sa.String(64), nullable=False),
                sa.Column("mode", sa.String(16), nullable=False, server_default="kitchen"),
                sa.Column("station", sa.String(64), nullable=False, server_default="kitchen"),
                sa.Column("interaction_mode", sa.String(16), nullable=False, server_default="wall"),
                sa.Column("tickets_per_page", sa.Integer, nullable=False, server_default="4"),
                sa.Column("is_active", sa.Boolean, nullable=False, server_default="true"),
                sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
                sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
                sa.UniqueConstraint("screen_key", name="uq_kds_screens_screen_key"),
                sa.CheckConstraint("mode IN ('kitchen', 'counter', 'service')", name="ck_kds_screens_mode"),
                sa.CheckConstraint(
                    "interaction_mode IN ('wall', 'touch')",
                    name="ck_kds_screens_interaction_mode",
                ),
                sa.CheckConstraint(
                    "tickets_per_page BETWEEN 1 AND 8",
                    name="ck_kds_screens_tickets_per_page",
                ),
                sa.CheckConstraint(
                    "length(btrim(station)) BETWEEN 1 AND 64",
                    name="ck_kds_screens_station_not_blank",
                ),
                schema=schema,
            )

        if not _table_exists(bind, schema, "kds_pairing_codes"):
            op.create_table(
                "kds_pairing_codes",
                sa.Column("id", sa.Integer, primary_key=True),
                sa.Column(
                    "screen_id",
                    sa.Integer,
                    sa.ForeignKey(f"{schema}.kds_screens.id", ondelete="CASCADE"),
                    nullable=False,
                ),
                sa.Column("code_hash", sa.String(64), nullable=False),
                sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
                sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
                sa.Column("created_by_user_id", sa.Integer, nullable=True),
                sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
                schema=schema,
            )
        if not _index_exists(bind, schema, "ix_kds_pairing_codes_code_hash"):
            op.create_index(
                "ix_kds_pairing_codes_code_hash",
                "kds_pairing_codes",
                ["code_hash"],
                schema=schema,
            )
        if not _index_exists(bind, schema, "ix_kds_pairing_codes_expires_at"):
            op.create_index(
                "ix_kds_pairing_codes_expires_at",
                "kds_pairing_codes",
                ["expires_at"],
                schema=schema,
            )
        if not _index_exists(bind, schema, "ix_kds_pairing_codes_screen_id"):
            op.create_index(
                "ix_kds_pairing_codes_screen_id",
                "kds_pairing_codes",
                ["screen_id"],
                schema=schema,
            )

        if not _table_exists(bind, schema, "kds_remote_sessions"):
            op.create_table(
                "kds_remote_sessions",
                sa.Column("id", sa.Integer, primary_key=True),
                sa.Column("session_token_hash", sa.String(64), nullable=False),
                sa.Column(
                    "screen_id",
                    sa.Integer,
                    sa.ForeignKey(f"{schema}.kds_screens.id", ondelete="CASCADE"),
                    nullable=False,
                ),
                sa.Column("paired_by_user_id", sa.Integer, nullable=True),
                sa.Column("device_label", sa.String(128), nullable=True),
                sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
                sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
                sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
                sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
                sa.UniqueConstraint(
                    "session_token_hash",
                    name="uq_kds_remote_sessions_session_token_hash",
                ),
                schema=schema,
            )
        if not _index_exists(bind, schema, "ix_kds_remote_sessions_screen_id"):
            op.create_index(
                "ix_kds_remote_sessions_screen_id",
                "kds_remote_sessions",
                ["screen_id"],
                schema=schema,
            )
        if not _index_exists(bind, schema, "ix_kds_remote_sessions_expires_at"):
            op.create_index(
                "ix_kds_remote_sessions_expires_at",
                "kds_remote_sessions",
                ["expires_at"],
                schema=schema,
            )


def downgrade() -> None:
    bind = op.get_bind()
    for slug in _get_tenant_slugs(bind):
        schema = f"tenant_{slug}"
        if _table_exists(bind, schema, "kds_remote_sessions"):
            if _index_exists(bind, schema, "ix_kds_remote_sessions_expires_at"):
                op.drop_index("ix_kds_remote_sessions_expires_at", table_name="kds_remote_sessions", schema=schema)
            if _index_exists(bind, schema, "ix_kds_remote_sessions_screen_id"):
                op.drop_index("ix_kds_remote_sessions_screen_id", table_name="kds_remote_sessions", schema=schema)
            op.drop_table("kds_remote_sessions", schema=schema)
        if _table_exists(bind, schema, "kds_pairing_codes"):
            if _index_exists(bind, schema, "ix_kds_pairing_codes_screen_id"):
                op.drop_index("ix_kds_pairing_codes_screen_id", table_name="kds_pairing_codes", schema=schema)
            if _index_exists(bind, schema, "ix_kds_pairing_codes_expires_at"):
                op.drop_index("ix_kds_pairing_codes_expires_at", table_name="kds_pairing_codes", schema=schema)
            if _index_exists(bind, schema, "ix_kds_pairing_codes_code_hash"):
                op.drop_index("ix_kds_pairing_codes_code_hash", table_name="kds_pairing_codes", schema=schema)
            op.drop_table("kds_pairing_codes", schema=schema)
        if _table_exists(bind, schema, "kds_screens"):
            op.drop_table("kds_screens", schema=schema)
