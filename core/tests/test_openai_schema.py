"""Tests for the OpenAI wire schema and the mapping back to DecisionOutput (§5, §10).

Entirely offline: no `openai` import, no network, no key. The wire contract is
a dict and a pure function, which is the whole reason it was written by hand
rather than derived from the Pydantic model.
"""

from __future__ import annotations

import json

import pytest

from tevnnis_core.instructions import Action, DecisionOutput, OrderType, ProposedInstruction
from tevnnis_core.llm.openai_schema import (
    DECISION_OUTPUT_JSON_SCHEMA,
    DecisionOutputMappingError,
    decision_output_from_json,
    response_format,
)

# Keywords OpenAI's strict mode either rejects or does not attest. The schema
# must contain NONE of them: an unsupported keyword is an HTTP 400 at call
# time, which is a hard failure rather than a HOLD.
UNSUPPORTED_KEYWORDS = frozenset(
    {
        "default",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "minItems",
        "maxItems",
        "uniqueItems",
        "minLength",
        "maxLength",
        "pattern",
        "format",
        "anyOf",
        "oneOf",
        "allOf",
        "not",
        "if",
        "then",
        "else",
    }
)


def _walk(node, path="$"):
    """Yield (path, dict) for every object node in the schema."""
    if isinstance(node, dict):
        yield path, node
        for key, value in node.items():
            yield from _walk(value, f"{path}.{key}")
    elif isinstance(node, list):
        for i, value in enumerate(node):
            yield from _walk(value, f"{path}[{i}]")


def valid_instruction(**overrides):
    """A schema-shaped BUY payload — every field present, as strict mode requires."""
    payload = {
        "action": "BUY",
        "symbol": "NVDA.US",
        "order_type": "LIMIT",
        "quantity": 3,
        "limit_price": 229.59,
        "valid_seconds": 300,
        "confidence": 0.7,
        "thesis": "guidance raise",
        "cited_event_ids": ["e1"],
    }
    payload.update(overrides)
    return payload


def valid_payload(**overrides):
    payload = {"instructions": [valid_instruction()], "session_note": "one catalyst"}
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# The schema is legal under strict mode
# ---------------------------------------------------------------------------


def test_no_unsupported_keyword_appears_anywhere_in_the_schema():
    offenders = {
        path: sorted(UNSUPPORTED_KEYWORDS & set(node))
        for path, node in _walk(DECISION_OUTPUT_JSON_SCHEMA)
        if UNSUPPORTED_KEYWORDS & set(node)
    }
    assert offenders == {}


def test_every_object_forbids_extra_properties_and_requires_all_of_them():
    """Strict mode's two structural demands, asserted at every level."""
    objects = [
        (path, node)
        for path, node in _walk(DECISION_OUTPUT_JSON_SCHEMA)
        if node.get("type") == "object"
    ]
    assert len(objects) == 2  # the root and proposed_instruction
    for path, node in objects:
        assert node["additionalProperties"] is False, path
        assert sorted(node["required"]) == sorted(node["properties"]), path


def test_actions_go_over_the_wire_as_names_not_intenum_values():
    """A model reasons over "BUY"; `Action.BUY` would serialise as `1`."""
    instruction = DECISION_OUTPUT_JSON_SCHEMA["$defs"]["proposed_instruction"]
    assert instruction["properties"]["action"]["enum"] == ["HOLD", "BUY", "SELL"]
    assert instruction["properties"]["order_type"]["enum"] == ["LIMIT"]


def test_schema_fields_match_the_canonical_models():
    """Drift guard: adding a field to §5 without updating the wire schema fails here."""
    instruction = DECISION_OUTPUT_JSON_SCHEMA["$defs"]["proposed_instruction"]
    assert sorted(instruction["properties"]) == sorted(ProposedInstruction.model_fields)
    assert sorted(DECISION_OUTPUT_JSON_SCHEMA["properties"]) == sorted(DecisionOutput.model_fields)


def test_response_format_is_strict_and_json_serialisable():
    fmt = response_format()
    assert fmt["type"] == "json_schema"
    assert fmt["json_schema"]["strict"] is True
    assert fmt["json_schema"]["name"] == "decision_output"
    json.dumps(fmt)  # must survive the wire


# ---------------------------------------------------------------------------
# Mapping back: the happy path
# ---------------------------------------------------------------------------


