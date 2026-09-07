#!/usr/bin/env python3
"""Integration acceptance check for the known outcome of scripts/run_e2e.sh's
scenario against the real (disposable) Postgres database it just ran against.

This is deliberately NOT a pytest test. It's the last step of the operational
runbook (scripts/run_e2e.sh), asserting against real Postgres -- the actual
migrated schema, not sqlite's dialect variants -- rather than against the
sqlite fixture core/tests/test_e2e_wired_md.py uses for its fast, Docker-free
inner-loop check of the same md<->core wiring. Keep the two scenario fixtures
(md/scenarios/e2e_wired.json, config/scenario.e2e_wired.json) and the
expectations below in sync -- see the wired integration section in docs/PROGRESS.md.

Every count assertion below is EXACT, not "at least": run_e2e.sh guarantees a
genuinely empty schema at the start of every run (a disposable Postgres
container + volume, `down -v`'d before and after), so an exact count is what
actually proves that guarantee held -- an "at least" count would pass even if
a previous run's rows had leaked in.

Usage:
    uv run python scripts/assert_e2e_db.py --database-url postgresql://...
"""

from __future__ import annotations

import argparse
import sys

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from tevnnis_core.db import repository as repo
from tevnnis_core.db.models import (
    ApiUsage,
    Decision,
    Event,
    Fill,
    Instruction,
    Order,
    Position,
    RiskAudit,
)
from tevnnis_core.llm.instruction_unifier import make_client_order_id

# Independently derived from md/scenarios/e2e_wired.json's NVDA quote event,
# the same way DataUnifier::UnifyQuote computes it (md/src/data_unifier.cpp):
# "quote:" + symbol + ":" + event_ts. NOT copied from
# config/scenario.e2e_wired.json's cited_event_ids -- checking that the two
# independently agree is the point (see that file's coupling comment).
EXPECTED_QUOTE_EVENT_ID = "quote:NVDA.US:1700000060000"
EXPECTED_NEWS_EVENT_ID = "news:e2e-n-1"


class AssertionFailed(RuntimeError):
    pass


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionFailed(message)


def _run_assertions(session: Session) -> None:
    decisions = session.query(Decision).order_by(Decision.ts).all()
    check(len(decisions) == 2, f"expected exactly 2 decisions, found {len(decisions)}")
    ok = [d for d in decisions if d.gate_result == "OK"]
    holds = [d for d in decisions if d.gate_result != "OK"]
    check(len(ok) == 1, f"expected exactly 1 OK decision, found {len(ok)}")
    check(len(holds) == 1, f"expected exactly 1 HOLD decision, found {len(holds)}")
    decided = ok[0]
    check(
        holds[0].gate_result == "HOLD_NO_EVENTS",
        f"unexpected hold reason: {holds[0].gate_result}",
    )
    check(
        (decided.tokens_in, decided.tokens_out) == (100, 50),
        f"unexpected token usage on the OK decision: "
        f"{decided.tokens_in}/{decided.tokens_out}",
    )

    events = {e.event_id: e for e in session.query(Event).all()}
    check(len(events) == 2, f"expected exactly 2 events, found {len(events)}")
    check(
        EXPECTED_QUOTE_EVENT_ID in events,
        f"the NVDA quote event id {EXPECTED_QUOTE_EVENT_ID!r} was not persisted -- "
        "md's event id scheme may have drifted from config/scenario.e2e_wired.json's "
        "scripted cited_event_ids",
    )
    check(
        EXPECTED_NEWS_EVENT_ID in events,
        f"the news event id {EXPECTED_NEWS_EVENT_ID!r} was not persisted",
    )

    instructions = session.query(Instruction).all()
    check(len(instructions) == 1, f"expected exactly 1 instruction, found {len(instructions)}")
    instruction = instructions[0]
    check(
        (instruction.action, instruction.symbol, instruction.quantity) == ("BUY", "NVDA.US", 10),
        f"unexpected instruction: {instruction.action} {instruction.symbol} "
        f"x{instruction.quantity}",
    )
    check(
        instruction.limit_price == 105.5,
        f"unexpected limit price: {instruction.limit_price}",
    )
    check(
        instruction.cited_event_ids == [EXPECTED_QUOTE_EVENT_ID],
        f"instruction cited {instruction.cited_event_ids}, expected "
        f"[{EXPECTED_QUOTE_EVENT_ID!r}] -- the coupling between "
        "config/scenario.e2e_wired.json's scripted LLM response and md's event id "
        "scheme has drifted",
    )
    check(
        instruction.decision_id == decided.decision_id,
        "instruction is not linked to the OK decision",
    )

    audits = session.query(RiskAudit).all()
    check(len(audits) == 1, f"expected exactly 1 risk_audit row, found {len(audits)}")
    check(
        (audits[0].allow, audits[0].rule_tripped) == (True, "ALLOWED"),
        f"unexpected risk verdict: allow={audits[0].allow} rule={audits[0].rule_tripped}",
    )

    orders = session.query(Order).all()
    check(len(orders) == 1, f"expected exactly 1 order, found {len(orders)}")
    order = orders[0]
    check(
        order.client_order_id == make_client_order_id(decided.decision_id, 0),
        "order id is not the deterministic §5 id derived from the decision identity",
    )
    check(
        order.status == repo.STATUS_FILLED,
        f"expected the order filled, got status={order.status}",
    )

    fills = session.query(Fill).all()
    check(len(fills) == 1, f"expected exactly 1 fill, found {len(fills)}")
    check(
        (fills[0].quantity, fills[0].price) == (10, 105.5),
        f"unexpected fill: {fills[0].quantity} @ {fills[0].price}",
    )

    positions = session.query(Position).all()
    check(len(positions) == 1, f"expected exactly 1 position, found {len(positions)}")
    check(
        (positions[0].symbol, positions[0].quantity) == ("NVDA.US", 10),
        f"unexpected position: {positions[0].symbol} x{positions[0].quantity}",
    )

    usage = session.query(ApiUsage).all()
    check(
        len(usage) == 2,
        f"expected exactly 2 api_usage rows (llm + broker), found {len(usage)}",
    )
    check(
        sorted(u.kind for u in usage) == ["broker", "llm"],
        f"unexpected api_usage kinds: {sorted(u.kind for u in usage)}",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", required=True, help="the disposable e2e Postgres URL")
    args = parser.parse_args(argv)

    engine = create_engine(args.database_url)
    with Session(engine) as session:
        try:
            _run_assertions(session)
        except AssertionFailed as exc:
            print(f"FAIL: {exc}", file=sys.stderr)
            return 1

    print("PASS: all wired e2e assertions held against Postgres.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
