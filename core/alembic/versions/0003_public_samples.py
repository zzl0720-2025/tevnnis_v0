"""Add portfolio_samples + price_samples — the time series behind the public snapshot.

§7's eight audit tables record what *happened*; neither records what the
portfolio was *worth* at a moment in time, nor what a symbol traded at on a
round where md's §4.4 throttling emitted no QUOTE_MOVE event. Without these the
public snapshot's equity curve and per-position sparklines cannot be built from
real data at all.

Both are written by core only (§7: single writer). `portfolio_samples` holds
absolute dollars and is PRIVATE — tevnnis_core/snapshot.py publishes only the
ratio `100 * equity / equity_at_window_start`. `price_samples` holds public
market prices and may be published as-is.

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-06
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import TIMESTAMP

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "portfolio_samples",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "ts", TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
        sa.Column("equity", sa.Float(), nullable=False),
        sa.Column("cash", sa.Float(), nullable=False, server_default="0"),
    )
    # Every snapshot query is "samples in a time window, oldest first".
    op.create_index("ix_portfolio_samples_ts", "portfolio_samples", ["ts"])

    op.create_table(
        "price_samples",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "ts", TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("last", sa.Float(), nullable=False),
        sa.Column("change_pct", sa.Float(), nullable=True),
    )
    # The sparkline query is "last N samples for one symbol, newest first".
    op.create_index("ix_price_samples_symbol_ts", "price_samples", ["symbol", "ts"])


def downgrade() -> None:
    op.drop_index("ix_price_samples_symbol_ts", table_name="price_samples")
    op.drop_table("price_samples")
    op.drop_index("ix_portfolio_samples_ts", table_name="portfolio_samples")
    op.drop_table("portfolio_samples")
