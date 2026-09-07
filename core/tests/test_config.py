"""Tests for the strategy config loader and Pydantic validation (§6)."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from tevnnis_core.config import LLMRouteConfig, StrategyConfig
from tevnnis_core.config_loader import load_config

CONFIG_DIR = Path(__file__).parent.parent.parent / "config"
CONFIG_EXAMPLE = CONFIG_DIR / "config.example.yaml"

# ---------------------------------------------------------------------------
# Minimal valid config fixture (avoids repeating the full dict in every test)
# ---------------------------------------------------------------------------

VALID_BASE: dict = {
    "account": {"mode": "paper", "managed_capital": 10000},
    "universe": {"Tech": ["AAPL.US"]},
    "instruments": {},
    "style": {"risk_appetite": "moderate", "holding_bias": "long_term"},
    "risk_limits": {
        "max_position_pct": 0.25,
        "max_sector_pct": 0.50,
        "min_cash_reserve_pct": 0.10,
        "limit_price_max_deviation_pct": 0.03,
        "max_order_notional": 3000,
        "max_day_trades_per_week": 3,
    },
    "budgets": {
        "llm_daily_token_budget": 500000,
        "llm_daily_call_cap": 50,
        "broker_max_trades_per_day": 10,
        "broker_max_turnover_per_day": 5000,
    },
    "triggers": {
        "entry_threshold_pct": 3.0,
        "escalation_bands": [3.0, 5.0, 8.0],
        "cooldown_minutes": 15,
        "sector_rate_cap_per_hour": 12,
        "critical_move_pct": 8.0,
    },
    "cadence": {
        "timezone": "America/New_York",
        "decision_interval_seconds": 300,
        "respect_market_hours": True,
    },
    "lifecycle": {},
    "llm_routing": {
        "cheap": {"provider": "openai", "model": "gpt-4o-mini"},
        "mid": {"provider": "anthropic", "model": "claude-sonnet-4-6"},
        "strong": {"provider": "anthropic", "model": "claude-opus-4-8"},
    },
}


def _merge(overrides: dict) -> dict:
    import copy

    d = copy.deepcopy(VALID_BASE)
    d.update(overrides)
    return d


# ---------------------------------------------------------------------------
# Acceptance: load the example file
# ---------------------------------------------------------------------------


def test_load_config_example_accepted():
    cfg = load_config(CONFIG_EXAMPLE)
    assert cfg.account.mode == "paper"
    assert cfg.account.managed_capital == 10000
    assert "Semiconductor" in cfg.universe
    assert "NVDA.US" in cfg.universe["Semiconductor"]
    assert "BroadETF" in cfg.universe
    assert cfg.style.risk_appetite == "moderate"
    assert cfg.risk_limits.max_position_pct == 0.25
    assert cfg.risk_limits.max_sector_pct == 0.50
    assert cfg.budgets.llm_daily_token_budget == 500_000
    assert cfg.triggers.entry_threshold_pct == 3.0
    assert cfg.triggers.escalation_bands == [3.0, 5.0, 8.0]
    assert cfg.cadence.timezone == "America/New_York"
    assert cfg.cadence.decision_interval_seconds == 300
    assert cfg.llm_routing.cheap.provider == "openai"
    assert cfg.llm_routing.strong.model == "claude-opus-4-8"
    assert cfg.lifecycle.cancel_open_orders_on_shutdown is True


# ---------------------------------------------------------------------------
# Reject unknown / extra fields
# ---------------------------------------------------------------------------


def test_extra_top_level_field_rejected():
    with pytest.raises(ValidationError):
        StrategyConfig.model_validate(_merge({"unknown_field": "oops"}))


def test_extra_nested_field_rejected():
    d = _merge({})
    d["account"]["surprise"] = True
    with pytest.raises(ValidationError):
        StrategyConfig.model_validate(d)


# ---------------------------------------------------------------------------
# Reject invalid field values
# ---------------------------------------------------------------------------


def test_invalid_account_mode_rejected():
    d = _merge({})
    d["account"]["mode"] = "futures"
    with pytest.raises(ValidationError):
        StrategyConfig.model_validate(d)


def test_invalid_risk_appetite_rejected():
    d = _merge({})
    d["style"]["risk_appetite"] = "reckless"
    with pytest.raises(ValidationError):
        StrategyConfig.model_validate(d)


def test_max_position_pct_out_of_range_rejected():
    d = _merge({})
    d["risk_limits"]["max_position_pct"] = 1.5
    with pytest.raises(ValidationError):
        StrategyConfig.model_validate(d)


def test_managed_capital_nonpositive_rejected():
    d = _merge({})
    d["account"]["managed_capital"] = -100
    with pytest.raises(ValidationError):
        StrategyConfig.model_validate(d)


def test_empty_universe_rejected():
    d = _merge({"universe": {}})
    with pytest.raises(ValidationError):
        StrategyConfig.model_validate(d)


def test_empty_sector_rejected():
    d = _merge({"universe": {"Tech": []}})
    with pytest.raises(ValidationError):
        StrategyConfig.model_validate(d)


def test_escalation_bands_not_ascending_rejected():
    d = _merge({})
    d["triggers"]["escalation_bands"] = [5.0, 3.0, 8.0]
    with pytest.raises(ValidationError):
        StrategyConfig.model_validate(d)


def test_decision_interval_nonpositive_rejected():
    d = _merge({})
    d["cadence"]["decision_interval_seconds"] = 0
    with pytest.raises(ValidationError):
        StrategyConfig.model_validate(d)


# ---------------------------------------------------------------------------
# llm_routing (§10)
# ---------------------------------------------------------------------------


def test_reasoning_effort_is_optional_and_defaults_to_none():
    """None means "use the provider's own default" — it is not a magic string."""
    route = LLMRouteConfig(provider="openai", model="gpt-5-nano")
    assert route.reasoning_effort is None
    assert LLMRouteConfig(
        provider="openai", model="gpt-5-nano", reasoning_effort="minimal"
    ).reasoning_effort == "minimal"


def test_the_paper_config_strong_route_is_the_model_we_verified():
    """v0 reads `strong` only; cheap/mid stay defined but unused."""
    config = load_config(CONFIG_DIR / "config.paper.yaml")
    assert config.llm_routing.strong.provider == "openai"
    assert config.llm_routing.strong.model == "gpt-5-nano"


def test_the_llm_acceptance_config_differs_only_in_the_market_hours_gate():
    """It exists to run the real LLM at any hour against a MOCK broker.

    If it ever diverges further from config.paper.yaml, the acceptance run
    stops proving anything about the configuration that is actually used.
    """
    paper = load_config(CONFIG_DIR / "config.paper.yaml").model_dump()
    acceptance = load_config(CONFIG_DIR / "config.paper.llm_acceptance.yaml").model_dump()

    assert acceptance["cadence"]["respect_market_hours"] is False
    assert paper["cadence"]["respect_market_hours"] is True

    acceptance["cadence"]["respect_market_hours"] = True
    assert acceptance == paper


# ---------------------------------------------------------------------------
# Public snapshot configuration and LLM pricing.
# ---------------------------------------------------------------------------


def test_a_config_without_a_public_snapshot_block_still_validates():
    """Older config files without snapshot fields must keep loading unchanged."""
    raw = dict(VALID_BASE)
    assert "public_snapshot" not in raw

    config = StrategyConfig(**raw)
    assert config.public_snapshot.enabled is True
    assert config.public_snapshot.output_dir == "../frontend"
    assert config.public_snapshot.stale_after_seconds == 900


def test_the_news_url_knobs_default_to_no_extra_restriction():
    """The default must let an external publisher link through.

    An allow-list over a non-enumerable domain space would silently null every
    external article url and break the clickthrough with no error surfaced.
    The token threat is closed by the sanitizer's other rules — see
    tevnnis_core.snapshot.sanitize_news_url.
    """
    config = StrategyConfig(**VALID_BASE)
    assert config.public_snapshot.news_url_allow_hosts == []
    assert config.public_snapshot.news_url_keep_params == []


def test_an_unknown_public_snapshot_key_is_refused():
    raw = dict(VALID_BASE)
    raw["public_snapshot"] = {"enabled": True, "typo_knob": 1}
    with pytest.raises(ValidationError):
        StrategyConfig(**raw)


@pytest.mark.parametrize(
    "field, value",
    [
        ("stale_after_seconds", 0),
        ("thesis_max_chars", 0),
        ("news_max_items", 0),
        ("trend_points", 0),
        ("series_max_points", 1),
    ],
)
def test_public_snapshot_bounds_are_enforced(field, value):
    raw = dict(VALID_BASE)
    raw["public_snapshot"] = {field: value}
    with pytest.raises(ValidationError):
        StrategyConfig(**raw)


def test_llm_route_pricing_is_optional_and_defaults_to_unpriced():
    route = LLMRouteConfig(provider="openai", model="gpt-5-nano")
    assert route.price_in_per_mtok is None
    assert route.price_out_per_mtok is None


def test_llm_route_pricing_round_trips():
    route = LLMRouteConfig(
        provider="openai",
        model="gpt-5-nano",
        price_in_per_mtok=0.05,
        price_out_per_mtok=0.40,
    )
    assert route.price_in_per_mtok == 0.05
    assert route.price_out_per_mtok == 0.40


def test_negative_pricing_is_refused():
    with pytest.raises(ValidationError):
        LLMRouteConfig(provider="openai", model="m", price_in_per_mtok=-1.0)
