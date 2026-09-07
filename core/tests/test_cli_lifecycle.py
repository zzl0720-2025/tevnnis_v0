"""§9 lifecycle: startup validation, reconcile, confirmation, run loop, shutdown."""

from __future__ import annotations

import io
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from tevnnis_core import cli
from tevnnis_core.db import repository as repo
from tevnnis_core.db.models import Position
from tevnnis_core.instructions import Action, OrderType, TradingInstruction
from tevnnis_core.mocks.broker import FillMode, MockBroker
from tevnnis_core.mocks.llm import MockLLM
from tevnnis_core.mocks.llm_scenarios import hold_scenario
from tevnnis_core.mocks.market_data import MockMarketDataClient, make_response
from tevnnis_core.ports import PositionSnapshot
from tevnnis_core.snapshot import SNAPSHOT_JS, SNAPSHOT_JSON

risk = pytest.importorskip(
    "tevnnis_risk",
    reason="build the extension first: cmake -S . -B build && cmake --build build -j",
)

TZ = "America/New_York"


def at(hour: int, minute: int = 0, day: int = 2) -> datetime:
    return datetime(2026, 9, day, hour, minute, tzinfo=ZoneInfo(TZ))


@pytest.fixture
def config_path(tmp_path, config):
    """The example config, written out so the CLI can parse it from disk."""
    import yaml

    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config.model_dump(mode="json")))
    return path


async def noop_sleep(_seconds: float) -> None:
    """Skip the cadence wait in a multi-round test."""
    return None


def deps_for(
    config,
    session,
    *,
    broker: MockBroker | None = None,
    responses=None,
    snapshot_dir=None,
):
    broker = broker or MockBroker(initial_cash=10_000.0)
    return cli.build_dependencies(
        config,
        session=session,
        md_client=MockMarketDataClient(responses if responses is not None else []),
        trade_port=broker,
        llm_provider=MockLLM(response=hold_scenario()),
        risk_module=risk,
        snapshot_dir=snapshot_dir,
    )


# --- startup validation -----------------------------------------------------


async def test_validate_dependencies_checks_every_external_system(config, full_db_session):
    checks = await cli.validate_dependencies(deps_for(config, full_db_session))
    joined = " ".join(checks)
    assert "risk engine" in joined
    assert "database" in joined
    assert "md plane" in joined
    assert "broker" in joined


async def test_an_unusable_broker_aborts_startup(config, full_db_session):
    class DeadBroker(MockBroker):
        async def query_account(self):
            raise ConnectionError("broker offline")

    deps = deps_for(config, full_db_session, broker=DeadBroker(initial_cash=0.0))
    with pytest.raises(cli.StartupError, match="broker not usable"):
        await cli.validate_dependencies(deps)


async def test_an_unreachable_md_plane_aborts_startup(config, full_db_session):
    deps = deps_for(config, full_db_session)

    async def fail(timeout: float = 5.0):
        raise TimeoutError("no answer")

    deps.md_client.validate = fail
    with pytest.raises(cli.StartupError, match="market-data plane unreachable"):
        await cli.validate_dependencies(deps)


# --- §9.1 step 3 reconcile --------------------------------------------------


async def test_reconcile_adopts_the_brokers_positions_over_the_db(config, full_db_session, now):
    repo.apply_fill_to_position(
        full_db_session, symbol="XOM.US", action=Action.BUY, quantity=99, price=100.0
    )
    full_db_session.commit()

    broker = MockBroker(
        initial_cash=10_000.0,
        initial_positions={
            "AMD.US": PositionSnapshot(symbol="AMD.US", quantity=20, cost_basis=48.0)
        },
    )
    result = await cli.reconcile(deps_for(config, full_db_session, broker=broker), now=now)

    cached = {p.symbol: p.quantity for p in full_db_session.query(Position).all()}
    assert cached == {"AMD.US": 20}
    assert any("AMD.US" in d for d in result.divergences)
    assert any("XOM.US" in d and "broker holds none" in d for d in result.divergences)


async def test_reconcile_adopts_orders_open_at_the_broker(config, full_db_session, now):
    broker = MockBroker(initial_cash=10_000.0, fill_mode=FillMode.NO_FILL)
    await broker.submit_order(
        TradingInstruction(
            action=Action.BUY,
            symbol="NVDA.US",
            order_type=OrderType.LIMIT,
            quantity=10,
            limit_price=100.0,
            valid_seconds=300,
            confidence=0.8,
            client_order_id="co_prev_run",
        )
    )
    deps = deps_for(config, full_db_session, broker=broker)

    result = await cli.reconcile(deps, now=now)

    assert any("co_prev_run" in d and "not live in the DB" in d for d in result.divergences)
    assert deps.execution.inflight_ids == ["co_prev_run"]
    assert repo.get_order(full_db_session, "co_prev_run") is not None


