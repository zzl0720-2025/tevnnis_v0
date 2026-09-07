"""SQLAlchemy ORM models for the §7 TEVNNIS tables.

Unique constraints per design:
  events      — event_id, news_id (nullable; Postgres allows multiple NULLs)
  orders      — client_order_id
  fills       — broker_fill_id
  positions   — symbol

Two dialect variants keep the schema portable so the full pipeline can be
tested against in-memory sqlite with no Docker Postgres (Postgres DDL is
unchanged by both):

  BigId  — BIGINT on Postgres, INTEGER on sqlite. sqlite's rowid-alias
           autoincrement only recognises a bare INTEGER primary key.
  Json   — JSONB on Postgres, sqlite's JSON (TEXT-backed) elsewhere.
"""

from __future__ import annotations

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Column,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    pass


def BigId() -> BigInteger:  # noqa: N802 — a type factory, named like the type it makes
    """BIGINT on Postgres, INTEGER on sqlite (see the module docstring)."""
    return BigInteger().with_variant(Integer, "sqlite")


def Json() -> JSONB:  # noqa: N802 — ditto
    """JSONB on Postgres, JSON on sqlite."""
    return JSONB().with_variant(JSON, "sqlite")


class Event(Base):
    __tablename__ = "events"

    id        = Column(BigId(), primary_key=True, autoincrement=True)
    event_id  = Column(String(128), nullable=False, unique=True)
    news_id   = Column(String(128), nullable=True, unique=True)
    type      = Column(String(32), nullable=False)
    symbol    = Column(String(32), nullable=True)
    sector    = Column(String(64), nullable=True)
    priority  = Column(String(16), nullable=False)
    event_ts  = Column(BigInteger, nullable=False)
    ingest_ts = Column(BigInteger, nullable=False)
    payload   = Column(Json(), nullable=False, default=dict)


class Decision(Base):
    __tablename__ = "decisions"

    decision_id  = Column(String(128), primary_key=True)
    ts           = Column(TIMESTAMP(timezone=True), nullable=False, server_default=func.now())
    model_used   = Column(String(128), nullable=True)
    tokens_in    = Column(Integer, nullable=True)
    tokens_out   = Column(Integer, nullable=True)
    gate_result  = Column(String(64), nullable=False)
    session_note = Column(Text, nullable=True)

    instructions = relationship("Instruction", back_populates="decision")


class Instruction(Base):
    __tablename__ = "instructions"

    id              = Column(BigId(), primary_key=True, autoincrement=True)
    decision_id     = Column(String(128), ForeignKey("decisions.decision_id"), nullable=False)
    action          = Column(String(8), nullable=False)
    symbol          = Column(String(32), nullable=True)
    order_type      = Column(String(16), nullable=True)
    quantity        = Column(BigInteger, nullable=True)
    limit_price     = Column(Float, nullable=True)
    valid_seconds   = Column(Integer, nullable=True)
    confidence      = Column(Float, nullable=True)
    thesis          = Column(Text, nullable=True)
    cited_event_ids = Column(Json(), nullable=False, default=list)

    decision   = relationship("Decision", back_populates="instructions")
    orders     = relationship("Order", back_populates="instruction")
    risk_audit = relationship("RiskAudit", back_populates="instruction")


class Order(Base):
    __tablename__ = "orders"

    id              = Column(BigId(), primary_key=True, autoincrement=True)
    client_order_id = Column(String(128), nullable=False, unique=True)
    broker_order_id = Column(String(128), nullable=True)
    instruction_id  = Column(BigId(), ForeignKey("instructions.id"), nullable=True)
    status          = Column(String(32), nullable=False)
    ts              = Column(TIMESTAMP(timezone=True), nullable=False, server_default=func.now())

    instruction = relationship("Instruction", back_populates="orders")
    fills       = relationship("Fill", back_populates="order")


class Fill(Base):
    __tablename__ = "fills"

    id             = Column(BigId(), primary_key=True, autoincrement=True)
    order_id       = Column(BigId(), ForeignKey("orders.id"), nullable=False)
    broker_fill_id = Column(String(128), nullable=False, unique=True)
    quantity       = Column(BigInteger, nullable=False)
    price          = Column(Float, nullable=False)
    fee            = Column(Float, nullable=False, default=0.0)
    ts             = Column(TIMESTAMP(timezone=True), nullable=False, server_default=func.now())

    order = relationship("Order", back_populates="fills")


