.PHONY: build e2e

# Configure and build the C++ planes: md, risk, the tevnnis_risk pybind11
# extension, and tevnnis-md.
build:
	cmake -S . -B build -DCMAKE_BUILD_TYPE=Debug
	cmake --build build -j$$(nproc 2>/dev/null || sysctl -n hw.logicalcpu)

# Full integration target: `build` is an explicit prerequisite so
# tevnnis-md always exists before scripts/run_e2e.sh runs — the acceptance
# path can never silently skip the integration the way a solo `pytest
# tests/test_e2e_wired_md.py` run is allowed to when unbuilt.
e2e: build
	scripts/run_e2e.sh
