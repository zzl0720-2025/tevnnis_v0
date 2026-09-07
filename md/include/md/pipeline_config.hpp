#pragma once
// PipelineConfig — the md data plane's view of the strategy configuration.
//
// It holds exactly two things from §6: the `universe` (sector -> symbols, which
// is both the symbol filter and the sector classification table) and the
// `triggers` block (the four §4.4 throttling knobs plus critical_move_pct),
// alongside a few md-local knobs that never leave this plane.
//
// CONFIG OWNERSHIP (v0 decision): the authored configuration file is
// config/config.example.yaml, owned by `core`. md is NOT a second source of
// truth. For standalone and integration runs, the `tevnnis-md`
// demo binary reads a small JSON file holding the same `universe` + `triggers`
// subset (§6 allows YAML or JSON) purely so it can run standalone; tests build
// PipelineConfig in code. When the planes are wired, `core` reads the YAML and
// hands md this subset over the wire / on startup, and the standalone JSON file
// goes away. Do not author and maintain two config files in parallel.

#include <cstddef>
#include <stdexcept>
#include <string>
#include <unordered_map>
#include <vector>

namespace tevnnis::md {

// Thrown when a configuration is missing, malformed, or internally invalid.
class ConfigError : public std::runtime_error {
   public:
    explicit ConfigError(const std::string& what) : std::runtime_error(what) {}
};

// One sector of the §6 universe allowlist. Order is significant: for news
// naming several universe symbols, the first match in universe order becomes
// the event's primary symbol/sector (v0 decision: one event, no fan-out).
struct SectorUniverse {
    std::string sector;
    std::vector<std::string> symbols;
};

// Knobs for the live news poller (md-local; not part of §6).
//
// They matter more than they look. The Longbridge news endpoint takes one
// symbol per call and has no `since` cursor, so the poller re-reads the same
// list every cycle, and news is exempt from §4.4's four quote-throttling knobs.
// These are therefore the ONLY bound on how much news enters the pipeline.
struct NewsPollConfig {
    int poll_interval_seconds = 300;  // one full cycle over the universe
    int min_call_spacing_ms = 250;    // pacing between per-symbol calls (§3 rate limits)
    int call_timeout_seconds = 20;    // per-call await bound
    int max_age_minutes = 120;        // backfill window; 0 disables the age filter
    std::size_t max_items_per_symbol = 5;  // newest N kept per symbol per poll
    std::size_t seen_capacity = 4096;      // bounded seen-news_id window
};

struct PipelineConfig {
    // §6 universe — the allowlist IS the filter; anything outside it is dropped.
    std::vector<SectorUniverse> universe;

    // §4.4 knobs (defaults match config/config.example.yaml).
    double entry_threshold_pct = 3.0;            // X — eligibility gate
    std::vector<double> escalation_bands{3.0, 5.0, 8.0};
    int cooldown_minutes = 15;                   // T — per-symbol quiet period
    int sector_rate_cap_per_hour = 12;           // N — per-sector emission cap
    double critical_move_pct = 8.0;              // narrow CRITICAL move rule

    // md-local knobs (not part of §6; they never affect core's behaviour).
    int news_title_max_chars = 200;  // §4.2 "truncated to N chars"
    int dedup_window_minutes = 60;   // rolling near-duplicate window
    // <= this 64-bit Hamming distance => near-duplicate. Calibrated on short
    // headlines: an identical headline scores 0 and a one-word rewording ~4,
    // while distinct stories sharing a headline skeleton ("Exxon announces
    // dividend increase" vs "... cut") score ~10. 8 is deliberately on the
    // conservative side — missing a collapse costs a few tokens, whereas a
    // false collapse hides a real story from the agent.
    int simhash_hamming_threshold = 8;
    std::size_t retain_buffer_capacity = 2048;  // events kept for cursor replay

    // Live news poller. Unused unless --news-source longbridge.
    NewsPollConfig news;

    // Derived index built by Finalize(); do not populate by hand.
    std::unordered_map<std::string, std::string> symbol_to_sector;

    // Validates the configuration and builds symbol_to_sector.
    // Throws ConfigError on: an empty universe, a duplicated symbol, an empty
    // sector name, non-ascending bands, or a non-positive knob.
    void Finalize();

    // Sector of `symbol`, or nullptr when it is outside the universe.
    // Requires Finalize() to have been called.
    [[nodiscard]] const std::string* SectorOf(const std::string& symbol) const;

    [[nodiscard]] bool InUniverse(const std::string& symbol) const {
        return SectorOf(symbol) != nullptr;
    }

    // Sector names in universe (authored) order.
    [[nodiscard]] std::vector<std::string> Sectors() const;
};

// Loads the `universe` + `triggers` subset from a JSON file shaped like the §6
// config. Used only by the standalone `tevnnis-md` demo binary — see the
// CONFIG OWNERSHIP note above. Throws ConfigError on missing/malformed input.
PipelineConfig LoadPipelineConfigFromJson(const std::string& path);

}  // namespace tevnnis::md
