"""Create all TEVNNIS tables (§7).

Revision ID: 0001
Revises:
Create Date: 2026-09-01
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # events — ingested market events; audit + cited_event_ids target
    op.create_table(
        "events",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("event_id", sa.String(128), nullable=False),
        sa.Column("news_id", sa.String(128), nullable=True),
        sa.Column("type", sa.String(32), nullable=False),
        sa.Column("symbol", sa.String(32), nullable=True),
        sa.Column("sector", sa.String(64), nullable=True),
        sa.Column("priority", sa.String(16), nullable=False),
        sa.Column("event_ts", sa.BigInteger, nullable=False),
        sa.Column("ingest_ts", sa.BigInteger, nullable=False),
        sa.Column("payload", JSONB, nullable=False, server_default="{}"),
        sa.UniqueConstraint("event_id", name="uq_events_event_id"),
        # news_id is nullable; Postgres allows multiple NULLs in a UNIQUE constraint
        sa.UniqueConstraint("news_id", name="uq_events_news_id"),
    )

    # decisions — every decision including HOLDs
    op.create_table(
        "decisions",
        sa.Column("decision_id", sa.String(128), primary_key=True),
        sa.Column(
            "ts",
            TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("model_used", sa.String(128), nullable=True),
        sa.Column("tokens_in", sa.Integer, nullable=True),
        sa.Column("tokens_out", sa.Integer, nullable=True),
        sa.Column("gate_result", sa.String(64), nullable=False),
        sa.Column("session_note", sa.Text, nullable=True),
    )

    # instructions — one row per ProposedInstruction in a DecisionOutput
    op.create_table(
        "instructions",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column(
            "decision_id",
            sa.String(128),
            sa.ForeignKey("decisions.decision_id"),
            nullable=False,
        ),
        sa.Column("action", sa.String(8), nullable=False),
        sa.Column("symbol", sa.String(32), nullable=True),
        sa.Column("order_type", sa.String(16), nullable=True),
        sa.Column("quantity", sa.BigInteger, nullable=True),
        sa.Column("limit_price", sa.Float, nullable=True),
        sa.Column("valid_seconds", sa.Integer, nullable=True),
        sa.Column("confidence", sa.Float, nullable=True),
        sa.Column("thesis", sa.Text, nullable=True),
        sa.Column("cited_event_ids", JSONB, nullable=False, server_default="[]"),
    )

    # orders — one row per submitted order
    op.create_table(
        "orders",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("client_order_id", sa.String(128), nullable=False),
        sa.Column("broker_order_id", sa.String(128), nullable=True),
        sa.Column(
            "instruction_id",
            sa.BigInteger,
            sa.ForeignKey("instructions.id"),
            nullable=True,
        ),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column(
            "ts",
            TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("client_order_id", name="uq_orders_client_order_id"),
    )

    # fills — one row per broker fill event
    op.create_table(
        "fills",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("order_id", sa.BigInteger, sa.ForeignKey("orders.id"), nullable=False),
        sa.Column("broker_fill_id", sa.String(128), nullable=False),
        sa.Column("quantity", sa.BigInteger, nullable=False),
        sa.Column("price", sa.Float, nullable=False),
        sa.Column("fee", sa.Float, nullable=False, server_default="0"),
        sa.Column(
            "ts",
            TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("broker_fill_id", name="uq_fills_broker_fill_id"),
    )

    # positions — current holdings snapshot (broker is source of truth; DB is cache/audit)
    op.create_table(
        "positions",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("quantity", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("cost_basis", sa.Float, nullable=False, server_default="0"),
        sa.Column(
            "last_updated",
            TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("symbol", name="uq_positions_symbol"),
    )

    # risk_audit — one row per risk check
    op.create_table(
        "risk_audit",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column(
            "instruction_id",
            sa.BigInteger,
            sa.ForeignKey("instructions.id"),
            nullable=False,
        ),
        sa.Column("allow", sa.Boolean, nullable=False),
        sa.Column("rule_tripped", sa.String(128), nullable=True),
        sa.Column(
            "ts",
            TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    # api_usage — budget ledger
    op.create_table(
        "api_usage",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column(
            "ts",
            TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("provider", sa.String(64), nullable=True),
        sa.Column("tokens_in", sa.Integer, nullable=True),
        sa.Column("tokens_out", sa.Integer, nullable=True),
        sa.Column("cost", sa.Float, nullable=True),
        sa.Column("call_count", sa.Integer, nullable=False, server_default="1"),
    )


def downgrade() -> None:
    op.drop_table("api_usage")
    op.drop_table("risk_audit")
    op.drop_table("positions")
    op.drop_table("fills")
    op.drop_table("orders")
    op.drop_table("instructions")
    op.drop_table("decisions")
    op.drop_table("events")
