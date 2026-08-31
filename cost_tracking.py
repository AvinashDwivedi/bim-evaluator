from __future__ import annotations

import os
import re
from typing import Any


PRICING_AS_OF = "2026-08-29"
PRICING_SOURCE = "https://developers.openai.com/api/docs/models/gpt-5.6-sol"

# USD per one million text tokens. BIM_EVALUATOR_PRICING_JSON may override
# these rates without requiring a code change when pricing changes.
DEFAULT_MODEL_PRICING: dict[str, dict[str, float]] = {
    "gpt-5.6-sol": {
        "input": 4.00,
        "cached_input": 0.40,
        "cache_write": 5.00,
        "output": 20.00,
        "large_context_threshold": 272_000,
        "large_context_input_multiplier": 2.0,
        "large_context_output_multiplier": 1.5,
    },
    "gpt-5.6": {
        "input": 4.00,
        "cached_input": 0.40,
        "cache_write": 5.00,
        "output": 20.00,
        "large_context_threshold": 272_000,
        "large_context_input_multiplier": 2.0,
        "large_context_output_multiplier": 1.5,
    },
}


def zero_cost(*, component: str) -> dict[str, Any]:
    return {
        "component": component,
        "currency": "USD",
        "status": "calculated",
        "estimated_cost_usd": 0.0,
        "is_complete": True,
        "api_requests": 0,
        "tokens": _empty_tokens(),
    }


def response_cost(response: Any, *, configured_model: str, component: str) -> dict[str, Any]:
    usage = _value(response, "usage")
    model = str(_value(response, "model") or configured_model)
    base = {
        "component": component,
        "currency": "USD",
        "model": model,
        "api_requests": 1,
        "pricing_as_of": PRICING_AS_OF,
        "pricing_source": PRICING_SOURCE,
    }
    if usage is None:
        return {
            **base, "status": "unavailable", "estimated_cost_usd": None,
            "is_complete": False, "tokens": _empty_tokens(),
        }

    details = _value(usage, "input_tokens_details")
    input_tokens = _integer(_value(usage, "input_tokens"))
    cached_tokens = min(input_tokens, _integer(_value(details, "cached_tokens")))
    cache_write_tokens = min(
        max(0, input_tokens - cached_tokens),
        _integer(
            _value(details, "cache_write_tokens")
            or _value(details, "input_cache_write_tokens")
            or _value(usage, "input_cache_write_tokens")
        ),
    )
    output_tokens = _integer(_value(usage, "output_tokens"))
    tokens = {
        "input_tokens": input_tokens,
        "cached_input_tokens": cached_tokens,
        "cache_write_input_tokens": cache_write_tokens,
        "output_tokens": output_tokens,
        "total_tokens": _integer(_value(usage, "total_tokens")) or input_tokens + output_tokens,
    }
    rates = _rates_for_model(model)
    if rates is None:
        return {
            **base, "status": "unavailable", "estimated_cost_usd": None,
            "is_complete": False, "tokens": tokens, "pricing_status": "model_unpriced",
        }

    uncached_tokens = max(0, input_tokens - cached_tokens - cache_write_tokens)
    large_context = input_tokens > int(rates.get("large_context_threshold", 10**18))
    input_multiplier = rates.get("large_context_input_multiplier", 1.0) if large_context else 1.0
    output_multiplier = rates.get("large_context_output_multiplier", 1.0) if large_context else 1.0
    estimated = (
        uncached_tokens * rates["input"] * input_multiplier
        + cached_tokens * rates.get("cached_input", rates["input"]) * input_multiplier
        + cache_write_tokens * rates.get("cache_write", rates["input"]) * input_multiplier
        + output_tokens * rates["output"] * output_multiplier
    ) / 1_000_000
    return {
        **base, "status": "calculated", "estimated_cost_usd": round(estimated, 10),
        "is_complete": True, "tokens": tokens, "large_context_pricing": large_context,
    }


