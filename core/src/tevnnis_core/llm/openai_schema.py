"""The OpenAI wire schema for a decision, and the mapping back to DecisionOutput (§5, §10).

Pure and offline: no `openai` import, no network, no I/O. Everything here is a
dict-in / model-out function, so the whole wire contract is unit-testable on a
machine that has never seen the SDK — the same discipline as
`brokers/mapping.py`.

WHY THIS SCHEMA IS HAND-WRITTEN RATHER THAN `DecisionOutput.model_json_schema()`
-------------------------------------------------------------------------------
Four reasons, each load-bearing:

1. `Action` / `OrderType` are `IntEnum`, so a derived schema would constrain
   the model to `enum: [0, 1, 2]`. A model reasons far better over the words
   "HOLD" / "BUY" / "SELL" than over integers, and the mapping back is trivial.
2. OpenAI strict mode requires EVERY property to appear in `required` and does
   not support `default`. `DecisionOutput` carries defaults on eight fields.
3. `Field(ge=0, le=1)` emits `minimum`/`maximum` and `Field(max_length=5)`
   emits `maxItems`. OpenAI documents that some JSON Schema keywords are
   unavailable but does not publish the definitive list, and an unsupported
   keyword is an HTTP 400 at call time — a hard failure, not a HOLD.
4. Decisively: the real validation lives in a Pydantic `@model_validator`
   (HOLD carries no quantity/price; BUY/SELL need `quantity > 0` and a finite
   positive `limit_price`). No JSON Schema can express it, so it has to run on
   the way back regardless of what the wire schema claims.

So the schema below is deliberately CONSERVATIVE — it uses only the subset
OpenAI's docs actually attest (`type`, `properties`, `required`,
`additionalProperties`, `items`, `enum`, `description`, `$defs`/`$ref`) — and
every VALUE constraint (the 0..1 confidence range, the cap of five
instructions, HOLD's empty fields) is stated in prose for the model and
ENFORCED by `DecisionOutput` on the way back. That is the safe direction to
fail: a value breach becomes an exception the Router turns into a HOLD, never
a 400 that the operator has to debug mid-session.

`instructions.py` is not modified by any of this. The canonical schema stays
the single source of truth; this module is only its wire clothing.
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from tevnnis_core.instructions import Action, DecisionOutput, OrderType, ProposedInstruction

SCHEMA_NAME = "decision_output"

# Wire spelling -> canonical enum. The wire uses names, not the IntEnum values.
_ACTIONS = {a.name: a for a in Action}
_ORDER_TYPES = {o.name: o for o in OrderType}

# Every field of ProposedInstruction, in wire order. Strict mode requires all
# of them to be present and listed in `required`; the drift-guard test asserts
# this tuple still matches ProposedInstruction.model_fields.
_INSTRUCTION_FIELDS = (
    "action",
    "symbol",
    "order_type",
    "quantity",
    "limit_price",
    "valid_seconds",
    "confidence",
    "thesis",
    "cited_event_ids",
)

_DECISION_FIELDS = ("instructions", "session_note")


DECISION_OUTPUT_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": list(_DECISION_FIELDS),
    "properties": {
        "instructions": {
            "type": "array",
            "description": (
                "At most 5. Empty, or one entry per proposal. Use a single HOLD "
                "entry to record an explicit decision not to trade."
            ),
            "items": {"$ref": "#/$defs/proposed_instruction"},
        },
        "session_note": {
            "type": "string",
            "description": "One short overall note for this round.",
        },
    },
    "$defs": {
        "proposed_instruction": {
            "type": "object",
            "additionalProperties": False,
            "required": list(_INSTRUCTION_FIELDS),
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [name for name in _ACTIONS],
                },
                "symbol": {
                    "type": "string",
                    "description": "Exactly as given in the state below; never invented.",
                },
                "order_type": {
                    "type": "string",
                    "enum": [name for name in _ORDER_TYPES],
                },
                "quantity": {
                    "type": "integer",
                    "description": "Shares. Greater than 0 for BUY/SELL; exactly 0 for HOLD.",
                },
                "limit_price": {
                    "type": "number",
                    "description": "Greater than 0 for BUY/SELL; exactly 0 for HOLD.",
                },
                "valid_seconds": {
                    "type": "integer",
                    "description": "Order lifetime; the unfilled remainder is cancelled after it.",
                },
                "confidence": {
                    "type": "number",
                    "description": "Between 0 and 1 inclusive.",
                },
                "thesis": {
                    "type": "string",
                    "description": "Why. Recorded, never executed.",
                },
                "cited_event_ids": {
                    "type": "array",
                    "description": "Which of the given event ids drove this. May be empty.",
                    "items": {"type": "string"},
                },
            },
        }
    },
}


def response_format() -> dict[str, Any]:
    """The `response_format` argument for a Chat Completions call."""
    return {
        "type": "json_schema",
        "json_schema": {
            "name": SCHEMA_NAME,
            "strict": True,
            "schema": DECISION_OUTPUT_JSON_SCHEMA,
        },
    }


class DecisionOutputMappingError(ValueError):
    """A decoded JSON payload could not be mapped to a DecisionOutput.

    Raised for every reason: wrong shape, unknown action name, or a value the
    canonical model rejects (HOLD carrying a quantity, BUY with quantity 0, a
    confidence outside 0..1, more than five instructions, an extra field).
    The provider wraps this into an OpenAIProviderError, and the Router turns
    that into HOLD_MALFORMED_OUTPUT — the LLM proposes; a bad proposal must
    never crash the loop.
    """


def _enum_from_name(raw: Any, table: dict[str, Any], field: str) -> Any:
    if not isinstance(raw, str) or raw not in table:
        raise DecisionOutputMappingError(
            f"{field}: expected one of {sorted(table)}, got {raw!r}"
        )
    return table[raw]


def _instruction_from_json(raw: Any, index: int) -> ProposedInstruction:
    if not isinstance(raw, dict):
        raise DecisionOutputMappingError(
            f"instructions[{index}]: expected an object, got {type(raw).__name__}"
        )
    try:
        return ProposedInstruction(
            action=_enum_from_name(raw.get("action"), _ACTIONS, f"instructions[{index}].action"),
            symbol=raw.get("symbol"),
            order_type=_enum_from_name(
                raw.get("order_type"), _ORDER_TYPES, f"instructions[{index}].order_type"
            ),
            quantity=raw.get("quantity"),
            limit_price=raw.get("limit_price"),
            valid_seconds=raw.get("valid_seconds"),
            confidence=raw.get("confidence"),
            thesis=raw.get("thesis", ""),
            cited_event_ids=raw.get("cited_event_ids", []),
        )
    except ValidationError as exc:
        # Compact, deterministic text: the Router persists this into
        # decisions.session_note, so it must stay short and readable.
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err['loc']) or '<model>'}: {err['msg']}"
            for err in exc.errors()
        )
        raise DecisionOutputMappingError(f"instructions[{index}] rejected: {problems}") from None


def decision_output_from_json(payload: Any) -> DecisionOutput:
    """Map one decoded JSON payload into a validated canonical DecisionOutput.

    Total: every failure path raises DecisionOutputMappingError. Never returns
    a partially-populated model, and never silently drops an instruction — a
    decision that cannot be mapped in full is not a decision.
    """
    if not isinstance(payload, dict):
        raise DecisionOutputMappingError(
            f"expected a JSON object at the root, got {type(payload).__name__}"
        )

    unknown = sorted(set(payload) - set(_DECISION_FIELDS))
    if unknown:
        raise DecisionOutputMappingError(f"unexpected top-level field(s): {unknown}")

    raw_instructions = payload.get("instructions", [])
    if not isinstance(raw_instructions, list):
        raise DecisionOutputMappingError(
            f"instructions: expected an array, got {type(raw_instructions).__name__}"
        )

    instructions = [_instruction_from_json(raw, i) for i, raw in enumerate(raw_instructions)]

    session_note = payload.get("session_note", "")
    if not isinstance(session_note, str):
        raise DecisionOutputMappingError(
            f"session_note: expected a string, got {type(session_note).__name__}"
        )

    try:
        # Enforces the <= 5 cap that the wire schema deliberately does not state.
        return DecisionOutput(instructions=instructions, session_note=session_note)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err['loc']) or '<model>'}: {err['msg']}"
            for err in exc.errors()
        )
        raise DecisionOutputMappingError(f"decision rejected: {problems}") from None