async def test_reconcile_closes_db_orders_the_broker_does_not_know_about(
    config, full_db_session, now
):
    repo.record_order(
        full_db_session, client_order_id="co_ghost", status=repo.STATUS_OPEN, ts=now
    )
    full_db_session.commit()

    result = await cli.reconcile(deps_for(config, full_db_session), now=now)

    assert any("co_ghost" in d and "not open at the broker" in d for d in result.divergences)
    assert repo.get_order(full_db_session, "co_ghost").status == repo.STATUS_CANCELLED


# --- §9.1 step 5 summary ----------------------------------------------------


async def test_startup_summary_states_what_the_operator_needs(config, full_db_session, now):
    broker = MockBroker(
        initial_cash=10_000.0,
        initial_positions={
            "AMD.US": PositionSnapshot(symbol="AMD.US", quantity=20, cost_basis=48.0)
        },
    )
    deps = deps_for(config, full_db_session, broker=broker)
    deps.agent.last_prices["AMD.US"] = 60.0
    reconciled = await cli.reconcile(deps, now=now)

    text = cli.render_startup_summary(deps, reconciled, now=now)

    assert "PAPER" in text
    assert "10,000.00 USD" in text
    assert "AMD.US" in text and "+25.00%" in text  # P&L computed in code, not by the LLM
    assert "reason_min_priority=HIGH" in text
    assert "cancel_open_orders_on_shutdown=True" in text
    assert "ALL MOCKED" in text
    assert "0 / 50 calls" in text
    # risk_limits are operator-facing here but never reach the prompt (§6).
    assert "position<=25%" in text


# --- §9.1 step 6 confirmation ----------------------------------------------


def test_the_loop_arms_only_on_an_explicit_yes(config, full_db_session):
    deps = deps_for(config, full_db_session)
    out = io.StringIO()

    assert cli.confirm_start(deps, prompt=lambda _: "y", out=out)
    assert cli.confirm_start(deps, prompt=lambda _: "YES", out=out)
    assert not cli.confirm_start(deps, prompt=lambda _: "", out=out)
    assert not cli.confirm_start(deps, prompt=lambda _: "n", out=out)
    assert not cli.confirm_start(deps, prompt=lambda _: "maybe", out=out)


def test_auto_confirm_checks_the_actual_broker_and_llm_types(config, full_db_session):
    """§15's invariant is checked, not trusted -- see cli.guard_allows_auto_confirm."""
    out = io.StringIO()
    assert cli.confirm_start(deps_for(config, full_db_session), assume_yes=True, out=out)

    live = cli.build_dependencies(
        config,
        session=full_db_session,
        md_client=MockMarketDataClient([]),
        trade_port=object(),  # not a MockBroker: confirm_start never calls it
        llm_provider=MockLLM(response=hold_scenario()),
        risk_module=risk,
    )
    assert not cli.confirm_start(live, assume_yes=True, out=out)
    assert "REFUSED" in out.getvalue()


def test_auto_confirm_allows_a_real_md_client_with_mocked_broker_and_llm(
    config, full_db_session
):
    """The clarified §15 invariant: "no real broker, no real LLM spend", not "md must
    be in-process". A real GrpcMarketDataClient is fine under --yes because it can
    neither place orders nor spend budget."""
    from tevnnis_core.market_data import GrpcMarketDataClient

    deps = cli.build_dependencies(
        config,
        session=full_db_session,
        md_client=GrpcMarketDataClient("127.0.0.1:1"),  # constructed only, never connected
        trade_port=MockBroker(initial_cash=10_000.0),
        llm_provider=MockLLM(response=hold_scenario()),
        risk_module=risk,
        md_address="127.0.0.1:1",
    )
    out = io.StringIO()
    assert cli.guard_allows_auto_confirm(deps)
    assert cli.confirm_start(deps, assume_yes=True, out=out)
    assert "REFUSED" not in out.getvalue()


