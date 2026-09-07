"""The deny-list test: things that must never appear in the published file.

The allow-list test proves the key set is exactly right. This one attacks from
the other side — it seeds rows carrying every field the snapshot must not
disclose and then searches the SERIALIZED snapshot for their names and values.
The two together are what make the sanitization boundary a claim you can check
rather than one you have to trust.
"""

from __future__ import annotations

import json

import pytest
from snapshot_fixtures import (
    LEAKY_THESIS,
    NOW,
    SECRET_BROKER_FILL_ID,
    SECRET_BROKER_ORDER_ID,
    SECRET_CASH,
    SECRET_CLIENT_ORDER_ID,
    SECRET_COST_BASIS,
    SECRET_EQUITY,
    SECRET_FEE,
    SECRET_QUANTITY,
    SECRET_RULE_TRIPPED,
    SECRET_SECTOR,
    seed,
)
from sqlalchemy import select

from tevnnis_core.db.models import Position
from tevnnis_core.snapshot import build_public_snapshot, forbidden_config_terms, scrub_thesis


@pytest.fixture
def snapshot(full_db_session, config) -> dict:
    seed(full_db_session)
    return build_public_snapshot(full_db_session, config=config, now=NOW)


@pytest.fixture
def serialized(snapshot) -> str:
    return json.dumps(snapshot)


def all_keys(node, out: set[str] | None = None) -> set[str]:
    """Every dict key anywhere in the snapshot."""
    out = set() if out is None else out
    if isinstance(node, dict):
        out.update(node.keys())
        for value in node.values():
            all_keys(value, out)
    elif isinstance(node, list):
        for item in node:
            all_keys(item, out)
    return out


def all_scalars(node, out: list | None = None) -> list:
    """Every scalar leaf anywhere in the snapshot."""
    out = [] if out is None else out
    if isinstance(node, dict):
        for value in node.values():
            all_scalars(value, out)
    elif isinstance(node, list):
        for item in node:
            all_scalars(item, out)
    else:
        out.append(node)
    return out


#: Column and concept names that must never appear as a key or in a value.
FORBIDDEN_NAMES = [
    "quantity",
    "cost_basis",
    "broker_order_id",
    "broker_fill_id",
    "client_order_id",
    "order_id",
    "fill_id",
    "fee",
    "sector",
    "rule_tripped",
    "cash",
    "equity",
    "net_liquidation",
    "buying_power",
    "managed_capital",
    "account",
    "api_key",
    "secret",
    "token",
    "password",
]


def test_no_forbidden_field_name_appears(snapshot):
    """Checked against the KEY SET, which is the structural claim.

    Not a substring search over the serialized blob: the English word "cash"
    legitimately occurs inside scrubbed prose, and matching that would be a
    false alarm that trains people to ignore this test. What must never happen
    is a *field* called cash — and that is what is asserted.
    """
    keys = {key.lower() for key in all_keys(snapshot)}
    for name in FORBIDDEN_NAMES:
        assert not any(name in key for key in keys), f"{name!r} leaked as a snapshot key"


def test_no_forbidden_value_appears(serialized):
    forbidden_values = [
        str(SECRET_COST_BASIS),
        str(SECRET_QUANTITY),
        str(SECRET_FEE),
        SECRET_BROKER_FILL_ID,
        SECRET_BROKER_ORDER_ID,
        SECRET_CLIENT_ORDER_ID,
        SECRET_RULE_TRIPPED,
        SECRET_SECTOR,
        str(SECRET_EQUITY),
        str(SECRET_CASH),
    ]
    for value in forbidden_values:
        assert value not in serialized, f"{value!r} leaked into the snapshot"


def test_no_risk_limit_or_budget_name_appears(snapshot, serialized, config):
    """§6 keeps these invisible even to the LLM; they must stay invisible here.

    Checked against the pydantic field sets, so a limit added to the config in
    future is covered by this test the day it is added.

    The NAMES are distinctive enough to search the whole serialized text for —
    they must not survive even inside scrubbed prose.

    The VALUES are checked against scalar leaves rather than as substrings
    (`0.5` would otherwise "match" inside an index point of `100.55`), and only
    the DISTINCTIVE ones: a fractional threshold like 0.25 or a notional like
    3000 identifies a limit, whereas a small integer like
    `max_day_trades_per_week: 3` inevitably collides with an honest count
    (`decisions_total: 3`) and reveals nothing. Asserting on those would be a
    test that cannot pass and would have to be deleted, not a real guarantee.
    """
    lowered = serialized.lower()
    for term in forbidden_config_terms():
        assert term not in lowered, f"risk/budget config name {term!r} leaked"

    def distinctive(value) -> bool:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return False
        return value != int(value) or abs(value) >= 1000

    published = set(all_scalars(snapshot))
    limits = config.risk_limits.model_dump() | config.budgets.model_dump()
    checked = [value for value in limits.values() if distinctive(value)]
    assert checked, "the fixture config must carry at least one distinctive limit"
    for value in checked:
        assert value not in published, f"risk/budget value {value!r} leaked"


