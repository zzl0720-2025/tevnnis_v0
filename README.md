# TEVNNIS v0

**Live demo:** [tevnnis.com](https://tevnnis.com)

TEVNNIS stands for **Trading Engine V0 Nascent, Nominal Intelligence System**.
The two N-adjectives are intentionally modest in v0; later versions are expected to earn stronger
adjectives. Why TEVNNIS? Perhaps because its author happens to like tennis.

It is a local-first, low-frequency trading-agent runtime for US-listed equities. The project
combines a C++ market-data plane and deterministic safety boundary with a Python orchestration
plane. v0 is designed for attended operation and Longbridge paper trading; the complete system
can also run offline with scripted market data, a mock broker, a mock model, and SQLite.

TEVNNIS is an engineering project, not a return-optimization claim. The v0 priorities are
reproducibility, bounded side effects, auditability, and clean separation between probabilistic
reasoning and deterministic execution controls.

## Source availability

This repository is the public v0 codebase, not a complete source distribution. The risk engine,
risk-context assembly, unifier implementations, their executable specifications, and internal
design records are maintained privately. Their interfaces and system boundaries are documented
here, but the private rule set, precedence, strategy parameters, and transformation logic are not
published.

As a result, a public checkout can be inspected and used as an architectural reference, but it
cannot build the complete trading runtime without the private components.

## System architecture

![TEVNNIS v0 system architecture](t0.drawio.png)

```text
quote/news source
      |
      v
tevnnis-md (C++17)
  normalization, deduplication, throttling, prioritized queues
      |
      | gRPC / protobuf
      v
tevnnis-core (Python 3.11+)
  cadence, state assembly, model routing, persistence, order lifecycle
      |
      +----> tevnnis_risk (C++ via pybind11)
      |
      +----> broker adapter
      |
      +----> PostgreSQL / SQLite ----> sanitized snapshot ----> static dashboard
```

The model produces proposals. It does not own the execution path, broker state, hard limits, or
the final authorization decision. Those boundaries are enforced by ordinary code and recorded
for later inspection.

## v0 feature set

- C++17 market-data service with protobuf contracts and a gRPC pull interface.
- Deterministic JSON replay for offline development and integration testing.
- Optional Longbridge quote subscription and news polling, compiled behind a CMake flag.
- Normalized market events, exact and near-duplicate suppression, bounded ingress, event
  throttling, and sector-aware priority queues.
- Python decision loop with mock and production adapter boundaries for market data, broker, and
  model providers.
- Structured model output, provider usage accounting, configurable call/token budgets, and a
  fail-closed HOLD path.
- In-process C++ risk evaluation exposed through pybind11. Internal rule composition is kept out
  of this overview; externally, the contract is a deterministic allow/reject result with an
  auditable reason identifier.
- Longbridge paper-account execution adapter with startup reconciliation, idempotent client order
  identifiers, timed limit-order handling, fill tracking, and graceful shutdown.
- SQLAlchemy persistence with Alembic migrations; PostgreSQL for the full local stack and SQLite
  for the offline walkthrough.
- Sanitized public snapshot generation and a dependency-free, read-only HTML dashboard. Published
  account performance is normalized rather than exposing account-size data.
- C++ unit tests, Python tests, and a disposable-PostgreSQL end-to-end runner.

Current v0 scope is deliberately narrow: US-listed common stocks and an explicitly curated ETF
allowlist, regular market hours, limit orders, one operator, and an attended command-line
lifecycle. It is not an HFT engine, portfolio backtester, or multi-user trading service.

## Repository layout

```text
.
├── proto/       protobuf contracts shared by md and core
├── md/          C++ market-data plane, adapters, tools, and tests
├── risk/        private C++ risk library and pybind11 module (not published)
├── core/        Python orchestration, persistence, adapters, and tests
├── config/      example, integration, and paper-mode configuration
├── frontend/    static read-only dashboard and committed demo snapshot
├── scripts/     protobuf, database, and end-to-end helpers
└── docs/        internal design and implementation records (not published)
```

Generated protobuf bindings, local databases, build output, caches, `.env`, and published live
snapshots are intentionally excluded from Git.

## Prerequisites

- Python 3.11 or newer
- [uv](https://docs.astral.sh/uv/)
- CMake 3.25 or newer
- A C++17 compiler
- `protoc` and `grpc_cpp_plugin`
- Docker, only for PostgreSQL and the full end-to-end test

On macOS, the native build dependencies can be installed with:

```bash
xcode-select --install
brew install cmake grpc
```

Install `uv` using its official installer or package manager. CMake fetches Catch2,
`nlohmann_json`, and pybind11 during configuration, so the first configure requires network
access.

## Build the complete private checkout

The following instructions apply to a complete private checkout containing the excluded risk and
unifier components. Create the Python environment first. CMake will use its interpreter when
building the pybind11 extension.

```bash
cd core
uv sync --group dev
cd ..
```

Generate Python and C++ protobuf bindings:

```bash
scripts/gen_proto.sh
```

Configure and build the C++ targets:

```bash
cmake -S . -B build -DCMAKE_BUILD_TYPE=Debug
cmake --build build -j$(sysctl -n hw.logicalcpu 2>/dev/null || nproc)
```

The default build does not compile the Longbridge C++ SDK adapter. It still builds the complete
offline market-data pipeline, risk library, Python extension, tools, and tests.

## Run the test suites

```bash
ctest --test-dir build --output-on-failure

cd core
uv run pytest
uv run ruff check .
cd ..
```

For the cross-process acceptance path, start Docker and run:

```bash
make e2e
```

This target creates an isolated PostgreSQL instance on port `5433`, applies migrations, starts a
real `tevnnis-md` process, runs `tevnnis-core` against it over gRPC with mock broker/model
adapters, verifies the resulting database state, and removes the disposable database volume.

## Offline walkthrough

No credentials, broker connection, external model call, or Docker service is required. The C++
risk extension must already exist from the build above.

```bash
cd core
uv run tevnnis-core \
  --config ../config/config.example.yaml \
  --scenario ../config/scenario.example.json \
  --database-url "sqlite:///tevnnis-demo.db" \
  --max-rounds 2
```

The process validates its configuration and dependencies, reconciles the mock broker state,
prints a startup summary, and waits for an explicit confirmation:

```text
Start TEVNNIS trading loop? [y/N]
```

Enter `y` to arm the loop. The example scenario is deterministic. Depending on the wall clock,
an order proposal may be rejected by the regular-session guard; that is an expected safety
result. `Ctrl-C` triggers the normal shutdown path.

For a fully non-interactive mock run:

```bash
cd core
uv run tevnnis-core \
  --config ../config/config.example.yaml \
  --scenario ../config/scenario.example.json \
  --database-url "sqlite:///tevnnis-demo.db" \
  --yes --once
```

`--yes` and the clock override are refused when either the broker or model provider is real.

## Dashboard

The offline run writes a sanitized snapshot to `frontend/` unless snapshot publication is
disabled. Open the dashboard directly:

```bash
open frontend/dashboard.html
```

On non-macOS systems, open `frontend/dashboard.html` in a browser. No web server or frontend build
step is required. A committed demo snapshot is used on a fresh checkout; a generated local
snapshot takes precedence when present.

Useful switches:

```text
--snapshot-dir PATH   write the snapshot to another directory
--no-snapshot         disable snapshot publication
--once                run one decision round
--max-rounds N        stop after N rounds
```

## Local PostgreSQL

The standard local database is PostgreSQL 16 under Docker:

```bash
scripts/dev_db_up.sh

cd core
DATABASE_URL=postgresql://tevnnis:tevnnis_dev@localhost:5432/tevnnis \
  uv run alembic -c alembic.ini upgrade head
cd ..
```

Pass the same URL through `--database-url` or export it as `DATABASE_URL`. Stop the service with:

```bash
scripts/dev_db_down.sh
```

## Optional external integrations

Copy the environment template only when testing real adapters:

```bash
cp .env.example .env
chmod 600 .env
```

The application does not parse `.env` automatically. Export variables in the shell before
starting a credentialed smoke test or process:

```bash
set -a
source .env
set +a
```

The repository contains isolated smoke tools for validating Longbridge paper-account access,
Longbridge market-data access, and the OpenAI provider before enabling them in the main loop.
Run those probes first. The live C++ market-data adapter is opt-in because the Longbridge C++ SDK
must be built separately with its Rust-backed native library:

```bash
cmake -S . -B build \
  -DCMAKE_BUILD_TYPE=Debug \
  -DTEVNNIS_ENABLE_LONGBRIDGE=ON \
  -DTEVNNIS_LONGPORT_ROOT=/absolute/path/to/longportapp-openapi
cmake --build build -j
```

Private checkouts contain additional adapter-specific build notes and verification history. Keep
`account.mode: paper` for v0.

## Operational constraints

- Do not commit `.env`, generated snapshots, databases, or broker credentials.
- Treat `config/config.example.yaml` as a schema example, not a ready-made investment strategy.
- Use the mock path for development and CI.
- Validate the intended paper account interactively before enabling the real broker adapter.
- Review every configuration change that affects universe membership, budgets, lifecycle, or
  external providers.

This repository is experimental software. It is not financial advice and should not be used with
capital you are not prepared to lose.