def test_a_real_longbridge_broker_refuses_yes_and_now(config, full_db_session):
    """§15's invariant, against the REAL adapter rather than a stand-in object.

    Live paper trading is interactive-confirm only: a real broker can place
    real orders, so `--yes` and `--now` must both be refused. The adapter is
    constructed but never connected -- no credentials, no network -- which is
    exactly why LongbridgeTradeBroker.__init__ does no I/O.
    """
    from tevnnis_core.brokers.longbridge import LongbridgeTradeBroker

    broker = LongbridgeTradeBroker()
    assert not broker.connected

    deps = cli.build_dependencies(
        config,
        session=full_db_session,
        md_client=MockMarketDataClient([]),
        trade_port=broker,
        llm_provider=MockLLM(response=hold_scenario()),
        risk_module=risk,
    )

    assert not cli.guard_allows_auto_confirm(deps)
    assert not cli.now_override_allowed(deps, at(11, 0))

    out = io.StringIO()
    assert not cli.confirm_start(deps, assume_yes=True, out=out)
    assert "REFUSED" in out.getvalue()

    # The startup summary must say so plainly, and never claim "ALL MOCKED".
    line = cli._dependency_line(deps)
    assert line.startswith("LIVE")
    assert "Longbridge PAPER account" in line
    assert "ALL MOCKED" not in line


def test_a_real_llm_refuses_yes_and_now(config, full_db_session):
    """§15 against the REAL LLM: real spend is interactive-confirm only.

    The mirror of the real-broker test above, on the axis that costs money
    rather than places orders. `OpenAIProvider.__init__` reads no key and makes
    no request, which is exactly what lets this run offline in CI.
    """
    from tevnnis_core.llm.openai_provider import OpenAIProvider

    deps = cli.build_dependencies(
        config,
        session=full_db_session,
        md_client=MockMarketDataClient([]),
        trade_port=MockBroker(initial_cash=10_000.0),  # broker is MOCKED
        llm_provider=OpenAIProvider(model="gpt-5-nano"),  # the LLM is NOT
        llm_provider_name="openai",
        llm_model_name="gpt-5-nano",
        risk_module=risk,
    )

    assert not cli.guard_allows_auto_confirm(deps)
    assert not cli.now_override_allowed(deps, at(11, 0))

    out = io.StringIO()
    assert not cli.confirm_start(deps, assume_yes=True, out=out)
    assert "REFUSED" in out.getvalue()


def test_a_real_llm_with_a_mock_broker_is_still_reported_as_live(config, full_db_session):
    """The banner must not read as safe just because the BROKER is mocked."""
    from tevnnis_core.llm.openai_provider import OpenAIProvider

    deps = cli.build_dependencies(
        config,
        session=full_db_session,
        md_client=MockMarketDataClient([]),
        trade_port=MockBroker(initial_cash=10_000.0),
        llm_provider=OpenAIProvider(model="gpt-5-nano"),
        llm_provider_name="openai",
        llm_model_name="gpt-5-nano",
        risk_module=risk,
    )

    line = cli._dependency_line(deps)
    assert line.startswith("LIVE")
    assert "gpt-5-nano" in line
    assert "REAL SPEND" in line
    assert "ALL MOCKED" not in line
    assert cli.describe_llm(deps) == "openai gpt-5-nano (REAL SPEND)"


def test_a_real_llm_still_confirms_interactively(config, full_db_session):
    """Refusing --yes must not make real-LLM runs impossible -- only attended."""
    from tevnnis_core.llm.openai_provider import OpenAIProvider

    deps = cli.build_dependencies(
        config,
        session=full_db_session,
        md_client=MockMarketDataClient([]),
        trade_port=MockBroker(initial_cash=10_000.0),
        llm_provider=OpenAIProvider(model="gpt-5-nano"),
        llm_provider_name="openai",
        risk_module=risk,
    )
    out = io.StringIO()
    assert cli.confirm_start(deps, prompt=lambda _: "y", out=out)
    assert not cli.confirm_start(deps, prompt=lambda _: "n", out=out)


def test_describe_llm_reads_the_actual_type(config, full_db_session):
    assert cli.describe_llm(deps_for(config, full_db_session)) == "mock (canned scenario)"


def test_llm_openai_builds_the_real_provider_from_the_strong_route(tmp_path, config):
    """v0 reads llm_routing.strong only, and this is the first thing that reads it."""
    import yaml

    from tevnnis_core.llm.openai_provider import OpenAIProvider

    raw = config.model_dump(mode="json")
    raw["llm_routing"]["strong"] = {"provider": "openai", "model": "gpt-5-nano"}
    path = tmp_path / "openai.yaml"
    path.write_text(yaml.safe_dump(raw))

    args = cli.parse_args(
        ["--config", str(path), "--database-url", "sqlite:///:memory:", "--llm", "openai"]
    )
    deps = cli._build_from_args(args)

    assert isinstance(deps.llm_provider, OpenAIProvider)
    assert deps.llm_provider.model == "gpt-5-nano"
    assert deps.llm_provider.reasoning_effort == "low"  # provider default
    assert not cli.guard_allows_auto_confirm(deps)
    # The two names stay apart: who was billed vs what reasoned.
    assert deps.router.provider_name == "openai"
    assert deps.router.model_used == "gpt-5-nano"


