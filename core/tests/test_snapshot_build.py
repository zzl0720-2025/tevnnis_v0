"""How each snapshot section is derived, and how it reaches disk.

The allow-list and deny-list tests pin *what* may be published. This one pins
that the published numbers are actually right — an indexed curve that silently
computed the wrong base would pass both of those and still be wrong.
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from snapshot_fixtures import NOW, seed
from sqlalchemy import select

from tevnnis_core.db import repository as repo
from tevnnis_core.db.models import (
    Decision,
    Fill,
    Instruction,
    Order,
    PortfolioSample,
    Position,
    PriceSample,
)
from tevnnis_core.snapshot import (
    SNAPSHOT_GLOBAL,
    SNAPSHOT_JS,
    SNAPSHOT_JSON,
    build_public_snapshot,
    write_public_snapshot,
)


def build(session, config, **kwargs):
    return build_public_snapshot(session, config=config, now=NOW, **kwargs)


# ---------------------------------------------------------------------------
# performance
# ---------------------------------------------------------------------------


def test_the_curve_is_indexed_to_100_from_the_first_sample(full_db_session, config):
    full_db_session.add(PortfolioSample(ts=NOW - timedelta(days=1), equity=10000.0, cash=0.0))
    full_db_session.add(PortfolioSample(ts=NOW, equity=10142.0, cash=0.0))
    full_db_session.commit()

    perf = build(full_db_session, config)["performance"]
    assert perf["index_start"] == 100.0
    assert perf["index_current"] == 101.42
    assert perf["cumulative_pct"] == 1.42
    assert perf["series"]["ALL"] == [100.0, 101.42]
    # The dollar equity behind it appears nowhere.
    assert 10142.0 not in perf["series"]["ALL"]


def test_a_window_with_fewer_than_two_samples_is_empty(full_db_session, config):
    full_db_session.add(PortfolioSample(ts=NOW, equity=10000.0, cash=0.0))
    full_db_session.commit()

    perf = build(full_db_session, config)["performance"]
    assert perf["series"] == {"1D": [], "1W": [], "1M": [], "ALL": []}
    assert perf["index_current"] == 100.0
    assert perf["today_pct"] is None


def test_the_1d_window_and_today_pct_describe_the_same_period(full_db_session, config):
    """Both use the calendar day, so the curve and the delta beside it agree."""
    seed(full_db_session)
    perf = build(full_db_session, config)["performance"]

    one_day = perf["series"]["1D"]
    assert len(one_day) >= 2
    implied = round((one_day[-1] - one_day[0]) / one_day[0] * 100.0, 2)
    assert implied == pytest.approx(perf["today_pct"], abs=0.01)


def test_a_long_series_is_downsampled_but_keeps_its_endpoints(full_db_session, config):
    for day in range(200, 0, -1):
        full_db_session.add(
            PortfolioSample(ts=NOW - timedelta(days=day), equity=10000.0 + day, cash=0.0)
        )
    full_db_session.add(PortfolioSample(ts=NOW, equity=12000.0, cash=0.0))
    full_db_session.commit()

    series = build(full_db_session, config)["performance"]["series"]["ALL"]
    assert len(series) <= config.public_snapshot.series_max_points
    assert series[0] == 100.0
    assert series[-1] == build(full_db_session, config)["performance"]["index_current"]


# ---------------------------------------------------------------------------
# positions
# ---------------------------------------------------------------------------


def test_weights_come_from_quantity_times_price_without_either_escaping(
    full_db_session, config
):
    full_db_session.add(Position(symbol="AAA.US", quantity=100, cost_basis=1.0))
    full_db_session.add(Position(symbol="BBB.US", quantity=50, cost_basis=1.0))
    full_db_session.add(PriceSample(ts=NOW, symbol="AAA.US", last=30.0, change_pct=1.0))
    full_db_session.add(PriceSample(ts=NOW, symbol="BBB.US", last=20.0, change_pct=-1.0))
    full_db_session.commit()

    positions = build(full_db_session, config)["positions"]
    weights = {p["symbol"]: p["weight_pct"] for p in positions}
    # 3000 vs 1000 -> 75% / 25%
    assert weights == {"AAA.US": 75.0, "BBB.US": 25.0}

    serialized = json.dumps(positions)
    for leaked in ("100", "3000", "1000", "quantity"):
        assert leaked not in serialized, leaked


def test_a_position_with_no_price_sample_has_no_weight(full_db_session, config):
    full_db_session.add(Position(symbol="ZZZ.US", quantity=10, cost_basis=1.0))
    full_db_session.commit()

    position = build(full_db_session, config)["positions"][0]
    assert position["weight_pct"] is None
    assert position["last"] is None
    assert position["trend"] == []


def test_a_trend_needs_at_least_two_points(full_db_session, config):
    full_db_session.add(Position(symbol="AAA.US", quantity=1, cost_basis=1.0))
    full_db_session.add(PriceSample(ts=NOW, symbol="AAA.US", last=10.0, change_pct=0.0))
    full_db_session.commit()
    assert build(full_db_session, config)["positions"][0]["trend"] == []

    full_db_session.add(
        PriceSample(ts=NOW + timedelta(minutes=5), symbol="AAA.US", last=11.0, change_pct=10.0)
    )
    full_db_session.commit()
    assert build(full_db_session, config)["positions"][0]["trend"] == [10.0, 11.0]


def test_a_closed_position_is_not_published(full_db_session, config):
    full_db_session.add(Position(symbol="GONE.US", quantity=0, cost_basis=1.0))
    full_db_session.commit()
    assert build(full_db_session, config)["positions"] == []


# ---------------------------------------------------------------------------
# telemetry
# ---------------------------------------------------------------------------


def test_telemetry_counts_match_the_seeded_day(full_db_session, config):
    seed(full_db_session)
    telemetry = build(full_db_session, config)["telemetry"]

    assert telemetry["events_seen"] == 4          # 3 news + 1 quote
    assert telemetry["events_acted"] == 2         # the BUY cited two event ids
    assert telemetry["decisions_total"] == 3
    assert telemetry["decisions_act"] == 2        # BUY + the blocked SELL
    assert telemetry["decisions_hold"] == 1       # the gated HOLD
    assert telemetry["risk_checks"] == 2
    assert telemetry["risk_cleared"] == 1
    assert telemetry["risk_blocked"] == 1
    assert telemetry["positions_open"] == 3


def test_yesterdays_activity_is_not_counted_today(full_db_session, config):
    seed(full_db_session)
    today = build(full_db_session, config)["telemetry"]
    tomorrow = build_public_snapshot(
        full_db_session, config=config, now=NOW + timedelta(days=1)
    )["telemetry"]

    assert today["decisions_total"] == 3
    assert tomorrow["decisions_total"] == 0
    assert tomorrow["events_seen"] == 0
    # Position counts are point-in-time, not daily, so they do not reset.
    assert tomorrow["positions_open"] == 3


def test_win_rate_is_null_until_a_round_trip_closes(full_db_session, config):
    seed(full_db_session)
    assert build(full_db_session, config)["telemetry"]["win_rate"] is None


def _round_trip(session, symbol, buy_price, sell_price, *, index):
    """A complete buy-then-sell of 10 shares, through orders and instructions."""
    decision = Decision(decision_id=f"d-{symbol}", ts=NOW, gate_result="OK")
    session.add(decision)
    session.flush()
    for leg, (action, price) in enumerate((("BUY", buy_price), ("SELL", sell_price))):
        instruction = Instruction(
            decision_id=decision.decision_id,
            action=action,
            symbol=symbol,
            order_type="LIMIT",
            quantity=10,
            limit_price=price,
            cited_event_ids=[],
        )
        session.add(instruction)
        session.flush()
        order = Order(
            client_order_id=f"co-{symbol}-{leg}",
            instruction_id=instruction.id,
            status="filled",
            ts=NOW + timedelta(seconds=index * 10 + leg),
        )
        session.add(order)
        session.flush()
        session.add(
            Fill(
                order_id=order.id,
                broker_fill_id=f"bf-{symbol}-{leg}",
                quantity=10,
                price=price,
                fee=0.0,
                ts=NOW + timedelta(seconds=index * 10 + leg),
            )
        )
    session.commit()


def test_win_rate_counts_closed_round_trips(full_db_session, config):
    _round_trip(full_db_session, "WIN.US", buy_price=100.0, sell_price=110.0, index=0)
    _round_trip(full_db_session, "LOSE.US", buy_price=100.0, sell_price=90.0, index=1)

    assert build(full_db_session, config)["telemetry"]["win_rate"] == 0.5

    _round_trip(full_db_session, "WIN2.US", buy_price=50.0, sell_price=60.0, index=2)
    win_rate = build(full_db_session, config)["telemetry"]["win_rate"]
    assert win_rate == pytest.approx(0.6667, abs=1e-4)


def test_an_open_position_is_not_a_closed_round_trip(full_db_session, config):
    decision = Decision(decision_id="d-open", ts=NOW, gate_result="OK")
    full_db_session.add(decision)
    full_db_session.flush()
    instruction = Instruction(
        decision_id="d-open", action="BUY", symbol="AAA.US", order_type="LIMIT",
        quantity=10, limit_price=100.0, cited_event_ids=[],
    )
    full_db_session.add(instruction)
    full_db_session.flush()
    order = Order(client_order_id="co-open", instruction_id=instruction.id, status="filled",
                  ts=NOW)
    full_db_session.add(order)
    full_db_session.flush()
    full_db_session.add(
        Fill(order_id=order.id, broker_fill_id="bf-open", quantity=10, price=100.0, fee=0.0,
             ts=NOW)
    )
    full_db_session.commit()

    assert build(full_db_session, config)["telemetry"]["win_rate"] is None


# ---------------------------------------------------------------------------
# reasoning
# ---------------------------------------------------------------------------


def test_risk_maps_from_the_allow_column_alone(full_db_session, config):
    seed(full_db_session)
    recent = {e["action"]: e["risk"] for e in build(full_db_session, config)["reasoning"]["recent"]}
    assert recent["BUY"] == "cleared"
    assert recent["SELL"] == "blocked"
    assert recent["HOLD"] == "none"  # a gated HOLD is never risk-checked


def test_a_gated_hold_publishes_its_session_note_as_the_thesis(full_db_session, config):
    seed(full_db_session)
    latest = build(full_db_session, config)["reasoning"]["latest"]
    assert latest["action"] == "HOLD"
    assert latest["symbol"] is None
    assert "MEDIUM" in latest["thesis"]


def test_the_acting_instruction_wins_over_a_hold_in_the_same_decision(
    full_db_session, config
):
    decision = Decision(decision_id="d-mixed", ts=NOW, gate_result="OK")
    full_db_session.add(decision)
    full_db_session.flush()
    full_db_session.add(
        Instruction(decision_id="d-mixed", action="HOLD", symbol="AAA.US",
                    order_type="LIMIT", thesis="holding AAA", cited_event_ids=[])
    )
    full_db_session.add(
        Instruction(decision_id="d-mixed", action="BUY", symbol="BBB.US", order_type="LIMIT",
                    quantity=1, limit_price=1.0, thesis="buying BBB", cited_event_ids=[])
    )
    full_db_session.commit()

    latest = build(full_db_session, config)["reasoning"]["latest"]
    assert latest["action"] == "BUY"
    assert latest["symbol"] == "BBB.US"
    assert latest["thesis"] == "buying BBB"


# ---------------------------------------------------------------------------
# record_samples
# ---------------------------------------------------------------------------


def test_record_samples_writes_one_equity_point_and_one_price_per_symbol(full_db_session):
    repo.record_samples(
        full_db_session,
        now=NOW,
        equity=10000.0,
        cash=500.0,
        prices={"AAA.US": (10.0, 1.5), "BBB.US": (20.0, None)},
    )
    full_db_session.commit()

    assert len(full_db_session.execute(select(PortfolioSample)).scalars().all()) == 1
    prices = (
        full_db_session.execute(select(PriceSample).order_by(PriceSample.symbol))
        .scalars()
        .all()
    )
    assert [(p.symbol, p.last, p.change_pct) for p in prices] == [
        ("AAA.US", 10.0, 1.5),
        ("BBB.US", 20.0, None),
    ]


@pytest.mark.parametrize("equity", [0.0, -1.0, float("nan"), float("inf")])
def test_record_samples_refuses_an_unusable_equity(full_db_session, equity):
    """A bad reading would become a permanent spike, or a window's divisor."""
    repo.record_samples(full_db_session, now=NOW, equity=equity, cash=0.0, prices={})
    full_db_session.commit()
    assert full_db_session.execute(select(PortfolioSample)).scalars().all() == []


