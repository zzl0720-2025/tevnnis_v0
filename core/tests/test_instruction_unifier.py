"""Test stable IDs, canonical fields and separation of persistence metadata.

Universe membership is checked by Risk, not by the instruction unifier.
"""

from __future__ import annotations

from tevnnis_core.instructions import Action, DecisionOutput, TradingInstruction
from tevnnis_core.llm.instruction_unifier import make_client_order_id, unify
from tevnnis_core.mocks.llm_scenarios import buy_scenario, hold_scenario, sell_scenario

# client_order_id determinism


def test_client_order_id_is_deterministic_for_the_same_identity():
    a = make_client_order_id("decision-1", 0)
    b = make_client_order_id("decision-1", 0)
    assert a == b


def test_client_order_id_varies_with_index():
    a = make_client_order_id("decision-1", 0)
    b = make_client_order_id("decision-1", 1)
    assert a != b


def test_client_order_id_varies_with_decision_id():
    a = make_client_order_id("decision-1", 0)
    b = make_client_order_id("decision-2", 0)
    assert a != b


def test_retrying_the_same_persisted_decision_reproduces_the_same_ids():
    output = buy_scenario(symbol="NVDA.US")
    first = unify("decision-42", output)
    second = unify("decision-42", output)

    first_ids = [i.trading_instruction.client_order_id for i in first.instructions]
    second_ids = [i.trading_instruction.client_order_id for i in second.instructions]
    assert first_ids == second_ids


# Canonical instruction shape


def test_unify_builds_canonical_trading_instruction():
    output = buy_scenario(symbol="AAPL.US", quantity=10, limit_price=150.0)
    result = unify("decision-1", output)

    assert len(result.instructions) == 1
    unified = result.instructions[0]
    assert isinstance(unified.trading_instruction, TradingInstruction)
    assert unified.trading_instruction.action == Action.BUY
    assert unified.trading_instruction.symbol == "AAPL.US"
    assert unified.trading_instruction.quantity == 10
    assert unified.trading_instruction.client_order_id.startswith("co_")


def test_thesis_and_cited_event_ids_are_split_off_for_persistence():
    output = buy_scenario(symbol="AAPL.US", cited_event_ids=["evt-001", "evt-002"])
    result = unify("decision-1", output)

    unified = result.instructions[0]
    # Execution-facing instruction carries no thesis/cited_event_ids fields at all.
    assert not hasattr(unified.trading_instruction, "thesis")
    assert not hasattr(unified.trading_instruction, "cited_event_ids")
    # They live on the persistable side instead.
    assert unified.persistable.thesis == "scripted buy scenario"
    assert unified.persistable.cited_event_ids == ["evt-001", "evt-002"]


def test_session_note_is_carried_through():
    output = DecisionOutput(instructions=[], session_note="quiet day, holding everything")
    result = unify("decision-1", output)
    assert result.session_note == "quiet day, holding everything"
    assert result.instructions == []


def test_hold_only_output_yields_no_trading_instructions():
    result = unify("decision-1", hold_scenario())
    assert result.instructions == []


def test_multiple_instructions_get_distinct_client_order_ids():
    output = DecisionOutput(
        instructions=[
            buy_scenario(symbol="AAPL.US").instructions[0],
            sell_scenario(symbol="NVDA.US").instructions[0],
        ],
        session_note="rotate",
    )
    result = unify("decision-1", output)
    ids = [i.trading_instruction.client_order_id for i in result.instructions]
    assert len(set(ids)) == 2


# Universe membership is explicitly NOT this unifier's job (UNIVERSE_ALLOWLIST)


def test_out_of_universe_buy_is_not_rejected_here_left_for_risk():
    # UNIVERSE_ALLOWLIST is enforced on BUY by the Risk Engine, not here.
    output = buy_scenario(symbol="NOTINUNIVERSE.US")
    result = unify("decision-1", output)
    assert len(result.instructions) == 1
    assert result.instructions[0].trading_instruction.symbol == "NOTINUNIVERSE.US"


def test_out_of_universe_sell_is_never_rejected_a_holding_must_stay_closeable():
    # UNIVERSE_ALLOWLIST is skipped for SELL precisely so a symbol dropped
    # from the universe can still be exited. The unifier must not re-add that
    # check and accidentally trap the position.
    output = sell_scenario(symbol="DROPPEDFROMUNIVERSE.US")
    result = unify("decision-1", output)
    assert len(result.instructions) == 1
    assert result.instructions[0].trading_instruction.symbol == "DROPPEDFROMUNIVERSE.US"
