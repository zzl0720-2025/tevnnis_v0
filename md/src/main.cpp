// tevnnis-md — the market-data plane process (§2.2).
//
// Two sources, selected with --source:
//
//   mock (default)
//     Replays a scenario file through the full pipeline (Data Unifier ->
//     throttle -> priority -> sector queues) and THEN serves §4.3
//     PullDecisionBatch over gRPC. Drain-then-serve is sound here because a
//     replay is finite and synchronous, which keeps the wired e2e path
//     deterministic: core's first pull always sees the whole scenario.
//
//   longbridge (live, read-only)
//     A real QuoteContext streams pushes over time, so drain-then-serve does
//     not carry over — the server must serve WHILE events arrive. Ordering:
//
//       1. load config              (fail on a bad universe before anything)
//       2. source.Connect()         (credentials, entitlement, prev_close seed)
//       3. BuildAndStart()          (bind + serve; readiness printed)
//       4. ingest thread runs source.run(): subscribe, then Pop -> Map ->
//          pipeline.OnSourceEvent, while gRPC handlers call pipeline.Pull
//          concurrently
//       5. SIGINT/SIGTERM -> source.Stop() -> join -> server->Shutdown()
//
//     --news-source longbridge (optional, default none) adds a second
//     ingest thread polling the §4.2 news feed. It requires --source longbridge:
//     a scripted replay carries its own news, and mixing it with a live feed
//     would make the mock path non-deterministic. Its Connect() also precedes
//     the bind, and a news failure is reported without taking the quote plane
//     down (news is context; a lost halt would be a lost safety signal).
//
//     Connect() precedes the bind deliberately (§9.1: any failure aborts before
//     trading), so bad credentials never produce a process that advertises
//     readiness it cannot back. Thread-safety: MdPipeline is already fully
//     mutex-guarded, and everything per-symbol and unlocked (the quote mapper's
//     prev_close and halt-edge tables) lives on the single ingest thread. See
//     md/ingress_queue.hpp for the full model.
//
// Usage:
//   tevnnis-md --config <md_config.json> --scenario <scenario.json> [--listen host:port]
//   tevnnis-md --config <md_config.json> --source longbridge [--news-source longbridge]
//              [--listen host:port]
//
// --config takes the `universe` + `triggers` subset as JSON. This is a
// standalone-run convenience only: core owns the authored YAML config and will
// hand md this subset in integrated deployments. Do not maintain a
// second authored config file.

#include <atomic>
#include <chrono>
#include <csignal>
#include <iostream>
#include <memory>
#include <string>
#include <thread>
#include <vector>

#include <grpcpp/grpcpp.h>

#include "md/mock_source.hpp"
#include "md/pipeline.hpp"
#include "md/service.hpp"

#ifdef TEVNNIS_ENABLE_LONGBRIDGE
#include "md/longbridge_news_source.hpp"
#include "md/longbridge_source.hpp"
#endif