def test_a_new_model_column_cannot_leak(full_db_session, config):
    """The anti-`asdict` guarantee, tested rather than asserted in a comment.

    `build_public_snapshot` assigns every leaf by name, so a field the builder
    does not know about has no path into the output. Simulated here by hanging
    a new attribute on a seeded row: an `asdict()`/`__dict__`-based projection
    would carry it straight through.
    """
    seed(full_db_session)
    position = full_db_session.execute(
        select(Position).where(Position.symbol == "NVDA.US")
    ).scalar_one()
    position.__dict__["margin_requirement_usd"] = 4242.42
    position.__dict__["custodian_account_ref"] = "ACCT-LEAK-9999"

    serialized = json.dumps(
        build_public_snapshot(full_db_session, config=config, now=NOW)
    )
    assert "margin_requirement_usd" not in serialized
    assert "4242.42" not in serialized
    assert "custodian_account_ref" not in serialized
    assert "ACCT-LEAK-9999" not in serialized


def test_the_risk_field_is_only_ever_one_of_three_words(snapshot):
    allowed = {"cleared", "blocked", "none"}
    assert snapshot["reasoning"]["latest"]["risk"] in allowed
    for entry in snapshot["reasoning"]["recent"]:
        assert entry["risk"] in allowed

    # The blocked decision is present — so the rule name was available to leak
    # and did not, rather than the test passing because nothing was blocked.
    assert any(e["risk"] == "blocked" for e in snapshot["reasoning"]["recent"])


def test_the_thesis_scrubber_masks_account_shaped_figures():
    scrubbed = scrub_thesis(LEAKY_THESIS, forbidden_terms=forbidden_config_terms())

    assert "$41,337.42" not in scrubbed          # a currency amount
    assert "9876" not in scrubbed                # a long digit run
    assert "500 shares" not in scrubbed          # a share count
    assert "max_position_pct" not in scrubbed    # a §6 limit name
    assert "0.25" not in scrubbed                # ...and its threshold
    assert "1234567890" not in scrubbed          # an account-shaped number
    # It is still readable prose, not a row of blanks.
    assert "NVDA" in scrubbed


def test_the_thesis_scrubber_caps_length():
    long_thesis = "word " * 500
    scrubbed = scrub_thesis(long_thesis, max_chars=100)
    assert len(scrubbed) <= 101  # the cap plus the ellipsis
    assert scrubbed.endswith("…")


def test_the_scrubber_handles_empty_and_missing_input():
    assert scrub_thesis(None) == ""
    assert scrub_thesis("") == ""
    assert scrub_thesis("   ") == ""


def test_position_weights_are_percentages_not_values(snapshot):
    """Weights sum to ~100 and no dollar position value is present."""
    weights = [p["weight_pct"] for p in snapshot["positions"]]
    assert all(0 <= w <= 100 for w in weights)
    assert sum(weights) == pytest.approx(100.0, abs=0.5)

    # 731 NVDA at 231.09 is ~168,927 — the product must appear nowhere.
    assert "168" not in json.dumps(snapshot["positions"])


# ---------------------------------------------------------------------------
# The wake threshold (cadence.reason_min_priority)
#
# This was a latent leak that passed only by coincidence. The gate reason core
# writes into a gated HOLD's session_note used to read
# "... below reason_min_priority=HIGH", and that note becomes the published
# thesis. It was masked ONLY because `reason_min_priority=HIGH` happens to be a
# 24-character run of [A-Za-z0-9_=-], tripping redact_secrets' 20-character
# credential-shape heuristic. Add one space, or rephrase, and the threshold went
# out to a world-readable file with nothing to catch it.
#
# Fixed at the source (agent.py's GateOutcome.public_reason) AND covered by name
# in the scrubber. These tests pin both, and deliberately do not lean on
# redact_secrets.
# ---------------------------------------------------------------------------


@pytest.fixture
def no_length_heuristic(monkeypatch):
    """Neuter redact_secrets so only the deliberate rules are under test.

    Without this, several of the assertions below would pass for the wrong
    reason — which is exactly the failure mode being fixed.
    """
    import tevnnis_core.snapshot as snapshot_module

    monkeypatch.setattr(snapshot_module, "redact_secrets", lambda text: text)


