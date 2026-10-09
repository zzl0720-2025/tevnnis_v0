"""Build model input from strategy settings and current trading state.

The static prefix contains strategy settings and the output format. The dynamic
section contains market, account and position facts with precomputed arithmetic.
Article bodies, URLs, risk limits and budgets are excluded from the prompt.
"""

from __future__ import annotations

from dataclasses import dataclass

from tevnnis_core.config import StyleConfig
from tevnnis_core.ports import AccountSnapshot, PositionSnapshot

# Dataclasses decouple prompt formatting from the gRPC wire schema.
# The decision loop maps market-data responses into these types.


@dataclass(frozen=True)
class SelectedEvent:
    event_id: str
    type: str  # "NEWS" | "QUOTE_MOVE" | "STATUS"
    symbol: str
    sector: str
    priority: str  # "LOW" | "MEDIUM" | "HIGH" | "CRITICAL"
    summary: str  # quote trigger, news title, or status value: never url/body
    change_pct: float | None = None


@dataclass(frozen=True)
class SectorSymbolFact:
    symbol: str
    last_price: float
    change_pct: float


@dataclass(frozen=True)
class SectorFact:
    sector: str
    symbols: list[SectorSymbolFact]


# Static prefix

_SYSTEM_PROMPT = (
    "You are TEVNNIS, a disciplined, low-frequency trading agent for a personal "
    "account. You propose; deterministic code disposes — your output never "
    "bypasses hard risk limits and is re-checked after you respond. HOLD is the "
    "default: propose a BUY or SELL only when the evidence below genuinely "
    "supports it, not to fill space."
)

_OUTPUT_FORMAT_SPEC = (
    "OUTPUT FORMAT\n"
    "Respond with a DecisionOutput: instructions (0-5 ProposedInstruction) and "
    "session_note (a short overall note). Each instruction: action=HOLD|BUY|SELL, "
    "symbol, order_type=LIMIT, quantity (shares; BUY/SELL only, >0), limit_price "
    "(BUY/SELL only, >0), valid_seconds (order lifetime), confidence (0-1), thesis "
    "(why), cited_event_ids (which of the event ids below drove this). HOLD carries "
    "no quantity or limit_price. Use only symbols, prices and events given below — "
    "never invent data. Prices, change%, and P&L are already computed for you; do "
    "not do your own arithmetic."
)


def build_static_prefix(style: StyleConfig) -> str:
    strategy = (
        f"STRATEGY\n"
        f"risk_appetite={style.risk_appetite} holding_bias={style.holding_bias} "
        f'notes="{style.notes}"'
    )
    return "\n\n".join([_SYSTEM_PROMPT, strategy, _OUTPUT_FORMAT_SPEC])


# Dynamic state


def _fmt_pct(value: float) -> str:
    return f"{value:+.2f}%"


def _fmt_money(value: float) -> str:
    return f"{value:.2f}"


def _positions_block(positions: list[PositionSnapshot], last_prices: dict[str, float]) -> str:
    if not positions:
        return "POSITIONS\n(none held)"
    lines = ["POSITIONS"]
    for p in positions:
        last = last_prices.get(p.symbol)
        if last is not None and p.cost_basis:
            pnl_pct = (last - p.cost_basis) / p.cost_basis * 100.0
            pnl = _fmt_pct(pnl_pct)
        else:
            pnl = "n/a"
        last_str = _fmt_money(last) if last is not None else "n/a"
        lines.append(
            f"{p.symbol} qty={p.quantity} cost={_fmt_money(p.cost_basis)} "
            f"last={last_str} pnl_pct={pnl}"
        )
    return "\n".join(lines)


def _sectors_block(sectors: list[SectorFact]) -> str:
    if not sectors:
        return "SECTORS\n(none)"
    lines = ["SECTORS"]
    for sector in sectors:
        symbols = "; ".join(
            f"{s.symbol} last={_fmt_money(s.last_price)} chg={_fmt_pct(s.change_pct)}"
            for s in sector.symbols
        )
        lines.append(f"{sector.sector}: {symbols}")
    return "\n".join(lines)


def _events_block(events: list[SelectedEvent]) -> str:
    if not events:
        return "EVENTS\n(none)"
    lines = ["EVENTS"]
    for e in events:
        change = f" change={_fmt_pct(e.change_pct)}" if e.change_pct is not None else ""
        lines.append(f'{e.event_id} {e.type} {e.symbol} {e.priority}{change} "{e.summary}"')
    return "\n".join(lines)


def _account_block(account: AccountSnapshot | None) -> str:
    if account is None:
        return "ACCOUNT\n(unavailable)"
    return (
        "ACCOUNT\n"
        f"cash={_fmt_money(account.cash)} buying_power={_fmt_money(account.buying_power)} "
        f"net_liq={_fmt_money(account.net_liquidation)}"
    )


def build_dynamic_state(
    events: list[SelectedEvent],
    sectors: list[SectorFact],
    positions: list[PositionSnapshot],
    account: AccountSnapshot | None = None,
) -> str:
    last_prices = {s.symbol: s.last_price for sector in sectors for s in sector.symbols}
    return "\n\n".join(
        [
            _positions_block(positions, last_prices),
            _sectors_block(sectors),
            _events_block(events),
            _account_block(account),
        ]
    )


# Assembled prompt


def build_prompt_messages(
    style: StyleConfig,
    events: list[SelectedEvent],
    sectors: list[SectorFact],
    positions: list[PositionSnapshot],
    account: AccountSnapshot | None = None,
) -> list[dict[str, str]]:
    """Keep the stable prefix before dynamic state for provider prefix caching.

    Cache eligibility and pricing depend on the provider and model.
    """
    return [
        {"role": "system", "content": build_static_prefix(style)},
        {"role": "user", "content": build_dynamic_state(events, sectors, positions, account)},
    ]