class Position(Base):
    __tablename__ = "positions"

    id           = Column(BigId(), primary_key=True, autoincrement=True)
    symbol       = Column(String(32), nullable=False, unique=True)
    quantity     = Column(BigInteger, nullable=False, default=0)
    cost_basis   = Column(Float, nullable=False, default=0.0)
    last_updated = Column(TIMESTAMP(timezone=True), nullable=False, server_default=func.now())


class RiskAudit(Base):
    __tablename__ = "risk_audit"

    id             = Column(BigId(), primary_key=True, autoincrement=True)
    instruction_id = Column(BigId(), ForeignKey("instructions.id"), nullable=False)
    allow          = Column(Boolean, nullable=False)
    rule_tripped   = Column(String(128), nullable=True)
    ts             = Column(TIMESTAMP(timezone=True), nullable=False, server_default=func.now())

    instruction = relationship("Instruction", back_populates="risk_audit")


class ApiUsage(Base):
    __tablename__ = "api_usage"

    id         = Column(BigId(), primary_key=True, autoincrement=True)
    ts         = Column(TIMESTAMP(timezone=True), nullable=False, server_default=func.now())
    kind       = Column(String(16), nullable=False)   # "llm" | "broker"
    provider   = Column(String(64), nullable=True)
    tokens_in  = Column(Integer, nullable=True)
    tokens_out = Column(Integer, nullable=True)
    cost       = Column(Float, nullable=True)
    call_count = Column(Integer, nullable=False, default=1)


class PortfolioSample(Base):
    """One point on the public snapshot's equity curve.

    §7's eight audit tables record *what happened*; nothing in them records
    *what the portfolio was worth at a moment in time*, so an equity curve
    cannot be reconstructed from them (unrealized drift leaves no row).
    core samples one of these per decision round.

    THESE ARE PRIVATE DOLLARS AND MUST STAY THAT WAY. Only the ratio
    `100 * equity / equity_at_window_start` ever leaves the process — see
    tevnnis_core/snapshot.py, which is the sole reader for publication.
    """

    __tablename__ = "portfolio_samples"
    # Every snapshot query is "samples in a time window, oldest first". Named
    # explicitly so alembic 0003 and this model create the same index.
    __table_args__ = (Index("ix_portfolio_samples_ts", "ts"),)

    id     = Column(BigId(), primary_key=True, autoincrement=True)
    ts     = Column(TIMESTAMP(timezone=True), nullable=False, server_default=func.now())
    equity = Column(Float, nullable=False)
    cash   = Column(Float, nullable=False, default=0.0)


class PriceSample(Base):
    """One observed price per symbol per round for position sparklines.

    Deliberately separate from `events`: a QUOTE_MOVE event only exists when
    md's §4.4 throttling decided the move was worth waking core for (0–3 per
    symbol per day by design), which is far too sparse to draw a trend from.
    This is the unthrottled sample, taken on core's own cadence.

    Public market prices, so unlike PortfolioSample these values may be
    published as-is.
    """

    __tablename__ = "price_samples"
    # The sparkline query is "last N samples for one symbol, newest first".
    __table_args__ = (Index("ix_price_samples_symbol_ts", "symbol", "ts"),)

    id         = Column(BigId(), primary_key=True, autoincrement=True)
    ts         = Column(TIMESTAMP(timezone=True), nullable=False, server_default=func.now())
    symbol     = Column(String(32), nullable=False)
    last       = Column(Float, nullable=False)
    change_pct = Column(Float, nullable=True)


class MdCursor(Base):
    """The §4.3 `next_cursor` from md, persisted so a core restart cannot replay events.

    Single logical row (`id="default"`). md never reads or writes this table —
    core remains the only DB writer (§7); this is core's own memory of how far
    it has consumed the md stream. §7 lists no table for it, so it is kept
    deliberately minimal and outside the eight audit tables.
    """

    __tablename__ = "md_cursor"

    id         = Column(String(32), primary_key=True)
    cursor     = Column(String(256), nullable=False, default="")
    updated_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=func.now())


DEFAULT_CURSOR_ID = "default"
