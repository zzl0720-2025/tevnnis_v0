// RedactSecrets: SDK-sourced text is scrubbed of credential-shaped runs before
// it is ever logged (§12), without the scrub ever being told a real secret.

#include <catch2/catch_test_macros.hpp>

#include <string>

#include "md/redact.hpp"

using tevnnis::md::RedactSecrets;

TEST_CASE("Ordinary error prose survives untouched", "[md][redact]") {
    REQUIRE(RedactSecrets("rate limit exceeded: 10 calls/sec") ==
            "rate limit exceeded: 10 calls/sec");
    REQUIRE(RedactSecrets("") == "");
}

TEST_CASE("URLs stay readable", "[md][redact]") {
    // '.' and '/' are excluded from the token alphabet precisely so a
    // diagnosable URL is not swallowed whole.
    const std::string url = "request to https://openapi.longportapp.com/v1/quote failed";
    REQUIRE(RedactSecrets(url) == url);
}

TEST_CASE("A long opaque token is masked", "[md][redact]") {
    REQUIRE(RedactSecrets("bad key AKIAIOSFODNN7EXAMPLEDEADBEEF01") == "bad key <redacted>");
}

TEST_CASE("JWT segments are masked individually", "[md][redact]") {
    const std::string jwt =
        "auth failed for eyJhbGciOiJIUzI1NiJ9.dGhpc2lzYXNlY3JldHRva2VuMDAw.c2ln";
    const std::string out = RedactSecrets(jwt);
    REQUIRE(out.find("eyJhbGciOiJIUzI1NiJ9") == std::string::npos);
    REQUIRE(out.find("dGhpc2lzYXNlY3JldHRva2VuMDAw") == std::string::npos);
    REQUIRE(out.find("<redacted>.<redacted>.") != std::string::npos);
}

TEST_CASE("The threshold is exact", "[md][redact]") {
    const std::string just_under(tevnnis::md::kSecretLikeRun - 1, 'a');
    const std::string exactly(tevnnis::md::kSecretLikeRun, 'a');
    REQUIRE(RedactSecrets(just_under) == just_under);
    REQUIRE(RedactSecrets(exactly) == "<redacted>");
}

TEST_CASE("A token at either end of the string is still masked", "[md][redact]") {
    const std::string token(30, 'Z');
    REQUIRE(RedactSecrets(token + " trailing") == "<redacted> trailing");
    REQUIRE(RedactSecrets("leading " + token) == "leading <redacted>");
}
