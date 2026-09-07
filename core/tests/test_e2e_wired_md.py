"""The real md<->core wire (§2.2), exercised with a SQLite-backed inner loop.

Spawns the actual `tevnnis-md` binary and drives it through core's real
`GrpcMarketDataClient` over a live (loopback) gRPC connection -- everything
else stays mocked (§13): the broker and the LLM. This is the fast,
Docker-free check that the wire and the real C++ pipeline actually produce
what core expects.

The Postgres-backed acceptance path is separate: `scripts/run_e2e.sh` runs
the same two fixtures (md/scenarios/e2e_wired.json,
config/scenario.e2e_wired.json) against a disposable Postgres instance and
asserts through `core/scripts/assert_e2e_db.py`. Keep both in sync with the
fixtures -- see the wired integration section in docs/PROGRESS.md.

Skips (not fails) when `build/md/tevnnis-md` hasn't been built -- acceptable
for a developer running this one file manually. `make e2e` builds first and
runs `scripts/run_e2e.sh` instead of this file, so the acceptance path can
never silently skip the integration.
"""

from __future__ import annotations

import io
import socket
import subprocess
import time
from datetime import timedelta
from pathlib import Path

import pytest

from tevnnis_core import cli
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
from tevnnis_core.market_data import GrpcMarketDataClient
from tevnnis_core.mocks.scenario import load_scenario

risk = pytest.importorskip(
    "tevnnis_risk",
    reason="build the extension first: cmake -S . -B build && cmake --build build -j",
)

REPO_ROOT = Path(__file__).resolve().parents[2]
MD_BINARY = REPO_ROOT / "build" / "md" / "tevnnis-md"
MD_CONFIG = REPO_ROOT / "md" / "scenarios" / "example_md_config.json"
MD_SCENARIO = REPO_ROOT / "md" / "scenarios" / "e2e_wired.json"
CORE_SCENARIO = REPO_ROOT / "config" / "scenario.e2e_wired.json"

if not MD_BINARY.exists():
    pytest.skip(
        f"tevnnis-md is not built ({MD_BINARY} missing). Build it first:\n"
        "    cmake -S . -B build -DCMAKE_BUILD_TYPE=Debug\n"
        "    cmake --build build -j",
        allow_module_level=True,
    )

# Independently derived from md/scenarios/e2e_wired.json's NVDA quote event,
# the same way DataUnifier::UnifyQuote computes it (md/src/data_unifier.cpp):
# "quote:" + symbol + ":" + event_ts. NOT copied from
# config/scenario.e2e_wired.json's cited_event_ids -- the assertions below
# check that the two independently agree, rather than assuming it.
_NVDA_SYMBOL = "NVDA.US"
_NVDA_EVENT_TS = 1700000060000
EXPECTED_QUOTE_EVENT_ID = f"quote:{_NVDA_SYMBOL}:{_NVDA_EVENT_TS}"
EXPECTED_NEWS_EVENT_ID = "news:e2e-n-1"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _no_sleep(seconds: float) -> None:
    return None


@pytest.fixture
def md_process():
    """Spawns the real tevnnis-md binary and waits for its readiness line."""
    address = f"127.0.0.1:{_free_port()}"
    proc = subprocess.Popen(
        [
            str(MD_BINARY),
            "--config", str(MD_CONFIG),
            "--scenario", str(MD_SCENARIO),
            "--listen", address,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        deadline = time.monotonic() + 15.0
        lines: list[str] = []
        ready = False
        while time.monotonic() < deadline:
            line = proc.stdout.readline()
            if line:
                lines.append(line)
                if "listening on" in line:
                    ready = True
                    break
            elif proc.poll() is not None:
                break
        if not ready:
            pytest.fail(
                "tevnnis-md did not report readiness within 15s; output:\n" + "".join(lines)
            )
        yield address
    finally:
        proc.kill()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


async def test_wired_scenario_produces_the_expected_db_state(
    config, full_db_session, now, md_process
):
    core_scenario = load_scenario(CORE_SCENARIO)  # broker + llm only; md comes from the wire
    deps = cli.build_dependencies(
        config,
        session=full_db_session,
        md_client=GrpcMarketDataClient(md_process),
        trade_port=core_scenario.broker,
        llm_provider=core_scenario.llm,
        risk_module=risk,
        md_address=md_process,
    )
    # Exercising the real wire, not a mock -- and it must be allowed under
    # --yes precisely because the broker/LLM (not md) are what's mocked.
    assert isinstance(deps.md_client, GrpcMarketDataClient)
    assert cli.guard_allows_auto_confirm(deps)

    out = io.StringIO()
    await cli.validate_dependencies(deps)
    await cli.reconcile(deps, now=now)
    assert cli.confirm_start(deps, assume_yes=True, out=out)

    # Both md-sourced events arrive in round 1 (md ingests its whole scenario
    # before serving -- see md/src/main.cpp), so round 2 sees nothing new.
    clock = [now, now + timedelta(seconds=1), now + timedelta(seconds=2)]
    stats = await cli.run_loop(
        deps,
        stop=cli.StopFlag(),
        max_rounds=2,
        now_fn=lambda: clock.pop(0),
        sleep=_no_sleep,
        out=out,
    )
    await cli.shutdown(deps, stats, now=now, reason="test run complete", out=out)

    session = full_db_session

    decisions = session.query(Decision).order_by(Decision.ts).all()
    assert len(decisions) == 2
    ok = [d for d in decisions if d.gate_result == "OK"]
    holds = [d for d in decisions if d.gate_result != "OK"]
    assert len(ok) == 1 and len(holds) == 1
    decided = ok[0]
    assert holds[0].gate_result == "HOLD_NO_EVENTS"
    assert (decided.tokens_in, decided.tokens_out) == (100, 50)
    assert decided.model_used == "mock"

    # The event-id coupling (see config/scenario.e2e_wired.json's comment):
    # assert the id actually appears in md's output, don't just assume it.
    events = {e.event_id: e for e in session.query(Event).all()}
    assert set(events) == {EXPECTED_NEWS_EVENT_ID, EXPECTED_QUOTE_EVENT_ID}
    assert events[EXPECTED_QUOTE_EVENT_ID].payload["change_pct"] == pytest.approx(5.5)

    instruction = session.query(Instruction).one()
    assert (instruction.action, instruction.symbol, instruction.quantity) == (
        "BUY",
        "NVDA.US",
        10,
    )
    assert instruction.limit_price == 105.5
    assert instruction.cited_event_ids == [EXPECTED_QUOTE_EVENT_ID]
    assert instruction.decision_id == decided.decision_id

    audit = session.query(RiskAudit).one()
    assert (audit.allow, audit.rule_tripped) == (True, "ALLOWED")

    order = session.query(Order).one()
    assert order.client_order_id == make_client_order_id(decided.decision_id, 0)
    assert order.broker_order_id == "MOCK-1"

    fill = session.query(Fill).one()
    assert (fill.quantity, fill.price) == (10, 105.5)
    assert fill.broker_fill_id == "MOCK-1-F1"

    position = session.query(Position).one()
    assert (position.symbol, position.quantity, position.cost_basis) == ("NVDA.US", 10, 105.5)

    usage = session.query(ApiUsage).all()
    assert sorted(u.kind for u in usage) == ["broker", "llm"]

    account = await deps.trade_port.query_account()
    assert account.cash == pytest.approx(10_000.0 - 10 * 105.5 - 0.50)
