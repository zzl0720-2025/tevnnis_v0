#!/usr/bin/env python3
"""Regenerate `frontend/public_snapshot.demo.js` — the committed sample snapshot.

`frontend/dashboard.html` loads two script tags: this committed demo file, then
the live `public_snapshot.js` that `tevnnis-core` writes (a 404 on the second is
silent). So the page opens by double-click and shows the approved visual even on
a fresh checkout where core has never run.

The demo is built by the REAL builder from the test seed data, not hand-written
JSON — which means it cannot drift out of the snapshot's allow-list. If a field
is added or removed, regenerating here picks it up, and the allow-list test
would have failed first anyway.

It is marked `demo: true`, which is what makes the dashboard show its DEMO DATA
badge instead of a freshness readout.

Usage (from core/):
    uv run python scripts/make_demo_snapshot.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "core" / "tests"))

from snapshot_fixtures import NOW, seed  # noqa: E402  (needs the path above)

from tevnnis_core.config_loader import load_config  # noqa: E402
from tevnnis_core.db.models import Base  # noqa: E402
from tevnnis_core.snapshot import SNAPSHOT_GLOBAL, build_public_snapshot  # noqa: E402

HEADER = f"""\
// Committed sample snapshot, so frontend/dashboard.html renders by double-click
// with no core run and no server. Regenerate with:
//   cd core && uv run python scripts/make_demo_snapshot.py
// The live file (public_snapshot.js) is loaded after this one and overwrites it
// whenever tevnnis-core has published a real snapshot into this directory.
window.{SNAPSHOT_GLOBAL} = """


def main() -> int:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        seed(session)
        snapshot = build_public_snapshot(
            session,
            config=load_config(REPO / "config" / "config.example.yaml"),
            now=NOW,
            demo=True,
        )

    target = REPO / "frontend" / "public_snapshot.demo.js"
    target.write_text(HEADER + json.dumps(snapshot, indent=2) + ";\n")
    print(f"wrote {target.relative_to(REPO)} ({target.stat().st_size} bytes, demo=True)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