def test_record_samples_skips_an_unusable_price(full_db_session):
    repo.record_samples(
        full_db_session,
        now=NOW,
        equity=100.0,
        cash=0.0,
        prices={"GOOD.US": (10.0, 1.0), "BAD.US": (0.0, 1.0), "NAN.US": (float("nan"), 1.0)},
    )
    full_db_session.commit()
    symbols = full_db_session.execute(select(PriceSample.symbol)).scalars().all()
    assert symbols == ["GOOD.US"]


# ---------------------------------------------------------------------------
# atomic publication
# ---------------------------------------------------------------------------


def test_write_produces_both_files_and_the_js_defines_the_global(
    full_db_session, config, tmp_path
):
    seed(full_db_session)
    snapshot = build(full_db_session, config)
    written = write_public_snapshot(snapshot, tmp_path)

    assert [path.name for path in written] == [SNAPSHOT_JSON, SNAPSHOT_JS]
    assert json.loads((tmp_path / SNAPSHOT_JSON).read_text()) == snapshot

    js = (tmp_path / SNAPSHOT_JS).read_text()
    assert js.startswith("//")
    assert f"window.{SNAPSHOT_GLOBAL} = " in js
    # The .js payload is byte-identical JSON to the .json payload.
    payload = js.split(f"window.{SNAPSHOT_GLOBAL} = ", 1)[1].rstrip().rstrip(";")
    assert json.loads(payload) == snapshot


