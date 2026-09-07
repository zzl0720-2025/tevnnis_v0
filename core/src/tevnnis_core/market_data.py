"""The md -> core boundary: the §4.3 pull client and its wire-to-core mapping.

Two responsibilities, deliberately split:

  `GrpcMarketDataClient` — the only place that speaks gRPC to `tevnnis-md`.
  `map_pull_response`    — turns one `PullResponse` into the plain core types
                           the rest of the loop consumes: rows for the `events`
                           table (§7), `SelectedEvent`/`SectorFact` for the
                           Protocol Unifier (§10), last prices and symbol
                           statuses for the RiskContext (§11).

Nothing downstream of this module imports a pb2 type, which is what lets the
prompt builder and the risk-context builder stay wire-agnostic.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from google.protobuf.json_format import MessageToDict

from tevnnis_core.llm.protocol_unifier import SectorFact, SectorSymbolFact, SelectedEvent
from tevnnis_core.pb import events_pb2, md_service_pb2, md_service_pb2_grpc

# Priority order, lowest first — the cheap gate compares against this (§8 step 3).
PRIORITY_ORDER = ("LOW", "MEDIUM", "HIGH", "CRITICAL")


def priority_rank(name: str) -> int:
    try:
        return PRIORITY_ORDER.index(name)
    except ValueError:
        return -1


@dataclass(frozen=True)
class EventRecord:
    """One row destined for the §7 `events` table."""

    event_id: str
    type: str
    priority: str
    event_ts: int
    ingest_ts: int
    symbol: str | None = None
    sector: str | None = None
    news_id: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PulledBatch:
    """Everything one PullDecisionBatch round yields, in core-native types."""

    records: list[EventRecord]
    selected: list[SelectedEvent]
    sectors: list[SectorFact]
    last_prices: dict[str, float]
    status_updates: dict[str, str]  # symbol -> "HALTED" | "NORMAL"
    next_cursor: str
    dropped_count: int

    @property
    def max_priority(self) -> str | None:
        """The highest priority present, or None for an empty batch."""
        if not self.records:
            return None
        return max((r.priority for r in self.records), key=priority_rank)


def build_pull_request(
    *,
    max_events: int,
    min_priority: str,
    sectors: list[str],
    since_cursor: str,
) -> Any:
    return md_service_pb2.PullRequest(
        max_events=max_events,
        min_priority=events_pb2.Priority.Value(min_priority),
        sectors=sectors,
        since_cursor=since_cursor,
    )


def _payload_dict(event: Any) -> dict[str, Any]:
    which = event.WhichOneof("payload")
    if which is None:
        return {}
    return MessageToDict(getattr(event, which), preserving_proto_field_name=True)


def _summary(event: Any) -> tuple[str, float | None]:
    """A one-line, token-cheap summary plus change_pct — never a url or body (§4.2)."""
    which = event.WhichOneof("payload")
    if which == "news":
        return event.news.title, None
    if which == "quote":
        return event.quote.trigger, event.quote.change_pct
    if which == "status":
        return events_pb2.StatusPayload.Status.Name(event.status.status), None
    return "", None


def map_pull_response(response: Any) -> PulledBatch:
    """Map one §4.3 PullResponse into core-native types."""
    records: list[EventRecord] = []
    selected: list[SelectedEvent] = []
    status_updates: dict[str, str] = {}

    for event in response.events:
        type_name = events_pb2.EventType.Name(event.type)
        priority_name = events_pb2.Priority.Name(event.priority)
        summary, change_pct = _summary(event)

        records.append(
            EventRecord(
                event_id=event.event_id,
                type=type_name,
                priority=priority_name,
                event_ts=event.event_ts,
                ingest_ts=event.ingest_ts,
                symbol=event.symbol or None,
                sector=event.sector or None,
                news_id=event.news.news_id or None if event.HasField("news") else None,
                payload=_payload_dict(event),
            )
        )
        selected.append(
            SelectedEvent(
                event_id=event.event_id,
                type=type_name,
                symbol=event.symbol,
                sector=event.sector,
                priority=priority_name,
                summary=summary,
                change_pct=change_pct,
            )
        )
        if event.HasField("status") and event.symbol:
            name = events_pb2.StatusPayload.Status.Name(event.status.status)
            if name in ("HALTED", "RESUMED"):
                status_updates[event.symbol] = "HALTED" if name == "HALTED" else "NORMAL"

    sectors: list[SectorFact] = []
    last_prices: dict[str, float] = {}
    for snapshot in response.snapshots:
        symbols = [
            SectorSymbolFact(
                symbol=s.symbol, last_price=s.last_price, change_pct=s.change_pct
            )
            for s in snapshot.symbols
        ]
        for s in symbols:
            last_prices[s.symbol] = s.last_price
        sectors.append(SectorFact(sector=snapshot.sector, symbols=symbols))

    # A quote event is newer than the snapshot it came with, so it wins.
    for event in response.events:
        if event.HasField("quote") and event.symbol:
            last_prices[event.symbol] = event.quote.last_price

    return PulledBatch(
        records=records,
        selected=selected,
        sectors=sectors,
        last_prices=last_prices,
        status_updates=status_updates,
        next_cursor=response.next_cursor,
        dropped_count=response.dropped_count,
    )


class MarketDataUnavailable(RuntimeError):
    """The md plane could not be reached — startup aborts before trading (§9.1 step 2)."""


class GrpcMarketDataClient:
    """MarketDataClient port over gRPC to `tevnnis-md` (§4.3, pull-only)."""

    def __init__(self, address: str) -> None:
        self.address = address
        self._channel: Any | None = None
        self._stub: Any | None = None

    async def connect(self) -> None:
        import grpc  # local import: only the real client needs the runtime

        self._channel = grpc.aio.insecure_channel(self.address)
        self._stub = md_service_pb2_grpc.MarketDataStub(self._channel)

    async def validate(self, timeout: float = 5.0) -> None:
        """Fail fast if md is not answering — §9.1 step 2 aborts before trading."""
        if self._channel is None:
            await self.connect()
        assert self._channel is not None
        try:
            await asyncio.wait_for(self._channel.channel_ready(), timeout=timeout)
        except (TimeoutError, asyncio.TimeoutError) as exc:
            raise MarketDataUnavailable(
                f"md did not answer at {self.address} within {timeout:.0f}s"
            ) from exc

    async def pull_decision_batch(self, request: Any) -> Any:
        if self._stub is None:
            await self.connect()
        assert self._stub is not None
        return await self._stub.PullDecisionBatch(request)

    async def close(self) -> None:
        if self._channel is not None:
            await self._channel.close()
            self._channel = None
            self._stub = None
