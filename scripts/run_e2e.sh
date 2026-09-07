#!/usr/bin/env bash
# Full-system acceptance runner (§2.2 md<->core wiring, §9 CLI lifecycle, §13
# mock-first): brings up a disposable Postgres, migrates it, launches a real
# tevnnis-md subprocess, drives tevnnis-core against it non-interactively
# (--yes --now; broker and LLM are mocked, so this never places a real order
# or spends real LLM budget -- see cli.guard_allows_auto_confirm), and
# asserts the resulting DB state against that same Postgres instance.
#
# Isolation: the Postgres instance is completely separate from
# docker-compose.yml's `postgres` dev service -- its own compose file
# (docker-compose.e2e.yml), its own project name, container, volume and port
# (5433). It is `down -v`'d both before AND after this script runs, so every
# run starts from a genuinely empty schema and this script can never read or
# clobber day-to-day dev data. assert_e2e_db.py asserts EXACT row counts,
# which only holds if the DB really is empty at the start.
#
# Determinism: the real Risk Engine reads the true wall clock for its
# no-trade-window check regardless of cadence.respect_market_hours (§11), so
# this script pins the loop's clock with tevnnis-core's --now override
# (refused unless broker/LLM are mocked -- same invariant as --yes) rather
# than depending on when it happens to be run.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

COMPOSE_FILE="docker-compose.e2e.yml"
PROJECT="tevnnis-e2e"
DB_URL="postgresql://tevnnis_e2e:tevnnis_e2e_dev@localhost:5433/tevnnis_e2e"
MD_BINARY="$REPO_ROOT/build/md/tevnnis-md"
MD_CONFIG="$REPO_ROOT/md/scenarios/example_md_config.json"
MD_SCENARIO="$REPO_ROOT/md/scenarios/e2e_wired.json"
CONFIG_YAML="$REPO_ROOT/config/config.e2e_wired.yaml"
CORE_SCENARIO="$REPO_ROOT/config/scenario.e2e_wired.json"
MD_LISTEN="127.0.0.1:50061"
# A Wednesday, mid-session in America/New_York -- matches the fixed clock
# core/tests/conftest.py's `now` fixture uses, so this script and the fast
# sqlite test (test_e2e_wired_md.py) reason about the same "now".
FIXED_NOW="2026-09-02T11:00:00-04:00"
MD_LOG="$(mktemp -t tevnnis-md-e2e.XXXXXX)"

MD_PID=""

cleanup() {
    local status=$?
    if [[ -n "$MD_PID" ]]; then
        kill "$MD_PID" 2>/dev/null || true
        wait "$MD_PID" 2>/dev/null || true
    fi
    echo "[e2e] tearing down the disposable Postgres (down -v)..."
    docker compose -f "$COMPOSE_FILE" -p "$PROJECT" down -v --remove-orphans >/dev/null 2>&1 || true
    rm -f "$MD_LOG"
    exit "$status"
}
trap cleanup EXIT

if [[ ! -x "$MD_BINARY" ]]; then
    echo "[e2e] tevnnis-md is not built ($MD_BINARY missing)." >&2
    echo "      Run: cmake -S . -B build -DCMAKE_BUILD_TYPE=Debug && cmake --build build -j" >&2
    echo "      (or just: make build)" >&2
    exit 1
fi

echo "[e2e] 1/6 starting a disposable Postgres (project=$PROJECT, port 5433)..."
# Belt and suspenders: drop any leftovers from a previous crashed run first,
# so "empty schema" never depends on a previous run having cleaned up after
# itself.
docker compose -f "$COMPOSE_FILE" -p "$PROJECT" down -v --remove-orphans >/dev/null 2>&1 || true
docker compose -f "$COMPOSE_FILE" -p "$PROJECT" up -d

echo "[e2e] 2/6 waiting for Postgres to accept connections..."
pg_ready=""
for _ in $(seq 1 60); do
    if docker compose -f "$COMPOSE_FILE" -p "$PROJECT" exec -T postgres-e2e \
        pg_isready -U tevnnis_e2e -d tevnnis_e2e >/dev/null 2>&1; then
        pg_ready=1
        break
    fi
    sleep 1
done
if [[ -z "$pg_ready" ]]; then
    echo "[e2e] Postgres did not become ready in time" >&2
    exit 1
fi

echo "[e2e] 3/6 applying migrations..."
(cd core && DATABASE_URL="$DB_URL" uv run alembic -c alembic.ini upgrade head)

echo "[e2e] 4/6 launching tevnnis-md ($MD_LISTEN)..."
"$MD_BINARY" --config "$MD_CONFIG" --scenario "$MD_SCENARIO" --listen "$MD_LISTEN" \
    >"$MD_LOG" 2>&1 &
MD_PID=$!

md_ready=""
for _ in $(seq 1 150); do
    if grep -q "listening on" "$MD_LOG" 2>/dev/null; then
        md_ready=1
        break
    fi
    if ! kill -0 "$MD_PID" 2>/dev/null; then
        break
    fi
    sleep 0.1
done
if [[ -z "$md_ready" ]]; then
    echo "[e2e] tevnnis-md did not report readiness -- log follows:" >&2
    cat "$MD_LOG" >&2
    exit 1
fi
echo "[e2e]     $(grep "listening on" "$MD_LOG")"

echo "[e2e] 5/6 running tevnnis-core non-interactively (--yes --now; broker/LLM mocked)..."
(cd core && uv run tevnnis-core \
    --config "$CONFIG_YAML" \
    --md-source grpc --md-address "$MD_LISTEN" \
    --scenario "$CORE_SCENARIO" \
    --database-url "$DB_URL" \
    --yes --now "$FIXED_NOW" --max-rounds 2)

echo "[e2e] 6/6 asserting the resulting DB state against Postgres..."
(cd core && uv run python scripts/assert_e2e_db.py --database-url "$DB_URL")

echo "[e2e] PASS"
