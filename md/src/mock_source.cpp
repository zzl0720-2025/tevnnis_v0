#include "md/mock_source.hpp"

#include <algorithm>
#include <fstream>
#include <sstream>

#include <nlohmann/json.hpp>

namespace tevnnis::md {

namespace {

using nlohmann::json;

SourceEvent ParseQuoteEvent(const json& j) {
    SourceEvent ev;
    ev.type = SourceEventType::kQuote;
    ev.quote.event_ts = j.at("event_ts").get<int64_t>();
    ev.quote.symbol = j.at("symbol").get<std::string>();
    ev.quote.last_price = j.at("last_price").get<double>();
    ev.quote.prev_close = j.at("prev_close").get<double>();
    ev.quote.volume = j.value("volume", int64_t{0});
    return ev;
}

SourceEvent ParseNewsEvent(const json& j) {
    SourceEvent ev;
    ev.type = SourceEventType::kNews;
    ev.news.event_ts = j.at("event_ts").get<int64_t>();
    ev.news.news_id = j.at("news_id").get<std::string>();
    ev.news.title = j.at("title").get<std::string>();
    ev.news.source = j.value("source", std::string{});
    ev.news.url = j.value("url", std::string{});
    if (j.contains("related_symbols")) {
        for (const auto& s : j.at("related_symbols")) {
            ev.news.related_symbols.push_back(s.get<std::string>());
        }
    }
    return ev;
}

SourceStatus ParseStatus(const std::string& name, const std::string& path) {
    if (name == "halted") return SourceStatus::kHalted;
    if (name == "resumed") return SourceStatus::kResumed;
    if (name == "pre_market") return SourceStatus::kPreMarket;
    if (name == "post_market") return SourceStatus::kPostMarket;
    if (name == "closed") return SourceStatus::kClosed;
    throw MockSourceError("unknown status '" + name + "' in " + path);
}

SourceEvent ParseStatusEvent(const json& j, const std::string& path) {
    SourceEvent ev;
    ev.type = SourceEventType::kStatus;
    ev.status.event_ts = j.at("event_ts").get<int64_t>();
    ev.status.symbol = j.value("symbol", std::string{});
    ev.status.status = ParseStatus(j.at("status").get<std::string>(), path);
    return ev;
}

}  // namespace

MockMarketDataSource::MockMarketDataSource(std::string scenario_path)
    : scenario_path_(std::move(scenario_path)) {
    std::ifstream in(scenario_path_);
    if (!in) {
        throw MockSourceError("cannot open scenario file: " + scenario_path_);
    }

    json root;
    try {
        in >> root;
    } catch (const json::parse_error& e) {
        throw MockSourceError("invalid JSON in scenario file " + scenario_path_ + ": " + e.what());
    }

    if (!root.contains("events") || !root.at("events").is_array()) {
        throw MockSourceError("scenario file missing 'events' array: " + scenario_path_);
    }

    for (const auto& j : root.at("events")) {
        const std::string type = j.at("type").get<std::string>();
        if (type == "quote") {
            events_.push_back(ParseQuoteEvent(j));
        } else if (type == "news") {
            events_.push_back(ParseNewsEvent(j));
        } else if (type == "status") {
            events_.push_back(ParseStatusEvent(j, scenario_path_));
        } else {
            throw MockSourceError("unknown event type '" + type + "' in " + scenario_path_);
        }
    }

    std::stable_sort(events_.begin(), events_.end(), [](const SourceEvent& a, const SourceEvent& b) {
        return SourceEventTs(a) < SourceEventTs(b);
    });
}

void MockMarketDataSource::run(const std::function<void(const SourceEvent&)>& on_event) {
    for (const auto& ev : events_) {
        on_event(ev);
    }
}

}  // namespace tevnnis::md
