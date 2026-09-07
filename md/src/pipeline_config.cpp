#include "md/pipeline_config.hpp"

#include <fstream>

#include <nlohmann/json.hpp>

namespace tevnnis::md {

namespace {
using nlohmann::json;
}  // namespace

void PipelineConfig::Finalize() {
    if (universe.empty()) {
        throw ConfigError("universe is empty: the allowlist is the symbol filter (§6)");
    }
    symbol_to_sector.clear();
    for (const auto& entry : universe) {
        if (entry.sector.empty()) {
            throw ConfigError("universe contains a sector with an empty name");
        }
        if (entry.symbols.empty()) {
            throw ConfigError("universe sector '" + entry.sector + "' has no symbols");
        }
        for (const auto& symbol : entry.symbols) {
            if (symbol.empty()) {
                throw ConfigError("universe sector '" + entry.sector + "' has an empty symbol");
            }
            const auto [it, inserted] = symbol_to_sector.emplace(symbol, entry.sector);
            if (!inserted) {
                throw ConfigError("symbol '" + symbol + "' appears in two sectors: '" +
                                  it->second + "' and '" + entry.sector + "'");
            }
        }
    }

    if (entry_threshold_pct <= 0.0) {
        throw ConfigError("entry_threshold_pct must be > 0");
    }
    if (escalation_bands.empty()) {
        throw ConfigError("escalation_bands must not be empty");
    }
    for (std::size_t i = 1; i < escalation_bands.size(); ++i) {
        if (escalation_bands[i - 1] >= escalation_bands[i]) {
            throw ConfigError("escalation_bands must be strictly ascending");
        }
    }
    if (cooldown_minutes < 0) {
        throw ConfigError("cooldown_minutes must be >= 0");
    }
    if (sector_rate_cap_per_hour <= 0) {
        throw ConfigError("sector_rate_cap_per_hour must be > 0");
    }
    if (critical_move_pct <= 0.0) {
        throw ConfigError("critical_move_pct must be > 0");
    }
    if (news_title_max_chars <= 0) {
        throw ConfigError("news_title_max_chars must be > 0");
    }
    if (dedup_window_minutes < 0) {
        throw ConfigError("dedup_window_minutes must be >= 0");
    }
    if (simhash_hamming_threshold < 0 || simhash_hamming_threshold > 64) {
        throw ConfigError("simhash_hamming_threshold must be within [0, 64]");
    }
    if (retain_buffer_capacity == 0) {
        throw ConfigError("retain_buffer_capacity must be > 0");
    }

    // News poller. Validated even when --news-source is none: a
    // typo'd knob should be a startup error, not a surprise the first time the
    // live news path is switched on.
    if (news.poll_interval_seconds <= 0) {
        throw ConfigError("news.poll_interval_seconds must be > 0");
    }
    if (news.min_call_spacing_ms < 0) {
        throw ConfigError("news.min_call_spacing_ms must be >= 0");
    }
    if (news.call_timeout_seconds <= 0) {
        throw ConfigError("news.call_timeout_seconds must be > 0");
    }
    if (news.max_age_minutes < 0) {
        throw ConfigError("news.max_age_minutes must be >= 0 (0 disables the age filter)");
    }
    if (news.max_items_per_symbol == 0) {
        throw ConfigError("news.max_items_per_symbol must be > 0");
    }
    if (news.seen_capacity == 0) {
        throw ConfigError("news.seen_capacity must be > 0");
    }
}

const std::string* PipelineConfig::SectorOf(const std::string& symbol) const {
    const auto it = symbol_to_sector.find(symbol);
    return it == symbol_to_sector.end() ? nullptr : &it->second;
}

std::vector<std::string> PipelineConfig::Sectors() const {
    std::vector<std::string> out;
    out.reserve(universe.size());
    for (const auto& entry : universe) {
        out.push_back(entry.sector);
    }
    return out;
}

PipelineConfig LoadPipelineConfigFromJson(const std::string& path) {
    std::ifstream in(path);
    if (!in) {
        throw ConfigError("cannot open md config file: " + path);
    }

    json root;
    try {
        in >> root;
    } catch (const json::parse_error& e) {
        throw ConfigError("invalid JSON in md config file " + path + ": " + e.what());
    }

    PipelineConfig cfg;

    if (!root.contains("universe") || !root.at("universe").is_object()) {
        throw ConfigError("md config missing 'universe' object: " + path);
    }
    // nlohmann's default object type sorts keys, so sector order here is
    // alphabetical rather than authored order — deterministic either way, and
    // it only affects which sector wins for multi-sector news in the demo
    // binary. Tests build PipelineConfig in code, preserving authored order.
    for (const auto& [sector, symbols] : root.at("universe").items()) {
        if (!symbols.is_array()) {
            throw ConfigError("universe sector '" + sector + "' must be an array of symbols");
        }
        SectorUniverse entry;
        entry.sector = sector;
        for (const auto& s : symbols) {
            entry.symbols.push_back(s.get<std::string>());
        }
        cfg.universe.push_back(std::move(entry));
    }

    if (root.contains("triggers")) {
        const auto& t = root.at("triggers");
        cfg.entry_threshold_pct = t.value("entry_threshold_pct", cfg.entry_threshold_pct);
        if (t.contains("escalation_bands")) {
            cfg.escalation_bands = t.at("escalation_bands").get<std::vector<double>>();
        }
        cfg.cooldown_minutes = t.value("cooldown_minutes", cfg.cooldown_minutes);
        cfg.sector_rate_cap_per_hour =
            t.value("sector_rate_cap_per_hour", cfg.sector_rate_cap_per_hour);
        cfg.critical_move_pct = t.value("critical_move_pct", cfg.critical_move_pct);
    }

    if (root.contains("md")) {
        const auto& m = root.at("md");
        cfg.news_title_max_chars = m.value("news_title_max_chars", cfg.news_title_max_chars);
        cfg.dedup_window_minutes = m.value("dedup_window_minutes", cfg.dedup_window_minutes);
        cfg.simhash_hamming_threshold =
            m.value("simhash_hamming_threshold", cfg.simhash_hamming_threshold);
        cfg.retain_buffer_capacity =
            m.value("retain_buffer_capacity", cfg.retain_buffer_capacity);

        if (m.contains("news")) {
            const auto& n = m.at("news");
            cfg.news.poll_interval_seconds =
                n.value("poll_interval_seconds", cfg.news.poll_interval_seconds);
            cfg.news.min_call_spacing_ms =
                n.value("min_call_spacing_ms", cfg.news.min_call_spacing_ms);
            cfg.news.call_timeout_seconds =
                n.value("call_timeout_seconds", cfg.news.call_timeout_seconds);
            cfg.news.max_age_minutes = n.value("max_age_minutes", cfg.news.max_age_minutes);
            cfg.news.max_items_per_symbol =
                n.value("max_items_per_symbol", cfg.news.max_items_per_symbol);
            cfg.news.seen_capacity = n.value("seen_capacity", cfg.news.seen_capacity);
        }
    }

    cfg.Finalize();
    return cfg;
}

}  // namespace tevnnis::md
