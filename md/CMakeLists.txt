# tevnnis_md — market-data plane (C++ long-running process): protobuf wiring,
# scripted replay, Data Unifier, sector priority queues, §4.4 throttling and
# the §4.3 gRPC MarketData service.

# ---------------------------------------------------------------------------
# Proto-generated C++ stubs
# Checked into md/gen/ by scripts/gen_proto.sh, which requires: brew install grpc
# The pipeline normalizes into the proto MarketEvent itself (one representation, no
# parallel struct to keep in sync), so these are now a hard requirement.
# ---------------------------------------------------------------------------
file(GLOB PROTO_GENERATED_SOURCES "${CMAKE_SOURCE_DIR}/md/gen/*.cc")
if(NOT PROTO_GENERATED_SOURCES)
    message(FATAL_ERROR
        "tevnnis: md/gen/ is empty — run: brew install grpc && scripts/gen_proto.sh")
endif()

# gRPC's config already calls find_dependency(Protobuf) internally; a separate
# find_package(Protobuf) would cause a partial-redefinition error when gRPC
# tries to define the remaining protobuf targets.
find_package(gRPC CONFIG REQUIRED)

add_library(tevnnis_proto_stubs STATIC ${PROTO_GENERATED_SOURCES})
target_include_directories(tevnnis_proto_stubs
    PUBLIC "${CMAKE_SOURCE_DIR}/md/gen")
target_link_libraries(tevnnis_proto_stubs
    PUBLIC protobuf::libprotobuf gRPC::grpc++)

# ---------------------------------------------------------------------------
# md data plane
# ---------------------------------------------------------------------------
add_library(tevnnis_md STATIC
    src/mock_source.cpp
    src/longbridge_mapping.cpp
    src/longbridge_news_mapping.cpp
    src/ingress_queue.cpp
    src/pipeline_config.cpp
    src/dedup.cpp
    src/data_unifier.cpp
    src/throttle.cpp
    src/priority.cpp
    src/sector_queue.cpp
    src/pipeline.cpp
    src/service.cpp)
target_include_directories(tevnnis_md PUBLIC include)
target_link_libraries(tevnnis_md
    PUBLIC nlohmann_json::nlohmann_json tevnnis_proto_stubs)

# ---------------------------------------------------------------------------
# tevnnis-md process (§2.2) — mock-source replay + gRPC MarketData service
# ---------------------------------------------------------------------------
add_executable(tevnnis-md src/main.cpp)
target_link_libraries(tevnnis-md PRIVATE tevnnis_md)

