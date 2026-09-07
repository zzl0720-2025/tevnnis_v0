// IngressQueue: FIFO, type-aware overflow, close semantics, and a real
// producer/consumer thread pair.
//
// The overflow rules are the safety-critical part: §4.4 makes halt/resume on a
// universe symbol CRITICAL, so a status tick must never be discarded to make
// room for a quote, and no drop may ever be silent.

#include <catch2/catch_test_macros.hpp>

#include <atomic>
#include <string>
#include <thread>
#include <vector>

#include "md/ingress_queue.hpp"

using tevnnis::md::IngressQueue;
using tevnnis::md::LongbridgeTick;
using tevnnis::md::LongbridgeTradeStatus;

namespace {

LongbridgeTick Quote(const std::string& symbol) {
    LongbridgeTick tick;
    tick.symbol = symbol;
    tick.last_done = 100.0;
    tick.timestamp_secs = 1788462657;
    tick.trade_status = LongbridgeTradeStatus::kNormal;
    return tick;
}

LongbridgeTick Halted(const std::string& symbol) {
    LongbridgeTick tick = Quote(symbol);
    tick.trade_status = LongbridgeTradeStatus::kHalted;
    return tick;
}

}  // namespace

TEST_CASE("Ticks come out in order", "[md][ingress]") {
    IngressQueue queue(8);
    queue.Push(Quote("A"));
    queue.Push(Quote("B"));
    queue.Push(Quote("C"));

    LongbridgeTick out;
    REQUIRE(queue.Pop(&out));
    REQUIRE(out.symbol == "A");
    REQUIRE(queue.Pop(&out));
    REQUIRE(out.symbol == "B");
    REQUIRE(queue.Pop(&out));
    REQUIRE(out.symbol == "C");
    REQUIRE(queue.stats().pushed == 3);
    REQUIRE(queue.stats().popped == 3);
}

TEST_CASE("Overflow drops the oldest quote and keeps the newest", "[md][ingress]") {
    IngressQueue queue(3);
    queue.Push(Quote("A"));
    queue.Push(Quote("B"));
    queue.Push(Quote("C"));
    queue.Push(Quote("D"));  // evicts A

    REQUIRE(queue.size() == 3);
    REQUIRE(queue.stats().dropped_quotes == 1);

    LongbridgeTick out;
    REQUIRE(queue.Pop(&out));
    REQUIRE(out.symbol == "B");
    REQUIRE(queue.Pop(&out));
    REQUIRE(out.symbol == "C");
    REQUIRE(queue.Pop(&out));
    REQUIRE(out.symbol == "D");
}

TEST_CASE("A status tick is never dropped to make room for a quote",
          "[md][ingress]") {
    IngressQueue queue(3);
    queue.Push(Halted("HALT"));  // oldest, but unevictable
    queue.Push(Quote("B"));
    queue.Push(Quote("C"));
    queue.Push(Quote("D"));  // must evict B, not HALT

    REQUIRE(queue.stats().dropped_quotes == 1);
    REQUIRE(queue.stats().dropped_protected == 0);

    LongbridgeTick out;
    REQUIRE(queue.Pop(&out));
    REQUIRE(out.symbol == "HALT");
    REQUIRE(out.trade_status == LongbridgeTradeStatus::kHalted);
    REQUIRE(queue.Pop(&out));
    REQUIRE(out.symbol == "C");
    REQUIRE(queue.Pop(&out));
    REQUIRE(out.symbol == "D");
}

TEST_CASE("A full queue of status ticks refuses an incoming quote rather than "
          "dropping one", "[md][ingress]") {
    IngressQueue queue(2);
    queue.Push(Halted("H1"));
    queue.Push(Halted("H2"));
    queue.Push(Quote("Q"));  // nothing evictable — refuse the incoming quote

    REQUIRE(queue.size() == 2);
    REQUIRE(queue.stats().dropped_quotes == 1);
    REQUIRE(queue.stats().dropped_protected == 0);

    LongbridgeTick out;
    REQUIRE(queue.Pop(&out));
    REQUIRE(out.symbol == "H1");
    REQUIRE(queue.Pop(&out));
    REQUIRE(out.symbol == "H2");
}

TEST_CASE("An incoming status tick is retained past capacity", "[md][ingress]") {
    IngressQueue queue(2);
    queue.Push(Halted("H1"));
    queue.Push(Halted("H2"));
    queue.Push(Halted("H3"));  // exceeds capacity on purpose

    REQUIRE(queue.size() == 3);
    REQUIRE(queue.stats().dropped_quotes == 0);
    REQUIRE(queue.stats().dropped_protected == 0);
}

TEST_CASE("The defensive ceiling bounds even unevictable ticks", "[md][ingress]") {
    // Should be unreachable in practice, but memory stays bounded and the loss
    // is reported rather than hidden.
    const std::size_t capacity = 2;
    IngressQueue queue(capacity);
    const std::size_t ceiling = capacity * IngressQueue::kProtectedOverflowFactor;
    for (std::size_t i = 0; i < ceiling + 3; ++i) {
        queue.Push(Halted("H" + std::to_string(i)));
    }
    REQUIRE(queue.size() <= ceiling);
    REQUIRE(queue.stats().dropped_protected > 0);
}

