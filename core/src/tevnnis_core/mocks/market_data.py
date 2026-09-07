"""MockMarketDataClient — a scripted §4.3 pull source (§13 mock-first).

Replays a fixed list of `PullResponse`s so a whole decision-loop scenario is
reproducible with no `tevnnis-md` process and no gRPC. Once the script is
exhausted it keeps answering with an empty batch (carrying the last cursor
forward), which is exactly what a quiet market looks like to the loop.

`since_cursor` is honoured the way md honours it (§4.3): a request carrying a
cursor this script recognises resumes *after* that response, so a core restart
replaying the same scenario does not re-receive events it already processed. An
unrecognised cursor falls through to the current position, mirroring md's
behaviour after its own restart (the `events.event_id` constraint is the
backstop there).

Every request is recorded, so tests can assert that core really does pass
`next_cursor` back as `since_cursor`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from tevnnis_core.pb import events_pb2, md_service_pb2


def make_event(
    event_id: str,
    *,
    type: str = "QUOTE_MOVE",
    symbol: str = "",
    sector: str = "",
    priority: str = "HIGH",
    event_ts: int = 0,
    ingest_ts: int = 0,
    quote: dict[str, Any] | None = None,
    news: dict[str, Any] | None = None,
    status: str | None = None,
) -> Any:
    """Build one `MarketEvent`. Exactly one payload kwarg should be given."""
    event = events_pb2.MarketEvent(
        event_id=event_id,
        type=events_pb2.EventType.Value(type),
        symbol=symbol,
        sector=sector,
        priority=events_pb2.Priority.Value(priority),
        event_ts=event_ts,
        ingest_ts=ingest_ts,
    )
    if quote is not None:
        event.quote.CopyFrom(events_pb2.QuotePayload(**quote))
    elif news is not None:
        event.news.CopyFrom(events_pb2.NewsPayload(**news))
    elif status is not None:
        event.status.CopyFrom(
            events_pb2.StatusPayload(status=events_pb2.StatusPayload.Status.Value(status))
        )
    return event


def make_response(
    *,
    events: list[Any] | None = None,
    snapshots: dict[str, list[tuple[str, float, float]]] | None = None,
    next_cursor: str = "",
    dropped_count: int = 0,
) -> Any:
    """Build one `PullResponse`; `snapshots` maps sector -> [(symbol, last, change_pct)]."""
    response = md_service_pb2.PullResponse(
        next_cursor=next_cursor, dropped_count=dropped_count
    )
    for event in events or []:
        response.events.append(event)
    for sector, symbols in (snapshots or {}).items():
        snapshot = response.snapshots.add()
        snapshot.sector = sector
        for symbol, last_price, change_pct in symbols:
            state = snapshot.symbols.add()
            state.symbol = symbol
            state.last_price = last_price
            state.change_pct = change_pct
    return response


@dataclass
class MockMarketDataClient:
    """In-memory MarketDataClient port. Not thread-safe; single-loop use."""

    responses: list[Any] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.requests: list[Any] = []
        self._index = 0
        self._last_cursor = ""

    async def pull_decision_batch(self, request: Any) -> Any:
        self.requests.append(request)

        cursor = getattr(request, "since_cursor", "")
        if cursor:
            for position, response in enumerate(self.responses):
                if response.next_cursor == cursor:
                    # Everything up to and including this response is consumed.
                    self._index = max(self._index, position + 1)
                    self._last_cursor = cursor
                    break

        if self._index < len(self.responses):
            response = self.responses[self._index]
            self._index += 1
            self._last_cursor = response.next_cursor or self._last_cursor
            return response
        return md_service_pb2.PullResponse(next_cursor=self._last_cursor)

    async def validate(self, timeout: float = 5.0) -> None:
        """No-op: a scripted source is always reachable."""

    async def close(self) -> None:
        """No-op."""

    @property
    def exhausted(self) -> bool:
        return self._index >= len(self.responses)
