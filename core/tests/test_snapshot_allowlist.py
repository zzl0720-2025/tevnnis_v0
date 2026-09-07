"""The allow-list test: the snapshot's key set is EXACTLY this, at every node.

`build_public_snapshot` is an explicit field-by-field projection so this test
can be exhaustive: it walks the
built dict recursively and demands set equality of keys at every level. A new
key fails, and so does a missing one — which means no field can reach the public
file without someone deliberately adding it here as well.
"""

from __future__ import annotations

from typing import Any

import pytest
from snapshot_fixtures import NOW, seed

from tevnnis_core.snapshot import SCHEMA_VERSION, build_public_snapshot

#: The contract. Keys are node paths; "[]" marks "every element of this list".
ALLOWED: dict[str, set[str]] = {
    "": {
        "schema_version",
        "generated_at",
        "status",
        "demo",
        "performance",
        "positions",
        "telemetry",
        "news",
        "reasoning",
        "footer",
    },
    "performance": {
        "index_current",
        "index_start",
        "cumulative_pct",
        "today_pct",
        "series",
    },
    "performance.series": {"1D", "1W", "1M", "ALL"},
    "positions[]": {"symbol", "weight_pct", "last", "today_pct", "trend"},
    "telemetry": {
        "events_seen",
        "events_acted",
        "decisions_total",
        "decisions_hold",
        "decisions_act",
        "risk_checks",
        "risk_cleared",
        "risk_blocked",
        "positions_open",
        "win_rate",
    },
    "news[]": {"time", "symbol", "headline", "url"},
    "reasoning": {"latest", "recent"},
    "reasoning.latest": {"ts", "action", "symbol", "model", "risk", "thesis"},
    "reasoning.recent[]": {"time", "action", "thesis", "risk"},
    "footer": {"universe", "held", "persona", "model", "ai_cost_today"},
}


@pytest.fixture
def snapshot(full_db_session, config) -> dict[str, Any]:
    seed(full_db_session)
    return build_public_snapshot(full_db_session, config=config, now=NOW)


def _walk(node: Any, path: str, seen: dict[str, set[str]]) -> None:
    """Collect the key set at every dict node, keyed by its allow-list path."""
    if isinstance(node, dict):
        seen.setdefault(path, set()).update(node.keys())
        for key, value in node.items():
            # A node whose own path is in ALLOWED describes its children by
            # path; anything else (e.g. a series entry) is a leaf list/scalar.
            child = f"{path}.{key}" if path else key
            _walk(value, child, seen)
    elif isinstance(node, list):
        for item in node:
            _walk(item, f"{path}[]", seen)


def test_the_snapshot_key_set_is_exactly_the_allow_list(snapshot):
    seen: dict[str, set[str]] = {}
    _walk(snapshot, "", seen)

    # Every dict node the builder produced must be one we described...
    described = set(ALLOWED)
    produced = set(seen)
    assert produced - described == set(), (
        f"undeclared dict node(s) in the snapshot: {sorted(produced - described)}. "
        "A new nested object must be added to ALLOWED deliberately."
    )
    # ...and every node we described must have appeared.
    assert described - produced == set(), (
        f"declared node(s) missing from the snapshot: {sorted(described - produced)}"
    )
    # ...with exactly the keys declared for it.
    for path, keys in ALLOWED.items():
        assert seen[path] == keys, (
            f"key mismatch at {path!r}: unexpected={sorted(seen[path] - keys)} "
            f"missing={sorted(keys - seen[path])}"
        )


def test_leaf_types_match_the_documented_schema(snapshot):
    assert snapshot["schema_version"] == SCHEMA_VERSION
    assert snapshot["generated_at"].endswith("Z")
    assert snapshot["status"] in {"running", "stopped", "starting"}
    assert snapshot["demo"] is False

    perf = snapshot["performance"]
    assert perf["index_start"] == 100.0
    assert isinstance(perf["index_current"], float)
    assert isinstance(perf["cumulative_pct"], float)
    assert perf["today_pct"] is None or isinstance(perf["today_pct"], float)
    for label, series in perf["series"].items():
        assert isinstance(series, list), label
        assert all(isinstance(point, float) for point in series), label

    for position in snapshot["positions"]:
        assert isinstance(position["symbol"], str)
        assert position["weight_pct"] is None or isinstance(position["weight_pct"], float)
        assert position["last"] is None or isinstance(position["last"], float)
        assert isinstance(position["trend"], list)
        assert len(position["trend"]) != 1, "a single point is not a trend"

    telemetry = snapshot["telemetry"]
    for key, value in telemetry.items():
        if key == "win_rate":
            assert value is None or isinstance(value, float)
        else:
            assert isinstance(value, int), key

    for item in snapshot["news"]:
        assert isinstance(item["headline"], str) and item["headline"]
        assert item["url"] is None or item["url"].startswith("https://")

    reasoning = snapshot["reasoning"]
    assert reasoning["latest"]["risk"] in {"cleared", "blocked", "none"}
    for entry in reasoning["recent"]:
        assert entry["risk"] in {"cleared", "blocked", "none"}
        assert isinstance(entry["thesis"], str)

    footer = snapshot["footer"]
    assert all(isinstance(symbol, str) for symbol in footer["universe"])
    assert all(isinstance(symbol, str) for symbol in footer["held"])
    assert all(isinstance(label, str) for label in footer["persona"])
    assert footer["ai_cost_today"] is None or isinstance(footer["ai_cost_today"], float)


def test_ai_cost_today_is_the_only_dollar_figure(snapshot):
    """Every other number is an index, a percentage, a count, or a market price.

    Enumerated rather than pattern-matched: this is the invariant that keeps
    account size out of the file, so it is worth stating explicitly.
    """
    assert snapshot["footer"]["ai_cost_today"] == pytest.approx(0.09)

    # The equity behind the curve is ~41337 dollars; the index is ~100-ish.
    assert 50.0 < snapshot["performance"]["index_current"] < 200.0
    # Weights are percentages of the sleeve and sum to ~100.
    weights = [p["weight_pct"] for p in snapshot["positions"] if p["weight_pct"] is not None]
    assert sum(weights) == pytest.approx(100.0, abs=0.5)


def test_an_empty_database_still_produces_a_complete_snapshot(full_db_session, config):
    """The very first round has no samples, no decisions and no news."""
    snapshot = build_public_snapshot(full_db_session, config=config, now=NOW)

    seen: dict[str, set[str]] = {}
    _walk(snapshot, "", seen)
    # Only the list-element and `latest` nodes may be absent when empty.
    optional = {"positions[]", "news[]", "reasoning.recent[]", "reasoning.latest"}
    for path, keys in ALLOWED.items():
        if path in optional and path not in seen:
            continue
        assert seen[path] == keys, path

    assert snapshot["performance"]["series"] == {"1D": [], "1W": [], "1M": [], "ALL": []}
    assert snapshot["performance"]["index_current"] == 100.0
    assert snapshot["positions"] == []
    assert snapshot["news"] == []
    assert snapshot["reasoning"]["latest"] is None
    assert snapshot["telemetry"]["win_rate"] is None
    assert snapshot["footer"]["ai_cost_today"] is None
