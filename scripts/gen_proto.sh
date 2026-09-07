#!/usr/bin/env bash
# Regenerate proto bindings for both planes from proto/*.proto.
# Python side: uses grpcio-tools from the core uv venv (no global install needed).
# C++ side:    requires protoc + grpc_cpp_plugin — install with: brew install grpc
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROTO_DIR="${REPO_ROOT}/proto"
PROTO_FILES=("${PROTO_DIR}"/*.proto)

# ---------------------------------------------------------------------------
# Python bindings
# ---------------------------------------------------------------------------
echo "==> Generating Python proto bindings..."
PY_OUT="${REPO_ROOT}/core/gen"
mkdir -p "${PY_OUT}"

cd "${REPO_ROOT}/core"
uv run python -m grpc_tools.protoc \
    -I "${PROTO_DIR}" \
    --python_out="${PY_OUT}" \
    --grpc_python_out="${PY_OUT}" \
    --pyi_out="${PY_OUT}" \
    "${PROTO_FILES[@]}"

# Make the output directory importable.
touch "${PY_OUT}/__init__.py"
echo "    Done -> core/gen/"

# ---------------------------------------------------------------------------
# C++ bindings
# ---------------------------------------------------------------------------
echo "==> Generating C++ proto bindings..."

if ! command -v protoc &>/dev/null; then
    echo "    ERROR: protoc not found. Install with: brew install grpc"
    exit 1
fi

GRPC_CPP_PLUGIN="$(command -v grpc_cpp_plugin 2>/dev/null || true)"
if [[ -z "${GRPC_CPP_PLUGIN}" ]]; then
    echo "    ERROR: grpc_cpp_plugin not found. Install with: brew install grpc"
    exit 1
fi

CPP_OUT="${REPO_ROOT}/md/gen"
mkdir -p "${CPP_OUT}"

protoc \
    -I "${PROTO_DIR}" \
    --cpp_out="${CPP_OUT}" \
    --grpc_out="${CPP_OUT}" \
    --plugin="protoc-gen-grpc=${GRPC_CPP_PLUGIN}" \
    "${PROTO_FILES[@]}"

echo "    Done -> md/gen/"