def test_cadence_gate_fields_are_in_the_forbidden_term_set():
    """Covered by NAME, not by the length of a particular rendering."""
    terms = forbidden_config_terms()
    assert "reason_min_priority" in terms
    # The whole CadenceConfig field set, so a future gate knob is covered the
    # day it is added rather than the day it leaks.
    from tevnnis_core.config import CadenceConfig

    assert set(CadenceConfig.model_fields) <= terms


@pytest.mark.parametrize(
    "note",
    [
        "highest priority in batch is MEDIUM, below reason_min_priority=HIGH",
        "highest priority in batch is MEDIUM, below reason_min_priority = HIGH",
        "highest priority in batch is MEDIUM, below reason_min_priority : HIGH",
        "highest priority in batch is MEDIUM, below reason_min_priority is HIGH",
        "highest priority in batch is MEDIUM, below the HIGH threshold",
        "batch under the wake threshold of CRITICAL",
        "nothing cleared the gate is HIGH this round",
        "reason_min_priority=HIGH",
    ],
)
def test_the_wake_threshold_never_survives_scrubbing(note, no_length_heuristic):
    """Robust to spacing and rephrasing, with the length heuristic disabled.

    Each of these renders the same fact. Masking must not depend on any of them
    happening to exceed a token-length threshold.
    """
    scrubbed = scrub_thesis(note)
    assert "reason_min_priority" not in scrubbed
    assert "HIGH" not in scrubbed
    assert "CRITICAL" not in scrubbed


def test_the_observed_batch_priority_is_still_published(no_length_heuristic):
    """The masking must be narrow enough to leave the useful half intact.

    "highest priority in batch is MEDIUM" is an OBSERVATION about the events
    that actually arrived — not a configuration value — and it is the entire
    reason the log line is worth showing. A rule that swallowed it too would
    make the AI Reasoning zone useless.
    """
    scrubbed = scrub_thesis("highest priority in batch is MEDIUM, below the wake threshold")
    assert "MEDIUM" in scrubbed
    assert "wake threshold" in scrubbed
    assert "[redacted]" not in scrubbed


def test_ordinary_prose_mentioning_a_level_is_untouched(no_length_heuristic):
    """No blanket ban on the words themselves — only on threshold contexts."""
    thesis = "Conviction is HIGH on NVDA after the move; volume confirms it."
    assert scrub_thesis(thesis) == thesis


def test_the_scrubber_defaults_to_the_forbidden_set(no_length_heuristic):
    """A caller that forgets the argument still gets the protection.

    Forgetting is how this leak survived in the first place, so the safe set is
    the default and opting out is the explicit act.
    """
    note = "below reason_min_priority = HIGH"
    assert "HIGH" not in scrub_thesis(note)
    assert "HIGH" in scrub_thesis(note, forbidden_terms=())


def test_a_gated_hold_publishes_no_threshold_end_to_end(full_db_session, config):
    """The real path: cheap_gate -> session_note -> snapshot thesis.

    Asserted on the built snapshot rather than on the scrubber, so it holds even
    if the scrubber and the source fix are both changed later.
    """
    from tevnnis_core.agent import GATE_HOLD_LOW_PRIORITY, cheap_gate
    from tevnnis_core.db.models import Decision
    from tevnnis_core.llm.budget import BudgetStatus
    from tevnnis_core.market_data import EventRecord, PulledBatch

    batch = PulledBatch(
        records=[
            EventRecord(
                event_id="e-1", type="QUOTE_MOVE", priority="MEDIUM",
                event_ts=0, ingest_ts=0, symbol="NVDA.US", sector="Semiconductor",
            )
        ],
        selected=[], sectors=[], last_prices={}, status_updates={},
        next_cursor="", dropped_count=0,
    )
    outcome = cheap_gate(
        batch,
        budget=BudgetStatus(True, 0, 0),
        reason_min_priority=config.cadence.reason_min_priority,
    )
    assert outcome.result == GATE_HOLD_LOW_PRIORITY

    # Exactly what AgentLoop._log_hold persists.
    full_db_session.add(
        Decision(
            decision_id="d-gated",
            ts=NOW,
            gate_result=outcome.result,
            session_note=outcome.published_reason,
        )
    )
    full_db_session.commit()

    snapshot = build_public_snapshot(full_db_session, config=config, now=NOW)
    serialized = json.dumps(snapshot)
    assert "reason_min_priority" not in serialized
    assert config.cadence.reason_min_priority not in serialized
    # ...and the round is still explained to the reader.
    assert "MEDIUM" in snapshot["reasoning"]["latest"]["thesis"]
