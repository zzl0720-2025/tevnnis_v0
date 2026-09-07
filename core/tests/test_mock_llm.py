"""Tests for MockLLM: deterministic canned/rule-based output, fake token usage."""

from __future__ import annotations

import pytest

from tevnnis_core.instructions import Action, DecisionOutput
from tevnnis_core.mocks.llm import MockLLM
from tevnnis_core.mocks.llm_scenarios import buy_scenario, hold_scenario, sell_scenario
from tevnnis_core.ports import LLMProvider


def test_satisfies_llm_provider_protocol():
    llm = MockLLM(response=hold_scenario())
    assert isinstance(llm, LLMProvider)


@pytest.mark.asyncio
async def test_canned_scenario_is_deterministic_across_calls():
    llm = MockLLM(response=hold_scenario("hold for now"))

    first = await llm.complete_structured(messages=[], response_model=DecisionOutput)
    second = await llm.complete_structured(messages=[], response_model=DecisionOutput)

    assert first == second
    assert first.instructions == []
    assert first.session_note == "hold for now"
    assert llm.call_count == 2


@pytest.mark.asyncio
async def test_buy_and_sell_canned_scenarios():
    buy_llm = MockLLM(response=buy_scenario(symbol="NVDA.US", quantity=5, limit_price=900.0))
    output = await buy_llm.complete_structured(messages=[], response_model=DecisionOutput)
    assert len(output.instructions) == 1
    assert output.instructions[0].action == Action.BUY
    assert output.instructions[0].symbol == "NVDA.US"
    assert output.instructions[0].quantity == 5

    sell_llm = MockLLM(response=sell_scenario(symbol="NVDA.US", quantity=5))
    output = await sell_llm.complete_structured(messages=[], response_model=DecisionOutput)
    assert output.instructions[0].action == Action.SELL


@pytest.mark.asyncio
async def test_rule_based_scenario_varies_with_messages():
    def rule(messages, response_model):
        if any("NVDA" in str(m) for m in messages):
            return buy_scenario(symbol="NVDA.US")
        return hold_scenario()

    llm = MockLLM(response=rule)

    hold_out = await llm.complete_structured(
        messages=["nothing interesting"], response_model=DecisionOutput
    )
    assert hold_out.instructions == []

    buy_out = await llm.complete_structured(
        messages=["NVDA spiked 5%"], response_model=DecisionOutput
    )
    assert buy_out.instructions[0].symbol == "NVDA.US"


@pytest.mark.asyncio
async def test_reports_fake_token_usage_for_budget_guard():
    llm = MockLLM(response=hold_scenario(), tokens_in=4000, tokens_out=500)

    await llm.complete_structured(messages=[], response_model=DecisionOutput)
    await llm.complete_structured(messages=[], response_model=DecisionOutput)

    assert llm.call_count == 2
    assert len(llm.usage_log) == 2
    assert llm.last_usage().tokens_in == 4000
    assert llm.last_usage().tokens_out == 500
    assert llm.total_tokens == 2 * (4000 + 500)


@pytest.mark.asyncio
async def test_scenario_returning_wrong_type_raises():
    llm = MockLLM(response="not a DecisionOutput")
    with pytest.raises(TypeError):
        await llm.complete_structured(messages=[], response_model=DecisionOutput)
