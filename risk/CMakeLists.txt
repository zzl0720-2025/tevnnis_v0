# Deterministic risk engine with no I/O.
# Linked into core in-process via pybind11, not a separate process.

add_library(tevnnis_risk STATIC
    src/context.cpp
    src/engine.cpp)
target_include_directories(tevnnis_risk PUBLIC include)
# Linked into a shared Python extension below.
set_target_properties(tevnnis_risk PROPERTIES POSITION_INDEPENDENT_CODE ON)

# Tests: one file per rule group
add_executable(risk_tests
    tests/smoke_test.cpp
    tests/context_test.cpp
    tests/engine_test.cpp
    tests/precedence_test.cpp
    tests/batch_test.cpp)
target_link_libraries(risk_tests PRIVATE tevnnis_risk Catch2::Catch2WithMain)
catch_discover_tests(risk_tests)

# Python extension: importable as `tevnnis_risk` from build/risk/
if(TEVNNIS_BUILD_PYTHON_BINDINGS)
    pybind11_add_module(tevnnis_risk_py src/bindings.cpp)
    set_target_properties(tevnnis_risk_py PROPERTIES OUTPUT_NAME tevnnis_risk)
    target_link_libraries(tevnnis_risk_py PRIVATE tevnnis_risk)
endif()
