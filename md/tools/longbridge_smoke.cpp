// tevnnis-md-longbridge-smoke — isolated quote connectivity probe.
//
// A standalone target that links ONLY the Longbridge SDK — deliberately not
// tevnnis_md, not the pipeline, not gRPC. It proves three things in isolation,
// before any md internals are touched:
//
//   1. the LONGPORT_* credentials in the environment actually work;
//   2. the account has US quote permission, and at what level — real-time or
//      delayed (§3: delayed is adequate for v0);
//   3. the SDK build/link wiring is correct on this machine.
//
// It also verifies two facts the quote mapping depends on, by printing
// evidence rather than asking anyone to trust a doc comment:
//
//   * `prev_close` is absent from the pushed `PushQuote` but present on the
//     snapshot `SecurityQuote`. DataUnifier rejects a quote with prev_close <= 0
//     as invalid and needs it for change_pct (§4.2), so the live source must
//     seed prev_close from a snapshot call like this one. This program reports
//     exactly which symbols would seed and which would not.
//   * `timestamp` carries no documented unit. The C binding calls Rust's
//     `unix_timestamp()`, i.e. SECONDS, while QuoteEvent::event_ts is epoch
//     MILLIS — so the value is decoded BOTH ways below. Whichever line reads as
//     "now" is the answer, and the mapping is written against that.
//
// Read-only and cheap: exactly two calls (one quote_level, one batched quote),
// once, then exit — far inside the §3 limit of 10 quote calls/sec. It never
// subscribes, never trades, and never touches the database (core is the sole
// DB writer, §2.2).
//
// The Redact/Await helpers this shares with the live source now live in
// md/redact.hpp and md/longbridge_await.hpp (both header-only), so this target
// still links the SDK ALONE — never tevnnis_md. Isolation is the point.
//
// Secrets (§12): the SDK itself reads LONGPORT_APP_KEY / LONGPORT_APP_SECRET /
// LONGPORT_ACCESS_TOKEN inside Config::from_apikey_env. This program never
// reads, copies, stores, prints or logs a credential VALUE — it only checks
// whether each variable is set and non-empty, and names the missing ones. Any
// text coming back from the SDK goes through Redact() before it is printed.
//
// Usage (the operator exports the credentials; nothing here reads .env):
//   set -a && source .env && set +a
//   ./build/md/tevnnis-md-longbridge-smoke [SYMBOL ...]     # default: AAPL.US

#include <algorithm>
#include <array>
#include <cctype>
#include <chrono>
#include <cstdint>
#include <cstdlib>
#include <ctime>
#include <future>
#include <iomanip>
#include <iostream>
#include <memory>
#include <optional>
#include <string>
#include <utility>
#include <vector>

#include <longport.hpp>

#include "md/longbridge_await.hpp"

namespace {

using longport::Config;
using longport::Status;
using longport::quote::QuoteContext;
using longport::quote::SecurityQuote;
using tevnnis::md::Await;
using tevnnis::md::AwaitResult;
using tevnnis::md::DescribeSdkStatus;

constexpr std::chrono::seconds kCallTimeout{20};

constexpr std::array<const char*, 3> kRequiredEnvVars = {
    "LONGPORT_APP_KEY", "LONGPORT_APP_SECRET", "LONGPORT_ACCESS_TOKEN"};

// Presence check only: getenv's result is compared against nullptr/empty and
// then discarded. The value is never read past its first byte, never copied and
// never printed — a missing credential is reported by variable NAME (§12).
std::vector<std::string> MissingCredentialNames() {
    std::vector<std::string> missing;
    for (const char* name : kRequiredEnvVars) {
        const char* value = std::getenv(name);
        if (value == nullptr || *value == '\0') {
            missing.emplace_back(name);
        }
    }
    return missing;
}

// ---------------------------------------------------------------------------
// Formatting
// ---------------------------------------------------------------------------

std::string FormatUtc(std::int64_t epoch_secs) {
    const auto t = static_cast<std::time_t>(epoch_secs);
    std::tm tm{};
    if (gmtime_r(&t, &tm) == nullptr) {
        return "<out of range>";
    }
    std::array<char, 32> buf{};
    if (std::strftime(buf.data(), buf.size(), "%Y-%m-%dT%H:%M:%SZ", &tm) == 0) {
        return "<out of range>";
    }
    return std::string(buf.data());
}

// Prints the raw timestamp decoded both ways, so the seconds-vs-millis question
// is answered by observation rather than by trusting a source-code reading.
void ReportTimestampUnit(std::int64_t raw) {
    std::cout << "    timestamp=" << raw << "\n"
              << "      as seconds -> " << FormatUtc(raw) << "\n"
              << "      as millis  -> " << FormatUtc(raw / 1000) << "\n";
}

}  // namespace