TEST_CASE("Every drop is reported", "[md][ingress]") {
    IngressQueue queue(1);
    std::vector<std::string> notices;
    queue.set_drop_logger([&notices](const std::string& notice) { notices.push_back(notice); });

    queue.Push(Quote("A"));
    queue.Push(Quote("B"));  // first drop always reports

    REQUIRE(notices.size() == 1);
    REQUIRE(notices[0].find("ingress overflow") != std::string::npos);
    REQUIRE(notices[0].find("A") != std::string::npos);
}

TEST_CASE("A lost status tick reports every time", "[md][ingress]") {
    const std::size_t capacity = 1;
    IngressQueue queue(capacity);
    std::vector<std::string> notices;
    queue.set_drop_logger([&notices](const std::string& notice) { notices.push_back(notice); });

    for (std::size_t i = 0; i < capacity * IngressQueue::kProtectedOverflowFactor + 2; ++i) {
        queue.Push(Halted("H" + std::to_string(i)));
    }
    REQUIRE(queue.stats().dropped_protected > 0);
    REQUIRE(notices.size() == static_cast<std::size_t>(queue.stats().dropped_protected));
    REQUIRE(notices.back().find("LOST") != std::string::npos);
}

TEST_CASE("Close drains what is queued, then ends the loop", "[md][ingress]") {
    IngressQueue queue(8);
    queue.Push(Quote("A"));
    queue.Push(Quote("B"));
    queue.Close();

    LongbridgeTick out;
    REQUIRE(queue.Pop(&out));
    REQUIRE(out.symbol == "A");
    REQUIRE(queue.Pop(&out));
    REQUIRE(out.symbol == "B");
    REQUIRE_FALSE(queue.Pop(&out));  // drained and closed
    REQUIRE(queue.closed());
}

TEST_CASE("A push after Close is ignored", "[md][ingress]") {
    // A late SDK callback during teardown is normal, not an error.
    IngressQueue queue(8);
    queue.Close();
    queue.Push(Quote("A"));

    LongbridgeTick out;
    REQUIRE_FALSE(queue.Pop(&out));
    REQUIRE(queue.stats().pushed == 0);
}

TEST_CASE("Close wakes a blocked consumer", "[md][ingress]") {
    IngressQueue queue(8);
    std::atomic<bool> returned{false};
    std::thread consumer([&queue, &returned]() {
        LongbridgeTick out;
        while (queue.Pop(&out)) {
        }
        returned.store(true);
    });

    queue.Close();
    consumer.join();  // hangs the test if Close does not wake Pop
    REQUIRE(returned.load());
}

TEST_CASE("A producer/consumer pair moves every tick exactly once",
          "[md][ingress]") {
    // Capacity is generous so nothing is dropped and the count is exact.
    constexpr int kTicks = 5000;
    IngressQueue queue(kTicks * 2);

    std::vector<std::string> received;
    std::thread consumer([&queue, &received]() {
        LongbridgeTick out;
        while (queue.Pop(&out)) {
            received.push_back(out.symbol);
        }
    });

    std::thread producer([&queue]() {
        for (int i = 0; i < kTicks; ++i) {
            queue.Push(Quote("S" + std::to_string(i)));
        }
    });

    producer.join();
    queue.Close();
    consumer.join();

    REQUIRE(queue.stats().dropped_quotes == 0);
    REQUIRE(received.size() == static_cast<std::size_t>(kTicks));
    // FIFO order is preserved with a single producer.
    for (int i = 0; i < kTicks; ++i) {
        REQUIRE(received[static_cast<std::size_t>(i)] == "S" + std::to_string(i));
    }
}

TEST_CASE("Concurrent producers lose nothing when capacity allows",
          "[md][ingress]") {
    // The SDK's tokio runtime is multi-threaded, so several callback threads
    // may push at once.
    constexpr int kProducers = 4;
    constexpr int kPerProducer = 1000;
    IngressQueue queue(kProducers * kPerProducer * 2);

    std::atomic<int> consumed{0};
    std::thread consumer([&queue, &consumed]() {
        LongbridgeTick out;
        while (queue.Pop(&out)) {
            consumed.fetch_add(1);
        }
    });

    std::vector<std::thread> producers;
    producers.reserve(kProducers);
    for (int p = 0; p < kProducers; ++p) {
        producers.emplace_back([&queue, p]() {
            for (int i = 0; i < kPerProducer; ++i) {
                queue.Push(Quote("P" + std::to_string(p) + "-" + std::to_string(i)));
            }
        });
    }
    for (std::thread& producer : producers) {
        producer.join();
    }
    queue.Close();
    consumer.join();

    REQUIRE(consumed.load() == kProducers * kPerProducer);
    REQUIRE(queue.stats().pushed == kProducers * kPerProducer);
    REQUIRE(queue.stats().dropped_quotes == 0);
}

TEST_CASE("High water mark is tracked", "[md][ingress]") {
    IngressQueue queue(8);
    queue.Push(Quote("A"));
    queue.Push(Quote("B"));
    LongbridgeTick out;
    REQUIRE(queue.Pop(&out));
    REQUIRE(queue.stats().high_water == 2);
}