def test_config_reasoning_effort_overrides_the_provider_default(tmp_path, config):
    import yaml

    raw = config.model_dump(mode="json")
    raw["llm_routing"]["strong"] = {
        "provider": "openai",
        "model": "gpt-5-nano",
        "reasoning_effort": "minimal",
    }
    path = tmp_path / "openai.yaml"
    path.write_text(yaml.safe_dump(raw))

    args = cli.parse_args(
        ["--config", str(path), "--database-url", "sqlite:///:memory:", "--llm", "openai"]
    )
    assert cli._build_from_args(args).llm_provider.reasoning_effort == "minimal"


def test_an_unusable_reasoning_effort_is_refused_at_startup(tmp_path, config):
    """A 400 on every round would be a permanent HOLD -- fail at startup instead."""
    import yaml

    raw = config.model_dump(mode="json")
    raw["llm_routing"]["strong"] = {
        "provider": "openai",
        "model": "gpt-5-nano",
        "reasoning_effort": "none",  # arrives with gpt-5.1+; this model rejects it
    }
    path = tmp_path / "openai.yaml"
    path.write_text(yaml.safe_dump(raw))

    args = cli.parse_args(
        ["--config", str(path), "--database-url", "sqlite:///:memory:", "--llm", "openai"]
    )
    with pytest.raises(cli.StartupError, match="reasoning_effort"):
        cli._build_from_args(args)


def test_llm_flag_and_config_provider_must_agree(tmp_path, config):
    """A mismatch means the operator believes they are running a model they are not."""
    import yaml

    raw = config.model_dump(mode="json")
    raw["llm_routing"]["strong"] = {"provider": "anthropic", "model": "claude-opus-4-8"}
    path = tmp_path / "mismatch.yaml"
    path.write_text(yaml.safe_dump(raw))

    args = cli.parse_args(
        ["--config", str(path), "--database-url", "sqlite:///:memory:", "--llm", "openai"]
    )
    with pytest.raises(cli.StartupError, match="disagrees with --config"):
        cli._build_from_args(args)


def test_the_llm_defaults_to_mock_and_never_touches_the_strong_route(tmp_path, config):
    """Every existing invocation keeps its old behaviour: no flag, no spend."""
    import yaml

    raw = config.model_dump(mode="json")
    raw["llm_routing"]["strong"] = {"provider": "anthropic", "model": "claude-opus-4-8"}
    path = tmp_path / "default.yaml"
    path.write_text(yaml.safe_dump(raw))

    args = cli.parse_args(["--config", str(path), "--database-url", "sqlite:///:memory:"])
    assert args.llm == "mock"

    # The strong route says anthropic, but --llm defaults to mock, so it is
    # never read and the mismatch check never fires.
    deps = cli._build_from_args(args)
    assert type(deps.llm_provider).__name__ == "MockLLM"
    assert deps.router.provider_name == "mock"


def test_the_broker_name_no_longer_leaks_into_the_llm_name(tmp_path, config):
    """The conflation this step fixed: --broker used to name the LLM too."""
    import yaml

    path = tmp_path / "paper.yaml"
    path.write_text(yaml.safe_dump(config.model_dump(mode="json")))
    args = cli.parse_args(
        [
            "--config", str(path),
            "--database-url", "sqlite:///:memory:",
            "--broker", "longbridge",
        ]
    )
    deps = cli._build_from_args(args)

    assert deps.execution.broker_name == "longbridge"
    assert deps.router.provider_name == "mock"  # NOT "longbridge"
    assert deps.router.model_used == "mock"


def test_a_real_broker_still_confirms_interactively(config, full_db_session):
    """Refusing --yes must not make live trading impossible -- only attended."""
    from tevnnis_core.brokers.longbridge import LongbridgeTradeBroker

    deps = cli.build_dependencies(
        config,
        session=full_db_session,
        md_client=MockMarketDataClient([]),
        trade_port=LongbridgeTradeBroker(),
        llm_provider=MockLLM(response=hold_scenario()),
        risk_module=risk,
    )
    out = io.StringIO()
    assert cli.confirm_start(deps, prompt=lambda _: "y", out=out)
    assert not cli.confirm_start(deps, prompt=lambda _: "n", out=out)


