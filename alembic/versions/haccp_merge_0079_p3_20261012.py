"""Merge the dispatch settings and HACCP strict migration branches.

Revision ID: haccp_merge_0079_p3_20261012
Revises: 0079, haccp_p3_20261004
Create Date: 2026-10-12
"""

from collections.abc import Sequence

from alembic import op

revision: str = "haccp_merge_0079_p3_20261012"
down_revision: str | tuple[str, str] | None = ("0079", "haccp_p3_20261004")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