namespace {

enum class SourceKind { kMock, kLongbridge };
enum class NewsSourceKind { kNone, kLongbridge };

struct Args {
    std::string config_path;
    std::string scenario_path;
    std::string listen = "127.0.0.1:50051";
    SourceKind source = SourceKind::kMock;  // backward compatible
    // Default off to preserve the quote-only execution path.
    NewsSourceKind news_source = NewsSourceKind::kNone;
};

// Set from a signal handler, so it must stay async-signal-safe: a lock-free
// atomic flag polled by main, never a condition variable.
std::atomic<bool> g_shutdown_requested{false};
static_assert(std::atomic<bool>::is_always_lock_free,
              "the shutdown flag is written from a signal handler and must be lock-free");

extern "C" void HandleShutdownSignal(int /*signum*/) { g_shutdown_requested.store(true); }

bool ParseArgs(int argc, char** argv, Args* args) {
    for (int i = 1; i < argc; ++i) {
        const std::string flag = argv[i];
        const bool has_value = i + 1 < argc;
        if (flag == "--config" && has_value) {
            args->config_path = argv[++i];
        } else if (flag == "--scenario" && has_value) {
            args->scenario_path = argv[++i];
        } else if (flag == "--listen" && has_value) {
            args->listen = argv[++i];
        } else if (flag == "--source" && has_value) {
            const std::string value = argv[++i];
            if (value == "mock") {
                args->source = SourceKind::kMock;
            } else if (value == "longbridge") {
                args->source = SourceKind::kLongbridge;
            } else {
                std::cerr << "unknown --source '" << value << "' (expected mock or longbridge)\n";
                return false;
            }
        } else if (flag == "--news-source" && has_value) {
            const std::string value = argv[++i];
            if (value == "none") {
                args->news_source = NewsSourceKind::kNone;
            } else if (value == "longbridge") {
                args->news_source = NewsSourceKind::kLongbridge;
            } else {
                std::cerr << "unknown --news-source '" << value
                          << "' (expected none or longbridge)\n";
                return false;
            }
        } else {
            std::cerr << "unknown or incomplete argument: " << flag << "\n";
            return false;
        }
    }

    if (args->config_path.empty()) {
        std::cerr << "--config is required\n";
        return false;
    }
    // --config carries the universe, which IS the subscription list live and
    // the allowlist in replay, so it is required for both sources.
    if (args->source == SourceKind::kMock && args->scenario_path.empty()) {
        std::cerr << "--scenario is required with --source mock\n";
        return false;
    }
    if (args->source == SourceKind::kLongbridge && !args->scenario_path.empty()) {
        std::cerr << "--scenario is meaningless with --source longbridge "
                     "(quotes come from the live subscription)\n";
        return false;
    }
    // Live news alongside a scripted quote replay would make the mock path
    // non-deterministic — replay is what makes the wired e2e path
    // reproducible. Keep the two worlds apart.
    if (args->news_source == NewsSourceKind::kLongbridge &&
        args->source != SourceKind::kLongbridge) {
        std::cerr << "--news-source longbridge requires --source longbridge "
                     "(a scripted replay carries its own news; mixing it with a live feed "
                     "would make the mock path non-deterministic)\n";
        return false;
    }
    return true;
}

void PrintUsage() {
    std::cerr << "usage: tevnnis-md --config <md_config.json> "
                 "[--source mock|longbridge] [--news-source none|longbridge] "
                 "[--scenario <scenario.json>] [--listen host:port]\n"
                 "  --source mock (default)      replay --scenario, then serve\n"
                 "  --source longbridge          live read-only quotes; --config supplies the "
                 "universe to subscribe to\n"
                 "  --news-source none (default) no live news; scenario news only\n"
                 "  --news-source longbridge     live read-only news polling (requires "
                 "--source longbridge)\n";
}

// Universe symbols in authored order.
std::vector<std::string> UniverseSymbols(const tevnnis::md::PipelineConfig& config) {
    std::vector<std::string> symbols;
    for (const auto& sector : config.universe) {
        for (const auto& symbol : sector.symbols) {
            symbols.push_back(symbol);
        }
    }
    return symbols;
}

void PrintPipelineStats(const tevnnis::md::MdPipeline::Stats& stats, const char* verb) {
    std::cout << verb << " " << stats.ingested << " source events -> " << stats.emitted
              << " market events (outside universe: " << stats.outside_universe
              << ", dedup: " << stats.dedup_exact + stats.dedup_near << ", throttled: "
              << stats.throttled_same_band + stats.throttled_cooldown + stats.throttled_rate_cap
              << ")" << std::endl;
}

std::unique_ptr<grpc::Server> StartServer(const std::string& listen,
                                          tevnnis::md::MarketDataServiceImpl* service) {
    grpc::ServerBuilder builder;
    builder.AddListeningPort(listen, grpc::InsecureServerCredentials());
    builder.RegisterService(service);
    return builder.BuildAndStart();
}

// ---------------------------------------------------------------------------
// mock replay, including the intentionally simple signal-handling model
// (run_e2e.sh kills the process, and server->Wait() must keep blocking).
// ---------------------------------------------------------------------------
int RunMock(const Args& args, const tevnnis::md::PipelineConfig& config) {
    // Replay drives the clock from source timestamps, so throttling windows
    // advance with scenario time and a run is reproducible.
    tevnnis::md::ReplayClock clock;
    tevnnis::md::MdPipeline pipeline(config, clock.AsClock());

    tevnnis::md::MockMarketDataSource source(args.scenario_path);
    source.run([&](const tevnnis::md::SourceEvent& event) {
        clock.set_now_ms(tevnnis::md::SourceEventTs(event));
        pipeline.OnSourceEvent(event);
    });

    PrintPipelineStats(pipeline.stats(), "replayed");

    tevnnis::md::MarketDataServiceImpl service(pipeline);
    const std::unique_ptr<grpc::Server> server = StartServer(args.listen, &service);
    if (server == nullptr) {
        std::cerr << "failed to bind " << args.listen << "\n";
        return 1;
    }
    // Flush before blocking in Wait(): stdout is fully buffered when it is
    // not a terminal, and this line is the process's readiness signal.
    std::cout << "tevnnis-md listening on " << args.listen << std::endl;
    server->Wait();
    return 0;
}

// ---------------------------------------------------------------------------
// longbridge — live, read-only
// ---------------------------------------------------------------------------
#ifdef TEVNNIS_ENABLE_LONGBRIDGE
int RunLongbridge(const Args& args, const tevnnis::md::PipelineConfig& config) {
    // Live uses the wall clock: §4.4 cooldowns and rate caps must advance in
    // real time. ReplayClock is replay-only and is not thread-safe.
    tevnnis::md::MdPipeline pipeline(config, tevnnis::md::SystemClockMs);

    const std::vector<std::string> symbols = UniverseSymbols(config);
    tevnnis::md::LongbridgeQuoteSource::Options options;
    options.symbols = symbols;
    tevnnis::md::LongbridgeQuoteSource source(options);

    const bool live_news = args.news_source == NewsSourceKind::kLongbridge;
    std::cout << "tevnnis-md source: longbridge (live, READ-ONLY - "
              << (live_news ? "quotes + news" : "quotes only") << ", no orders)" << std::endl;

    // §9.1: validate the connection before we advertise readiness.
    source.Connect();
    std::cout << "tevnnis-md quote level: " << source.quote_level() << "\n"
              << "tevnnis-md seeded prev_close for " << (symbols.size() -
                                                         source.unseeded_symbols().size())
              << "/" << symbols.size() << " symbol(s)" << std::endl;
    if (!source.unseeded_symbols().empty()) {
        std::cerr << "tevnnis-md: WARNING - no usable prev_close for these symbols; every push "
                     "for them will be rejected:\n";
        for (const std::string& symbol : source.unseeded_symbols()) {
            std::cerr << "  - " << symbol << "\n";
        }
        std::cerr.flush();
    }
    std::cout << "tevnnis-md subscribing to " << symbols.size() << " symbol(s):";
    for (const std::string& symbol : symbols) {
        std::cout << " " << symbol;
    }
    std::cout << std::endl;

    // The optional news poller. Constructed and connected here — before the
    // bind, like the quote source — so a missing news entitlement aborts
    // startup rather than showing up as a permanently empty News zone.
    std::unique_ptr<tevnnis::md::LongbridgeNewsSource> news_source;
    if (live_news) {
        tevnnis::md::LongbridgeNewsSource::Options news_options;
        news_options.symbols = symbols;
        news_options.poll_interval = std::chrono::seconds(config.news.poll_interval_seconds);
        news_options.min_call_spacing =
            std::chrono::milliseconds(config.news.min_call_spacing_ms);
        news_options.call_timeout = std::chrono::seconds(config.news.call_timeout_seconds);
        news_options.mapping.max_age_minutes = config.news.max_age_minutes;
        news_options.mapping.max_items_per_symbol = config.news.max_items_per_symbol;
        news_options.mapping.seen_capacity = config.news.seen_capacity;
        news_source = std::make_unique<tevnnis::md::LongbridgeNewsSource>(news_options);
        news_source->Connect();
    }

    tevnnis::md::MarketDataServiceImpl service(pipeline);
    const std::unique_ptr<grpc::Server> server = StartServer(args.listen, &service);
    if (server == nullptr) {
        std::cerr << "failed to bind " << args.listen << "\n";
        return 1;
    }
    std::cout << "tevnnis-md listening on " << args.listen << std::endl;

    std::signal(SIGINT, HandleShutdownSignal);
    std::signal(SIGTERM, HandleShutdownSignal);

    // The single ingest thread: the ONLY driver of the mapper and of
    // OnSourceEvent. gRPC handlers call Pull concurrently under MdPipeline's
    // own mutex.
    std::string ingest_error;
    std::thread ingest([&]() {
        try {
            source.run([&](const tevnnis::md::SourceEvent& event) {
                pipeline.OnSourceEvent(event);
            });
        } catch (const std::exception& e) {
            ingest_error = e.what();
            g_shutdown_requested.store(true);
        }
    });

    // The second ingest thread is independent of the quote thread: it
    // owns its own mapper and shares only MdPipeline, which is mutex-guarded.
    // A news failure must never take the quote plane down, so its error is
    // reported but does not trip the shutdown flag.
    std::string news_error;
    std::thread news_ingest;
    if (news_source != nullptr) {
        news_ingest = std::thread([&]() {
            try {
                news_source->run([&](const tevnnis::md::SourceEvent& event) {
                    pipeline.OnSourceEvent(event);
                });
            } catch (const std::exception& e) {
                news_error = e.what();
            }
        });
    }

    while (!g_shutdown_requested.load()) {
        std::this_thread::sleep_for(std::chrono::milliseconds(200));
    }

    std::cout << "\ntevnnis-md: shutting down..." << std::endl;
    source.Stop();
    ingest.join();
    if (news_source != nullptr) {
        news_source->Stop();
        news_ingest.join();
    }
    server->Shutdown();
    server->Wait();

    if (!ingest_error.empty()) {
        std::cerr << "tevnnis-md: ingest failed: " << ingest_error << "\n";
    }
    if (!news_error.empty()) {
        std::cerr << "tevnnis-md: news ingest failed: " << news_error << "\n";
    }

    PrintPipelineStats(pipeline.stats(), "ingested");
    const tevnnis::md::IngressQueue::Stats ingress = source.ingress_stats();
    const tevnnis::md::QuoteTickMapper::Counters& mapped = source.map_counters();
    std::cout << "ingress: pushed=" << ingress.pushed << " popped=" << ingress.popped
              << " high_water=" << ingress.high_water
              << " dropped_quotes=" << ingress.dropped_quotes
              << " dropped_status=" << ingress.dropped_protected << "\n"
              << "mapping: quotes=" << mapped.mapped_quotes
              << " status_edges=" << mapped.status_edges
              << " rejected(non_intraday=" << mapped.rejected_non_intraday
              << ", not_trading=" << mapped.rejected_not_trading
              << ", invalid_price=" << mapped.rejected_invalid_price
              << ", no_prev_close=" << mapped.rejected_no_prev_close
              << ", invalid_ts=" << mapped.rejected_invalid_timestamp
              << ", suspect_ts_unit=" << mapped.rejected_suspect_timestamp_unit << ")"
              << std::endl;
    if (ingress.dropped_quotes > 0 || ingress.dropped_protected > 0) {
        std::cerr << "tevnnis-md: WARNING - the live ingress dropped "
                  << ingress.dropped_quotes << " quote(s) and " << ingress.dropped_protected
                  << " status tick(s) under backpressure\n";
    }
    if (news_source != nullptr) {
        const tevnnis::md::LongbridgeNewsSource::Stats news = news_source->stats();
        const tevnnis::md::NewsItemMapper::Counters& nmap = news_source->map_counters();
        std::cout << "news: cycles=" << news.cycles << " calls=" << news.calls
                  << " failures=" << news.call_failures << " items=" << news.items_received
                  << " emitted=" << news.events_emitted << "\n"
                  << "news mapping: mapped=" << nmap.mapped << " already_seen="
                  << nmap.already_seen << " too_old=" << nmap.too_old << " batch_trimmed="
                  << nmap.batch_trimmed << " rejected(empty_id=" << nmap.rejected_empty_id
                  << ", empty_title=" << nmap.rejected_empty_title
                  << ", invalid_ts=" << nmap.rejected_invalid_timestamp
                  << ", suspect_ts_unit=" << nmap.rejected_suspect_timestamp_unit << ")"
                  << std::endl;
    }
    return ingest_error.empty() && news_error.empty() ? 0 : 1;
}
#else
int RunLongbridge(const Args& /*args*/, const tevnnis::md::PipelineConfig& /*config*/) {
    std::cerr << "tevnnis-md was built without the Longbridge SDK, so --source longbridge is "
                 "unavailable.\n"
                 "  Reconfigure with: cmake -S . -B build -DCMAKE_BUILD_TYPE=Debug "
                 "-DTEVNNIS_ENABLE_LONGBRIDGE=ON -DTEVNNIS_LONGPORT_ROOT=<sdk>\n"
                 "  See md/CMakeLists.txt for the SDK build recipe.\n";
    return 2;
}
#endif

}  // namespace

int main(int argc, char** argv) {
    Args args;
    if (!ParseArgs(argc, argv, &args)) {
        PrintUsage();
        return 2;
    }

    try {
        const tevnnis::md::PipelineConfig config =
            tevnnis::md::LoadPipelineConfigFromJson(args.config_path);
        return args.source == SourceKind::kMock ? RunMock(args, config)
                                                : RunLongbridge(args, config);
    } catch (const std::exception& e) {
        std::cerr << "tevnnis-md: " << e.what() << "\n";
        return 1;
    }
}
