"""Abstract interfaces (Protocols) for all external dependencies (§13 mock-first).

Every external system sits behind one of these interfaces; tests use mock
implementations and real adapters swap in without changing any call site.

Protocol typing uses Any for proto-generated types (PullRequest, PullResponse,
TradingInstruction) so this module has no import-time dependency on the gRPC
stubs or the broker SDK.  Implementations are expected to narrow these types.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

# ---------------------------------------------------------------------------
# Shared snapshot types (broker-agnostic; broker adapters map into these)
# ---------------------------------------------------------------------------


@dataclass
class PositionSnapshot:
    symbol: str
    quantity: int
    cost_basis: float


@dataclass
class OrderSnapshot:
    client_order_id: str
    status: str
    symbol: str | None = None
    broker_order_id: str | None = None


@dataclass
class AccountSnapshot:
    buying_power: float
    cash: float
    net_liquidation: float


@dataclass
class OrderUpdate:
    """One order-lifecycle update, as reported by the broker (§8 step 8).

    A real adapter fills these from Longbridge's order-update push; the mock
    broker queues them synchronously. `broker_fill_id` is present only on an
    update that carries a fill, and it is the broker's own id — §7 makes it the
    `fills` dedup key, so core must never invent one.
    """

    client_order_id: str
    broker_order_id: str
    status: str  # "open" | "partially_filled" | "filled" | "cancelled" | "rejected"
    filled_quantity: int = 0
    fill_price: float | None = None
    fee: float = 0.0
    broker_fill_id: str | None = None


# ---------------------------------------------------------------------------
# Symbol codec — identity in v0 (Longbridge canonical == broker symbol)
# ---------------------------------------------------------------------------


@runtime_checkable
class SymbolCodec(Protocol):
    def to_broker(self, symbol: str) -> str:
        """Convert canonical symbol to broker-specific format."""
        ...

    def from_broker(self, broker_symbol: str) -> str:
        """Convert broker-specific format to canonical symbol."""
        ...


# ---------------------------------------------------------------------------
# Trade port — broker order lifecycle
# ---------------------------------------------------------------------------


@runtime_checkable
class TradePort(Protocol):
    async def submit_order(self, instruction: Any) -> str:
        """Submit a TradingInstruction; return the broker order id."""
        ...

    async def cancel_order(self, client_order_id: str) -> None:
        """Cancel an open order by its idempotency key."""
        ...

    async def query_positions(self) -> list[PositionSnapshot]:
        """Return current holdings from the broker (source of truth)."""
        ...

    async def query_open_orders(self) -> list[OrderSnapshot]:
        """Return all open (unfilled) orders from the broker."""
        ...

    async def query_account(self) -> AccountSnapshot:
        """Return buying power, cash, and net liquidation value."""
        ...

    async def poll_order_updates(self) -> list[OrderUpdate]:
        """Drain and return order-lifecycle updates observed since the last call.

        Pull-shaped rather than callback-shaped so Execution owns the loop and
        the DB transaction boundary; a real push-based adapter buffers the
        broker's callbacks and hands them over here.
        """
        ...


# ---------------------------------------------------------------------------
# LLM provider — structured-output completion
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TokenUsage:
    tokens_in: int
    tokens_out: int

    @property
    def total(self) -> int:
        return self.tokens_in + self.tokens_out


@runtime_checkable
class LLMProvider(Protocol):
    async def complete_structured(
        self,
        messages: list[Any],
        response_model: type,
    ) -> Any:
        """Call the LLM and parse the response into response_model."""
        ...

    def last_usage(self) -> TokenUsage | None:
        """Token usage (in/out) for the most recent complete_structured call.

        The LLM Router reads this after each call to log real usage to
        api_usage (§10 budget guard) — providers report usage out-of-band
        from the parsed response so response_model stays a pure decision
        schema.
        """
        ...


# ---------------------------------------------------------------------------
# Market data client — gRPC pull interface (§4.3)
# ---------------------------------------------------------------------------


@runtime_checkable
class MarketDataClient(Protocol):
    async def pull_decision_batch(self, request: Any) -> Any:
        """Call PullDecisionBatch and return a PullResponse.

        request  — md_service_pb2.PullRequest
        returns  — md_service_pb2.PullResponse
        """
        ...
