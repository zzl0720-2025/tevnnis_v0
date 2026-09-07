"""Tests for OpenAIProvider — the first real LLM behind LLMProvider (§8, §10, §12).

ENTIRELY OFFLINE. Every test drives a fake client, so the whole call path runs
in CI with no key, no network and no spend. The live path is proven by
`scripts/openai_llm_smoke.py`, by hand, deliberately never here.

What these tests pin down:
  * the API's ACTUAL token counts reach TokenUsage, never an estimate;
  * a PAID call that then fails to parse is still billable, so the daily cap
    cannot be walked past by a run of malformed responses;
  * every malformed/refused/truncated response degrades to a Router HOLD;
  * nothing that came from the SDK reaches an exception message unredacted.
"""

from __future__ import annotations

import json
import types
from datetime import datetime, timezone

import pytest
from sqlalchemy import func, select

from tevnnis_core.config import BudgetsConfig
from tevnnis_core.db.models import ApiUsage, Decision
from tevnnis_core.instructions import Action, DecisionOutput
from tevnnis_core.llm.openai_provider import (
    DEFAULT_MAX_COMPLETION_TOKENS,
    DEFAULT_REASONING_EFFORT,
    OpenAIProvider,
    OpenAIProviderError,
)
from tevnnis_core.llm.router import GATE_HOLD_MALFORMED_OUTPUT, LLMRouter
from tevnnis_core.ports import LLMProvider

MODEL = "gpt-5-nano"
NOW = datetime(2026, 9, 3, 15, 0, tzinfo=timezone.utc)

# Deliberately odd, unround numbers: if either appears in TokenUsage it can
# only have come from the response, never from a length-based estimate.
PROMPT_TOKENS = 4242
COMPLETION_TOKENS = 917

BUDGETS = BudgetsConfig(
    llm_daily_token_budget=1_000_000,
    llm_daily_call_cap=50,
    broker_max_trades_per_day=10,
    broker_max_turnover_per_day=5000,
)

DECISION_JSON = json.dumps(
    {
        "instructions": [
            {
                "action": "BUY",
                "symbol": "NVDA.US",
                "order_type": "LIMIT",
                "quantity": 3,
                "limit_price": 229.59,
                "valid_seconds": 300,
                "confidence": 0.68,
                "thesis": "guidance raise",
                "cited_event_ids": ["smoke-1"],
            }
        ],
        "session_note": "one semis catalyst",
    }
)


# ---------------------------------------------------------------------------
# A fake OpenAI client — duck-typed, exactly like the broker mapping tests
# ---------------------------------------------------------------------------


def make_response(
    content=DECISION_JSON,
    *,
    finish_reason="stop",
    refusal=None,
    prompt_tokens=PROMPT_TOKENS,
    completion_tokens=COMPLETION_TOKENS,
    with_usage=True,
    cached_tokens=128,
    reasoning_tokens=576,
    choice_count=1,
):
    message = types.SimpleNamespace(content=content, refusal=refusal)
    choice = types.SimpleNamespace(message=message, finish_reason=finish_reason)
    usage = None
    if with_usage:
        usage = types.SimpleNamespace(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            prompt_tokens_details=types.SimpleNamespace(cached_tokens=cached_tokens),
            completion_tokens_details=types.SimpleNamespace(reasoning_tokens=reasoning_tokens),
        )
    return types.SimpleNamespace(choices=[choice] * choice_count, usage=usage)


class FakeClient:
    """Records the kwargs it was called with; returns a response or raises."""

    def __init__(self, result):
        self.result = result
        self.calls: list[dict] = []

    @property
    def chat(self):
        return types.SimpleNamespace(completions=self)

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def provider_for(result, **kwargs):
    return OpenAIProvider(model=MODEL, client=FakeClient(result), **kwargs)


async def expect_error(provider, match=None):
    with pytest.raises(OpenAIProviderError, match=match) as excinfo:
        await provider.complete_structured([], DecisionOutput)
    return excinfo.value


# ---------------------------------------------------------------------------
# Contract and request shape
# ---------------------------------------------------------------------------


