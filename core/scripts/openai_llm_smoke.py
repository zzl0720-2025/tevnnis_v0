#!/usr/bin/env python3
"""Isolated OpenAI LLM smoke test: one call, then exit.

Deliberately NOT a pytest and NOT wired into core: no CLI, no agent loop, no
database, no broker, no md. It builds the real shipping `OpenAIProvider` from
`config.llm_routing.strong`, sends one canned prompt, prints what came back,
and exits. Nothing it does can place an order or write a row.

It verifies the key, model id, strict structured-output parsing and, above all,
that the API's reported token counts reach `TokenUsage`. The check remains
isolated from the Router and agent loop so its cost and side effects are bounded.

WHAT THE NUMBERS ARE FOR. This prints latency and the cached/reasoning token
breakdown alongside the totals because two provider ceilings must be sized from
evidence rather than guessed:

  * `--reasoning-effort` — the dominant term. gpt-5-nano defaults to "medium"
    and was measured spending 3520 to >4096 reasoning tokens on ~121 tokens of
    JSON, truncating about half the time. Sweep minimal/low/medium here and
    read `of which reasoning` to see the effect directly.
  * `--max-completion-tokens` — REASONING TOKENS COME OUT OF THIS SAME CEILING,
    before a single visible JSON character is emitted. Too low and every call
    ends in finish_reason=length, i.e. a HOLD.
  * `--timeout` — a reasoning model is slower than a plain completion, and a
    timeout set too tight turns every call into a HOLD.

Run it a few times per setting and read the SPREAD, not one sample: the whole
point is that the variance, not the mean, is what causes truncation.

COST. One gpt-5-nano call on this prompt. At $0.05/M in and $0.40/M out it is
a fraction of a cent, but it is REAL money on a prepaid, hard-capped account
(§10 layer 1) — which is exactly why this exists as one call you trigger by
hand rather than anything automatic.

Secrets (§12): the SDK reads OPENAI_API_KEY itself. This script
never reads, copies, stores or prints the VALUE — it only checks that the
variable is set and non-empty, and names it if it is missing. Every string that
came from outside this program is passed through `redact_secrets` before it is
printed.

Usage (you export the credential; nothing here parses .env):
    cd core
    set -a && source ../.env && set +a
    uv run python scripts/openai_llm_smoke.py --config ../config/config.paper.yaml
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from tevnnis_core.config_loader import load_config
from tevnnis_core.instructions import Action, DecisionOutput
from tevnnis_core.llm.openai_provider import (
    DEFAULT_MAX_COMPLETION_TOKENS,
    DEFAULT_REASONING_EFFORT,
    DEFAULT_TIMEOUT_SECONDS,
    PROVIDER_NAME,
    REASONING_EFFORTS,
    OpenAIProvider,
    OpenAIProviderError,
)
from tevnnis_core.llm.protocol_unifier import (
    SectorFact,
    SectorSymbolFact,
    SelectedEvent,
    build_prompt_messages,
)
from tevnnis_core.ports import AccountSnapshot, PositionSnapshot
from tevnnis_core.redact import redact_secrets

REQUIRED_ENV_VARS = ("OPENAI_API_KEY",)


# ---------------------------------------------------------------------------
# The canned round
#
# Built with the REAL protocol unifier, not a hand-written string: the point is
# to exercise the prompt shape the agent loop will actually send (cacheable
# static prefix as the system message, dynamic state as the user message), so
# what the smoke proves is what production does.
#
# The numbers are synthetic and self-consistent. A single HIGH event on a
# symbol already held, with room in cash to act — a round where HOLD, BUY and
# SELL are all defensible. Whichever it picks is fine; this proves the pipe,
# not the trade.
# ---------------------------------------------------------------------------

CANNED_EVENTS = [
    SelectedEvent(
        event_id="smoke-1",
        type="QUOTE_MOVE",
        symbol="NVDA.US",
        sector="Semiconductor",
        priority="HIGH",
        summary="cross_+3pct",
        change_pct=4.2,
    ),
    SelectedEvent(
        event_id="smoke-2",
        type="NEWS",
        symbol="NVDA.US",
        sector="Semiconductor",
        priority="HIGH",
        summary="Datacenter demand guidance raised at supplier conference",
    ),
]

CANNED_SECTORS = [
    SectorFact(
        sector="Semiconductor",
        symbols=[
            SectorSymbolFact(symbol="NVDA.US", last_price=229.59, change_pct=4.2),
            SectorSymbolFact(symbol="AMD.US", last_price=142.10, change_pct=1.1),
        ],
    )
]

CANNED_POSITIONS = [PositionSnapshot(symbol="AMD.US", quantity=5, cost_basis=138.00)]

CANNED_ACCOUNT = AccountSnapshot(buying_power=3000.0, cash=3000.0, net_liquidation=3710.50)


def preflight_env() -> list[str]:
    """Names of required env vars that are unset or empty. VALUES ARE NEVER READ."""
    return [name for name in REQUIRED_ENV_VARS if not os.environ.get(name)]


def render_decision(output: DecisionOutput) -> list[str]:
    lines = [f'  session_note : "{redact_secrets(output.session_note)}"']
    if not output.instructions:
        lines.append("  instructions : (none — an empty decision, which is valid)")
        return lines
    lines.append(f"  instructions : {len(output.instructions)}")
    for i, instruction in enumerate(output.instructions):
        head = f"    [{i}] {Action(instruction.action).name} {instruction.symbol}"
        if instruction.action != Action.HOLD:
            head += (
                f" qty={instruction.quantity} limit={instruction.limit_price:.2f}"
                f" valid_s={instruction.valid_seconds}"
            )
        lines.append(f"{head} confidence={instruction.confidence:.2f}")
        lines.append(f'         thesis: "{redact_secrets(instruction.thesis)}"')
        lines.append(f"         cited : {instruction.cited_event_ids}")
    return lines


async def run(args: argparse.Namespace) -> int:
    print("[smoke] 1/4 configuration")
    config = load_config(args.config)
    strong = config.llm_routing.strong
    print(f"[smoke]     llm_routing.strong: provider={strong.provider} model={strong.model}")
    if strong.provider != PROVIDER_NAME:
        print(
            f"[smoke] REFUSED: this smoke only drives the {PROVIDER_NAME!r} provider, but "
            f"{args.config} says llm_routing.strong.provider={strong.provider!r}.",
            file=sys.stderr,
        )
        return 2

    print("[smoke] 2/4 credentials (checked BY NAME — no value is ever read)")
    missing = preflight_env()
    if missing:
        print(
            f"[smoke] REFUSED: {', '.join(missing)} is not set in the environment.\n"
            "[smoke]          Export it first:  set -a && source ../.env && set +a",
            file=sys.stderr,
        )
        return 2
    print(f"[smoke]     {', '.join(REQUIRED_ENV_VARS)}: set (value not read)")

    effort = None if args.reasoning_effort == "omit" else args.reasoning_effort
    provider = OpenAIProvider(
        model=strong.model,
        max_completion_tokens=args.max_completion_tokens,
        timeout_seconds=args.timeout,
        reasoning_effort=effort,
    )
    messages = build_prompt_messages(
        config.style, CANNED_EVENTS, CANNED_SECTORS, CANNED_POSITIONS, CANNED_ACCOUNT
    )

    print("[smoke] 3/4 one structured call — REAL SPEND")
    print(f"[smoke]     model={strong.model} max_completion_tokens={args.max_completion_tokens}")
    print(
        "[smoke]     reasoning_effort="
        + (f"{effort!r}" if effort is not None else "omitted (the model's own default)")
    )
    print(f"[smoke]     timeout={args.timeout:.0f}s  max_retries=0 (a retry is silent extra spend)")
    for message in messages:
        print(f"[smoke]     prompt[{message['role']}]: {len(message['content'])} chars")
    if args.show_prompt:
        for message in messages:
            print(f"\n----- {message['role']} -----\n{message['content']}")
        print()

    try:
        output = await provider.complete_structured(messages, DecisionOutput)
    except OpenAIProviderError as exc:
        # Already redacted by the provider. This is the shape the Router turns
        # into HOLD_MALFORMED_OUTPUT — here it is simply a failed smoke.
        print(f"[smoke] FAILED: {exc}", file=sys.stderr)
        usage = provider.last_usage()
        if usage is not None:
            print(
                f"[smoke]         the call still SPENT tokens_in={usage.tokens_in} "
                f"tokens_out={usage.tokens_out} — a paid call that failed to parse.",
                file=sys.stderr,
            )
        return 1

    print("[smoke] 4/4 parsed DecisionOutput")
    for line in render_decision(output):
        print(line)

    usage = provider.last_usage()
    stats = provider.last_call_stats()
    assert usage is not None and stats is not None  # a returned decision always has both
    print("")
    print("[smoke] ACTUAL token usage, as reported by the API (never estimated)")
    print(f"  prompt_tokens     : {usage.tokens_in}")
    print(f"  completion_tokens : {usage.tokens_out}")
    print(f"  total             : {usage.total}")
    print(
        "  of which cached   : "
        + (f"{stats.cached_tokens} (billed 0.1x; counted in full by the daily cap)"
           if stats.cached_tokens is not None else "not reported")
    )
    print(
        "  of which reasoning: "
        + (f"{stats.reasoning_tokens} (already inside completion_tokens)"
           if stats.reasoning_tokens is not None else "not reported")
    )
    print(f"  finish_reason     : {stats.finish_reason!r}")
    print("")
    print("[smoke] SIZING EVIDENCE — set the provider ceilings from these, not from a guess")
    print(f"  reasoning_effort  : {stats.reasoning_effort!r}")
    print(f"  latency           : {stats.latency_seconds:.2f}s  (timeout is {args.timeout:.0f}s)")
    print(
        f"  completion ceiling: used {usage.tokens_out} of {args.max_completion_tokens}"
        f" ({usage.tokens_out / args.max_completion_tokens * 100:.1f}%)"
    )
    print("")
    print("[smoke] PASS — key, model id, strict structured output and real usage all verified.")
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="openai_llm_smoke",
        description="One isolated OpenAI structured-output call. Real spend; no orders, no DB.",
    )
    parser.add_argument("--config", required=True, help="path to the §6 strategy YAML")
    parser.add_argument(
        "--max-completion-tokens",
        type=int,
        default=DEFAULT_MAX_COMPLETION_TOKENS,
        help=(
            "ceiling for reasoning + visible output tokens "
            f"(default {DEFAULT_MAX_COMPLETION_TOKENS}); lower it to observe the "
            "finish_reason=length path that degrades to a HOLD"
        ),
    )
    parser.add_argument(
        "--reasoning-effort",
        choices=[*REASONING_EFFORTS, "omit"],
        default=DEFAULT_REASONING_EFFORT,
        help=(
            f"reasoning effort for gpt-5-nano (default {DEFAULT_REASONING_EFFORT!r}); "
            "'omit' sends no parameter at all and gets the model's own default. "
            "Note: 'none' is NOT in this list — it arrives with gpt-5.1+ and this "
            "model rejects it"
        ),
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        help=f"request timeout in seconds (default {DEFAULT_TIMEOUT_SECONDS:.0f})",
    )
    parser.add_argument(
        "--show-prompt",
        action="store_true",
        help="print the full prompt that is sent (no secrets are in it)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\n[smoke] interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
