// tevnnis-md-longbridge-news-smoke — isolated news connectivity probe.
//
// The news counterpart of tevnnis-md-longbridge-smoke, and built the same way:
// a standalone target that links ONLY the Longbridge SDK — deliberately not
// tevnnis_md, not the pipeline, not gRPC — so a failure here can only be the
// SDK or the account, never our own code.
//
// It verifies the facts the mapping and snapshot URL sanitizer are
// built on, by printing evidence rather than asking anyone to trust a comment:
//
//   1. the LONGPORT_* credentials work and the account can read news at all;
//   2. `published_at` carries no documented unit. The C binding calls Rust's
//      `unix_timestamp()`, i.e. SECONDS, while NewsEvent::event_ts is epoch
//      MILLIS — so the value is decoded BOTH ways below. Whichever line reads
//      as a plausible recent time is the answer;
//   3. `NewsItem` has no `source` field, so md derives one from the url host.
//      The derived value is printed next to the raw url so it can be sanity
//      checked;
//   4. WHAT THE URLS ACTUALLY LOOK LIKE. This is the point of the exercise.
//      The url goes into a world-readable snapshot file, so the raw url, its
//      parsed host, and the NAMES of its query parameters are printed. That
//      evidence decides two config defaults:
//        * `news_url_allow_hosts` ships EMPTY (any https host allowed) because
//          a news feed's publisher domains are not enumerable — a Longbridge-only
//          list would silently null every external link. If these urls turn out
//          to be uniformly Longbridge-internal, populate the list then, on this
//          evidence rather than on a guess.
//        * `news_url_keep_params` ships EMPTY (every query param stripped). If
//          a param below is load-bearing for the article to resolve, that is
//          when to add it — and anything token-shaped stays stripped regardless.
//      Param VALUES are deliberately NOT printed: one of them could be exactly
//      the session token this whole sanitizer exists to keep out of a file.
//
// Read-only and cheap: one call per symbol, once, then exit. It never
// subscribes, never trades, and never touches the database (core is the sole DB
// writer, §2.2).
//
// Secrets (§12): the SDK itself reads LONGPORT_APP_KEY / LONGPORT_APP_SECRET /
// LONGPORT_ACCESS_TOKEN inside Config::from_apikey_env. This program never
// reads, copies, stores, prints or logs a credential VALUE — it only checks
// whether each variable is set and non-empty, and names the missing ones. Any
// text coming back from the SDK goes through Redact() before it is printed.
//
// Usage (the operator exports the credentials; nothing here reads .env):
//   set -a && source .env && set +a
//   ./build/md/tevnnis-md-longbridge-news-smoke [SYMBOL ...]   # default: AAPL.US

#include <array>
#include <chrono>
#include <cstdint>
#include <cstdlib>
#include <ctime>
#include <iostream>
#include <string>
#include <utility>
#include <vector>

#include <longport.hpp>

#include "md/longbridge_await.hpp"

namespace {

using longport::Config;
using longport::Status;
using longport::content::ContentContext;
using longport::content::NewsItem;
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

// Splits a url into (scheme, host, path, [param names]). Intentionally a tiny
// hand-rolled parser: this target links the SDK alone, and the real sanitizer
// lives in Python (core/src/tevnnis_core/snapshot.py) where it is unit-tested.
struct UrlParts {
    std::string scheme;
    std::string host;
    std::string path;
    std::vector<std::string> param_names;
    bool has_fragment = false;
};

UrlParts SplitUrl(const std::string& url) {
    UrlParts parts;
    const std::size_t scheme_end = url.find("://");
    if (scheme_end == std::string::npos) {
        return parts;
    }
    parts.scheme = url.substr(0, scheme_end);
    const std::size_t authority_start = scheme_end + 3;

    std::size_t authority_end = url.size();
    for (std::size_t i = authority_start; i < url.size(); ++i) {
        if (url[i] == '/' || url[i] == '?' || url[i] == '#') {
            authority_end = i;
            break;
        }
    }
    parts.host = url.substr(authority_start, authority_end - authority_start);

    const std::size_t query_start = url.find('?', authority_end);
    const std::size_t fragment_start = url.find('#', authority_end);
    parts.has_fragment = fragment_start != std::string::npos;

    const std::size_t path_end =
        std::min(query_start == std::string::npos ? url.size() : query_start,
                 fragment_start == std::string::npos ? url.size() : fragment_start);
    parts.path = url.substr(authority_end, path_end - authority_end);

    if (query_start != std::string::npos) {
        const std::size_t query_end =
            (fragment_start != std::string::npos && fragment_start > query_start)
                ? fragment_start
                : url.size();
        const std::string query = url.substr(query_start + 1, query_end - query_start - 1);
        std::size_t pos = 0;
        while (pos < query.size()) {
            const std::size_t amp = query.find('&', pos);
            const std::size_t end = amp == std::string::npos ? query.size() : amp;
            const std::string pair = query.substr(pos, end - pos);
            const std::size_t eq = pair.find('=');
            // NAME only. A value could be the very token this exists to catch.
            parts.param_names.push_back(eq == std::string::npos ? pair : pair.substr(0, eq));
            pos = end + 1;
        }
    }
    return parts;
}

// Mirrors NewsSourceFromUrl in md/longbridge_news_mapping.cpp closely enough to
// eyeball; the authoritative version is there and is unit-tested.
std::string DerivedSource(const std::string& host) {
    std::string h = host;
    if (const std::size_t at = h.rfind('@'); at != std::string::npos) {
        h = h.substr(at + 1);
    }
    if (const std::size_t colon = h.find(':'); colon != std::string::npos) {
        h = h.substr(0, colon);
    }
    for (char& c : h) {
        c = static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
    }
    if (h.rfind("www.", 0) == 0) {
        h = h.substr(4);
    }
    return h.empty() ? "longbridge" : h;
}

}  // namespace

