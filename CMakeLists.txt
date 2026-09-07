cmake_minimum_required(VERSION 3.25)
project(tevnnis LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

include(CTest)

# ---------------------------------------------------------------------------
# Catch2 — fetched once at root; md/ and risk/ link against it.
# ---------------------------------------------------------------------------
include(FetchContent)
FetchContent_Declare(
  Catch2
  GIT_REPOSITORY https://github.com/catchorg/Catch2.git
  GIT_TAG        v3.7.1
)
FetchContent_MakeAvailable(Catch2)
list(APPEND CMAKE_MODULE_PATH "${catch2_SOURCE_DIR}/extras")
include(Catch)

# ---------------------------------------------------------------------------
# nlohmann_json — header-only; md uses it to parse mock scenario files (§13).
# ---------------------------------------------------------------------------
set(JSON_BuildTests OFF CACHE INTERNAL "")
FetchContent_Declare(
  nlohmann_json
  GIT_REPOSITORY https://github.com/nlohmann/json.git
  GIT_TAG        v3.11.3
)
FetchContent_MakeAvailable(nlohmann_json)

# ---------------------------------------------------------------------------
# pybind11 — risk/ is linked into core in-process (§2.1), so the risk engine
# ships as an importable Python extension. Optional: a machine without Python
# development headers still builds and tests the C++ planes.
# ---------------------------------------------------------------------------
option(TEVNNIS_BUILD_PYTHON_BINDINGS "Build the pybind11 risk extension" ON)

if(TEVNNIS_BUILD_PYTHON_BINDINGS)
    # Default to core's uv-managed venv so the extension's ABI matches the
    # interpreter `uv run pytest` uses. Override with -DPython3_EXECUTABLE=...
    if(NOT DEFINED Python3_EXECUTABLE AND EXISTS "${CMAKE_SOURCE_DIR}/core/.venv/bin/python")
        set(Python3_EXECUTABLE "${CMAKE_SOURCE_DIR}/core/.venv/bin/python")
    endif()

    find_package(Python3 COMPONENTS Interpreter Development.Module QUIET)
    if(Python3_FOUND)
        FetchContent_Declare(
          pybind11
          GIT_REPOSITORY https://github.com/pybind/pybind11.git
          GIT_TAG        v2.13.6
        )
        FetchContent_MakeAvailable(pybind11)
        message(STATUS "tevnnis: python bindings ON (${Python3_EXECUTABLE})")
    else()
        set(TEVNNIS_BUILD_PYTHON_BINDINGS OFF)
        message(STATUS
            "tevnnis: python bindings OFF - no Python development headers found. "
            "C++ builds and tests are unaffected; core/tests/test_risk_engine.py will skip.")
    endif()
endif()

# ---------------------------------------------------------------------------
# Longbridge / LongPort OpenAPI C++ SDK — optional, default OFF.
#
# md's live quote source needs the SDK, but the SDK's core is Rust behind a C
# ABI (built with Corrosion), so having it requires a Rust toolchain. Keeping
# this OFF by default means a stock checkout still configures, builds and passes
# every test with no Rust installed — `tevnnis-md` simply refuses
# `--source longbridge` and keeps serving the mock source.
#
# See md/CMakeLists.txt for the SDK build recipe and TEVNNIS_LONGPORT_ROOT.
# ---------------------------------------------------------------------------
option(TEVNNIS_ENABLE_LONGBRIDGE
    "Build the live Longbridge quote source and its connectivity smoke" OFF)

# ---------------------------------------------------------------------------
# Sub-projects
# ---------------------------------------------------------------------------
add_subdirectory(md)
add_subdirectory(risk)
