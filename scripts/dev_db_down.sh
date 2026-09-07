#!/usr/bin/env bash
# Stop the local development Postgres 16 container.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

docker compose -f "${REPO_ROOT}/docker-compose.yml" down

echo "Postgres stopped."
