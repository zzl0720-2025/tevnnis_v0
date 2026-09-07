#include "md/throttle.hpp"

#include <algorithm>
#include <cmath>
#include <sstream>

namespace tevnnis::md {

namespace {

constexpr std::int64_t kRateCapWindowMs = 60 * 60 * 1000;  // one rolling hour
// Guards against a tick sitting exactly on a threshold being classified as
// "below" because of binary floating-point representation.
constexpr double kEpsilon = 1e-9;

// "3", "5", "8.5" — no trailing zeros, so triggers read cleanly in a prompt.
std::string FormatPct(double value) {
    std::ostringstream out;
    if (std::fabs(value - std::round(value)) < 1e-6) {
        out << static_cast<long long>(std::llround(value));
    } else {
        out.setf(std::ios::fixed);
        out.precision(1);
        out << value;
    }
    return out.str();
}

}  // namespace

QuoteThrottle::QuoteThrottle(const PipelineConfig& config)
    : config_(config),
      cooldown_ms_(static_cast<std::int64_t>(config.cooldown_minutes) * 60 * 1000) {}

int QuoteThrottle::BandIndexFor(double abs_change_pct) const {
    int crossed = 0;
    for (const double band : config_.escalation_bands) {
        if (abs_change_pct + kEpsilon >= band) {
            ++crossed;
        }
    }
    return crossed;
}

ThrottleResult QuoteThrottle::Evaluate(const std::string& symbol, const std::string& sector,
                                       double change_pct, std::int64_t now_ms) {
    const double abs_change = std::fabs(change_pct);

    ThrottleResult result;
    result.band_index = BandIndexFor(abs_change);
    result.critical = abs_change + kEpsilon >= config_.critical_move_pct;

    SymbolState& state = symbols_[symbol];

    // Layer 1 — entry threshold X.
    if (abs_change + kEpsilon < config_.entry_threshold_pct) {
        state.current_band = -1;  // the move is over; a fresh one may re-trigger
        result.decision = ThrottleDecision::kBelowEntryThreshold;
        ++counters_.below_threshold;
        return result;
    }

    // Layer 2 — escalation bands (CRITICAL obeys this one too).
    if (result.band_index <= state.current_band) {
        result.decision = ThrottleDecision::kSameOrLowerBand;
        ++counters_.same_band;
        return result;
    }

    // Layer 3 — per-symbol cooldown T, overridden by a higher emitted band.
    if (!result.critical && state.ever_emitted && cooldown_ms_ > 0 &&
        now_ms - state.last_emit_ms < cooldown_ms_ &&
        result.band_index <= state.last_emitted_band) {
        result.decision = ThrottleDecision::kCooldown;
        ++counters_.cooldown;
        return result;
    }

    // Layer 4 — sector rate cap N per rolling hour.
    std::deque<std::int64_t>& emissions = sector_emissions_[sector];
    while (!emissions.empty() && emissions.front() <= now_ms - kRateCapWindowMs) {
        emissions.pop_front();
    }
    if (!result.critical &&
        emissions.size() >= static_cast<std::size_t>(config_.sector_rate_cap_per_hour)) {
        result.decision = ThrottleDecision::kSectorRateCap;
        ++counters_.rate_cap;
        return result;
    }

    state.current_band = result.band_index;
    state.last_emitted_band = std::max(state.last_emitted_band, result.band_index);
    state.last_emit_ms = now_ms;
    state.ever_emitted = true;
    if (!result.critical) {
        // CRITICAL emissions bypass the cap and are not counted against it.
        emissions.push_back(now_ms);
    }

    const double band_value = result.band_index > 0
                                  ? config_.escalation_bands[static_cast<std::size_t>(
                                        result.band_index - 1)]
                                  : config_.entry_threshold_pct;
    result.trigger =
        std::string("cross_") + (change_pct >= 0.0 ? "+" : "-") + FormatPct(band_value) + "pct";
    result.decision = ThrottleDecision::kEmit;
    ++counters_.emitted;
    return result;
}

}  // namespace tevnnis::md
