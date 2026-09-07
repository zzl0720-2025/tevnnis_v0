#include "md/sector_queue.hpp"

#include <algorithm>
#include <chrono>
#include <iomanip>
#include <random>
#include <sstream>
#include <unordered_set>

namespace tevnnis::md {

namespace {

std::size_t LevelOf(tevnnis::Priority priority) {
    return static_cast<std::size_t>(std::clamp(static_cast<int>(priority), 0, 3));
}

}  // namespace

CursorCodec::CursorCodec(std::string epoch) : epoch_(std::move(epoch)) {}

std::string CursorCodec::NewEpochId() {
    std::random_device rd;
    const std::uint64_t value = (static_cast<std::uint64_t>(rd()) << 32) ^ rd();
    std::ostringstream out;
    out << std::hex << std::setw(16) << std::setfill('0') << value;
    return out.str();
}

std::string CursorCodec::Encode(std::uint64_t seq) const {
    return epoch_ + ":" + std::to_string(seq);
}

std::optional<std::uint64_t> CursorCodec::Decode(const std::string& cursor) const {
    const auto colon = cursor.rfind(':');
    if (cursor.empty() || colon == std::string::npos) {
        return std::nullopt;
    }
    if (cursor.substr(0, colon) != epoch_) {
        return std::nullopt;  // from a previous md process — unrecognized
    }
    const std::string digits = cursor.substr(colon + 1);
    if (digits.empty() ||
        digits.find_first_not_of("0123456789") != std::string::npos) {
        return std::nullopt;
    }
    try {
        return std::stoull(digits);
    } catch (const std::exception&) {
        return std::nullopt;
    }
}

SectorQueues::SectorQueues(std::size_t capacity) : capacity_(capacity == 0 ? 1 : capacity) {}

void SectorQueues::DropOldest() {
    const Buffered& oldest = buffer_.front();
    auto it = sectors_.find(oldest.event.sector());
    if (it != sectors_.end()) {
        std::deque<std::uint64_t>& level = it->second[LevelOf(oldest.event.priority())];
        if (!level.empty() && level.front() == oldest.seq) {
            level.pop_front();  // seqs are ascending within a level
        } else {
            level.erase(std::remove(level.begin(), level.end(), oldest.seq), level.end());
        }
    }
    buffer_.pop_front();
    ++evicted_;
}

std::uint64_t SectorQueues::Enqueue(tevnnis::MarketEvent event) {
    const std::uint64_t seq = next_seq_++;
    sectors_[event.sector()][LevelOf(event.priority())].push_back(seq);
    buffer_.push_back(Buffered{seq, std::move(event)});
    while (buffer_.size() > capacity_) {
        DropOldest();
    }
    return seq;
}

const SectorQueues::Buffered* SectorQueues::Find(std::uint64_t seq) const {
    if (buffer_.empty() || seq < buffer_.front().seq || seq > buffer_.back().seq) {
        return nullptr;
    }
    // Sequences are consecutive within the buffer, so the index is exact.
    return &buffer_[static_cast<std::size_t>(seq - buffer_.front().seq)];
}

std::size_t SectorQueues::LevelSize(const std::string& sector,
                                    tevnnis::Priority priority) const {
    const auto it = sectors_.find(sector);
    return it == sectors_.end() ? 0 : it->second[LevelOf(priority)].size();
}

SectorQueues::Selection SectorQueues::Select(std::uint64_t since_seq, int max_events,
                                             tevnnis::Priority min_priority,
                                             const std::vector<std::string>& sectors) const {
    const std::unordered_set<std::string> wanted(sectors.begin(), sectors.end());

    // Walk the multi-level queues from CRITICAL down, gathering everything that
    // matches. The whole matching set is needed, not just the top N: the
    // watermark below depends on what was left behind.
    std::vector<const Buffered*> candidates;
    for (const auto& [sector, levels] : sectors_) {
        // An empty sector marks a market-wide event; it is context for every
        // sector, so a sector filter never excludes it.
        if (!wanted.empty() && !sector.empty() && wanted.count(sector) == 0) {
            continue;
        }
        for (std::size_t level = kLevels; level-- > LevelOf(min_priority);) {
            for (const std::uint64_t seq : levels[level]) {
                if (seq <= since_seq) {
                    continue;
                }
                if (const Buffered* buffered = Find(seq); buffered != nullptr) {
                    candidates.push_back(buffered);
                }
            }
        }
    }

    std::sort(candidates.begin(), candidates.end(),
              [](const Buffered* a, const Buffered* b) {
                  if (a->event.priority() != b->event.priority()) {
                      return a->event.priority() > b->event.priority();
                  }
                  if (a->event.event_ts() != b->event.event_ts()) {
                      return a->event.event_ts() < b->event.event_ts();
                  }
                  return a->seq < b->seq;
              });

    Selection selection;
    selection.considered = static_cast<int>(candidates.size());

    const std::size_t take =
        max_events <= 0 ? candidates.size()
                        : std::min(candidates.size(), static_cast<std::size_t>(max_events));

    std::uint64_t max_delivered = since_seq;
    selection.events.reserve(take);
    for (std::size_t i = 0; i < take; ++i) {
        selection.events.push_back(&candidates[i]->event);
        max_delivered = std::max(max_delivered, candidates[i]->seq);
    }

    // Watermark: advance only past events that were actually delivered. If the
    // batch was truncated, hold just below the lowest event left behind — that
    // may re-deliver higher-seq events already sent, which is the deliberate
    // trade for never skipping one (see the cursor contract in the header).
    std::uint64_t watermark = max_delivered;
    for (std::size_t i = take; i < candidates.size(); ++i) {
        watermark = std::min(watermark, candidates[i]->seq - 1);
    }
    selection.next_seq = std::max(since_seq, watermark);
    return selection;
}

}  // namespace tevnnis::md
