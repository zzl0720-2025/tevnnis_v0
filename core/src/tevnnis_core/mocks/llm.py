"""MockLLM — deterministic LLMProvider implementation (§13 mock-first).

Returns a canned DecisionOutput or runs a rule-based callable over the prompt
messages — zero cost, fully deterministic, better than a real model for test
assertions. Also records fake token usage so the app-side budget guard (§10)
can be exercised without spending anything.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from tevnnis_core.ports import TokenUsage

ResponseFn = Callable[[Sequence[Any], type], Any]


@dataclass
class MockLLM:
    """Wraps a fixed response or a rule-based response function.

    `response` is either:
      - a single object returned for every call (canned scenario), or
      - a callable `(messages, response_model) -> object` (rule-based scenario)
        so a scenario can vary its output based on the prompt content.
    """

    response: Any | ResponseFn
    tokens_in: int = 100
    tokens_out: int = 50

    def __post_init__(self) -> None:
        self.call_count: int = 0
        self.usage_log: list[TokenUsage] = []
        self._last_usage: TokenUsage | None = None

    async def complete_structured(self, messages: list[Any], response_model: type) -> Any:
        self.call_count += 1
        # Cleared first, then set before the response is inspected — the exact
        # ordering the real OpenAI provider uses, so `last_usage()` carries one
        # meaning across both: the usage of the call just attempted, or None if
        # that call spent nothing. The Router relies on it to bill a call that
        # reached the provider and then failed to parse (§10).
        self._last_usage = None
        usage = TokenUsage(tokens_in=self.tokens_in, tokens_out=self.tokens_out)
        self.usage_log.append(usage)
        self._last_usage = usage

        if callable(self.response) and not isinstance(self.response, response_model):
            output = self.response(messages, response_model)
        else:
            output = self.response

        if not isinstance(output, response_model):
            raise TypeError(
                f"MockLLM scenario returned {type(output)!r}, expected {response_model!r}"
            )
        return output

    @property
    def total_tokens(self) -> int:
        return sum(u.total for u in self.usage_log)

    def last_usage(self) -> TokenUsage | None:
        return self._last_usage