def combine_costs(system_cost: dict[str, Any] | None, judge_cost: dict[str, Any] | None) -> dict[str, Any]:
    system = _normalize_component(system_cost, "system")
    judge = _normalize_component(judge_cost, "judge")
    components = [system, judge]
    known = [item for item in components if item.get("estimated_cost_usd") is not None]
    complete = all(bool(item.get("is_complete")) for item in components)
    status = "calculated" if complete else ("partial" if known else "unavailable")
    estimated = sum(float(item["estimated_cost_usd"]) for item in known) if known else None
    return {
        "currency": "USD",
        "status": status,
        "estimated_cost_usd": round(estimated, 10) if estimated is not None else None,
        "is_complete": complete,
        "system_estimated_cost_usd": system.get("estimated_cost_usd"),
        "judge_estimated_cost_usd": judge.get("estimated_cost_usd"),
        "api_requests": sum(_integer(item.get("api_requests")) for item in components),
        "tokens": _sum_tokens(components),
        "system": system,
        "judge": judge,
    }


def summarize_costs(costs: list[dict[str, Any] | None]) -> dict[str, Any]:
    normalized = [item for item in costs if isinstance(item, dict)]
    known = [item for item in normalized if item.get("estimated_cost_usd") is not None]
    complete = len(normalized) == len(costs) and all(bool(item.get("is_complete")) for item in normalized)
    status = "calculated" if complete else ("partial" if known else "unavailable")
    estimated = sum(float(item["estimated_cost_usd"]) for item in known) if known else None
    return {
        "currency": "USD",
        "status": status,
        "estimated_cost_usd": round(estimated, 10) if estimated is not None else None,
        "is_complete": complete,
        "questions": len(costs),
        "questions_with_cost": len(known),
        "api_requests": sum(_integer(item.get("api_requests")) for item in normalized),
        "tokens": _sum_tokens(normalized),
    }


def _normalize_component(value: dict[str, Any] | None, component: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not value:
        return {
            "component": component, "currency": "USD", "status": "unavailable",
            "estimated_cost_usd": None, "is_complete": False,
            "api_requests": 0, "tokens": _empty_tokens(),
        }
    normalized = dict(value)
    normalized.setdefault("component", component)
    normalized.setdefault("currency", "USD")
    normalized.setdefault("is_complete", normalized.get("status") == "calculated")
    normalized.setdefault("api_requests", 0)
    normalized.setdefault("tokens", _empty_tokens())
    return normalized


def _rates_for_model(model: str) -> dict[str, float] | None:
    catalog = {key: dict(value) for key, value in DEFAULT_MODEL_PRICING.items()}
    raw_overrides = os.getenv("BIM_EVALUATOR_PRICING_JSON")
    if raw_overrides:
        import json
        try:
            overrides = json.loads(raw_overrides)
            for name, rates in overrides.items():
                catalog.setdefault(str(name), {}).update(
                    {str(key): float(value) for key, value in rates.items()}
                )
        except (AttributeError, TypeError, ValueError):
            pass
    if model in catalog:
        return catalog[model]
    for name, rates in catalog.items():
        if re.fullmatch(re.escape(name) + r"-\d{4}-\d{2}-\d{2}", model):
            return rates
    return None


def _sum_tokens(costs: list[dict[str, Any]]) -> dict[str, int]:
    totals = _empty_tokens()
    for cost in costs:
        tokens = cost.get("tokens") or {}
        for key in totals:
            totals[key] += _integer(tokens.get(key))
    return totals


def _empty_tokens() -> dict[str, int]:
    return {
        "input_tokens": 0, "cached_input_tokens": 0, "cache_write_input_tokens": 0,
        "output_tokens": 0, "total_tokens": 0,
    }


def _value(value: Any, key: str) -> Any:
    if value is None:
        return None
    if isinstance(value, dict):
        return value.get(key)
    return getattr(value, key, None)


def _integer(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0