def test_satisfies_the_llm_provider_protocol():
    assert isinstance(OpenAIProvider(model=MODEL), LLMProvider)


def test_construction_needs_no_key_and_makes_no_network_call():
    """Mirrors LongbridgeTradeBroker: __init__ only allocates.

    This is what lets the §15 guard tests build the real class offline to
    prove it refuses --yes.
    """
    provider = OpenAIProvider(model=MODEL)
    assert provider.client is None
    assert provider.last_usage() is None
    assert "gpt-5-nano" in provider.describe()


def test_an_empty_model_id_is_refused():
    with pytest.raises(ValueError, match="model id"):
        OpenAIProvider(model="")


@pytest.mark.parametrize("effort", ["minimal", "low", "medium", "high"])
def test_accepted_reasoning_efforts(effort):
    assert OpenAIProvider(model=MODEL, reasoning_effort=effort).reasoning_effort == effort


def test_an_unknown_reasoning_effort_fails_loudly_at_construction():
    """"none" arrives with gpt-5.1+; this model rejects it.

    Caught locally rather than as a 400, because a 400 on every round would be
    a permanent HOLD that looks exactly like a market-driven one.
    """
    with pytest.raises(ValueError, match="reasoning_effort"):
        OpenAIProvider(model=MODEL, reasoning_effort="none")


async def test_the_request_carries_strict_structured_output_and_the_ceilings():
    provider = provider_for(make_response())
    await provider.complete_structured([{"role": "user", "content": "state"}], DecisionOutput)

    (call,) = provider.client.calls
    assert call["model"] == MODEL
    assert call["response_format"]["json_schema"]["strict"] is True
    assert call["max_completion_tokens"] == DEFAULT_MAX_COMPLETION_TOKENS
    assert call["reasoning_effort"] == DEFAULT_REASONING_EFFORT
    # Reasoning models take max_completion_tokens on Chat Completions, never max_tokens.
    assert "max_tokens" not in call


async def test_reasoning_effort_none_omits_the_parameter_entirely():
    """So a non-reasoning model stays usable behind the same provider."""
    provider = provider_for(make_response(), reasoning_effort=None)
    await provider.complete_structured([], DecisionOutput)
    assert "reasoning_effort" not in provider.client.calls[0]


async def test_an_unsupported_response_model_is_refused():
    provider = provider_for(make_response())
    with pytest.raises(OpenAIProviderError, match="DecisionOutput"):
        await provider.complete_structured([], dict)


# ---------------------------------------------------------------------------
# The happy path, and the real token counts
# ---------------------------------------------------------------------------


async def test_parses_a_well_formed_response_into_a_decision_output():
    provider = provider_for(make_response())
    output = await provider.complete_structured([], DecisionOutput)

    assert isinstance(output, DecisionOutput)
    assert output.session_note == "one semis catalyst"
    assert output.instructions[0].action is Action.BUY
    assert output.instructions[0].symbol == "NVDA.US"


async def test_token_usage_is_the_apis_own_count_never_an_estimate():
    """The app-side cap is only real if the numbers feeding it are (§10)."""
    provider = provider_for(make_response())
    await provider.complete_structured([], DecisionOutput)

    usage = provider.last_usage()
    assert usage.tokens_in == PROMPT_TOKENS
    assert usage.tokens_out == COMPLETION_TOKENS
    assert usage.total == PROMPT_TOKENS + COMPLETION_TOKENS


async def test_usage_reflects_each_call_not_an_accumulation():
    provider = provider_for(make_response(prompt_tokens=11, completion_tokens=22))
    await provider.complete_structured([], DecisionOutput)
    provider.client.result = make_response(prompt_tokens=33, completion_tokens=44)
    await provider.complete_structured([], DecisionOutput)

    assert provider.last_usage().tokens_in == 33
    assert provider.last_usage().tokens_out == 44


async def test_call_stats_carry_the_reasoning_and_cache_breakdown():
    """What the smoke prints to size the ceilings from evidence."""
    provider = provider_for(make_response())
    await provider.complete_structured([], DecisionOutput)

    stats = provider.last_call_stats()
    assert stats.model == MODEL
    assert stats.reasoning_effort == DEFAULT_REASONING_EFFORT
    assert stats.cached_tokens == 128
    assert stats.reasoning_tokens == 576
    assert stats.finish_reason == "stop"
    assert stats.latency_seconds >= 0.0


