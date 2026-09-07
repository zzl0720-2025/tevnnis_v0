"""Add md_cursor — core's persisted §4.3 next_cursor.

§4.3 requires the cursor to survive a *core* restart ("core persists
next_cursor") so a restart cannot replay events; §7's eight audit tables have
nowhere to hold it, so it gets its own minimal single-row table. md never
touches it — core remains the only DB writer (§7).

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-02
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import TIMESTAMP

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "md_cursor",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("cursor", sa.String(256), nullable=False, server_default=""),
        sa.Column(
            "updated_at",
            TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )


def downgrade() -> None:
    op.drop_table("md_cursor")
