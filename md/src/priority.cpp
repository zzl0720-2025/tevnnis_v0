#include "md/priority.hpp"

#include <cmath>

namespace tevnnis::md {

namespace {
constexpr double kEpsilon = 1e-9;
}  // namespace

tevnnis::Priority AssignPriority(const tevnnis::MarketEvent& event,
                                 const PipelineConfig& config) {
    switch (event.type()) {
        case tevnnis::STATUS: {
            const auto status = event.status().status();
            const bool halt_or_resume = status == tevnnis::StatusPayload::HALTED ||
                                        status == tevnnis::StatusPayload::RESUMED;
            // Only a halt/resume on a specific universe symbol is CRITICAL; a
            // market-wide session change is context, not an alarm.
            if (halt_or_resume && !event.symbol().empty()) {
                return tevnnis::CRITICAL;
            }
            return tevnnis::LOW;
        }
        case tevnnis::QUOTE_MOVE: {
            const double abs_change = std::fabs(event.quote().change_pct());
            if (abs_change + kEpsilon >= config.critical_move_pct) {
                return tevnnis::CRITICAL;
            }
            int crossed = 0;
            for (const double band : config.escalation_bands) {
                if (abs_change + kEpsilon >= band) {
                    ++crossed;
                }
            }
            if (crossed >= 2) {
                return tevnnis::HIGH;
            }
            if (crossed >= 1) {
                return tevnnis::MEDIUM;
            }
            return tevnnis::LOW;
        }
        case tevnnis::NEWS:
            // News is never CRITICAL in v0 (§4.4). It reaches this point only
            // when it relates to a universe symbol, so it is worth a look.
            return tevnnis::MEDIUM;
        default:
            return tevnnis::LOW;
    }
}

}  // namespace tevnnis::md