def test_describe_broker_reads_the_actual_type(config, full_db_session):
    from tevnnis_core.brokers.longbridge import LongbridgeTradeBroker

    mocked = deps_for(config, full_db_session)
    assert cli.describe_broker(mocked) == "mock (in-memory)"

    live = cli.build_dependencies(
        config,
        session=full_db_session,
        md_client=MockMarketDataClient([]),
        trade_port=LongbridgeTradeBroker(),
        llm_provider=MockLLM(response=hold_scenario()),
        risk_module=risk,
    )
    assert cli.describe_broker(live) == "Longbridge PAPER account (credentials from env)"


def test_broker_longbridge_is_refused_unless_the_config_says_paper(tmp_path, config):
    """Fail-closed guard: it routes nothing, it can only refuse."""
    import yaml

    live_config = config.model_dump(mode="json")
    live_config["account"]["mode"] = "live"
    path = tmp_path / "live.yaml"
    path.write_text(yaml.safe_dump(live_config))

    args = cli.parse_args(
        [
            "--config", str(path),
            "--database-url", "sqlite:///:memory:",
            "--broker", "longbridge",
        ]
    )
    with pytest.raises(cli.StartupError, match="requires account.mode: paper"):
        cli._build_from_args(args)


def test_broker_longbridge_builds_the_real_adapter_on_a_paper_config(tmp_path, config):
    import yaml

    path = tmp_path / "paper.yaml"
    path.write_text(yaml.safe_dump(config.model_dump(mode="json")))
    args = cli.parse_args(
        [
            "--config", str(path),
            "--database-url", "sqlite:///:memory:",
            "--broker", "longbridge",
        ]
    )
    deps = cli._build_from_args(args)
    assert type(deps.trade_port).__name__ == "LongbridgeTradeBroker"
    assert not deps.trade_port.connected  # constructed, never connected
    assert not cli.guard_allows_auto_confirm(deps)


def test_the_broker_defaults_to_mock(tmp_path, config):
    import yaml

    path = tmp_path / "paper.yaml"
    path.write_text(yaml.safe_dump(config.model_dump(mode="json")))
    args = cli.parse_args(
        ["--config", str(path), "--database-url", "sqlite:///:memory:"]
    )
    assert args.broker == "mock"
    deps = cli._build_from_args(args)
    assert type(deps.trade_port).__name__ == "MockBroker"
    assert cli.guard_allows_auto_confirm(deps)


def test_now_override_allowed_matches_the_yes_guard(config, full_db_session):
    mocked = deps_for(config, full_db_session)
    assert cli.now_override_allowed(mocked, at(11, 0))
    assert cli.now_override_allowed(mocked, None)

    live = cli.build_dependencies(
        config,
        session=full_db_session,
        md_client=MockMarketDataClient([]),
        trade_port=object(),
        llm_provider=MockLLM(response=hold_scenario()),
        risk_module=risk,
    )
    assert not cli.now_override_allowed(live, at(11, 0))
    assert cli.now_override_allowed(live, None)  # nothing requested, nothing to refuse


def test_resolve_now_override_parses_iso8601_with_an_offset():
    args = cli.parse_args(
        ["--config", "c.yaml", "--now", "2026-09-02T11:00:00-04:00"]
    )
    parsed = cli._resolve_now_override(args)
    assert parsed == datetime(2026, 9, 2, 11, 0, tzinfo=timezone(-timedelta(hours=4)))


def test_resolve_now_override_is_none_when_not_given():
    args = cli.parse_args(["--config", "c.yaml"])
    assert cli._resolve_now_override(args) is None


def test_resolve_now_override_rejects_a_naive_datetime():
    args = cli.parse_args(["--config", "c.yaml", "--now", "2026-09-02T11:00:00"])
    with pytest.raises(cli.StartupError, match="UTC offset"):
        cli._resolve_now_override(args)


def test_resolve_now_override_rejects_garbage():
    args = cli.parse_args(["--config", "c.yaml", "--now", "not-a-date"])
    with pytest.raises(cli.StartupError, match="not a valid"):
        cli._resolve_now_override(args)


def test_md_source_grpc_builds_a_grpc_market_data_client(tmp_path, config):
    import yaml

    from tevnnis_core.market_data import GrpcMarketDataClient

    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config.model_dump(mode="json")))
    args = cli.parse_args(
        [
            "--config", str(path),
            "--md-source", "grpc", "--md-address", "127.0.0.1:50061",
            "--database-url", "sqlite:///:memory:",
        ]
    )
    deps = cli._build_from_args(args)
    assert isinstance(deps.md_client, GrpcMarketDataClient)
    assert deps.md_client.address == "127.0.0.1:50061"


