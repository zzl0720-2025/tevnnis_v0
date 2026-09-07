"""Pydantic v2 models for the strategy configuration (§6)."""

from __future__ import annotations

from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator


class AccountConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["paper", "live"]
    managed_capital: Annotated[float, Field(gt=0)]
    base_currency: str = "USD"


class InstrumentsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    allow_common_stock: bool = True
    allow_etf: bool = True
    etf_denylist_patterns: list[str] = []


class StyleConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    risk_appetite: Literal["conservative", "moderate", "aggressive"]
    holding_bias: str
    notes: str = ""


class RiskLimitsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_position_pct: Annotated[float, Field(gt=0, le=1)]
    max_sector_pct: Annotated[float, Field(gt=0, le=1)]
    min_cash_reserve_pct: Annotated[float, Field(ge=0, lt=1)]
    limit_price_max_deviation_pct: Annotated[float, Field(gt=0, lt=1)]
    max_order_notional: Annotated[float, Field(gt=0)]
    max_day_trades_per_week: Annotated[int, Field(ge=0)]
    # §11 open/close volatility no-trade windows. Promoted from risk-local C++
    # defaults into §6 config when core began constructing RiskContext.
    # 0 disables a window.
    no_trade_after_open_minutes: Annotated[int, Field(ge=0)] = 5
    no_trade_before_close_minutes: Annotated[int, Field(ge=0)] = 5


class BudgetsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    llm_daily_token_budget: Annotated[int, Field(gt=0)]
    llm_daily_call_cap: Annotated[int, Field(gt=0)]
    broker_max_trades_per_day: Annotated[int, Field(gt=0)]
    broker_max_turnover_per_day: Annotated[float, Field(gt=0)]


class TriggersConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entry_threshold_pct: Annotated[float, Field(gt=0)]
    escalation_bands: Annotated[list[float], Field(min_length=1)]
    cooldown_minutes: Annotated[int, Field(ge=0)]
    sector_rate_cap_per_hour: Annotated[int, Field(gt=0)]
    critical_move_pct: Annotated[float, Field(gt=0)]

    @field_validator("escalation_bands")
    @classmethod
    def _bands_ascending(cls, v: list[float]) -> list[float]:
        for a, b in zip(v, v[1:]):
            if a >= b:
                raise ValueError("escalation_bands must be strictly ascending")
        return v


class CadenceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timezone: str
    decision_interval_seconds: Annotated[int, Field(gt=0)]
    respect_market_hours: bool
    # Cheap gate (§8 step 3): the minimum event priority in a pulled batch that
    # wakes the strong model. core pulls every priority (all events are
    # persisted for audit and keep the snapshots current) but a batch whose
    # highest priority is below this logs a HOLD and spends no tokens.
    reason_min_priority: Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"] = "HIGH"

    @field_validator("timezone")
    @classmethod
    def _timezone_resolvable(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown timezone {v!r}") from exc
        return v


class LifecycleConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cancel_open_orders_on_shutdown: bool = True


class LLMRouteConfig(BaseModel):
    """One routing tier. v0 reads `strong` only (§10 defers cheap/local).

    `reasoning_effort` is optional and provider-specific: it tunes how many
    reasoning tokens a reasoning model spends before answering. None means
    "use the provider's own default" — for OpenAI that is `low`, chosen from
    measured evidence (see llm/openai_provider.py). It lives here rather than
    in code because it is the knob most likely to need tuning per model.
    """

    model_config = ConfigDict(extra="forbid")

    provider: str
    model: str
    reasoning_effort: str | None = None
    # USD per 1M tokens, from the provider's public price list. Optional
    # because §7's `api_usage.cost` column has to mean something real or
    # nothing at all: with these set, record_usage() stores a true cost and the
    # public snapshot can report `ai_cost_today`; with them unset, cost stays
    # 0.0 and the snapshot reports null rather than a confident $0.00.
    price_in_per_mtok: Annotated[float, Field(ge=0)] | None = None
    price_out_per_mtok: Annotated[float, Field(ge=0)] | None = None


class LLMRoutingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cheap: LLMRouteConfig
    mid: LLMRouteConfig
    strong: LLMRouteConfig


class PublicSnapshotConfig(BaseModel):
    """The §7/§14 sanitized public snapshot.

    Every field is defaulted, so a config file written before this block existed
    still validates. The two url knobs both default to "no extra restriction" —
    see `tevnnis_core.snapshot.sanitize_news_url` for why an empty host list is
    the correct default rather than a lax one.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    #: Where public_snapshot.json / .js are written. Relative paths resolve
    #: against the process CWD; --snapshot-dir overrides.
    output_dir: str = "../frontend"
    #: Older than this and the dashboard shows STALE. 3x the default cadence.
    stale_after_seconds: Annotated[int, Field(gt=0)] = 900
    thesis_max_chars: Annotated[int, Field(gt=0)] = 400
    news_max_items: Annotated[int, Field(gt=0)] = 12
    recent_decisions: Annotated[int, Field(gt=0)] = 8
    trend_points: Annotated[int, Field(gt=0)] = 12
    series_max_points: Annotated[int, Field(gt=1)] = 60
    #: EMPTY MEANS "ANY https HOST IS ALLOWED", and that is the right default.
    #: The token-leak threat is already closed by the sanitizer's other rules
    #: (https-only, no userinfo, no non-443 port, rebuild from scheme+host+path,
    #: strip every query param, drop the fragment). A host allow-list is a
    #: separate restriction that is only sound over an enumerable domain space,
    #: and a news feed's publisher domains are not: a Longbridge-only list would
    #: silently null every external article link. Populate it only if the news
    #: smoke shows the urls really are all Longbridge-internal.
    news_url_allow_hosts: list[str] = []
    #: Query parameters worth keeping. Empty = strip every one of them.
    news_url_keep_params: list[str] = []


class StrategyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account: AccountConfig
    universe: dict[str, list[str]]  # sector -> [symbol, ...]
    instruments: InstrumentsConfig
    style: StyleConfig
    risk_limits: RiskLimitsConfig
    budgets: BudgetsConfig
    triggers: TriggersConfig
    cadence: CadenceConfig
    lifecycle: LifecycleConfig
    llm_routing: LLMRoutingConfig
    public_snapshot: PublicSnapshotConfig = PublicSnapshotConfig()

    @field_validator("universe")
    @classmethod
    def _universe_non_empty(cls, v: dict[str, list[str]]) -> dict[str, list[str]]:
        if not v:
            raise ValueError("universe must have at least one sector")
        for sector, symbols in v.items():
            if not symbols:
                raise ValueError(f"sector '{sector}' must have at least one symbol")
        return v