# ---------------------------------------------------------------------------
# Longbridge / LongPort OpenAPI C++ SDK — optional, default OFF
#
# The SDK ships no prebuilt C++ artefact (its GitHub releases carry Node.js
# `.node` binaries only) and defines no CMake install() rules, and its core is a
# Rust crate (`longport-c`, a cdylib) imported through Corrosion. So it is built
# from source and consumed straight out of that source tree, and because that
# needs a Rust toolchain it stays strictly opt-in — with
# TEVNNIS_ENABLE_LONGBRIDGE=OFF (the default) nothing here is compiled and every
# existing target and test builds exactly as before.
#
#   # one-time, anywhere outside this repo:
#   curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh   # Rust
#   git clone --branch v4.3.7 https://github.com/longportapp/openapi.git
#   cd openapi
#   cmake -S . -B cmake.build -DCMAKE_BUILD_TYPE=Release
#   cmake --build cmake.build -j
#
#   # then, in this repo:
#   cmake -S . -B build -DCMAKE_BUILD_TYPE=Debug \
#         -DTEVNNIS_ENABLE_LONGBRIDGE=ON -DTEVNNIS_LONGPORT_ROOT=/path/to/openapi
#
# TEVNNIS_LONGPORT_ROOT points at the SDK's SOURCE root (the clone), not an
# install prefix: headers come from <root>/cpp/include and the two shared
# libraries from its build directory. $LONGPORT_ROOT is honoured as a fallback.
# ---------------------------------------------------------------------------
if(TEVNNIS_ENABLE_LONGBRIDGE)
    if(NOT TEVNNIS_LONGPORT_ROOT AND DEFINED ENV{LONGPORT_ROOT})
        set(TEVNNIS_LONGPORT_ROOT "$ENV{LONGPORT_ROOT}")
    endif()
    if(NOT TEVNNIS_LONGPORT_ROOT)
        message(FATAL_ERROR
            "tevnnis: TEVNNIS_ENABLE_LONGBRIDGE=ON but TEVNNIS_LONGPORT_ROOT is unset.\n"
            "  Pass -DTEVNNIS_LONGPORT_ROOT=/path/to/longportapp-openapi (the SDK clone, "
            "already built), or export LONGPORT_ROOT. See the recipe in md/CMakeLists.txt.")
    endif()

    find_path(TEVNNIS_LONGPORT_INCLUDE_DIR
        NAMES longport.hpp
        HINTS "${TEVNNIS_LONGPORT_ROOT}/cpp/include"
        NO_DEFAULT_PATH)

    # longport_cpp is a SHARED library and links longport_c (the Rust cdylib),
    # so BOTH must be located and both directories must reach the RPATH or the
    # binaries fail to load at run time.
    set(TEVNNIS_LONGPORT_LIB_HINTS
        "${TEVNNIS_LONGPORT_ROOT}/cmake.build"
        "${TEVNNIS_LONGPORT_ROOT}/build"
        "${TEVNNIS_LONGPORT_ROOT}/target/release"
        "${TEVNNIS_LONGPORT_ROOT}/target/debug")
    find_library(TEVNNIS_LONGPORT_CPP_LIBRARY
        NAMES longport_cpp
        HINTS ${TEVNNIS_LONGPORT_LIB_HINTS}
        NO_DEFAULT_PATH)
    find_library(TEVNNIS_LONGPORT_C_LIBRARY
        NAMES longport_c
        HINTS ${TEVNNIS_LONGPORT_LIB_HINTS}
        NO_DEFAULT_PATH)

    if(NOT TEVNNIS_LONGPORT_INCLUDE_DIR
       OR NOT TEVNNIS_LONGPORT_CPP_LIBRARY
       OR NOT TEVNNIS_LONGPORT_C_LIBRARY)
        message(FATAL_ERROR
            "tevnnis: could not locate the Longbridge SDK under "
            "'${TEVNNIS_LONGPORT_ROOT}'.\n"
            "  headers (longport.hpp): ${TEVNNIS_LONGPORT_INCLUDE_DIR}\n"
            "  longport_cpp:           ${TEVNNIS_LONGPORT_CPP_LIBRARY}\n"
            "  longport_c:             ${TEVNNIS_LONGPORT_C_LIBRARY}\n"
            "  A 'NOTFOUND' above usually means the SDK is cloned but not built yet:\n"
            "    cd ${TEVNNIS_LONGPORT_ROOT} && cmake -S . -B cmake.build "
            "-DCMAKE_BUILD_TYPE=Release && cmake --build cmake.build -j")
    endif()

    # The SDK dispatches its callbacks from a multi-threaded tokio runtime
    # (longport-c depends on tokio with `rt-multi-thread`), so anything linking
    # it is a threaded program.
    find_package(Threads REQUIRED)

    add_library(tevnnis_longport INTERFACE)
    target_include_directories(tevnnis_longport SYSTEM INTERFACE
        "${TEVNNIS_LONGPORT_INCLUDE_DIR}")
    target_link_libraries(tevnnis_longport INTERFACE
        "${TEVNNIS_LONGPORT_CPP_LIBRARY}"
        "${TEVNNIS_LONGPORT_C_LIBRARY}"
        Threads::Threads)

    get_filename_component(TEVNNIS_LONGPORT_CPP_LIBDIR
        "${TEVNNIS_LONGPORT_CPP_LIBRARY}" DIRECTORY)
    get_filename_component(TEVNNIS_LONGPORT_C_LIBDIR
        "${TEVNNIS_LONGPORT_C_LIBRARY}" DIRECTORY)

    message(STATUS "tevnnis: longbridge SDK ON (${TEVNNIS_LONGPORT_ROOT})")

    # The live sources themselves. Attached to tevnnis_md here rather than
    # listed in add_library above, because these are the only md source files
    # that cannot compile without the SDK — their PURE mapping layers
    # (longbridge_mapping.cpp, longbridge_news_mapping.cpp) are unconditional
    # and always tested. TEVNNIS_ENABLE_LONGBRIDGE is PUBLIC so main.cpp (which
    # links tevnnis_md) compiles its `--source`/`--news-source longbridge`
    # branches too; without it main.cpp keeps the flags but exits with a clear
    # message.
    target_sources(tevnnis_md PRIVATE
        src/longbridge_source.cpp
        src/longbridge_news_source.cpp)
    target_link_libraries(tevnnis_md PUBLIC tevnnis_longport)
    target_compile_definitions(tevnnis_md PUBLIC TEVNNIS_ENABLE_LONGBRIDGE)
    set_target_properties(tevnnis-md PROPERTIES
        BUILD_RPATH "${TEVNNIS_LONGPORT_CPP_LIBDIR};${TEVNNIS_LONGPORT_C_LIBDIR}")

    # -----------------------------------------------------------------------
    # Isolated connectivity smoke.
    # Links ONLY the SDK, never tevnnis_md: it proves credentials, US quote
    # permission and link wiring with none of our own code in the way, so a
    # failure here can only be the SDK/account, not the pipeline.
    # -----------------------------------------------------------------------
    # md/include only for the header-only Redact/Await helpers these share with
    # the live sources — they still link the SDK alone, never tevnnis_md.
    #
    # tevnnis-md-longbridge-news-smoke is the news counterpart. It
    # additionally prints each url's host and query-param NAMES, which is the
    # evidence behind the snapshot sanitizer's news_url_allow_hosts /
    # news_url_keep_params defaults (both empty).
    foreach(smoke_target longbridge_smoke longbridge_news_smoke)
        string(REPLACE "_" "-" smoke_name "tevnnis-md-${smoke_target}")
        add_executable(${smoke_name} tools/${smoke_target}.cpp)
        target_include_directories(${smoke_name} PRIVATE include)
        target_link_libraries(${smoke_name} PRIVATE tevnnis_longport)
        set_target_properties(${smoke_name} PROPERTIES
            BUILD_RPATH "${TEVNNIS_LONGPORT_CPP_LIBDIR};${TEVNNIS_LONGPORT_C_LIBDIR}")
    endforeach()
