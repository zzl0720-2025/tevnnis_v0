#pragma once
// Await — run one async Longbridge SDK call and block until it answers.
//
// The SDK's API is callback-based on a multi-threaded tokio runtime, but md's
// startup sequence (§9.1: connect, validate, reconcile — any failure aborts
// before trading) is a straight line. This turns one async call into a
// synchronous, timeout-bounded step.
//
// SDK-dependent, header-only: included by the live source and by the standalone
// connectivity smoke, which links the SDK but deliberately not tevnnis_md.

#include <chrono>
#include <future>
#include <memory>
#include <optional>
#include <stdexcept>
#include <string>
#include <utility>

#include <longport.hpp>

#include "md/redact.hpp"

namespace tevnnis::md {

// Thrown when a live plane cannot be brought up (§9.1: any failure aborts
// before we start serving). Shared by every SDK-facing source — the quote
// quote source and news source, so neither has to include
// the other just to throw. The message is always already redacted (§12).
class LongbridgeConnectError : public std::runtime_error {
   public:
    explicit LongbridgeConnectError(const std::string& what) : std::runtime_error(what) {}
};

// Renders an SDK error for a log line. §12: SDK-sourced text is always redacted
// before it is printed — we never know what an auth error might echo back.
inline std::string DescribeSdkStatus(const longport::Status& status) {
    const std::optional<const char*> message = status.message();
    std::string text =
        (message.has_value() && *message != nullptr) ? std::string(*message) : "unknown error";
    if (const std::optional<std::int64_t> code = status.code(); code.has_value()) {
        text += " (code " + std::to_string(*code) + ")";
    }
    return RedactSecrets(text);
}

template <typename T>
struct AwaitResult {
    bool ok = false;
    bool timed_out = false;
    T value{};
    std::string error;

    [[nodiscard]] std::string failure() const {
        return timed_out ? std::string("timed out") : error;
    }
};

// Invokes `invoke(callback)` and waits up to `timeout` for the callback.
//
// The promise is held by shared_ptr and captured BY VALUE: the SDK dispatches
// completions on its own worker threads, so a call that times out here may
// still complete afterwards — writing into a live shared object rather than a
// dangling stack slot.
template <typename T, typename Invoke>
AwaitResult<T> Await(Invoke&& invoke, std::chrono::seconds timeout) {
    const auto promise = std::make_shared<std::promise<AwaitResult<T>>>();
    std::future<AwaitResult<T>> future = promise->get_future();

    std::forward<Invoke>(invoke)([promise](auto result) {
        AwaitResult<T> out;
        if (result) {
            out.ok = true;
            out.value = *result;
        } else {
            out.error = DescribeSdkStatus(result.status());
        }
        try {
            promise->set_value(std::move(out));
        } catch (const std::future_error&) {
            // Already satisfied; a duplicate completion is not an error.
        }
    });

    if (future.wait_for(timeout) != std::future_status::ready) {
        AwaitResult<T> out;
        out.timed_out = true;
        return out;
    }
    return future.get();
}

// Same, for calls whose result type is void (subscribe/unsubscribe).
template <typename Invoke>
AwaitResult<bool> AwaitVoid(Invoke&& invoke, std::chrono::seconds timeout) {
    const auto promise = std::make_shared<std::promise<AwaitResult<bool>>>();
    std::future<AwaitResult<bool>> future = promise->get_future();

    std::forward<Invoke>(invoke)([promise](auto result) {
        AwaitResult<bool> out;
        if (result) {
            out.ok = true;
            out.value = true;
        } else {
            out.error = DescribeSdkStatus(result.status());
        }
        try {
            promise->set_value(std::move(out));
        } catch (const std::future_error&) {
        }
    });

    if (future.wait_for(timeout) != std::future_status::ready) {
        AwaitResult<bool> out;
        out.timed_out = true;
        return out;
    }
    return future.get();
}

}  // namespace tevnnis::md
