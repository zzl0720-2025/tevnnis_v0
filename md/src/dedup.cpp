#include "md/dedup.hpp"

#include <array>
#include <bitset>
#include <cctype>

namespace tevnnis::md {

namespace {

// FNV-1a, 64-bit. Chosen for being tiny, dependency-free and deterministic
// across platforms — the whole md plane must replay identically in tests.
std::uint64_t Fnv1a64(std::string_view s) {
    std::uint64_t h = 1469598103934665603ULL;
    for (const unsigned char c : s) {
        h ^= static_cast<std::uint64_t>(c);
        h *= 1099511628211ULL;
    }
    return h;
}

}  // namespace

std::uint64_t SimHash64(std::string_view text) {
    std::array<int, 64> votes{};
    std::unordered_set<std::string> seen;

    std::string token;
    const auto flush = [&]() {
        if (token.empty()) return;
        if (seen.insert(token).second) {
            const std::uint64_t h = Fnv1a64(token);
            for (int bit = 0; bit < 64; ++bit) {
                votes[static_cast<std::size_t>(bit)] += ((h >> bit) & 1ULL) ? 1 : -1;
            }
        }
        token.clear();
    };

    for (const unsigned char c : text) {
        if (std::isalnum(c) != 0) {
            token.push_back(static_cast<char>(std::tolower(c)));
        } else {
            flush();
        }
    }
    flush();

    std::uint64_t out = 0;
    for (int bit = 0; bit < 64; ++bit) {
        if (votes[static_cast<std::size_t>(bit)] > 0) {
            out |= (1ULL << bit);
        }
    }
    return out;
}

int HammingDistance(std::uint64_t a, std::uint64_t b) {
    return static_cast<int>(std::bitset<64>(a ^ b).count());
}

DedupWindow::DedupWindow(std::int64_t window_ms, int hamming_threshold, std::size_t max_entries)
    : window_ms_(window_ms),
      hamming_threshold_(hamming_threshold),
      max_entries_(max_entries == 0 ? 1 : max_entries) {}

void DedupWindow::Evict(std::int64_t now_ms) {
    if (window_ms_ > 0) {
        while (!entries_.empty() && entries_.front().ts < now_ms - window_ms_) {
            ids_.erase(entries_.front().news_id);
            entries_.pop_front();
        }
    }
    while (entries_.size() > max_entries_) {
        ids_.erase(entries_.front().news_id);
        entries_.pop_front();
    }
}

DedupWindow::Verdict DedupWindow::CheckAndRecord(const std::string& news_id,
                                                 const std::string& title,
                                                 std::int64_t now_ms) {
    Evict(now_ms);

    if (ids_.count(news_id) != 0) {
        return Verdict::kDuplicateExact;
    }

    const std::uint64_t hash = SimHash64(title);
    if (hamming_threshold_ >= 0) {
        for (const auto& entry : entries_) {
            if (HammingDistance(entry.simhash, hash) <= hamming_threshold_) {
                return Verdict::kDuplicateNear;
            }
        }
    }

    entries_.push_back(Entry{news_id, hash, now_ms});
    ids_.insert(news_id);
    Evict(now_ms);
    return Verdict::kNew;
}

}  // namespace tevnnis::md