int main(int argc, char** argv) {
    std::vector<std::string> symbols;
    for (int i = 1; i < argc; ++i) {
        const std::string arg = argv[i];
        if (arg == "-h" || arg == "--help") {
            std::cout << "usage: tevnnis-md-longbridge-smoke [SYMBOL ...]   "
                         "(default: AAPL.US)\n";
            return 0;
        }
        if (!arg.empty() && arg.front() == '-') {
            std::cerr << "unknown argument: " << arg << "\n";
            return 2;
        }
        symbols.push_back(arg);
    }
    if (symbols.empty()) {
        symbols.emplace_back("AAPL.US");
    }

    std::cout << "tevnnis-md longbridge connectivity smoke (read-only, no "
                 "subscription, no orders)\n";

    // ----- 1. credentials present? (names only, never values) --------------
    const std::vector<std::string> missing = MissingCredentialNames();
    if (!missing.empty()) {
        std::cerr << "missing credential environment variable(s):\n";
        for (const std::string& name : missing) {
            std::cerr << "  - " << name << "\n";
        }
        std::cerr << "export them first, e.g.:  set -a && source .env && set +a\n";
        return 1;
    }
    std::cout << "credentials: all 3 LONGPORT_* variables are set "
                 "(values never read or printed)\n";

    // ----- 2. build a Config from the environment --------------------------
    // The SDK reads the three variables itself; this program never sees them.
    Status config_status;
    Config config = Config::from_apikey_env(config_status);
    if (config_status.is_err()) {
        std::cerr << "failed to build a Config from the environment: "
                  << DescribeSdkStatus(config_status) << "\n";
        return 1;
    }

    // ----- 3. connect ------------------------------------------------------
    QuoteContext ctx = QuoteContext::create(config);
    std::cout << "QuoteContext: created\n";

    // ----- 4. quote entitlement (§3: real-time may be paid; delayed is free
    //          and adequate for v0) ----------------------------------------
    const AwaitResult<std::string> level = Await<std::string>(
        [&ctx](auto callback) { ctx.quote_level(std::move(callback)); }, kCallTimeout);
    if (level.timed_out) {
        std::cerr << "quote_level timed out after " << kCallTimeout.count() << "s\n";
        return 1;
    }
    if (!level.ok) {
        std::cerr << "quote_level failed: " << level.error << "\n";
        return 1;
    }
    std::cout << "quote level: " << level.value << "\n";

    // ----- 5. one batched snapshot call ------------------------------------
    std::cout << "requesting a snapshot for " << symbols.size() << " symbol(s):";
    for (const std::string& symbol : symbols) {
        std::cout << " " << symbol;
    }
    std::cout << "\n";

    const AwaitResult<std::vector<SecurityQuote>> quotes =
        Await<std::vector<SecurityQuote>>(
            [&ctx, &symbols](auto callback) { ctx.quote(symbols, std::move(callback)); },
            kCallTimeout);
    if (quotes.timed_out) {
        std::cerr << "quote timed out after " << kCallTimeout.count() << "s\n";
        return 1;
    }
    if (!quotes.ok) {
        std::cerr << "quote failed: " << quotes.error << "\n";
        return 1;
    }

    std::cout << std::fixed << std::setprecision(4);
    std::vector<std::string> unseedable;
    for (const SecurityQuote& q : quotes.value) {
        const auto prev_close = static_cast<double>(q.prev_close);
        std::cout << "  " << q.symbol << "\n"
                  << "    last_done=" << static_cast<double>(q.last_done)
                  << " prev_close=" << prev_close
                  << " open=" << static_cast<double>(q.open)
                  << " high=" << static_cast<double>(q.high)
                  << " low=" << static_cast<double>(q.low) << "\n"
                  << "    volume=" << q.volume
                  << " turnover=" << static_cast<double>(q.turnover)
                  << " trade_status=" << static_cast<int>(q.trade_status) << "\n";
        ReportTimestampUnit(q.timestamp);
        if (prev_close > 0.0) {
            std::cout << "    change_pct vs prev_close="
                      << (static_cast<double>(q.last_done) - prev_close) / prev_close * 100.0
                      << "%\n";
        } else {
            // A symbol whose prev_close cannot be seeded would have every one
            // of its live pushes rejected downstream, so name it here.
            unseedable.push_back(q.symbol);
            std::cout << "    change_pct: UNAVAILABLE (prev_close <= 0)\n";
        }
    }

    // ----- 6. verdict ------------------------------------------------------
    for (const std::string& requested : symbols) {
        const bool returned =
            std::any_of(quotes.value.begin(), quotes.value.end(),
                        [&requested](const SecurityQuote& q) { return q.symbol == requested; });
        if (!returned) {
            unseedable.push_back(requested + " (no quote returned)");
        }
    }
    std::cout << "returned " << quotes.value.size() << "/" << symbols.size() << " symbol(s)\n";
    if (!unseedable.empty()) {
        std::cerr << "WARNING: these symbols could not supply a usable prev_close and would "
                     "have every live push rejected:\n";
        for (const std::string& symbol : unseedable) {
            std::cerr << "  - " << symbol << "\n";
        }
        return 1;
    }

    std::cout << "OK — credentials, US quote permission and SDK link wiring all verified\n";
    return 0;
}
