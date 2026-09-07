"""Import shim for the generated protobuf modules (`core/gen/`).

grpcio-tools emits flat imports (`import md_service_pb2`), so `core/gen` has to
be on `sys.path` before any of them can be loaded. Every module that needs a
wire type imports it from here rather than repeating the path juggling:

    from tevnnis_core.pb import events_pb2, md_service_pb2

Regenerate the stubs with `scripts/gen_proto.sh` if this import fails.
"""

from __future__ import annotations

import sys
from pathlib import Path

_GEN_DIR = Path(__file__).resolve().parents[2] / "gen"

if not (_GEN_DIR / "md_service_pb2.py").is_file():  # pragma: no cover - setup error
    raise ImportError(
        f"generated protobuf modules not found in {_GEN_DIR}. "
        "Run scripts/gen_proto.sh to generate them."
    )

if str(_GEN_DIR) not in sys.path:
    sys.path.insert(0, str(_GEN_DIR))

import events_pb2  # noqa: E402
import md_service_pb2  # noqa: E402
import md_service_pb2_grpc  # noqa: E402

__all__ = ["events_pb2", "md_service_pb2", "md_service_pb2_grpc"]