async def test_startup_summary_distinguishes_real_md_from_all_mocked(
    config, full_db_session, now
):
    """A real md source must never be reported as "ALL MOCKED" -- it isn't (§15)."""
    from tevnnis_core.market_data import GrpcMarketDataClient

    deps = cli.build_dependencies(
        config,
        session=full_db_session,
        md_client=GrpcMarketDataClient("127.0.0.1:1"),
        trade_port=MockBroker(initial_cash=10_000.0),
        llm_provider=MockLLM(response=hold_scenario()),
        risk_module=risk,
        md_address="127.0.0.1:1",
    )
    reconciled = await cli.reconcile(deps, now=now)
    text = cli.render_startup_summary(deps, reconciled, now=now)
    assert "ALL MOCKED" not in text
    assert "safe for --yes" in text
    assert "no real broker" in text


# --- §9.1 step 7 market-hours gate -----------------------------------------


def test_market_gate_trades_during_regular_hours(config):
    assert cli.market_gate(config, at(11, 0))[0] == cli.TRADE


def test_market_gate_waits_before_the_open(config):
    verdict, message = cli.market_gate(config, at(8, 0))
    assert verdict == cli.WAIT
    assert "09:30" in message


def test_market_gate_stops_after_the_close(config):
    verdict, message = cli.market_gate(config, at(16, 30))
    assert verdict == cli.STOP
    assert "session is over" in message


def test_market_gate_stops_on_a_weekend(config):
    assert cli.market_gate(config, at(11, 0, day=5))[0] == cli.STOP


def test_respect_market_hours_false_always_trades(config):
    config.cadence.respect_market_hours = False
    assert cli.market_gate(config, at(3, 0))[0] == cli.TRADE
    assert cli.market_gate(config, at(11, 0, day=6))[0] == cli.TRADE


# --- the run loop -----------------------------------------------------------


async def test_run_loop_stops_at_the_close_without_running_a_round(config, full_db_session):
    deps = deps_for(config, full_db_session, responses=[make_response()])
    stop = cli.StopFlag()

    stats = await cli.run_loop(
        deps, stop=stop, now_fn=lambda: at(16, 30), out=io.StringIO()
    )

    assert stats.rounds == 0
    assert "session is over" in stop.reason


async def test_run_loop_waits_before_the_open_then_trades(config, full_db_session):
    clock = [at(9, 0), at(9, 45)]
    deps = deps_for(config, full_db_session, responses=[make_response(), make_response()])
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    stats = await cli.run_loop(
        deps,
        stop=cli.StopFlag(),
        max_rounds=1,
        now_fn=lambda: clock.pop(0),
        sleep=sleep,
        out=io.StringIO(),
    )

    assert slept == [config.cadence.decision_interval_seconds]  # one wait, no trailing sleep
    assert stats.rounds == 1


async def test_run_loop_once_runs_exactly_one_round(config, full_db_session):
    deps = deps_for(config, full_db_session, responses=[make_response(), make_response()])
    stop = cli.StopFlag()

    stats = await cli.run_loop(
        deps, stop=stop, once=True, now_fn=lambda: at(11, 0), out=io.StringIO()
    )

    assert stats.rounds == 1
    assert stop.reason == "--once"


async def test_stop_flag_ends_the_loop_gracefully(config, full_db_session):
    deps = deps_for(config, full_db_session, responses=[make_response()])
    stop = cli.StopFlag()
    stop.stop("interrupted by the operator (Ctrl-C)")

    stats = await cli.run_loop(deps, stop=stop, now_fn=lambda: at(11, 0), out=io.StringIO())

    assert stats.rounds == 0


# --- §9.2 shutdown ----------------------------------------------------------


async def test_shutdown_cancels_in_flight_orders_when_configured(config, full_db_session, now):
    broker = MockBroker(initial_cash=10_000.0, fill_mode=FillMode.NO_FILL)
    deps = deps_for(config, full_db_session, broker=broker)
    await deps.execution.submit(
        TradingInstruction(
            action=Action.BUY,
            symbol="NVDA.US",
            order_type=OrderType.LIMIT,
            quantity=10,
            limit_price=100.0,
            valid_seconds=300,
            confidence=0.8,
            client_order_id="co_1",
        ),
        1,
        now=now,
    )
    out = io.StringIO()

    cancelled = await cli.shutdown(
        deps, cli.SessionStats(), now=now, reason="test", out=out
    )

    assert cancelled == ["co_1"]
    assert repo.get_order(full_db_session, "co_1").status == repo.STATUS_CANCELLED
    assert await broker.query_open_orders() == []
    assert "cancelled 1 in-flight order(s)" in out.getvalue()


