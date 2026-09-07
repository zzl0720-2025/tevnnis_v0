#include "md/ingress_queue.hpp"

#include <algorithm>
#include <string>

namespace tevnnis::md {

void IngressQueue::Push(LongbridgeTick tick) {
    std::string notice;
    bool loud = false;
    {
        const std::lock_guard<std::mutex> lock(mutex_);
        if (closed_) {
            return;  // shutting down: a late SDK callback is not an error
        }

        bool refuse_incoming = false;
        if (queue_.size() >= capacity_) {
            // Drop the oldest EVICTABLE tick to make room. Anything abnormal is
            // skipped over rather than dropped (§4.4: halt/resume is CRITICAL).
            const auto victim =
                std::find_if(queue_.begin(), queue_.end(),
                             [](const LongbridgeTick& t) { return IsEvictable(t); });
            if (victim != queue_.end()) {
                notice = "ingress overflow: dropped a queued quote for " + victim->symbol +
                         " (capacity " + std::to_string(capacity_) + ")";
                queue_.erase(victim);
                ++stats_.dropped_quotes;
                loud = stats_.dropped_quotes == 1 || stats_.dropped_quotes % kDropLogEvery == 0;
            } else if (IsEvictable(tick)) {
                // Every queued tick is unevictable, so there is nothing we may
                // discard — refuse the incoming quote instead of losing a halt.
                notice = "ingress overflow: refused an incoming quote for " + tick.symbol +
                         " (queue is full of non-droppable status ticks)";
                ++stats_.dropped_quotes;
                loud = stats_.dropped_quotes == 1 || stats_.dropped_quotes % kDropLogEvery == 0;
                refuse_incoming = true;
            } else if (queue_.size() >= capacity_ * kProtectedOverflowFactor) {
                // Should be unreachable. Bounded memory wins over an unbounded
                // backlog, but this is reported every single time it happens.
                notice = "ingress overflow: DROPPED A STATUS TICK for " + queue_.front().symbol +
                         " - the ingest thread is not draining; a halt/resume signal was LOST";
                queue_.pop_front();
                ++stats_.dropped_protected;
                loud = true;
            }
            // Otherwise the incoming tick is unevictable and we are still under
            // the defensive ceiling: let the queue exceed capacity to retain it.
        }

        if (!refuse_incoming) {
            queue_.push_back(std::move(tick));
            ++stats_.pushed;
            stats_.high_water = std::max(stats_.high_water, queue_.size());
        }
    }

    not_empty_.notify_one();

    // Logged outside the lock: the sink may do I/O, and a producer must never
    // hold the queue lock across a write.
    if (loud && drop_logger_) {
        drop_logger_(notice);
    }
}

bool IngressQueue::Pop(LongbridgeTick* out) {
    std::unique_lock<std::mutex> lock(mutex_);
    not_empty_.wait(lock, [this]() { return !queue_.empty() || closed_; });
    if (queue_.empty()) {
        return false;  // closed and drained
    }
    *out = std::move(queue_.front());
    queue_.pop_front();
    ++stats_.popped;
    return true;
}

void IngressQueue::Close() {
    {
        const std::lock_guard<std::mutex> lock(mutex_);
        closed_ = true;
    }
    not_empty_.notify_all();
}

bool IngressQueue::closed() const {
    const std::lock_guard<std::mutex> lock(mutex_);
    return closed_;
}

IngressQueue::Stats IngressQueue::stats() const {
    const std::lock_guard<std::mutex> lock(mutex_);
    return stats_;
}

std::size_t IngressQueue::size() const {
    const std::lock_guard<std::mutex> lock(mutex_);
    return queue_.size();
}

}  // namespace tevnnis::md
