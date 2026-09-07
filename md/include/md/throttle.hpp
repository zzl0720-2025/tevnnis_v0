#pragma once
// QuoteThrottle — the four §4.4 down-sampling knobs, the first LLM-cost gate.
//
// Layers, applied in order to every in-universe quote tick:
//   1. ENTRY THRESHOLD X   — |change_pct| must reach `entry_threshold_pct`.
//   2. ESCALATION BANDS    — emit only when the tick crosses into a band higher
//      than the symbol's current band; wobbling within a band emits nothing.
//      A tick that falls back below X resets the current band, so a fresh move
//      can trigger again later (that is what makes the cooldown observable).
//   3. PER-SYMBOL COOLDOWN T — after an emission, further events for that
//      symbol are suppressed for T minutes unless the tick reaches a band
//      strictly higher than the last EMITTED band.
//   4. SECTOR RATE CAP N   — at most N emissions per sector per rolling hour;
//      excess is dropped and counted into `dropped_count` (§4.3).
//
// CRITICAL bypass (v0 decision): a tick at or beyond `critical_move_pct`
// bypasses layers 3 and 4 — a halt-grade move must never be swallowed by a
// cooldown or a rate cap — but still obeys layer 2, so it cannot repeat itself.
// CRITICAL emissions are not counted against the sector rate cap either, so a
// burst of them cannot starve ordinary events out of the next hour.
//
// KNOWN LIMITATION (§4.4 read literally): bands are computed on |change_pct|,
// so they are sign-agnostic. A symbol that swings from +4% to -4% stays inside
// band 1 and re-triggers nothing; only a *larger absolute* move escalates, or a
// return below X followed by a fresh move once the cooldown has expired.

#include <cstdint>
#include <deque>
#include <string>
#include <unordered_map>

#include "md/pipeline_config.hpp"

namespace tevnnis::md {

enum class ThrottleDecision {
    kEmit,
    kBelowEntryThreshold,  // layer 1 — not eligible at all
    kSameOrLowerBand,      // layer 2 — in-band wobble, collapsed
    kCooldown,             // layer 3 — suppressed inside T
    kSectorRateCap,        // layer 4 — over N per sector per hour, dropped
};

struct ThrottleResult {
    ThrottleDecision decision = ThrottleDecision::kBelowEntryThreshold;
    int band_index = 0;      // number of configured bands the tick has crossed
    bool critical = false;   // |change_pct| >= critical_move_pct
    std::string trigger;     // §4.2 QuotePayload.trigger, set when kEmit
};

class QuoteThrottle {
   public:
    struct Counters {
        std::int64_t emitted = 0;
        std::int64_t below_threshold = 0;
        std::int64_t same_band = 0;
        std::int64_t cooldown = 0;
        std::int64_t rate_cap = 0;
    };

    // `config` must outlive the throttle and must already be Finalize()d.
    explicit QuoteThrottle(const PipelineConfig& config);

    // Evaluates one tick. `now_ms` drives the cooldown and rate-cap windows and
    // must be non-decreasing across calls.
    ThrottleResult Evaluate(const std::string& symbol, const std::string& sector,
                            double change_pct, std::int64_t now_ms);

    [[nodiscard]] const Counters& counters() const { return counters_; }

   private:
    struct SymbolState {
        int current_band = -1;       // resets when the move falls back below X
        int last_emitted_band = -1;  // never resets; guards the cooldown override
        std::int64_t last_emit_ms = 0;
        bool ever_emitted = false;
    };

    [[nodiscard]] int BandIndexFor(double abs_change_pct) const;

    const PipelineConfig& config_;
    std::int64_t cooldown_ms_;
    std::unordered_map<std::string, SymbolState> symbols_;
    std::unordered_map<std::string, std::deque<std::int64_t>> sector_emissions_;
    Counters counters_;
};

}  // namespace tevnnis::md