async def test_shutdown_leaves_orders_working_when_configured_not_to_cancel(
    config, full_db_session, now
):
    config.lifecycle.cancel_open_orders_on_shutdown = False
    broker = MockBroker(initial_cash=10_000.0, fill_mode=FillMode.NO_FILL)
    deps = deps_for(config, full_db_session, broker=broker)
    await deps.execution.submit(
        TradingInstruction(
            action=Action.BUY,
            symbol="NVDA.US",
            order_type=OrderType.LIMIT,
            quantity=10,
            limit_price=100.0,
            valid_seconds=300,
            confidence=0.8,
            client_order_id="co_1",
        ),
        1,
        now=now,
    )
    out = io.StringIO()

    cancelled = await cli.shutdown(deps, cli.SessionStats(), now=now, reason="test", out=out)

    assert cancelled == []
    assert len(await broker.query_open_orders()) == 1
    assert "leaving 1 order(s) working" in out.getvalue()


async def test_shutdown_mirrors_final_positions_from_the_broker(config, full_db_session, now):
    broker = MockBroker(
        initial_cash=10_000.0,
        initial_positions={
            "AMD.US": PositionSnapshot(symbol="AMD.US", quantity=20, cost_basis=48.0)
        },
    )
    deps = deps_for(config, full_db_session, broker=broker)
    out = io.StringIO()

    await cli.shutdown(deps, cli.SessionStats(), now=now, reason="test", out=out)

    assert "AMD.US" in out.getvalue()
    assert "end of session" in out.getvalue()


def test_session_summary_reports_the_reason_and_the_counts(config, full_db_session, now):
    deps = deps_for(config, full_db_session)
    stats = cli.SessionStats(rounds=3, submitted=1, rejected_by_risk=2, tokens_in=400)
    stats.gate_results["HOLD_NO_EVENTS"] = 2
    stats.gate_results["OK"] = 1

    text = cli.render_session_summary(deps, stats, [], now=now, reason="Ctrl-C")

    assert "Ctrl-C" in text
    assert "rounds run       : 3" in text
    assert "HOLD_NO_EVENTS x2" in text
    assert "1 submitted, 2 rejected by risk" in text


# --- argument handling ------------------------------------------------------


def test_md_address_defaults_and_can_come_from_the_environment(monkeypatch):
    args = cli.parse_args(["--config", "c.yaml"])
    assert args.md_address == cli.DEFAULT_MD_ADDRESS

    monkeypatch.setenv("TEVNNIS_MD_ADDRESS", "10.0.0.5:6000")
    assert cli.parse_args(["--config", "c.yaml"]).md_address == "10.0.0.5:6000"
    assert (
        cli.parse_args(["--config", "c.yaml", "--md-address", "h:1"]).md_address == "h:1"
    )


def test_the_bare_invocation_is_the_all_mock_run(tmp_path, config):
    """The legacy `--mock` flag is retired; the three axes default to mock.

    It had stopped being true -- `--mock --broker longbridge` placed real
    orders -- so it asserted nothing while reading like a safety guarantee.
    What actually gates --yes/--now is guard_allows_auto_confirm, below.
    """
    import yaml

    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config.model_dump(mode="json")))
    args = cli.parse_args(["--config", str(path), "--database-url", "sqlite:///:memory:"])

    assert (args.md_source, args.broker, args.llm) == ("mock", "mock", "mock")
    assert not hasattr(args, "mock")

    deps = cli._build_from_args(args)
    assert type(deps.trade_port).__name__ == "MockBroker"
    assert type(deps.llm_provider).__name__ == "MockLLM"
    assert cli.guard_allows_auto_confirm(deps)


def test_a_run_without_a_database_url_explains_itself(tmp_path, config, monkeypatch):
    import yaml

    monkeypatch.delenv("DATABASE_URL", raising=False)
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config.model_dump(mode="json")))
    args = cli.parse_args(["--config", str(path)])
    with pytest.raises(cli.StartupError, match="no database URL"):
        cli._build_from_args(args)


def test_expired_orders_are_reported_in_the_round_line():
    from tevnnis_core.agent import RoundReport

    text = cli.render_round(RoundReport(gate_result="OK", expired=["co_1"], instructions=0))
    assert "TIF expired, remainder cancelled: co_1" in text
    assert "no order placed this round" in text


def test_a_rolled_back_round_is_reported_as_an_error():
    from tevnnis_core.agent import RoundReport

    text = cli.render_round(RoundReport(gate_result="ERROR", error="RuntimeError: boom"))
    assert "rolled back" in text and "boom" in text


def test_time_deltas_used_by_the_loop_are_timezone_aware(config):
    """A naive clock would silently mis-window every §11 check."""
    assert at(11, 0).tzinfo is not None
    assert (at(11, 0) + timedelta(hours=1)).astimezone(ZoneInfo("UTC")).hour == 16


