#pragma once
// News de-duplication for the Data Unifier (§2.1 "normalize + dedup").
//
// Two layers, both in-memory and both scoped to a rolling time window:
//   1. EXACT — the same `news_id` seen again (the §4.2 dedup primary key).
//   2. NEAR  — a different id carrying substantially the same headline, caught
//      with a 64-bit SimHash over the title's token set and a Hamming-distance
//      threshold. Wire services routinely re-issue a story under a new id with
//      a lightly reworded headline; without this, each re-issue would wake the
//      LLM again.
//
// The window is deliberately in-memory only: md persists nothing (§2.2, core is
// the sole DB writer). The DB's `event_id` unique constraint is the durable
// backstop for the exact layer across an md restart.

#include <cstdint>
#include <deque>
#include <string>
#include <string_view>
#include <unordered_set>

namespace tevnnis::md {

// 64-bit SimHash over the title's token set. Tokens are maximal [a-z0-9] runs
// after lowercasing, so case, punctuation and word order do not affect the
// result; a token appearing twice is counted once.
[[nodiscard]] std::uint64_t SimHash64(std::string_view text);

// Number of differing bits between two hashes.
[[nodiscard]] int HammingDistance(std::uint64_t a, std::uint64_t b);

class DedupWindow {
   public:
    enum class Verdict { kNew, kDuplicateExact, kDuplicateNear };

    // `window_ms` <= 0 disables time-based eviction (entries only age out via
    // `max_entries`). `hamming_threshold` < 0 disables the near-duplicate layer.
    DedupWindow(std::int64_t window_ms, int hamming_threshold, std::size_t max_entries = 4096);

    // Classifies (news_id, title) against the window and, when the verdict is
    // kNew, records it. `now_ms` drives eviction, so it must be non-decreasing
    // across calls for the window to behave as a time window.
    Verdict CheckAndRecord(const std::string& news_id, const std::string& title,
                           std::int64_t now_ms);

    [[nodiscard]] std::size_t size() const { return entries_.size(); }

   private:
    struct Entry {
        std::string news_id;
        std::uint64_t simhash = 0;
        std::int64_t ts = 0;
    };

    void Evict(std::int64_t now_ms);

    std::int64_t window_ms_;
    int hamming_threshold_;
    std::size_t max_entries_;
    std::deque<Entry> entries_;             // oldest first
    std::unordered_set<std::string> ids_;   // mirrors entries_' news_ids
};

}  // namespace tevnnis::md