async def test_a_missing_usage_breakdown_is_not_an_error():
    """Older/absent detail blocks are normal; only the totals are required."""
    response = make_response()
    response.usage.prompt_tokens_details = None
    response.usage.completion_tokens_details = None
    provider = provider_for(response)
    await provider.complete_structured([], DecisionOutput)

    assert provider.last_call_stats().reasoning_tokens is None
    assert provider.last_usage().tokens_in == PROMPT_TOKENS


# ---------------------------------------------------------------------------
# Every failure raises — the Router turns each into a HOLD
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "response, match",
    [
        pytest.param(make_response(finish_reason="length"), "truncated", id="truncated"),
        pytest.param(
            make_response(content=None, refusal="I can't help with that"),
            "refused",
            id="refusal",
        ),
        pytest.param(
            make_response(content=None, finish_reason="content_filter"),
            "content filter",
            id="content_filter",
        ),
        pytest.param(make_response(content='{"instructions": '), "not valid JSON", id="bad_json"),
        pytest.param(make_response(content=None), "no content", id="empty_content"),
        pytest.param(make_response(content=""), "no content", id="blank_content"),
        pytest.param(make_response(choice_count=2), "2 choices", id="two_choices"),
        pytest.param(make_response(choice_count=0), "0 choices", id="no_choices"),
    ],
)
async def test_unusable_responses_raise(response, match):
    await expect_error(provider_for(response), match)


async def test_schema_valid_json_that_breaks_a_section_5_rule_raises():
    """A HOLD carrying a quantity satisfies the wire schema but not §5."""
    payload = json.loads(DECISION_JSON)
    payload["instructions"][0]["action"] = "HOLD"
    provider = provider_for(make_response(content=json.dumps(payload)))
    await expect_error(provider, "did not fit DecisionOutput")


async def test_a_missing_usage_block_raises_rather_than_estimating():
    """A call whose cost cannot be measured must never be recorded as free."""
    provider = provider_for(make_response(with_usage=False))
    await expect_error(provider, "refusing to estimate")
    assert provider.last_usage() is None


async def test_an_sdk_error_surfaces_as_a_provider_error():
    provider = provider_for(RuntimeError("connection reset"))
    error = await expect_error(provider, "openai call failed")
    assert "connection reset" in str(error)


async def test_the_truncation_message_names_the_ceiling_and_the_effort():
    """The operator has to be able to act on this without reading the source."""
    provider = provider_for(make_response(finish_reason="length"))
    error = await expect_error(provider)
    assert str(DEFAULT_MAX_COMPLETION_TOKENS) in str(error)
    assert DEFAULT_REASONING_EFFORT in str(error)


# ---------------------------------------------------------------------------
# §12 — nothing from the SDK reaches a message unredacted
# ---------------------------------------------------------------------------


async def test_a_secret_shaped_token_in_an_sdk_error_is_redacted():
    leaked = "sk-proj-AbCdEfGh1234567890XyZwVuTsRq"
    provider = provider_for(RuntimeError(f"401 invalid api key {leaked}"))
    error = await expect_error(provider)

    assert leaked not in str(error)
    assert "<redacted>" in str(error)
    assert "401 invalid api key" in str(error)  # the useful part survives


async def test_the_original_exception_is_not_chained():
    """A chained __cause__ would print the unredacted text in any traceback."""
    provider = provider_for(RuntimeError("token AbCdEfGh1234567890XyZwVuTsRq"))
    error = await expect_error(provider)
    assert error.__cause__ is None


async def test_a_refusal_string_is_redacted_before_it_is_raised():
    provider = provider_for(
        make_response(content=None, refusal="denied: AbCdEfGh1234567890XyZwVuTsRq")
    )
    error = await expect_error(provider)
    assert "AbCdEfGh1234567890XyZwVuTsRq" not in str(error)


# ---------------------------------------------------------------------------
# Integration with the Router: failures become HOLDs, and paid failures bill
# ---------------------------------------------------------------------------


