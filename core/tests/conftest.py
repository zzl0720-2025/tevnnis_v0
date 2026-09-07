"""Make the C++ risk extension importable from the cmake build tree.

`risk/` is linked into core in-process via pybind11 (§2.1). The extension is a
build artefact, so it lives in `build/risk/` rather than the source tree; tests
that need it use `pytest.importorskip("tevnnis_risk")`.

Build it with:
    cmake -S . -B build -DCMAKE_BUILD_TYPE=Debug
    cmake --build build -j
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from tevnnis_core.config_loader import load_config
from tevnnis_core.db.models import ApiUsage, Base, Decision

_RISK_MODULE_DIR = Path(__file__).resolve().parents[2] / "build" / "risk"

if _RISK_MODULE_DIR.is_dir() and str(_RISK_MODULE_DIR) not in sys.path:
    sys.path.insert(0, str(_RISK_MODULE_DIR))


@pytest.fixture
def db_session():
    """An in-memory sqlite session with just the decisions + api_usage tables.

    The LLM reasoning path only reads/writes these two tables, and
    neither has a JSONB column (unlike events/instructions), so a real SQL
    round-trip is possible here without a Postgres dependency for unit tests.
    """
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[Decision.__table__, ApiUsage.__table__])
    with Session(engine) as session:
        yield session


@pytest.fixture
def full_db_session():
    """An in-memory sqlite session with the *whole* §7 schema created.

    The ORM carries dialect variants (BIGINT->INTEGER, JSONB->JSON on
    sqlite) precisely so the full decision loop can be tested end to end with
    no Docker Postgres. Postgres DDL is unchanged; alembic still owns the real
    schema.
    """
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


@pytest.fixture
def config():
    """The committed example config (§6) — the same one the CLI ships with."""
    return load_config(Path(__file__).resolve().parents[2] / "config" / "config.example.yaml")


#: A Wednesday, mid-session in America/New_York — every §11 window check passes.
MID_SESSION = datetime(2026, 9, 2, 11, 0, tzinfo=ZoneInfo("America/New_York"))


@pytest.fixture
def now():
    return MID_SESSION
