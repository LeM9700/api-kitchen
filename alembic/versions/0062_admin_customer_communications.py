"""Admin customers, communications and marketing consent

Revision ID: 0062
Revises: 0061
Create Date: 2026-09-16
"""

from alembic import op
import sqlalchemy as sa

revision = "0062"
down_revision = "0061"
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
            "users",
            sa.Column("marketing_email_opt_in", sa.Boolean(), nullable=False, server_default=sa.text("false")),
            schema=schema,
        )
        op.add_column(
            "users",
            sa.Column("marketing_push_opt_in", sa.Boolean(), nullable=False, server_default=sa.text("false")),
            schema=schema,
        )
        op.create_table(
            "customer_communications",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("channel", sa.String(16), nullable=False),
            sa.Column("message_type", sa.String(32), nullable=False),
            sa.Column("template_key", sa.String(64), nullable=True),
            sa.Column("subject", sa.String(255), nullable=True),
            sa.Column("body", sa.Text(), nullable=False),
            sa.Column("status", sa.String(32), nullable=False, server_default="queued"),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("sent_by_user_id", sa.Integer(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
            sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
            schema=schema,
        )
        op.create_index(
            "ix_customer_communications_user_created",
            "customer_communications",
            ["user_id", "created_at"],
            schema=schema,
        )
        op.create_index("ix_customer_communications_channel", "customer_communications", ["channel"], schema=schema)
        op.create_index(
            "ix_customer_communications_message_type",
            "customer_communications",
            ["message_type"],
            schema=schema,
        )
        op.create_table(
            "admin_audit_logs",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("actor_user_id", sa.Integer(), nullable=True),
            sa.Column("actor_email", sa.String(255), nullable=True),
            sa.Column("action", sa.String(64), nullable=False),
            sa.Column("target_type", sa.String(64), nullable=False),
            sa.Column("target_id", sa.String(128), nullable=False),
            sa.Column("metadata", sa.JSON(), nullable=True),
            sa.Column("ip_address", sa.String(45), nullable=True),
            sa.Column("user_agent", sa.String(512), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
            schema=schema,
        )
        op.create_index("ix_admin_audit_logs_created_at", "admin_audit_logs", ["created_at"], schema=schema)
        op.create_index("ix_admin_audit_logs_actor", "admin_audit_logs", ["actor_user_id"], schema=schema)
        op.create_index("ix_admin_audit_logs_target", "admin_audit_logs", ["target_type", "target_id"], schema=schema)
        op.create_index("ix_admin_audit_logs_action", "admin_audit_logs", ["action"], schema=schema)


def downgrade() -> None:
    bind = op.get_bind()
    for slug in _get_tenant_slugs(bind):
        schema = f"tenant_{slug}"
        op.drop_index("ix_admin_audit_logs_action", table_name="admin_audit_logs", schema=schema)
        op.drop_index("ix_admin_audit_logs_target", table_name="admin_audit_logs", schema=schema)
        op.drop_index("ix_admin_audit_logs_actor", table_name="admin_audit_logs", schema=schema)
        op.drop_index("ix_admin_audit_logs_created_at", table_name="admin_audit_logs", schema=schema)
        op.drop_table("admin_audit_logs", schema=schema)
        op.drop_index(
            "ix_customer_communications_message_type",
            table_name="customer_communications",
            schema=schema,
        )
        op.drop_index("ix_customer_communications_channel", table_name="customer_communications", schema=schema)
        op.drop_index(
            "ix_customer_communications_user_created",
            table_name="customer_communications",
            schema=schema,
        )
        op.drop_table("customer_communications", schema=schema)
        op.drop_column("users", "marketing_push_opt_in", schema=schema)
        op.drop_column("users", "marketing_email_opt_in", schema=schema)