def usage_totals(session):
    tokens, calls = session.execute(
        select(
            func.coalesce(func.sum(ApiUsage.tokens_in + ApiUsage.tokens_out), 0),
            func.coalesce(func.sum(ApiUsage.call_count), 0),
        ).where(ApiUsage.kind == "llm")
    ).one()
    return int(tokens), int(calls)


async def test_router_records_the_real_provider_and_model_separately(db_session):
    provider = provider_for(make_response())
    router = LLMRouter(
        provider=provider,
        budgets=BUDGETS,
        tz="America/New_York",
        provider_name="openai",
        model_name=MODEL,
    )

    outcome = await router.decide(db_session, messages=[], now=NOW)

    assert outcome.status == "ok"
    decision = db_session.get(Decision, outcome.decision_id)
    assert decision.model_used == MODEL  # what reasoned
    assert decision.tokens_in == PROMPT_TOKENS
    assert decision.tokens_out == COMPLETION_TOKENS
    row = db_session.execute(select(ApiUsage)).scalars().one()
    assert row.provider == "openai"  # who was billed


async def test_a_provider_failure_becomes_a_hold_not_a_crash(db_session):
    """§10: the LLM proposes; a bad proposal must never crash the loop."""
    provider = provider_for(make_response(content="{ truncated"))
    router = LLMRouter(provider=provider, budgets=BUDGETS, tz="America/New_York")

    outcome = await router.decide(db_session, messages=[], now=NOW)

    assert outcome.status == "hold_malformed_output"
    assert outcome.output is None
    assert db_session.get(Decision, outcome.decision_id).gate_result == GATE_HOLD_MALFORMED_OUTPUT


async def test_a_paid_call_that_fails_to_parse_is_still_billed(db_session):
    """THE BUDGET HOLE THIS CLOSES.

    The request reached the API and burned tokens; only the parse failed. If
    that spend never reached api_usage, a run of malformed responses would
    bill real money without ever moving the daily cap.
    """
    provider = provider_for(make_response(finish_reason="length"))
    router = LLMRouter(
        provider=provider, budgets=BUDGETS, tz="America/New_York", provider_name="openai"
    )

    outcome = await router.decide(db_session, messages=[], now=NOW)

    assert outcome.status == "hold_malformed_output"
    assert (outcome.tokens_in, outcome.tokens_out) == (PROMPT_TOKENS, COMPLETION_TOKENS)
    assert usage_totals(db_session) == (PROMPT_TOKENS + COMPLETION_TOKENS, 1)

    decision = db_session.get(Decision, outcome.decision_id)
    assert decision.tokens_in == PROMPT_TOKENS
    assert decision.tokens_out == COMPLETION_TOKENS


async def test_repeated_parse_failures_still_trip_the_daily_cap(db_session):
    """The hole, closed end to end: malformed output cannot spend without limit."""
    budgets = BudgetsConfig(
        llm_daily_token_budget=1_000_000,
        llm_daily_call_cap=3,
        broker_max_trades_per_day=10,
        broker_max_turnover_per_day=5000,
    )
    provider = provider_for(make_response(content="{ truncated"))
    router = LLMRouter(
        provider=provider, budgets=budgets, tz="America/New_York", provider_name="openai"
    )

    statuses = [
        (await router.decide(db_session, messages=[], now=NOW)).status for _ in range(4)
    ]

    assert statuses == [
        "hold_malformed_output",
        "hold_malformed_output",
        "hold_malformed_output",
        "hold_budget",  # the cap engaged; the provider was not called a 4th time
    ]
    assert len(provider.client.calls) == 3


async def test_a_call_that_never_reached_the_api_is_not_billed(db_session):
    """The other half of the invariant: nothing spent, nothing recorded."""
    provider = provider_for(RuntimeError("connection reset"))
    router = LLMRouter(
        provider=provider, budgets=BUDGETS, tz="America/New_York", provider_name="openai"
    )

    outcome = await router.decide(db_session, messages=[], now=NOW)

    assert outcome.status == "hold_malformed_output"
    assert outcome.tokens_in is None
    assert usage_totals(db_session) == (0, 0)
