# TEVNNIS dashboard

A read-only static page. **No build step, no server, no framework, no keys.**

```
open frontend/dashboard.html      # macOS; or just double-click it
```

## What it can see

Exactly one thing: `window.__TEVNNIS_SNAPSHOT__`, set by the two script tags in
the page head.

```html
<script src="public_snapshot.demo.js"></script>   <!-- committed sample -->
<script src="public_snapshot.js"></script>        <!-- written by core; 404 is silent -->
```

The demo file is committed, so a fresh checkout renders immediately. The live
file is written by `tevnnis-core` into this directory and, when present,
overwrites the global — so the page upgrades from DEMO to LIVE with no code
change and no reload machinery.

Two `<script src>` tags rather than `fetch()` deliberately: a missing script
fails silently, whereas fetching a local file is CORS-blocked under `file://`.
That is the whole reason the page opens by double-click.

The dashboard **imports nothing from `core`**, holds no credentials, opens no
socket, and makes no network request of its own. §14 of `docs/DESIGN.md` puts
the public frontend on a different machine from the trading core precisely so
that even a full compromise of this page reaches only public data.

## What is in the snapshot

Whatever `build_public_snapshot()` decided to publish — see
`core/src/tevnnis_core/snapshot.py`, which is the single sanitization boundary,
and the allow-list pinned in `core/tests/test_snapshot_allowlist.py`.

Everything about the account is **indexed to 100 or expressed as a percentage**.
There is exactly one dollar figure in the file, `footer.ai_cost_today` — LLM
operating spend, which says nothing about account size.

## Publishing a real snapshot

```bash
cd core
uv run tevnnis-core --config ../config/config.example.yaml \
    --scenario ../config/scenario.example.json \
    --database-url "sqlite:///../tevnnis-stage8.db" --yes --once
```

Reload the page: the DEMO DATA badge disappears and the status pill shows how
long ago the snapshot was written.

* `--snapshot-dir PATH` writes somewhere else (default: `public_snapshot.output_dir`
  in the strategy config, i.e. this directory).
* `--no-snapshot` disables publication entirely.

## States the page renders

| State | Shown |
|---|---|
| no `public_snapshot.js` | `NO SNAPSHOT`, `NO DATA` badge |
| `demo: true` | `DEMO · sample`, **DEMO DATA** badge |
| fresh (< `stale_after_seconds`, default 900s) | `LIVE · 42s ago`, green dot |
| older than that | `STALE · 41m ago`, **STALE** badge, red dot |
| `status: "stopped"` | `STOPPED · 2m ago` |

Empty zones say so ("No open positions", "Not enough history for this range
yet") rather than drawing invented data. A position with fewer than two price
samples shows an em-dash instead of a sparkline, and a news item whose url could
not be sanitized renders as plain text rather than a dead link.

## Regenerating the committed demo

```bash
cd core && uv run python scripts/make_demo_snapshot.py
```

It is built by the real builder from the test seed data, so it cannot drift out
of the snapshot's allow-list.
