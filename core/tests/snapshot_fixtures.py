"""Seed data for the public-snapshot tests.

Deliberately hostile: every row carries the fields the snapshot must NEVER
publish (share quantities, cost basis, broker ids, fees, a tripped risk rule
name), so the deny-list test has something real to fail on rather than an
empty DB that would pass by accident.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from tevnnis_core.db.models import (
    ApiUsage,
    Decision,
    Event,
    Fill,
    Instruction,
    Order,
    PortfolioSample,
    Position,
    PriceSample,
    RiskAudit,
)

#: Mid-session on a Wednesday in America/New_York, matching conftest.MID_SESSION.
NOW = datetime(2026, 9, 2, 15, 0, tzinfo=timezone.utc)

# Values the snapshot must never disclose. Each is distinctive enough that a
# substring search over the serialized snapshot is a meaningful assertion.
SECRET_COST_BASIS = 187.6543
SECRET_QUANTITY = 731
SECRET_FEE = 3.7788
SECRET_BROKER_FILL_ID = "brokerfill-DEADBEEF-0001"
SECRET_BROKER_ORDER_ID = "brokerorder-DEADBEEF-0002"
SECRET_CLIENT_ORDER_ID = "client-DEADBEEF-0003"
SECRET_RULE_TRIPPED = "max_sector_pct"
SECRET_SECTOR = "Semiconductor"
SECRET_EQUITY = 10142.37
SECRET_CASH = 9876.54

NEWS_URL_WITH_TOKEN = "https://www.reuters.com/business/nvda-pt?token=SUPERSECRETTOKEN99&utm=x"
NEWS_URL_INTERNAL = "https://longbridge.com/news/n-002"
NEWS_URL_INSECURE = "http://insecure.example.com/n-003"

LEAKY_THESIS = (
    "Portfolio equity is $41,337.42 with 9876 in cash; sold 500 shares of NVDA "
    "because max_position_pct 0.25 was close. Account 1234567890 looks fine."
)


def seed(session: Session, *, now: datetime = NOW) -> None:
    """Populate every table the snapshot builder reads."""
    _seed_samples(session, now=now)
    _seed_positions(session)
    _seed_events(session, now=now)
    _seed_decisions(session, now=now)
    _seed_usage(session, now=now)
    session.commit()


def _seed_samples(session: Session, *, now: datetime) -> None:
    # An equity curve from 10000 (inception) to SECRET_EQUITY, spread over 40
    # days so every window (1D/1W/1M/ALL) has at least two points. The numbers
    # are chosen to land on the figures the approved dashboard mock shows:
    # index 101.42, today +0.37%.
    for day in range(40, 0, -1):
        session.add(
            PortfolioSample(
                ts=now - timedelta(days=day),
                equity=10000.0 + (40 - day) * 2.6,
                cash=SECRET_CASH,
            )
        )
    # Two points inside today, so today_pct is computable.
    session.add(PortfolioSample(ts=now - timedelta(hours=2), equity=10105.0, cash=SECRET_CASH))
    session.add(PortfolioSample(ts=now, equity=SECRET_EQUITY, cash=SECRET_CASH))

    for minutes, price in enumerate([229.0, 229.8, 230.1, 229.6, 230.9, 231.1, 231.09]):
        session.add(
            PriceSample(
                ts=now - timedelta(minutes=(6 - minutes) * 5),
                symbol="NVDA.US",
                last=price,
                change_pct=0.9,
            )
        )
    session.add(PriceSample(ts=now, symbol="SPY.US", last=773.62, change_pct=1.1))
    # GOOGL gets a single sample: not enough for a trend line.
    session.add(PriceSample(ts=now, symbol="GOOGL.US", last=201.44, change_pct=-0.4))


def _seed_positions(session: Session) -> None:
    session.add(
        Position(symbol="NVDA.US", quantity=SECRET_QUANTITY, cost_basis=SECRET_COST_BASIS)
    )
    session.add(Position(symbol="SPY.US", quantity=60, cost_basis=740.10))
    session.add(Position(symbol="GOOGL.US", quantity=140, cost_basis=195.00))


def _seed_events(session: Session, *, now: datetime) -> None:
    base_ms = int(now.timestamp() * 1000)
    news = [
        ("n-001", "NVDA.US", "Analyst lifts price target on data-center demand",
         NEWS_URL_WITH_TOKEN),
        ("n-002", "SPY.US", "US equities extend gains into the afternoon session",
         NEWS_URL_INTERNAL),
        ("n-003", "AMD.US", "New accelerator lineup detailed at industry conference",
         NEWS_URL_INSECURE),
    ]
    for index, (news_id, symbol, title, url) in enumerate(news):
        session.add(
            Event(
                event_id=f"news:{news_id}",
                news_id=news_id,
                type="NEWS",
                symbol=symbol,
                sector=SECRET_SECTOR,
                priority="MEDIUM",
                event_ts=base_ms - index * 60_000,
                ingest_ts=base_ms - index * 60_000,
                payload={
                    "news_id": news_id,
                    "title": title,
                    "source": "reuters.com",
                    "url": url,
                    "related_symbols": [symbol],
                },
            )
        )
    session.add(
        Event(
            event_id="quote:NVDA.US:1",
            type="QUOTE_MOVE",
            symbol="NVDA.US",
            sector=SECRET_SECTOR,
            priority="HIGH",
            event_ts=base_ms,
            ingest_ts=base_ms,
            payload={"last_price": 231.09, "change_pct": 4.2, "trigger": "cross_+3pct"},
        )
    )


def _seed_decisions(session: Session, *, now: datetime) -> None:
    # 1) an acting decision that cleared risk and filled
    buy = Decision(
        decision_id="d-buy",
        ts=now - timedelta(minutes=30),
        model_used="gpt-5-nano",
        tokens_in=4000,
        tokens_out=500,
        gate_result="OK",
        session_note="acted",
    )
    session.add(buy)
    session.flush()
    buy_instruction = Instruction(
        decision_id="d-buy",
        action="BUY",
        symbol="NVDA.US",
        order_type="LIMIT",
        quantity=SECRET_QUANTITY,
        limit_price=231.0,
        valid_seconds=120,
        confidence=0.7,
        thesis=LEAKY_THESIS,
        cited_event_ids=["news:n-001", "quote:NVDA.US:1"],
    )
    session.add(buy_instruction)
    session.flush()
    session.add(
        RiskAudit(instruction_id=buy_instruction.id, allow=True, rule_tripped="", ts=now)
    )
    order = Order(
        client_order_id=SECRET_CLIENT_ORDER_ID,
        broker_order_id=SECRET_BROKER_ORDER_ID,
        instruction_id=buy_instruction.id,
        status="filled",
        ts=now - timedelta(minutes=29),
    )
    session.add(order)
    session.flush()
    session.add(
        Fill(
            order_id=order.id,
            broker_fill_id=SECRET_BROKER_FILL_ID,
            quantity=SECRET_QUANTITY,
            price=231.0,
            fee=SECRET_FEE,
            ts=now - timedelta(minutes=28),
        )
    )

    # 2) a blocked decision — this is what carries rule_tripped
    blocked = Decision(
        decision_id="d-blocked",
        ts=now - timedelta(minutes=15),
        model_used="gpt-5-nano",
        gate_result="OK",
        session_note="trim proposed",
    )
    session.add(blocked)
    session.flush()
    blocked_instruction = Instruction(
        decision_id="d-blocked",
        action="SELL",
        symbol="SPY.US",
        order_type="LIMIT",
        quantity=10,
        limit_price=773.0,
        valid_seconds=120,
        confidence=0.4,
        thesis="Trim into strength.",
        cited_event_ids=[],
    )
    session.add(blocked_instruction)
    session.flush()
    session.add(
        RiskAudit(
            instruction_id=blocked_instruction.id,
            allow=False,
            rule_tripped=SECRET_RULE_TRIPPED,
            ts=now,
        )
    )

    # 3) a gated HOLD — no instruction row at all
    session.add(
        Decision(
            decision_id="d-hold",
            ts=now - timedelta(minutes=5),
            gate_result="HOLD_LOW_PRIORITY",
            session_note="highest priority in batch is MEDIUM",
        )
    )


def _seed_usage(session: Session, *, now: datetime) -> None:
    session.add(
        ApiUsage(
            ts=now - timedelta(minutes=30),
            kind="llm",
            provider="openai",
            tokens_in=4000,
            tokens_out=500,
            cost=0.04,
            call_count=1,
        )
    )
    session.add(
        ApiUsage(
            ts=now - timedelta(minutes=15),
            kind="llm",
            provider="openai",
            tokens_in=4100,
            tokens_out=450,
            cost=0.05,
            call_count=1,
        )
    )