def test_a_rewrite_replaces_in_place_and_leaves_no_temp_files(
    full_db_session, config, tmp_path
):
    seed(full_db_session)
    write_public_snapshot(build(full_db_session, config), tmp_path)
    write_public_snapshot(build(full_db_session, config, status="stopped"), tmp_path)

    assert sorted(p.name for p in tmp_path.iterdir()) == [SNAPSHOT_JS, SNAPSHOT_JSON]
    assert json.loads((tmp_path / SNAPSHOT_JSON).read_text())["status"] == "stopped"


def test_a_failed_write_leaves_the_previous_file_intact(
    full_db_session, config, tmp_path, monkeypatch
):
    """The point of the temp-file + os.replace dance.

    The dashboard may read at any moment, so a write that dies partway must
    leave the last good snapshot in place rather than a truncated file.
    """
    seed(full_db_session)
    write_public_snapshot(build(full_db_session, config), tmp_path)
    good = (tmp_path / SNAPSHOT_JSON).read_text()

    import tevnnis_core.snapshot as snapshot_module

    def explode(src, dst):
        raise OSError("simulated failure after the temp file was written")

    monkeypatch.setattr(snapshot_module.os, "replace", explode)
    with pytest.raises(OSError):
        write_public_snapshot(build(full_db_session, config, status="stopped"), tmp_path)

    assert (tmp_path / SNAPSHOT_JSON).read_text() == good
    assert sorted(p.name for p in tmp_path.iterdir()) == [SNAPSHOT_JS, SNAPSHOT_JSON]


def test_the_output_directory_is_created_if_missing(full_db_session, config, tmp_path):
    target = tmp_path / "nested" / "frontend"
    write_public_snapshot(build(full_db_session, config), target)
    assert (target / SNAPSHOT_JSON).exists()


def test_the_snapshot_is_json_serializable_without_nan(full_db_session, config):
    """`allow_nan=False` — a NaN would serialize to invalid JSON the page cannot parse."""
    seed(full_db_session)
    json.dumps(build(full_db_session, config), allow_nan=False)