else()
    message(STATUS
        "tevnnis: longbridge SDK OFF - the live quote source is not compiled and "
        "`tevnnis-md --source longbridge` will refuse to start. Reconfigure with "
        "-DTEVNNIS_ENABLE_LONGBRIDGE=ON -DTEVNNIS_LONGPORT_ROOT=<sdk> to enable it.")
endif()

# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
add_executable(md_tests
    tests/smoke_test.cpp
    tests/mock_source_test.cpp
    tests/dedup_test.cpp
    tests/pipeline_config_test.cpp
    tests/data_unifier_test.cpp
    tests/throttle_test.cpp
    tests/priority_test.cpp
    tests/pipeline_test.cpp
    tests/grpc_service_test.cpp
    tests/redact_test.cpp
    tests/ingress_queue_test.cpp
    tests/longbridge_mapping_test.cpp
    tests/longbridge_news_mapping_test.cpp)
target_link_libraries(md_tests PRIVATE tevnnis_md Catch2::Catch2WithMain)
target_compile_definitions(md_tests PRIVATE
    TEVNNIS_MD_SCENARIOS_DIR="${CMAKE_CURRENT_SOURCE_DIR}/scenarios")
if(TEVNNIS_ENABLE_LONGBRIDGE)
    # md_tests links tevnnis_md, which now links the SDK shared libraries.
    set_target_properties(md_tests PROPERTIES
        BUILD_RPATH "${TEVNNIS_LONGPORT_CPP_LIBDIR};${TEVNNIS_LONGPORT_C_LIBDIR}")
endif()

catch_discover_tests(md_tests)
