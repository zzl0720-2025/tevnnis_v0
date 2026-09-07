"""The dashboard reads the snapshot and nothing else — pinned, not assumed.

`frontend/dashboard.html` has no test runner of its own, and it is the one
artefact that faces outward. These are cheap static checks over the file that
catch the failures that would actually matter: the page reaching for data the
snapshot does not carry, gaining a network call, or drifting out of sync with
the builder's field names.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from tevnnis_core.snapshot import SCHEMA_VERSION, SNAPSHOT_GLOBAL, SNAPSHOT_JS

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"
DASHBOARD = FRONTEND / "dashboard.html"
DEMO = FRONTEND / "public_snapshot.demo.js"


@pytest.fixture(scope="module")
def page() -> str:
    return DASHBOARD.read_text()


def strip_comments(text: str) -> str:
    """Executable text only.

    The page's comments deliberately mention `fetch()` and `$0.00` to explain
    why neither is used, so scanning raw source would flag the explanation as
    the violation it describes.
    """
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    return re.sub(r"^\s*//.*$", "", text, flags=re.MULTILINE)


@pytest.fixture(scope="module")
def code(page) -> str:
    return strip_comments(page)


@pytest.fixture(scope="module")
def demo_snapshot() -> dict:
    raw = DEMO.read_text()
    payload = raw.split(f"window.{SNAPSHOT_GLOBAL} = ", 1)[1].rstrip().rstrip(";\n").rstrip(";")
    return json.loads(payload)


# ---------------------------------------------------------------------------
# isolation
# ---------------------------------------------------------------------------


def test_the_page_loads_the_snapshot_the_writer_actually_writes(page):
    """The filename is a contract between snapshot.py and the page."""
    assert f'<script src="{SNAPSHOT_JS}"></script>' in page
    assert '<script src="public_snapshot.demo.js"></script>' in page
    # Demo first, live second: the live file must be able to overwrite it.
    assert page.index("public_snapshot.demo.js") < page.index(f'src="{SNAPSHOT_JS}"')


def test_the_page_makes_no_network_call_of_its_own(code):
    """No fetch, no XHR, no socket — its only input is the global.

    Also the reason the snapshot ships as .js rather than being fetched: under
    file:// a fetch would be CORS-blocked, so the page could not open by
    double-click at all.
    """
    for forbidden in (
        "fetch(",
        "XMLHttpRequest",
        "WebSocket",
        "EventSource",
        "navigator.sendBeacon",
        "import(",
    ):
        assert forbidden not in code, f"the dashboard must not use {forbidden}"


def test_the_only_remote_resources_are_the_two_font_hosts(code):
    """Anything else would be a new third party in front of a public page."""
    hosts = set(re.findall(r'https?://([A-Za-z0-9.\-]+)', code))
    assert hosts <= {"fonts.googleapis.com", "fonts.gstatic.com"}, hosts


def test_the_page_reads_exactly_one_global(code):
    assert f"window.{SNAPSHOT_GLOBAL}" in code
    # No other window.* data source sneaking in.
    globals_used = set(re.findall(r"window\.([A-Za-z_][A-Za-z0-9_]*)", code))
    assert globals_used == {SNAPSHOT_GLOBAL}, globals_used


def test_the_page_holds_no_credential_shaped_literal(page):
    # Raw source on purpose here: a credential must not appear even in a comment.
    for forbidden in ("api_key", "apiKey", "LONGPORT_", "OPENAI_", "Bearer ", "password"):
        assert forbidden not in page, f"{forbidden!r} must never appear in a public page"


# ---------------------------------------------------------------------------
# the committed demo file
# ---------------------------------------------------------------------------


def test_the_demo_snapshot_is_marked_as_demo(demo_snapshot):
    """This is what drives the DEMO DATA badge; a false here would mislabel it."""
    assert demo_snapshot["demo"] is True


def test_the_demo_snapshot_matches_the_current_schema(demo_snapshot):
    """Regenerate it (scripts/make_demo_snapshot.py) when the schema moves."""
    assert demo_snapshot["schema_version"] == SCHEMA_VERSION


def test_the_demo_snapshot_has_the_same_shape_as_a_built_one(
    demo_snapshot, full_db_session, config
):
    from snapshot_fixtures import NOW, seed

    from tevnnis_core.snapshot import build_public_snapshot

    seed(full_db_session)
    built = build_public_snapshot(full_db_session, config=config, now=NOW, demo=True)
    assert demo_snapshot.keys() == built.keys()
    for section in ("performance", "telemetry", "reasoning", "footer"):
        assert demo_snapshot[section].keys() == built[section].keys(), section


def test_the_demo_snapshot_carries_no_secret(demo_snapshot):
    """It is committed to the repo, so it gets the deny-list treatment too."""
    serialized = json.dumps(demo_snapshot).lower()
    for forbidden in ("cost_basis", "broker_fill_id", "rule_tripped", "quantity", "token"):
        assert forbidden not in serialized


# ---------------------------------------------------------------------------
# field-name drift between the builder and the page
# ---------------------------------------------------------------------------


def test_every_snapshot_field_the_page_reads_exists_in_the_schema(code, demo_snapshot):
    """Catches a typo'd field name, which would silently render an em-dash.

    Only the sections whose keys the page dereferences by name are checked;
    a mis-typed key is exactly the failure that renders as "—" and gets
    shipped unnoticed.
    """
    # Names the renderer uses, gathered from the source rather than restated.
    referenced = set(re.findall(r"\b(?:SNAP|perf|t|f|r|p|n|e)\.([a-z_]+[a-z0-9_]*)", code))
    known = (
        set(demo_snapshot)
        | set(demo_snapshot["performance"])
        | set(demo_snapshot["telemetry"])
        | set(demo_snapshot["footer"])
        | set(demo_snapshot["reasoning"])
        | set(demo_snapshot["reasoning"]["latest"])
        | {k for item in demo_snapshot["positions"] for k in item}
        | {k for item in demo_snapshot["news"] for k in item}
        | {k for item in demo_snapshot["reasoning"]["recent"] for k in item}
        # JS builtins and locals that the regex above also catches.
        | {"length", "style", "textContent", "innerHTML", "hidden", "title",
           "background", "boxShadow", "toFixed", "split", "map", "join", "forEach",
           "dataset", "value", "className"}
    )
    unknown = {name for name in referenced if name not in known}
    assert not unknown, f"the dashboard reads field(s) the snapshot does not define: {unknown}"


def test_the_page_never_hardcodes_a_dollar_amount(code):
    """The approved mock had `$0.09` baked in; it must come from the snapshot."""
    body = code.split("<script>")[-1]
    hardcoded = re.findall(r"\$\d", body)
    assert not hardcoded, f"hardcoded currency literal(s) in the renderer: {hardcoded}"


def test_the_synthetic_data_generator_is_gone(code):
    """The mock filled its chart from a seeded PRNG. Nothing may invent data."""
    for forbidden in ("Math.random", "function gen(", "var SERIES=", "var POS=", "var NEWS="):
        assert forbidden not in code, f"{forbidden!r} would fabricate data"
