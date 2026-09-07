"""OpenAIProvider — production LLMProvider implementation (§8 step 5, §10, §12).

This is where real spend begins, so the module is written around three rules.

**1. Every failure raises.** Truncation, refusal, content filter, bad JSON, a
payload the canonical schema rejects, a missing usage block, any SDK or
transport error — all become `OpenAIProviderError`. `LLMRouter` already catches
that and logs `HOLD_MALFORMED_OUTPUT` (§10). The LLM proposes; a bad proposal
must degrade to a HOLD, never crash the loop and never reach Execution.

**2. Reported usage is the API's own count, never an estimate.** `tokens_in` is
`usage.prompt_tokens` and `tokens_out` is `usage.completion_tokens`, verbatim.
The app-side daily cap (§10 layer 2) is only real if the numbers feeding it are.
Two properties of those numbers worth knowing:

  - `completion_tokens` ALREADY INCLUDES `completion_tokens_details.reasoning_tokens`.
    gpt-5-nano is a reasoning model and reasoning tokens bill at the output
    rate, so counting them is correct, not double-counting.
  - `prompt_tokens` ALREADY INCLUDES `prompt_tokens_details.cached_tokens`,
    which bill at 0.1x. v0's ledger counts tokens rather than dollars, so a
    cached token costs the budget its full weight. That OVER-counts spend —
    the cap errs toward stopping early, which is the right direction.

`last_usage()` is set the MOMENT the response is in hand, BEFORE any parsing,
and cleared at the start of every call. That gives it one exact meaning — *the
usage of the call just attempted, or None if that call spent nothing* — which
is what lets the Router bill a call that succeeded at the API and then failed
to parse. Without it, a run of malformed responses would spend real money
without ever moving the daily cap.

**3. No secret ever leaves this module.** The SDK reads `OPENAI_API_KEY` from
the environment itself; nothing here reads, copies, stores or logs a credential
value. Every SDK-sourced string is passed through `redact_secrets` before it
reaches an exception message, and the original exception is deliberately NOT
chained (`from None`) so an unredacted `__cause__` can never surface in a
traceback.

CONSTRUCTION MAKES NO NETWORK CALL and needs no API key — `__init__` only
allocates. That mirrors `LongbridgeTradeBroker` and is what lets the §15
guard tests construct the real class offline to prove it refuses `--yes`.

NO RETRIES. The client is built with `max_retries=0`: a retry is silent extra
spend, while a HOLD costs nothing and the next cadence tick re-decides. A
transient 500 becomes one HOLD, deliberately.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

from tevnnis_core.instructions import DecisionOutput
from tevnnis_core.llm.openai_schema import (
    DecisionOutputMappingError,
    decision_output_from_json,
    response_format,
)
from tevnnis_core.ports import TokenUsage
from tevnnis_core.redact import redact_secrets

PROVIDER_NAME = "openai"

# ---------------------------------------------------------------------------
# Ceilings derived from repeated smoke-test measurements
# ---------------------------------------------------------------------------
#
# What the smoke found on gpt-5-nano at the default reasoning effort: 3520 to
# >4096 reasoning tokens to produce ~121 tokens of JSON. Reasoning was ~97% of
# the completion, it varied wildly run to run, and it overran a 4096 ceiling
# about half the time. A truncated response is finish_reason="length", which
# this provider turns into a HOLD — so at that setting the agent was holding
# half the time for a reason that had nothing to do with the market.
#
# Cost was never the issue (4096 output tokens is ~$0.0016; a 50-call day is
# ~$0.08). RELIABILITY was. Both knobs below attack that:
#
# REASONING EFFORT — the root cause. gpt-5-nano is one of the original GPT-5
# reasoning models, which are the only ones that accept "minimal"; the ladder
# here is minimal | low | medium | high (NOT "none" — that arrives with
# gpt-5.1+ and this model rejects it), and it defaults to "medium". This task
# does not need medium: §10 already computes every number in code and feeds
# them as facts, so the model is choosing among pre-computed options, not
# deriving anything. Sent as the STRING `reasoning_effort` — the Chat
# Completions spelling. The Responses API's `reasoning={"effort": ...}` object
# is a different endpoint's shape and is rejected here.
#
# MAX COMPLETION TOKENS — headroom for whatever reasoning still happens.
# Deliberately generous: reasoning tokens are drawn from this same ceiling
# before a single visible JSON character is emitted, and a rare reasoning
# spike costing a fraction of a cent is far better than a spurious HOLD.
#
# A rejected reasoning_effort value would be a 400 on EVERY round, i.e. a
# permanent HOLD. That is exactly what the smoke exists to catch before the
# Router is ever wired to this provider.
DEFAULT_MAX_COMPLETION_TOKENS = 8192
DEFAULT_TIMEOUT_SECONDS = 90.0
DEFAULT_REASONING_EFFORT = "low"

# Accepted by gpt-5-nano (2025-08-07). Checked locally so a typo fails fast and
# loudly at construction, instead of as an identical-looking HOLD every round.
REASONING_EFFORTS = ("minimal", "low", "medium", "high")


class OpenAIProviderError(RuntimeError):
    """An OpenAI call did not produce a usable decision. Always already redacted."""


@dataclass(frozen=True)
class OpenAICallStats:
    """Observability for one call — never part of the decision, never persisted in v0.

    Exists so the provider smoke test can size `max_completion_tokens` and the timeout
    from what nano actually does on this prompt, instead of from a guess.
    """

    model: str
    reasoning_effort: str | None
    latency_seconds: float
    prompt_tokens: int
    completion_tokens: int
    cached_tokens: int | None
    reasoning_tokens: int | None
    finish_reason: str | None


@dataclass
class OpenAIProvider:
    """LLMProvider over OpenAI Chat Completions with strict structured output.

    `client` is injectable so the whole call path is testable offline against a
    fake; left None, an `AsyncOpenAI` is built lazily on first use.
    """

    model: str
    max_completion_tokens: int = DEFAULT_MAX_COMPLETION_TOKENS
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    # None omits the parameter entirely, so a non-reasoning model stays usable.
    reasoning_effort: str | None = DEFAULT_REASONING_EFFORT
    client: Any | None = field(default=None)

    def __post_init__(self) -> None:
        if not self.model:
            raise ValueError("OpenAIProvider requires a model id (config.llm_routing.strong.model)")
        if self.reasoning_effort is not None and self.reasoning_effort not in REASONING_EFFORTS:
            raise ValueError(
                f"unknown reasoning_effort {self.reasoning_effort!r}; "
                f"expected one of {list(REASONING_EFFORTS)} or None to omit it"
            )
        self._last_usage: TokenUsage | None = None
        self._last_stats: OpenAICallStats | None = None

    # -- description ------------------------------------------------------

    def describe(self) -> str:
        """A short, honest label for the startup banner (§9.1). Never guessed."""
        return f"openai {self.model} (REAL SPEND)"

    # -- LLMProvider ------------------------------------------------------

    async def complete_structured(self, messages: list[Any], response_model: type) -> Any:
        if response_model is not DecisionOutput:
            raise OpenAIProviderError(
                "unsupported response_model "
                f"{getattr(response_model, '__name__', response_model)!r}: "
                "v0 has one wire schema, DecisionOutput"
            )

        # Cleared first, so last_usage() can never report a previous call's
        # spend as if it belonged to this one.
        self._last_usage = None
        self._last_stats = None

        client = self._ensure_client()
        extra: dict[str, Any] = {}
        if self.reasoning_effort is not None:
            extra["reasoning_effort"] = self.reasoning_effort
        started = time.perf_counter()
        try:
            response = await client.chat.completions.create(
                model=self.model,
                messages=messages,
                response_format=response_format(),
                max_completion_tokens=self.max_completion_tokens,
                **extra,
            )
        except Exception as exc:
            # `from None`: the original exception's text is unredacted, and a
            # chained __cause__ would print it in any traceback (§12).
            raise OpenAIProviderError(
                f"openai call failed: {type(exc).__name__}: {redact_secrets(str(exc))}"
            ) from None
        latency = time.perf_counter() - started

        # Usage FIRST, before any inspection that can raise: from here on the
        # call has been paid for, and the Router must be able to bill it even
        # if everything below fails.
        usage = self._usage_from(response)
        self._last_usage = usage  # billable from here on, whatever happens below
        choice = self._single_choice(response)
        self._last_stats = OpenAICallStats(
            model=self.model,
            reasoning_effort=self.reasoning_effort,
            latency_seconds=latency,
            prompt_tokens=usage.tokens_in,
            completion_tokens=usage.tokens_out,
            cached_tokens=_detail(response.usage, "prompt_tokens_details", "cached_tokens"),
            reasoning_tokens=_detail(
                response.usage, "completion_tokens_details", "reasoning_tokens"
            ),
            finish_reason=getattr(choice, "finish_reason", None),
        )

        return self._parse_choice(choice)

    def last_usage(self) -> TokenUsage | None:
        """Usage of the call just attempted, or None if that call spent nothing.

        Non-None from the instant the response arrives, INCLUDING on every
        parse-failure path, so a call that burned tokens and then failed is
        still billable to the daily cap (§10).
        """
        return self._last_usage

    def last_call_stats(self) -> OpenAICallStats | None:
        return self._last_stats

    # -- internals --------------------------------------------------------

    def _ensure_client(self) -> Any:
        if self.client is not None:
            return self.client
        try:
            # Lazy, like the longport import: nothing in the mock plane loads
            # the SDK, so `--llm mock` never pays for it.
            from openai import AsyncOpenAI
        except ImportError:
            raise OpenAIProviderError(
                "the `openai` package is not installed (run `uv sync` in core/)"
            ) from None
        # The SDK reads OPENAI_API_KEY from the environment itself (§12) — this
        # module never touches the value.
        self.client = AsyncOpenAI(max_retries=0, timeout=self.timeout_seconds)
        return self.client

    def _usage_from(self, response: Any) -> TokenUsage:
        usage = getattr(response, "usage", None)
        prompt_tokens = getattr(usage, "prompt_tokens", None)
        completion_tokens = getattr(usage, "completion_tokens", None)
        if not isinstance(prompt_tokens, int) or not isinstance(completion_tokens, int):
            # Refuse to invent a number. A call whose cost cannot be measured
            # must never be recorded as free, and estimating it would make the
            # app-side cap a guess (§10).
            raise OpenAIProviderError(
                "openai response carried no usable token usage; refusing to estimate it"
            )
        return TokenUsage(tokens_in=prompt_tokens, tokens_out=completion_tokens)

    def _single_choice(self, response: Any) -> Any:
        choices = getattr(response, "choices", None) or []
        if len(choices) != 1:
            raise OpenAIProviderError(
                f"openai response carried {len(choices)} choices, expected exactly 1"
            )
        return choices[0]

    def _parse_choice(self, choice: Any) -> DecisionOutput:
        finish_reason = getattr(choice, "finish_reason", None)
        if finish_reason == "length":
            raise OpenAIProviderError(
                "openai response was truncated (finish_reason=length): the model ran out of "
                f"max_completion_tokens ({self.max_completion_tokens}) before closing the JSON "
                f"(reasoning_effort={self.reasoning_effort!r} — reasoning is drawn from the "
                "same ceiling)"
            )
        if finish_reason == "content_filter":
            raise OpenAIProviderError("openai response was stopped by the content filter")

        message = getattr(choice, "message", None)
        refusal = getattr(message, "refusal", None)
        if refusal:
            raise OpenAIProviderError(f"openai refused the request: {redact_secrets(str(refusal))}")

        content = getattr(message, "content", None)
        if not content:
            raise OpenAIProviderError(
                f"openai response carried no content (finish_reason={finish_reason!r})"
            )

        try:
            payload = json.loads(content)
        except json.JSONDecodeError as exc:
            raise OpenAIProviderError(
                f"openai response was not valid JSON: {exc.msg} at position {exc.pos}"
            ) from None

        try:
            return decision_output_from_json(payload)
        except DecisionOutputMappingError as exc:
            raise OpenAIProviderError(
                f"openai response did not fit DecisionOutput: {exc}"
            ) from None


def _detail(usage: Any, group: str, name: str) -> int | None:
    """Read an optional usage sub-count; absence is normal, not an error."""
    value = getattr(getattr(usage, group, None), name, None)
    return value if isinstance(value, int) else None
