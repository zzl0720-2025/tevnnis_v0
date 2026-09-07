"""Load a JSON scenario into a full set of mocks (§13 mock-first).

One file scripts an entire offline session — what md serves each round, what
the LLM proposes, and what the broker holds — so `tevnnis-core --scenario ...`
replays a complete decision loop with zero external calls and
zero cost. This is the guided first-run path and the shape the end-to-end test
drives. Each axis can be swapped for the real thing independently (`--md-source
grpc`, `--broker longbridge`, `--llm openai`), in which case that axis's block
here is simply ignored.

    {
      "broker":      {"initial_cash": 10000,
                      "positions": [{"symbol": "AMD.US", "quantity": 20,
                                     "cost_basis": 48.0}],
                      "fill_mode": "immediate_full"},
      "market_data": [{"events": [{"event_id": "e1", "type": "QUOTE_MOVE",
                                   "symbol": "NVDA.US", "sector": "Semiconductor",
                                   "priority": "HIGH",
                                   "quote": {"last_price": 104.0, "change_pct": 4.0,
                                             "trigger": "cross_+3pct"}}],
                       "snapshots": {"Semiconductor": [["NVDA.US", 104.0, 4.0]]},
                       "next_cursor": "c1"}],
      "llm":         [{"session_note": "...",
                       "instructions": [{"action": "BUY", "symbol": "NVDA.US",
                                         "quantity": 10, "limit_price": 104.0,
                                         "valid_seconds": 300, "confidence": 0.8,
                                         "thesis": "...", "cited_event_ids": ["e1"]}]}]
    }

`market_data` and `llm` are per-round lists: round *n* gets element *n*, and
once a list runs out the mock keeps answering (an empty batch / the last
scripted response), which is what a quiet market looks like.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tevnnis_core.instructions import Action, DecisionOutput, OrderType, ProposedInstruction
from tevnnis_core.mocks.broker import FillMode, MockBroker
from tevnnis_core.mocks.llm import MockLLM
from tevnnis_core.mocks.market_data import MockMarketDataClient, make_event, make_response
from tevnnis_core.ports import PositionSnapshot


@dataclass
class ScenarioMocks:
    broker: MockBroker
    llm: MockLLM
    md_client: MockMarketDataClient


def build_decision_output(spec: dict[str, Any]) -> DecisionOutput:
    return DecisionOutput(
        instructions=[
            ProposedInstruction(
                action=Action[i.get("action", "HOLD")],
                symbol=i.get("symbol", ""),
                order_type=OrderType[i.get("order_type", "LIMIT")],
                quantity=i.get("quantity", 0),
                limit_price=i.get("limit_price", 0.0),
                valid_seconds=i.get("valid_seconds", 0),
                confidence=i.get("confidence", 0.5),
                thesis=i.get("thesis", ""),
                cited_event_ids=list(i.get("cited_event_ids", [])),
            )
            for i in spec.get("instructions", [])
        ],
        session_note=spec.get("session_note", ""),
    )


def scripted_llm(outputs: list[DecisionOutput], **usage: int) -> MockLLM:
    """A MockLLM that returns `outputs` in order, repeating the last one after."""
    remaining = list(outputs)
    last: list[DecisionOutput] = [DecisionOutput()]

    def respond(messages: Any, response_model: type) -> DecisionOutput:
        if remaining:
            last[0] = remaining.pop(0)
        return last[0]

    return MockLLM(response=respond, **usage)


def build_mocks(spec: dict[str, Any]) -> ScenarioMocks:
    broker_spec = spec.get("broker", {})
    broker = MockBroker(
        initial_cash=float(broker_spec.get("initial_cash", 10_000.0)),
        initial_positions={
            p["symbol"]: PositionSnapshot(
                symbol=p["symbol"],
                quantity=int(p["quantity"]),
                cost_basis=float(p["cost_basis"]),
            )
            for p in broker_spec.get("positions", [])
        },
        fill_mode=FillMode(broker_spec.get("fill_mode", "immediate_full")),
    )

    responses = []
    for round_spec in spec.get("market_data", []):
        events = [
            make_event(
                e["event_id"],
                type=e.get("type", "QUOTE_MOVE"),
                symbol=e.get("symbol", ""),
                sector=e.get("sector", ""),
                priority=e.get("priority", "HIGH"),
                event_ts=e.get("event_ts", 0),
                ingest_ts=e.get("ingest_ts", 0),
                quote=e.get("quote"),
                news=e.get("news"),
                status=e.get("status"),
            )
            for e in round_spec.get("events", [])
        ]
        snapshots = {
            sector: [(s[0], float(s[1]), float(s[2])) for s in symbols]
            for sector, symbols in round_spec.get("snapshots", {}).items()
        }
        responses.append(
            make_response(
                events=events,
                snapshots=snapshots,
                next_cursor=round_spec.get("next_cursor", ""),
                dropped_count=int(round_spec.get("dropped_count", 0)),
            )
        )

    llm = scripted_llm([build_decision_output(o) for o in spec.get("llm", [])])
    return ScenarioMocks(broker=broker, llm=llm, md_client=MockMarketDataClient(responses))


def load_scenario(path: str | Path) -> ScenarioMocks:
    with open(path) as f:
        return build_mocks(json.load(f))
