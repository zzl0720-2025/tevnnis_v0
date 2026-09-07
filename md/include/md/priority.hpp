#pragma once
// Priority assignment (§4.1 four levels, §4.4 narrow CRITICAL rules).
//
// CRITICAL is defined narrowly so it stays inherently rare — that is what makes
// the deferred WakeSignals channel safe to enable later:
//   * a trading halt or resume on a universe symbol;
//   * an extreme intraday move, |change_pct| >= critical_move_pct (default 8%);
//   * a circuit-breaker / LULD-type event, which arrives as a halt status.
// News is never CRITICAL in v0.
//
//   Event                                        Priority
//   -------------------------------------------  --------
//   STATUS  HALTED / RESUMED on a universe symbol CRITICAL
//   QUOTE   |change_pct| >= critical_move_pct     CRITICAL
//   QUOTE   crossed >= 2 escalation bands         HIGH
//   QUOTE   crossed >= 1 escalation band          MEDIUM
//   NEWS    any (universe-related by construction)MEDIUM
//   STATUS  PRE_MARKET / POST_MARKET / CLOSED     LOW
//   QUOTE   below the first band                  LOW

#include "events.pb.h"
#include "md/pipeline_config.hpp"

namespace tevnnis::md {

// Pure function of the normalized event and the configured thresholds.
[[nodiscard]] tevnnis::Priority AssignPriority(const tevnnis::MarketEvent& event,
                                               const PipelineConfig& config);

}  // namespace tevnnis::md
