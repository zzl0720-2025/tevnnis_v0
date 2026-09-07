// Dedup layer: SimHash properties, exact + near-duplicate collapse, window aging.

#include <catch2/catch_test_macros.hpp>

#include "md/dedup.hpp"

using tevnnis::md::DedupWindow;
using tevnnis::md::HammingDistance;
using tevnnis::md::SimHash64;

namespace {
constexpr std::int64_t kHourMs = 60 * 60 * 1000;
constexpr int kThreshold = 8;  // PipelineConfig::simhash_hamming_threshold default
}  // namespace

TEST_CASE("SimHash ignores case, punctuation and word order", "[md][dedup]") {
    const std::uint64_t base = SimHash64("Nvidia raises full year revenue guidance");
    REQUIRE(SimHash64("nvidia raises full-year revenue guidance.") == base);
    REQUIRE(SimHash64("guidance revenue year full raises Nvidia") == base);
    REQUIRE(SimHash64("Nvidia   raises  full year revenue guidance!!") == base);
}

TEST_CASE("SimHash separates a rewording from a different story", "[md][dedup]") {
    const std::uint64_t original =
        SimHash64("NVDA jumps after strong earnings report beats estimates");
    const std::uint64_t reworded =
        SimHash64("NVDA jumps after strong earnings report tops estimates");
    const std::uint64_t unrelated =
        SimHash64("Exxon announces dividend increase for shareholders");

    REQUIRE(HammingDistance(original, reworded) <= kThreshold);
    REQUIRE(HammingDistance(original, unrelated) > kThreshold);
}

TEST_CASE("DedupWindow collapses the same news_id", "[md][dedup]") {
    DedupWindow window(kHourMs, kThreshold);

    REQUIRE(window.CheckAndRecord("n-001", "Nvidia raises full year revenue guidance", 0) ==
            DedupWindow::Verdict::kNew);
    REQUIRE(window.CheckAndRecord("n-001", "Nvidia raises full year revenue guidance", 1000) ==
            DedupWindow::Verdict::kDuplicateExact);
    // Same id even under a rewritten headline is still the same story.
    REQUIRE(window.CheckAndRecord("n-001", "Something else entirely happened today", 2000) ==
            DedupWindow::Verdict::kDuplicateExact);
    REQUIRE(window.size() == 1);
}

TEST_CASE("DedupWindow collapses a re-issue under a new id", "[md][dedup]") {
    DedupWindow window(kHourMs, kThreshold);

    REQUIRE(window.CheckAndRecord("n-001", "Nvidia raises full year revenue guidance", 0) ==
            DedupWindow::Verdict::kNew);
    REQUIRE(window.CheckAndRecord("n-002", "Nvidia raises full-year revenue guidance.", 1000) ==
            DedupWindow::Verdict::kDuplicateNear);
    // A genuinely different story in the same sector still gets through.
    REQUIRE(window.CheckAndRecord("n-003", "Exxon announces dividend increase for shareholders",
                                  2000) == DedupWindow::Verdict::kNew);
    REQUIRE(window.size() == 2);
}

TEST_CASE("DedupWindow forgets entries older than the window", "[md][dedup]") {
    DedupWindow window(kHourMs, kThreshold);

    REQUIRE(window.CheckAndRecord("n-001", "Nvidia raises full year revenue guidance", 0) ==
            DedupWindow::Verdict::kNew);
    REQUIRE(window.CheckAndRecord("n-002", "Nvidia raises full year revenue guidance",
                                  kHourMs / 2) == DedupWindow::Verdict::kDuplicateNear);

    // Two hours later both the id and the headline are new again.
    REQUIRE(window.CheckAndRecord("n-001", "Nvidia raises full year revenue guidance",
                                  2 * kHourMs) == DedupWindow::Verdict::kNew);
    REQUIRE(window.size() == 1);
}

TEST_CASE("DedupWindow near-layer can be disabled", "[md][dedup]") {
    DedupWindow window(kHourMs, /*hamming_threshold=*/-1);

    REQUIRE(window.CheckAndRecord("n-001", "Nvidia raises full year revenue guidance", 0) ==
            DedupWindow::Verdict::kNew);
    REQUIRE(window.CheckAndRecord("n-002", "Nvidia raises full year revenue guidance", 1000) ==
            DedupWindow::Verdict::kNew);
}
