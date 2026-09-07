"""Pydantic v2 models for LLM-facing and canonical trading instructions (§5).

Validation rules (§5, "Validation ≠ Risk"):
  - HOLD carries no price or quantity.
  - BUY/SELL require quantity > 0 and a finite positive limit_price.
  - confidence is 0..1.
  - Extra fields are forbidden (LLM output must be schema-exact).
"""

from __future__ import annotations

import math
from enum import IntEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Action(IntEnum):
    HOLD = 0
    BUY = 1
    SELL = 2


class OrderType(IntEnum):
    LIMIT = 0


class TIF(IntEnum):
    DAY = 0
    TIMED = 1


class ProposedInstruction(BaseModel):
    """What the LLM emits — no client_order_id."""

    model_config = ConfigDict(extra="forbid")

    action: Action
    symbol: str
    order_type: OrderType = OrderType.LIMIT
    quantity: int = 0
    limit_price: float = 0.0
    valid_seconds: int = 0
    confidence: Annotated[float, Field(ge=0.0, le=1.0)]
    thesis: str = ""
    cited_event_ids: list[str] = []

    @model_validator(mode="after")
    def _validate_action_fields(self) -> ProposedInstruction:
        if self.action == Action.HOLD:
            if self.quantity != 0:
                raise ValueError("HOLD must not carry quantity")
            if self.limit_price != 0.0:
                raise ValueError("HOLD must not carry limit_price")
        elif self.action in (Action.BUY, Action.SELL):
            if self.quantity <= 0:
                raise ValueError("BUY/SELL requires quantity > 0")
            if not math.isfinite(self.limit_price) or self.limit_price <= 0:
                raise ValueError("BUY/SELL requires a finite positive limit_price")
        return self


class TradingInstruction(BaseModel):
    """Canonical, execution-ready instruction; core attaches client_order_id.

    thesis and cited_event_ids are stripped to the DB before this type is
    constructed — execution receives only execution fields.
    """

    model_config = ConfigDict(extra="forbid")

    action: Action
    symbol: str
    order_type: OrderType = OrderType.LIMIT
    quantity: int
    limit_price: float
    valid_seconds: int
    confidence: Annotated[float, Field(ge=0.0, le=1.0)]
    client_order_id: str


class DecisionOutput(BaseModel):
    """Full LLM response for one decision round."""

    model_config = ConfigDict(extra="forbid")

    instructions: Annotated[list[ProposedInstruction], Field(max_length=5)] = []
    session_note: str = ""
