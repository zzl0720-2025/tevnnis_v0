#pragma once
// RedactSecrets — scrub credential-shaped runs out of text before logging it.
//
// §12: never log a secret; redact on error. Text coming back from
// the Longbridge SDK is the one place in md where a credential could
// conceivably surface (an auth error that echoes what it was given), so every
// SDK-sourced string goes through here before it reaches stdout/stderr.
//
// This is a SHAPE-based scrub on purpose. Matching against the real credential
// values would mean reading them, which §12 forbids — and a scrub that was
// never told the secret cannot leak it. '.' and '/' are deliberately NOT part
// of the token alphabet so URLs and dotted hostnames stay readable, while a JWT
// still splits into segments that each trip the length threshold.
//
// Header-only so the standalone connectivity smoke can use it without linking
// tevnnis_md (its isolation is the point of that target).

#include <cctype>
#include <cstddef>
#include <string>

namespace tevnnis::md {

// Longest run of token-alphabet characters still plausibly ordinary prose.
inline constexpr std::size_t kSecretLikeRun = 20;

inline std::string RedactSecrets(const std::string& text) {
    std::string out;
    std::string run;
    const auto flush = [&out, &run]() {
        out += (run.size() >= kSecretLikeRun) ? "<redacted>" : run;
        run.clear();
    };
    for (const char c : text) {
        const bool token_char =
            std::isalnum(static_cast<unsigned char>(c)) != 0 || c == '_' || c == '-' || c == '=';
        if (token_char) {
            run.push_back(c);
        } else {
            flush();
            out.push_back(c);
        }
    }
    flush();
    return out;
}

}  // namespace tevnnis::md