# --- §7/§14 public snapshot -------------------------------------------------


def read_snapshot(directory):
    return json.loads((directory / SNAPSHOT_JSON).read_text())


async def test_a_round_publishes_a_live_snapshot(config, full_db_session, tmp_path):
    deps = deps_for(config, full_db_session, snapshot_dir=tmp_path)
    stop = cli.StopFlag()
    await cli.run_loop(
        deps, stop=stop, once=True, now_fn=lambda: at(11), out=io.StringIO()
    )

    snapshot = read_snapshot(tmp_path)
    assert snapshot["demo"] is False
    assert snapshot["status"] == "running"
    assert snapshot["schema_version"] == 1
    # The round sampled the broker, so the curve has its first point.
    assert (tmp_path / SNAPSHOT_JS).exists()


async def test_shutdown_publishes_a_final_stopped_snapshot(config, full_db_session, tmp_path):
    deps = deps_for(config, full_db_session, snapshot_dir=tmp_path)
    stop = cli.StopFlag()
    stats = await cli.run_loop(
        deps, stop=stop, once=True, now_fn=lambda: at(11), out=io.StringIO()
    )
    assert read_snapshot(tmp_path)["status"] == "running"

    await cli.shutdown(deps, stats, now=at(16), reason="test", out=io.StringIO())
    assert read_snapshot(tmp_path)["status"] == "stopped"


async def test_no_snapshot_directory_means_nothing_is_written(
    config, full_db_session, tmp_path
):
    deps = deps_for(config, full_db_session, snapshot_dir=None)
    stop = cli.StopFlag()
    await cli.run_loop(
        deps, stop=stop, once=True, now_fn=lambda: at(11), out=io.StringIO()
    )
    assert list(tmp_path.iterdir()) == []


async def test_a_failing_snapshot_never_breaks_the_round(config, full_db_session, tmp_path):
    """Publication is presentation; a trading session must outlive its failure."""

    class DeadBroker(MockBroker):
        async def query_account(self):
            raise ConnectionError("broker went away after the round")

    deps = deps_for(
        config, full_db_session, broker=DeadBroker(initial_cash=10_000.0),
        snapshot_dir=tmp_path,
    )
    out = io.StringIO()
    stop = cli.StopFlag()
    stats = await cli.run_loop(deps, stop=stop, once=True, now_fn=lambda: at(11), out=out)

    assert stats.rounds == 1
    assert "[snapshot] could not publish" in out.getvalue()
    assert not (tmp_path / SNAPSHOT_JSON).exists()


async def test_the_published_snapshot_reflects_real_round_activity(
    config, full_db_session, tmp_path
):
    deps = deps_for(config, full_db_session, snapshot_dir=tmp_path)
    stop = cli.StopFlag()
    await cli.run_loop(
        deps, stop=stop, max_rounds=2, now_fn=lambda: at(11), sleep=noop_sleep,
        out=io.StringIO(),
    )

    snapshot = read_snapshot(tmp_path)
    # Two rounds, each logging a decision (HOLD is always logged, §8). Asserted
    # via `reasoning`, which is not day-filtered: `decisions.ts` comes from the
    # DB's server_default clock, so it does not follow this test's injected
    # `now` the way the explicitly-stamped samples do. In a real session the
    # two agree; here they need not, and telemetry's day window is covered
    # directly in test_snapshot_build.py.
    assert len(snapshot["reasoning"]["recent"]) == 2
    assert snapshot["reasoning"]["latest"] is not None
    assert snapshot["reasoning"]["latest"]["action"] == "HOLD"
    # Two equity samples means the curve is drawable.
    assert len(snapshot["performance"]["series"]["1D"]) == 2


def test_snapshot_dir_resolution_prefers_the_flag_over_config(tmp_path, config_path):
    args = cli.parse_args(
        [
            "--config", str(config_path),
            "--database-url", "sqlite:///:memory:",
            "--snapshot-dir", str(tmp_path / "elsewhere"),
        ]
    )
    deps = cli._build_from_args(args)
    assert deps.snapshot_dir == tmp_path / "elsewhere"


def test_no_snapshot_flag_disables_publication(config_path):
    args = cli.parse_args(
        ["--config", str(config_path), "--database-url", "sqlite:///:memory:",
         "--no-snapshot"]
    )
    assert cli._build_from_args(args).snapshot_dir is None


def test_snapshot_defaults_to_the_configured_output_dir(config_path):
    args = cli.parse_args(
        ["--config", str(config_path), "--database-url", "sqlite:///:memory:"]
    )
    deps = cli._build_from_args(args)
    assert deps.snapshot_dir == Path(deps.config.public_snapshot.output_dir)
