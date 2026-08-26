"""Provider pricing, usage accounting, and subscription unit economics.

The agent is screenshot-driven, so a single task can make many model calls.
Keeping pricing here makes the per-call estimate explicit and lets the product
use real provider usage metadata instead of guessing from iteration counts.
Prices are standard paid-tier USD rates and should be reviewed when a provider
changes its pricing page.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import date
from typing import Any, Iterable


PRICING_AS_OF = "2026-08-21"
MILLION = 1_000_000.0
# Homework Pilot is billed weekly in the active Stripe catalog. The margin
# comparison normalizes that price to a monthly equivalent; it is not a
# customer-facing usage allowance.
HOMEWORK_PILOT_WEEKLY_PRICE_USD = 9.99
HOMEWORK_PILOT_MONTHLY_EQUIVALENT_USD = round(HOMEWORK_PILOT_WEEKLY_PRICE_USD * 52 / 12, 2)


@dataclass(frozen=True)
class ModelPrice:
    model: str
    input_per_million: float
    output_per_million: float
    cached_input_per_million: float | None = None
    source_note: str = "Provider standard paid tier"
    known: bool = True


@dataclass(frozen=True)
class UsageRecord:
    model: str
    mode: str
    input_tokens: int
    output_tokens: int
    thinking_tokens: int
    cached_input_tokens: int
    total_tokens: int
    estimated_cost_usd: float
    pricing_known: bool
    pricing_note: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class MarginEstimate:
    plan_id: str
    monthly_price_usd: float
    monthly_calls: int
    model: str
    average_input_tokens_per_call: int
    average_output_tokens_per_call: int
    monthly_api_cost_usd: float
    fixed_cost_per_user_usd: float
    gross_profit_usd: float
    gross_margin_percent: float | None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class TierMarginEstimate:
    """Blended unit economics for one monthly Viszmo subscription tier."""

    plan_id: str
    plan_name: str
    description: str
    monthly_price_usd: float
    monthly_general_calls: int
    monthly_math_calls: int
    monthly_calls: int
    general_model: str
    math_model: str
    average_general_input_tokens_per_call: int
    average_general_output_tokens_per_call: int
    average_math_input_tokens_per_call: int
    average_math_output_tokens_per_call: int
    general_api_cost_usd: float
    math_api_cost_usd: float
    base_api_cost_usd: float
    operational_buffer_percent: float
    operational_buffer_cost_usd: float
    monthly_api_cost_usd: float
    fixed_cost_per_user_usd: float
    total_cogs_usd: float
    gross_profit_usd: float
    gross_margin_percent: float | None
    pricing_known: bool
    pricing_notes: tuple[str, ...]
    model_mix: tuple[dict[str, Any], ...] = ()

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["pricing_notes"] = list(self.pricing_notes)
        data["model_mix"] = list(self.model_mix)
        data["usage_unit"] = "model_turn"
        return data


# Standard paid-tier rates from Google's Gemini Developer API pricing page.
# Gemini 3.6/3.7 have an introductory rate through 2026-12-31; the helper
# below selects the post-promotion rate automatically after that date.
MODEL_PRICES: dict[str, ModelPrice] = {
    "gemini-3.7-flash": ModelPrice("gemini-3.7-flash", 0.75, 3.75, 0.075, "Google Gemini paid standard tier"),
    "gemini-3.6-flash": ModelPrice("gemini-3.6-flash", 0.75, 3.75, 0.075, "Google Gemini paid standard tier"),
    "gemini-3.5-flash": ModelPrice("gemini-3.5-flash", 1.50, 9.00, 0.15, "Google Gemini paid standard tier"),
    "gemini-3.5-flash-lite": ModelPrice("gemini-3.5-flash-lite", 0.30, 2.50, 0.03, "Google Gemini paid standard tier"),
    "gemini-3.1-flash-lite": ModelPrice("gemini-3.1-flash-lite", 0.25, 1.50, 0.025, "Google Gemini paid standard tier"),
    "gemini-2.5-flash": ModelPrice("gemini-2.5-flash", 0.30, 2.50, 0.03, "Google Gemini paid standard tier"),
    "gemini-2.5-flash-lite": ModelPrice("gemini-2.5-flash-lite", 0.10, 0.40, 0.01, "Google Gemini paid standard tier"),

    # OpenAI model cards. Cached-input pricing is intentionally left unset
    # until the active pricing card is configured, so cached tokens are not
    # under-counted.
    "gpt-5.6-sol": ModelPrice("gpt-5.6-sol", 4.00, 20.00, source_note="OpenAI API standard pricing"),
    "gpt-5.6-terra": ModelPrice("gpt-5.6-terra", 2.00, 12.00, source_note="OpenAI API standard pricing"),
    "gpt-5.6-luna": ModelPrice("gpt-5.6-luna", 0.20, 1.20, source_note="OpenAI API standard pricing"),
    "gpt-5-nano": ModelPrice("gpt-5-nano", 0.05, 0.40, 0.005, "OpenAI API standard pricing"),
    "gpt-5-mini": ModelPrice("gpt-5-mini", 0.25, 2.00, 0.025, "OpenAI API standard pricing"),
}


# These are deliberately kept as plain dictionaries so product and finance
# assumptions can be edited in one obvious place.  VISZMO_PLANS_JSON can
# replace the full list at runtime; the dashboard also exposes these values
# as a local scenario editor.
DEFAULT_SHARED_USAGE_ASSUMPTIONS: dict[str, Any] = {
    "general_input_tokens_per_call": 4_500,
    "general_output_tokens_per_call": 700,
    "math_input_tokens_per_call": 6_000,
    "math_output_tokens_per_call": 1_000,
    "operational_buffer_percent": 20.0,
}

DEFAULT_TIER_ASSUMPTIONS: tuple[dict[str, Any], ...] = (
    {
        "id": "study_tools",
        "name": "Study Tools",
        "description": "Study-first usage across flashcards, quizzes, notes, and tutoring.",
        "monthly_price_usd": 11.99,
        "monthly_general_calls": 250,
        "monthly_math_calls": 100,
        "usage_components": [
            {
                "id": "study_tools",
                "label": "Study Tools",
                "monthly_general_calls": 250,
                "monthly_math_calls": 100,
            },
        ],
        "fixed_cost_per_user_usd": 0.75,
        **DEFAULT_SHARED_USAGE_ASSUMPTIONS,
    },
    {
        "id": "homework_autopilot",
        "name": "Homework Pilot",
        "description": "Weekly Homework Pilot access; the $9.99 weekly price is normalized to a monthly equivalent for margin math.",
        "monthly_price_usd": HOMEWORK_PILOT_MONTHLY_EQUIVALENT_USD,
        "monthly_general_calls": 200,
        "monthly_math_calls": 300,
        "usage_components": [
            {
                "id": "homework_pilot",
                "label": "Homework Pilot",
                "monthly_general_calls": 200,
                "monthly_math_calls": 300,
            },
        ],
        "fixed_cost_per_user_usd": 0.85,
        **DEFAULT_SHARED_USAGE_ASSUMPTIONS,
    },
    {
        "id": "bundle",
        "name": "Bundle",
        "description": "Both separate entitlements together; modeled turns are used only for margin scenarios.",
        "monthly_price_usd": 22.99,
        "monthly_general_calls": 250,
        "monthly_math_calls": 500,
        "usage_components": [
            {
                "id": "study_tools",
                "label": "Study Tools",
                "monthly_general_calls": 250,
                "monthly_math_calls": 100,
            },
            {
                "id": "homework_pilot",
                "label": "Homework Pilot",
                "monthly_general_calls": 200,
                "monthly_math_calls": 300,
            },
        ],
        "fixed_cost_per_user_usd": 1.05,
        **DEFAULT_SHARED_USAGE_ASSUMPTIONS,
    },
)


def normalize_model(model: str | None) -> str:
    return str(model or "").strip().lower().removeprefix("models/")


def provider_for_model(model: str | None) -> str:
    """Infer the API provider from a model ID for pricing fallback purposes."""

    normalized = normalize_model(model)
    if normalized.startswith(("gpt-", "o1", "o3", "o4")):
        return "openai"
    return "gemini"


def _configured_provider() -> str:
    value = os.getenv("LLM_PROVIDER", "gemini").strip().lower()
    return "openai" if value in {"openai", "openai-api", "gpt"} else "gemini"


def _promotion_active(model: str, as_of: date | None = None) -> bool:
    normalized = normalize_model(model)
    return normalized in {"gemini-3.6-flash", "gemini-3.7-flash"} and (
        as_of or date.today()
    ) <= date(2026, 12, 31)


def price_for_model(model: str, as_of: date | None = None) -> ModelPrice:
    """Return the current paid-tier price card for a model.

    Unknown model IDs remain explicitly marked unknown. That prevents the
    dashboard from presenting a false precision estimate if a new model is
    configured before its price is added here.
    """

    normalized = normalize_model(model)
    base = MODEL_PRICES.get(normalized)
    if base is None:
        provider = provider_for_model(normalized)
        prefix = "OPENAI" if provider == "openai" else "GEMINI"
        try:
            input_price = float(os.getenv(f"{prefix}_INPUT_PRICE_PER_MILLION", "0"))
            output_price = float(os.getenv(f"{prefix}_OUTPUT_PRICE_PER_MILLION", "0"))
        except ValueError:
            input_price = output_price = 0.0
        if input_price > 0 and output_price > 0:
            return ModelPrice(
                normalized or "unknown",
                input_price,
                output_price,
                source_note=f"Configured via {prefix}_*_PRICE_PER_MILLION",
            )
        return ModelPrice(
            normalized or "unknown",
            0.0,
            0.0,
            source_note="No configured price for this model",
            known=False,
        )

    if _promotion_active(normalized, as_of):
        return base
    if normalized in {"gemini-3.6-flash", "gemini-3.7-flash"}:
        return ModelPrice(
            normalized,
            1.50,
            7.50,
            0.15,
            source_note="Google Gemini paid standard tier after 2026-12-31",
        )
    return base


def estimate_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cached_input_tokens: int = 0,
    as_of: date | None = None,
) -> tuple[float, bool, str]:
    """Estimate USD cost from provider token counts."""

    price = price_for_model(model, as_of=as_of)
    input_count = max(0, int(input_tokens))
    cached_count = min(input_count, max(0, int(cached_input_tokens)))
    uncached_count = input_count - cached_count
    input_cost = uncached_count / MILLION * price.input_per_million
    if cached_count and price.cached_input_per_million is not None:
        input_cost += cached_count / MILLION * price.cached_input_per_million
    else:
        input_cost += cached_count / MILLION * price.input_per_million
    output_cost = max(0, int(output_tokens)) / MILLION * price.output_per_million
    return input_cost + output_cost, price.known, price.source_note


def _usage_value(metadata: Any, *names: str) -> int:
    for name in names:
        value: Any = None
        if isinstance(metadata, dict):
            value = metadata.get(name)
        else:
            value = getattr(metadata, name, None)
        try:
            if value is not None:
                return max(0, int(value))
        except (TypeError, ValueError):
            continue
    return 0


def _nested_usage_value(metadata: Any, field: str, *names: str) -> int:
    if isinstance(metadata, dict):
        nested = metadata.get(field)
    else:
        nested = getattr(metadata, field, None)
    return _usage_value(nested, *names)


def usage_from_openai_response(response: Any, model: str, mode: str = "unknown") -> UsageRecord:
    """Extract usage metadata from an OpenAI Responses API response."""

    usage = getattr(response, "usage", None)
    input_tokens = _usage_value(usage, "input_tokens", "prompt_tokens")
    output_tokens = _usage_value(usage, "output_tokens", "completion_tokens")
    thinking = _nested_usage_value(
        usage,
        "output_tokens_details",
        "reasoning_tokens",
        "thinking_tokens",
    )
    cached = _nested_usage_value(
        usage,
        "input_tokens_details",
        "cached_tokens",
        "cached_input_tokens",
    )
    total = _usage_value(usage, "total_tokens")
    if not output_tokens and total:
        output_tokens = max(0, total - input_tokens)
    if not total:
        total = input_tokens + output_tokens

    cost, known, note = estimate_cost(
        model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_input_tokens=cached,
    )
    return UsageRecord(
        model=normalize_model(model),
        mode=str(mode or "unknown"),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        thinking_tokens=thinking,
        cached_input_tokens=cached,
        total_tokens=total,
        estimated_cost_usd=cost,
        pricing_known=known,
        pricing_note=note,
    )


def usage_from_response(response: Any, model: str, mode: str = "unknown") -> UsageRecord:
    """Extract usage metadata from Gemini or OpenAI responses defensively."""

    metadata = getattr(response, "usage_metadata", None)
    if metadata is None and getattr(response, "usage", None) is not None:
        return usage_from_openai_response(response, model, mode=mode)
    input_tokens = _usage_value(metadata, "prompt_token_count", "input_token_count")
    candidates = _usage_value(metadata, "candidates_token_count", "output_token_count")
    thinking = _usage_value(metadata, "thoughts_token_count", "thinking_token_count")
    cached = _usage_value(metadata, "cached_content_token_count", "cached_input_token_count")
    total = _usage_value(metadata, "total_token_count")

    # Gemini reports thinking separately on thinking models. Output pricing
    # includes those tokens, so include them unless the SDK has already folded
    # them into candidates_token_count.
    output_tokens = candidates + thinking if thinking else candidates
    if not output_tokens and total:
        output_tokens = max(0, total - input_tokens)
    if not total:
        total = input_tokens + output_tokens

    cost, known, note = estimate_cost(
        model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_input_tokens=cached,
    )
    return UsageRecord(
        model=normalize_model(model),
        mode=str(mode or "unknown"),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        thinking_tokens=thinking,
        cached_input_tokens=cached,
        total_tokens=total,
        estimated_cost_usd=cost,
        pricing_known=known,
        pricing_note=note,
    )


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def estimate_plan_margin(
    *,
    plan_id: str,
    monthly_price_usd: float,
    monthly_calls: int,
    model: str,
    average_input_tokens_per_call: int,
    average_output_tokens_per_call: int,
    fixed_cost_per_user_usd: float = 0.0,
) -> MarginEstimate:
    """Project gross margin for a plan using configurable usage assumptions."""

    per_call, _, _ = estimate_cost(
        model,
        average_input_tokens_per_call,
        average_output_tokens_per_call,
    )
    monthly_api_cost = max(0, int(monthly_calls)) * per_call
    price = _number(monthly_price_usd)
    fixed = max(0.0, _number(fixed_cost_per_user_usd))
    gross_profit = price - monthly_api_cost - fixed
    margin = (gross_profit / price * 100.0) if price > 0 else None
    return MarginEstimate(
        plan_id=str(plan_id),
        monthly_price_usd=price,
        monthly_calls=max(0, int(monthly_calls)),
        model=normalize_model(model),
        average_input_tokens_per_call=max(0, int(average_input_tokens_per_call)),
        average_output_tokens_per_call=max(0, int(average_output_tokens_per_call)),
        monthly_api_cost_usd=monthly_api_cost,
        fixed_cost_per_user_usd=fixed,
        gross_profit_usd=gross_profit,
        gross_margin_percent=margin,
    )

def _openai_route_models() -> dict[str, str]:
    return {
        "general": os.getenv("OPENAI_GENERAL_MODEL")
        or os.getenv("OPENAI_MODEL")
        or "gpt-5-nano",
        "math": os.getenv("OPENAI_MATH_MODEL")
        or os.getenv("OPENAI_MODEL")
        or "gpt-5-mini",
    }


def _component_route_models(
    component_id: str,
    plan_id: str,
    configured: dict[str, str],
) -> dict[str, str]:
    normalized = normalize_model(component_id)
    if plan_id == "study_tools" or normalized in {"study", "study_tools"}:
        return _openai_route_models()
    return configured


def _normalise_usage_components(
    merged: dict[str, Any],
    plan_id: str,
    configured: dict[str, str],
) -> None:
    raw_components = merged.get("usage_components")
    if not isinstance(raw_components, list):
        return

    components: list[dict[str, Any]] = []
    for index, raw_component in enumerate(raw_components):
        if not isinstance(raw_component, dict):
            continue
        component = dict(raw_component)
        component_id = str(
            component.get("id")
            or component.get("key")
            or f"{plan_id}_component_{index + 1}",
        ).strip()
        route_defaults = _component_route_models(component_id, plan_id, configured)
        component["id"] = component_id
        component["label"] = str(
            component.get("label")
            or component.get("name")
            or component_id.replace("_", " ").title(),
        )
        component["monthly_general_calls"] = max(0, int(_number(
            component.get("monthly_general_calls", component.get("general_calls", 0)),
        )))
        component["monthly_math_calls"] = max(0, int(_number(
            component.get("monthly_math_calls", component.get("math_calls", 0)),
        )))
        component["general_model"] = str(
            component.get("general_model")
            or route_defaults["general"],
        )
        component["math_model"] = str(
            component.get("math_model")
            or route_defaults["math"],
        )
        for field in (
            "general_input_tokens_per_call",
            "general_output_tokens_per_call",
            "math_input_tokens_per_call",
            "math_output_tokens_per_call",
        ):
            component[field] = max(0, int(_number(
                component.get(field, merged.get(field)),
            )))
        components.append(component)

    if not components:
        merged.pop("usage_components", None)
        return

    merged["usage_components"] = components
    merged["monthly_general_calls"] = sum(
        int(component["monthly_general_calls"]) for component in components
    )
    merged["monthly_math_calls"] = sum(
        int(component["monthly_math_calls"]) for component in components
    )
    general_models = list(dict.fromkeys(
        normalize_model(str(component["general_model"])) for component in components
    ))
    math_models = list(dict.fromkeys(
        normalize_model(str(component["math_model"])) for component in components
    ))
    merged["general_model"] = general_models[0] if len(general_models) == 1 else "mixed"
    merged["math_model"] = math_models[0] if len(math_models) == 1 else "mixed"

def configured_models() -> dict[str, str]:
    """Return the effective General and Math models used by the agent."""

    if _configured_provider() == "openai":
        return (
            {
                "general": os.getenv("OPENAI_GENERAL_MODEL")
                or os.getenv("OPENAI_MODEL")
                or "gpt-5-nano",
                "math": os.getenv("OPENAI_MATH_MODEL")
                or os.getenv("OPENAI_MODEL")
                or "gpt-5-mini",
            }
        )
    return {
        "general": os.getenv("GEMINI_GENERAL_MODEL")
        or os.getenv("GEMINI_MODEL")
        or "gemini-3.5-flash",
        "math": os.getenv("GEMINI_MATH_MODEL") or "gemini-3.7-flash",
    }


def _default_configured_model() -> str:
    return configured_models()["general"]


def _first_value(plan: dict[str, Any], *names: str, default: Any = None) -> Any:
    for name in names:
        if name in plan and plan[name] is not None:
            return plan[name]
    return default


def _normalise_tier_assumptions(
    plan: dict[str, Any],
    index: int,
    models: dict[str, str],
) -> dict[str, Any]:
    """Fill defaults and accept both the new blended schema and the old one."""

    plan_id = str(plan.get("id") or f"plan_{index + 1}").strip() or f"plan_{index + 1}"
    defaults_by_id = {str(item["id"]): item for item in DEFAULT_TIER_ASSUMPTIONS}
    merged = dict(defaults_by_id.get(plan_id, {
        "id": plan_id,
        "name": plan_id.replace("_", " ").title(),
        "description": "Configured Viszmo subscription tier.",
        "monthly_price_usd": 0.0,
        "monthly_general_calls": 0,
        "monthly_math_calls": 0,
        "fixed_cost_per_user_usd": 0.0,
        **DEFAULT_SHARED_USAGE_ASSUMPTIONS,
    }))
    merged.update(plan)
    merged["id"] = plan_id
    merged["name"] = str(merged.get("name") or plan_id.replace("_", " ").title())
    merged["description"] = str(merged.get("description") or "Configured Viszmo subscription tier.")

    blended_keys = {
        "monthly_general_calls",
        "general_calls",
        "monthly_math_calls",
        "math_calls",
        "general_model",
        "math_model",
        "usage_components",
    }
    has_blended_values = bool(blended_keys.intersection(plan))
    if not has_blended_values and "monthly_calls" in plan:
        # Preserve the previous one-model VISZMO_PLANS_JSON format.  A legacy
        # model is assigned to the active route when its mode is identifiable;
        # otherwise it is treated as General for a conservative migration.
        legacy_model = str(plan.get("model") or models["general"])
        legacy_mode = str(plan.get("mode") or "").strip().lower()
        is_math = legacy_mode == "math" or normalize_model(legacy_model) == normalize_model(models["math"])
        legacy_calls = max(0, int(_number(plan.get("monthly_calls"))))
        merged["monthly_general_calls"] = 0 if is_math else legacy_calls
        merged["monthly_math_calls"] = legacy_calls if is_math else 0
        merged["general_model"] = models["general"]
        merged["math_model"] = legacy_model if is_math else models["math"]
        legacy_input = max(0, int(_number(plan.get("average_input_tokens_per_call"))))
        legacy_output = max(0, int(_number(plan.get("average_output_tokens_per_call"))))
        if is_math:
            merged["math_input_tokens_per_call"] = legacy_input
            merged["math_output_tokens_per_call"] = legacy_output
        else:
            merged["general_input_tokens_per_call"] = legacy_input
            merged["general_output_tokens_per_call"] = legacy_output
        if "operational_buffer_percent" not in plan and "retry_buffer_percent" not in plan:
            merged["operational_buffer_percent"] = 0.0
    else:
        merged["monthly_general_calls"] = max(0, int(_number(
            _first_value(plan, "monthly_general_calls", "general_calls", default=merged.get("monthly_general_calls")),
        )))
        merged["monthly_math_calls"] = max(0, int(_number(
            _first_value(plan, "monthly_math_calls", "math_calls", default=merged.get("monthly_math_calls")),
        )))
        merged["general_model"] = str(plan.get("general_model") or merged.get("general_model") or models["general"])
        merged["math_model"] = str(plan.get("math_model") or merged.get("math_model") or models["math"])

    if "retry_buffer_percent" in plan and "operational_buffer_percent" not in plan:
        merged["operational_buffer_percent"] = plan["retry_buffer_percent"]
    if "usage_components" in plan:
        _normalise_usage_components(merged, plan_id, models)
    else:
        merged.pop("usage_components", None)
    return merged


def _load_tier_assumptions() -> tuple[list[dict[str, Any]], str, str | None]:
    """Load tier assumptions, falling back safely to the built-in proposal."""

    raw = os.getenv("VISZMO_PLANS_JSON", "").strip()
    if not raw:
        return [dict(item) for item in DEFAULT_TIER_ASSUMPTIONS], "built-in defaults", None
    try:
        plans = json.loads(raw)
    except json.JSONDecodeError:
        return (
            [dict(item) for item in DEFAULT_TIER_ASSUMPTIONS],
            "built-in defaults",
            "VISZMO_PLANS_JSON is not valid JSON; using the built-in tier proposal.",
        )
    if not isinstance(plans, list):
        return (
            [dict(item) for item in DEFAULT_TIER_ASSUMPTIONS],
            "built-in defaults",
            "VISZMO_PLANS_JSON must contain a JSON list; using the built-in tier proposal.",
        )

    models = configured_models()
    normalized = [
        _normalise_tier_assumptions(plan, index, models)
        for index, plan in enumerate(plans)
        if isinstance(plan, dict)
    ]
    if not normalized:
        return (
            [dict(item) for item in DEFAULT_TIER_ASSUMPTIONS],
            "built-in defaults",
            "VISZMO_PLANS_JSON did not contain any tier objects; using the built-in tier proposal.",
        )
    return normalized, "VISZMO_PLANS_JSON", None


def estimate_tier_margin(
    *,
    plan_id: str,
    plan_name: str,
    description: str,
    monthly_price_usd: float,
    monthly_general_calls: int,
    monthly_math_calls: int,
    general_model: str,
    math_model: str,
    average_general_input_tokens_per_call: int,
    average_general_output_tokens_per_call: int,
    average_math_input_tokens_per_call: int,
    average_math_output_tokens_per_call: int,
    fixed_cost_per_user_usd: float = 0.0,
    operational_buffer_percent: float = 0.0,
    usage_components: Iterable[dict[str, Any]] | None = None,
) -> TierMarginEstimate:
    """Estimate COGS and gross margin for a blended General/Math usage scenario."""

    general_input = max(0, int(average_general_input_tokens_per_call))
    general_output = max(0, int(average_general_output_tokens_per_call))
    math_input = max(0, int(average_math_input_tokens_per_call))
    math_output = max(0, int(average_math_output_tokens_per_call))
    raw_components = list(usage_components or [])
    if not raw_components:
        raw_components = [{
            "id": str(plan_id),
            "label": str(plan_name),
            "monthly_general_calls": monthly_general_calls,
            "monthly_math_calls": monthly_math_calls,
            "general_model": general_model,
            "math_model": math_model,
            "general_input_tokens_per_call": general_input,
            "general_output_tokens_per_call": general_output,
            "math_input_tokens_per_call": math_input,
            "math_output_tokens_per_call": math_output,
        }]

    general_calls = 0
    math_calls = 0
    general_api_cost = 0.0
    math_api_cost = 0.0
    pricing_known = True
    notes: list[str] = []
    model_mix: list[dict[str, Any]] = []
    for index, component in enumerate(raw_components):
        if not isinstance(component, dict):
            continue
        component_general_calls = max(0, int(_number(
            component.get("monthly_general_calls", component.get("general_calls", 0)),
        )))
        component_math_calls = max(0, int(_number(
            component.get("monthly_math_calls", component.get("math_calls", 0)),
        )))
        component_general_model = str(component.get("general_model") or general_model)
        component_math_model = str(component.get("math_model") or math_model)
        component_general_input = max(0, int(_number(
            component.get("general_input_tokens_per_call", general_input),
        )))
        component_general_output = max(0, int(_number(
            component.get("general_output_tokens_per_call", general_output),
        )))
        component_math_input = max(0, int(_number(
            component.get("math_input_tokens_per_call", math_input),
        )))
        component_math_output = max(0, int(_number(
            component.get("math_output_tokens_per_call", math_output),
        )))
        component_general_per_call, component_general_known, component_general_note = estimate_cost(
            component_general_model,
            component_general_input,
            component_general_output,
        )
        component_math_per_call, component_math_known, component_math_note = estimate_cost(
            component_math_model,
            component_math_input,
            component_math_output,
        )
        component_general_api_cost = component_general_calls * component_general_per_call
        component_math_api_cost = component_math_calls * component_math_per_call
        general_calls += component_general_calls
        math_calls += component_math_calls
        general_api_cost += component_general_api_cost
        math_api_cost += component_math_api_cost
        pricing_known = pricing_known and (
            (not component_general_calls or component_general_known)
            and (not component_math_calls or component_math_known)
        )
        if component_general_calls and component_general_note not in notes:
            notes.append(component_general_note)
        if component_math_calls and component_math_note not in notes:
            notes.append(component_math_note)
        model_mix.append({
            "id": str(component.get("id") or f"{plan_id}_component_{index + 1}"),
            "label": str(component.get("label") or component.get("name") or "Component"),
            "general_model": normalize_model(component_general_model),
            "math_model": normalize_model(component_math_model),
            "monthly_general_calls": component_general_calls,
            "monthly_math_calls": component_math_calls,
            "general_api_cost_usd": component_general_api_cost,
            "math_api_cost_usd": component_math_api_cost,
        })

    base_api_cost = general_api_cost + math_api_cost
    buffer_percent = max(0.0, _number(operational_buffer_percent))
    buffer_cost = base_api_cost * buffer_percent / 100.0
    monthly_api_cost = base_api_cost + buffer_cost
    price = max(0.0, _number(monthly_price_usd))
    fixed = max(0.0, _number(fixed_cost_per_user_usd))
    total_cogs = monthly_api_cost + fixed
    gross_profit = price - total_cogs
    margin = (gross_profit / price * 100.0) if price > 0 else None
    return TierMarginEstimate(
        plan_id=str(plan_id),
        plan_name=str(plan_name),
        description=str(description),
        monthly_price_usd=price,
        monthly_general_calls=general_calls,
        monthly_math_calls=math_calls,
        monthly_calls=general_calls + math_calls,
        general_model=normalize_model(general_model),
        math_model=normalize_model(math_model),
        average_general_input_tokens_per_call=general_input,
        average_general_output_tokens_per_call=general_output,
        average_math_input_tokens_per_call=math_input,
        average_math_output_tokens_per_call=math_output,
        general_api_cost_usd=general_api_cost,
        math_api_cost_usd=math_api_cost,
        base_api_cost_usd=base_api_cost,
        operational_buffer_percent=buffer_percent,
        operational_buffer_cost_usd=buffer_cost,
        monthly_api_cost_usd=monthly_api_cost,
        fixed_cost_per_user_usd=fixed,
        total_cogs_usd=total_cogs,
        gross_profit_usd=gross_profit,
        gross_margin_percent=margin,
        pricing_known=pricing_known,
        pricing_notes=tuple(notes),
        model_mix=tuple(model_mix),
    )


def _estimate_configured_tier(plan: dict[str, Any], models: dict[str, str]) -> TierMarginEstimate:
    plan = _normalise_tier_assumptions(plan, 0, models)
    return estimate_tier_margin(
        plan_id=str(plan.get("id") or "plan"),
        plan_name=str(plan.get("name") or plan.get("id") or "Plan"),
        description=str(plan.get("description") or ""),
        monthly_price_usd=_number(plan.get("monthly_price_usd")),
        monthly_general_calls=int(_number(plan.get("monthly_general_calls"))),
        monthly_math_calls=int(_number(plan.get("monthly_math_calls"))),
        general_model=str(plan.get("general_model") or models["general"]),
        math_model=str(plan.get("math_model") or models["math"]),
        average_general_input_tokens_per_call=int(_number(
            plan.get("general_input_tokens_per_call"),
        )),
        average_general_output_tokens_per_call=int(_number(
            plan.get("general_output_tokens_per_call"),
        )),
        average_math_input_tokens_per_call=int(_number(
            plan.get("math_input_tokens_per_call"),
        )),
        average_math_output_tokens_per_call=int(_number(
            plan.get("math_output_tokens_per_call"),
        )),
        fixed_cost_per_user_usd=_number(plan.get("fixed_cost_per_user_usd")),
        operational_buffer_percent=_number(
            _first_value(
                plan,
                "operational_buffer_percent",
                "retry_buffer_percent",
                default=DEFAULT_SHARED_USAGE_ASSUMPTIONS["operational_buffer_percent"],
            ),
        ),
        usage_components=plan.get("usage_components"),
    )


def configured_plan_margins() -> list[dict[str, Any]]:
    """Return the three default or environment-configured tier estimates."""

    assumptions, _, _ = _load_tier_assumptions()
    models = configured_models()
    return [_estimate_configured_tier(plan, models).as_dict() for plan in assumptions]


def pricing_snapshot() -> dict[str, Any]:
    """Return a JSON-safe snapshot for the local diagnostics endpoint."""

    models: list[dict[str, Any]] = []
    for model in MODEL_PRICES:
        price = price_for_model(model)
        item = asdict(price)
        item["provider"] = provider_for_model(model)
        item["promotion_active"] = _promotion_active(model)
        models.append(item)
    provider = _configured_provider()
    configured = configured_models()
    configured_prices: dict[str, dict[str, Any]] = {}
    for role, model in configured.items():
        price = price_for_model(model)
        item = asdict(price)
        item["provider"] = provider_for_model(model)
        item["promotion_active"] = _promotion_active(model)
        configured_prices[role] = item

    assumptions, assumptions_source, assumptions_error = _load_tier_assumptions()
    plan_margins = [
        _estimate_configured_tier(plan, configured).as_dict()
        for plan in assumptions
    ]
    return {
        "pricing_as_of": PRICING_AS_OF,
        "currency": "USD",
        "provider": provider,
        "recommended_routing": configured,
        "configured_models": configured,
        "configured_model_prices": configured_prices,
        "models": models,
        "plans": plan_margins,
        "plan_margins": plan_margins,
        "plan_assumptions": assumptions,
        "plan_assumptions_source": assumptions_source,
        "plan_assumptions_error": assumptions_error,
        "plan_margins_configured": bool(os.getenv("VISZMO_PLANS_JSON", "").strip()),
        "token_assumptions": dict(DEFAULT_SHARED_USAGE_ASSUMPTIONS),
        "usage_logging": {
            "usage_unit": "model_turn",
            "definition": "One successful provider response recorded by cost_model.usage_from_response.",
            "event_types": ["usage", "usage_summary"],
        },
    }


def sum_usage(records: Iterable[UsageRecord]) -> dict[str, Any]:
    """Aggregate usage records for a task summary."""

    items = list(records)
    return {
        "calls": len(items),
        "input_tokens": sum(item.input_tokens for item in items),
        "output_tokens": sum(item.output_tokens for item in items),
        "thinking_tokens": sum(item.thinking_tokens for item in items),
        "total_tokens": sum(item.total_tokens for item in items),
        "estimated_cost_usd": round(sum(item.estimated_cost_usd for item in items), 8),
        "pricing_known": all(item.pricing_known for item in items) if items else True,
        "models": sorted({item.model for item in items}),
    }
