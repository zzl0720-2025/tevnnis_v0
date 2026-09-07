#!/usr/bin/env bash
# Start the local development Postgres 16 container.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

docker compose -f "${REPO_ROOT}/docker-compose.yml" up -d postgres

echo "Postgres is up on localhost:5432"
echo "  DB: tevnnis | user: tevnnis | password: tevnnis_dev"
echo "  DATABASE_URL=postgresql://tevnnis:tevnnis_dev@localhost:5432/tevnnis"