def test_maps_a_well_formed_payload_into_canonical_types():
    output = decision_output_from_json(valid_payload())

    assert isinstance(output, DecisionOutput)
    assert output.session_note == "one catalyst"
    (instruction,) = output.instructions
    assert instruction.action is Action.BUY
    assert instruction.order_type is OrderType.LIMIT
    assert instruction.symbol == "NVDA.US"
    assert instruction.quantity == 3
    assert instruction.limit_price == pytest.approx(229.59)
    assert instruction.cited_event_ids == ["e1"]


def test_maps_a_hold_and_an_empty_decision():
    hold = valid_instruction(action="HOLD", quantity=0, limit_price=0.0, valid_seconds=0)
    output = decision_output_from_json(valid_payload(instructions=[hold]))
    assert output.instructions[0].action is Action.HOLD

    empty = decision_output_from_json({"instructions": [], "session_note": "quiet"})
    assert empty.instructions == []


def test_maps_the_maximum_of_five_instructions():
    hold = valid_instruction(action="HOLD", quantity=0, limit_price=0.0, valid_seconds=0)
    output = decision_output_from_json(valid_payload(instructions=[hold] * 5))
    assert len(output.instructions) == 5


# ---------------------------------------------------------------------------
# Mapping back: every failure is one exception type
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload, expected",
    [
        pytest.param("a string", "root", id="not_an_object"),
        pytest.param({"instructions": {}, "session_note": ""}, "array", id="instructions_not_list"),
        pytest.param(
            {"instructions": [], "session_note": 7}, "session_note", id="session_note_not_str"
        ),
        pytest.param(
            {"instructions": [], "session_note": "", "extra": 1}, "unexpected", id="extra_field"
        ),
        pytest.param(
            {"instructions": ["nope"], "session_note": ""}, "object", id="item_not_object"
        ),
    ],
)
def test_malformed_shapes_raise(payload, expected):
    with pytest.raises(DecisionOutputMappingError, match=expected):
        decision_output_from_json(payload)


@pytest.mark.parametrize(
    "overrides, expected",
    [
        pytest.param({"action": "YOLO"}, "action", id="unknown_action"),
        pytest.param({"action": 1}, "action", id="action_as_int_not_name"),
        pytest.param({"order_type": "MARKET"}, "order_type", id="unsupported_order_type"),
        pytest.param({"confidence": 1.5}, "confidence", id="confidence_above_one"),
        pytest.param({"confidence": -0.1}, "confidence", id="confidence_below_zero"),
        pytest.param({"quantity": 0}, "quantity", id="buy_without_quantity"),
        pytest.param({"limit_price": 0.0}, "limit_price", id="buy_without_price"),
        pytest.param({"limit_price": -5.0}, "limit_price", id="buy_with_negative_price"),
    ],
)
def test_values_the_canonical_model_rejects_raise(overrides, expected):
    """The §5 validators the wire schema deliberately cannot express."""
    with pytest.raises(DecisionOutputMappingError, match=expected):
        decision_output_from_json(valid_payload(instructions=[valid_instruction(**overrides)]))


def test_hold_carrying_execution_fields_raises():
    """§5: HOLD must not carry a quantity or a price."""
    with pytest.raises(DecisionOutputMappingError):
        decision_output_from_json(
            valid_payload(instructions=[valid_instruction(action="HOLD", limit_price=0.0)])
        )
    with pytest.raises(DecisionOutputMappingError):
        decision_output_from_json(
            valid_payload(instructions=[valid_instruction(action="HOLD", quantity=0)])
        )


def test_more_than_five_instructions_raises():
    """The cap lives in DecisionOutput, not in the schema — this is where it bites."""
    hold = valid_instruction(action="HOLD", quantity=0, limit_price=0.0, valid_seconds=0)
    with pytest.raises(DecisionOutputMappingError, match="instructions"):
        decision_output_from_json(valid_payload(instructions=[hold] * 6))


def test_error_messages_name_the_offending_instruction_index():
    """The Router persists this text into decisions.session_note — it must be readable."""
    good = valid_instruction()
    bad = valid_instruction(action="NOPE")
    with pytest.raises(DecisionOutputMappingError, match=r"instructions\[1\]"):
        decision_output_from_json(valid_payload(instructions=[good, bad]))