int main(int argc, char** argv) {
    std::vector<std::string> symbols;
    for (int i = 1; i < argc; ++i) {
        const std::string arg = argv[i];
        if (arg == "-h" || arg == "--help") {
            std::cout << "usage: tevnnis-md-longbridge-news-smoke [SYMBOL ...]   "
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

    std::cout << "tevnnis-md longbridge NEWS smoke (read-only, one call per symbol, no "
                 "orders)\n";

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
    Status config_status;
    Config config = Config::from_apikey_env(config_status);
    if (config_status.is_err()) {
        std::cerr << "failed to build a Config from the environment: "
                  << DescribeSdkStatus(config_status) << "\n";
        return 1;
    }

    // ----- 3. connect ------------------------------------------------------
    ContentContext ctx = ContentContext::create(config);
    std::cout << "ContentContext: created\n";

    // ----- 4. one news call per symbol -------------------------------------
    std::size_t total_items = 0;
    std::vector<std::string> failed;
    for (const std::string& symbol : symbols) {
        std::cout << "\n=== " << symbol << " ===\n";
        const AwaitResult<std::vector<NewsItem>> items = Await<std::vector<NewsItem>>(
            [&ctx, &symbol](auto callback) { ctx.news(symbol, std::move(callback)); },
            kCallTimeout);
        if (!items.ok) {
            std::cerr << "  news failed: " << items.failure() << "\n";
            failed.push_back(symbol);
            continue;
        }
        std::cout << "  " << items.value.size() << " item(s)\n";
        total_items += items.value.size();

        for (const NewsItem& item : items.value) {
            const UrlParts parts = SplitUrl(item.url);
            std::cout << "  - news_id : " << item.id << "\n"
                      << "    title   : " << item.title << "\n"
                      << "    source  : " << DerivedSource(parts.host)
                      << "   (derived from the url host; NewsItem has no source field)\n"
                      << "    url     : " << item.url << "\n"
                      << "      scheme=" << (parts.scheme.empty() ? "<none>" : parts.scheme)
                      << " host=" << (parts.host.empty() ? "<none>" : parts.host)
                      << " path=" << (parts.path.empty() ? "<none>" : parts.path)
                      << " fragment=" << (parts.has_fragment ? "YES" : "no") << "\n"
                      << "      query params (NAMES only, values withheld by design): ";
            if (parts.param_names.empty()) {
                std::cout << "<none>";
            } else {
                for (const std::string& name : parts.param_names) {
                    std::cout << name << " ";
                }
            }
            std::cout << "\n"
                      << "    published_at=" << item.published_at << "\n"
                      << "      as seconds -> " << FormatUtc(item.published_at) << "\n"
                      << "      as millis  -> " << FormatUtc(item.published_at / 1000) << "\n"
                      << "      (mapping treats it as SECONDS; the 'as seconds' line should "
                         "read as a plausible recent time)\n";
        }
    }

    // ----- 5. verdict ------------------------------------------------------
    std::cout << "\n";
    if (!failed.empty()) {
        std::cerr << "news failed for " << failed.size() << " symbol(s):";
        for (const std::string& symbol : failed) {
            std::cerr << " " << symbol;
        }
        std::cerr << "\n";
        return 1;
    }
    if (total_items == 0) {
        std::cerr << "WARNING: every call succeeded but returned zero items. The live news "
                     "source would emit nothing. Check the account's news entitlement and "
                     "try a widely-covered symbol.\n";
        return 1;
    }
    std::cout << "OK - credentials, news access and SDK link wiring all verified ("
              << total_items << " item(s) across " << symbols.size() << " symbol(s))\n"
              << "Now eyeball the urls above: hosts decide news_url_allow_hosts, param names "
                 "decide news_url_keep_params. Both ship EMPTY.\n";
    return 0;
}
