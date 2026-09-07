"""Secret redaction, shared by every adapter that talks to a credentialed API (§12).

Broker and LLM adapters both use this boundary to scrub SDK-sourced text before
it reaches a log or exception message. `brokers.mapping` re-exports
`redact_secrets` for compatibility with existing imports.

There is deliberately no "list of secrets to mask" here: matching against the
real credential VALUES would mean reading them, which §12 forbids — and a
scrub that was never told the secret cannot leak it.
"""

from __future__ import annotations

import re

# Longest run of token-alphabet characters still plausibly ordinary prose.
# Mirrors md/include/md/redact.hpp so both planes scrub identically.
_SECRET_LIKE_RUN = 20
_TOKEN_RUN = re.compile(rf"[A-Za-z0-9_=-]{{{_SECRET_LIKE_RUN},}}")


def redact_secrets(text: str) -> str:
    """Mask credential-shaped runs in text that came from outside this process.

    Shape-based on purpose (see the module docstring). '.' and '/' are not in
    the alphabet, so URLs and dotted hostnames stay readable while JWT segments,
    `sk-...` API keys and other long opaque tokens are masked.
    """
    return _TOKEN_RUN.sub("<redacted>", text)
