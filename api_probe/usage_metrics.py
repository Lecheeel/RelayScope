from __future__ import annotations

from dataclasses import dataclass
from statistics import mean, pstdev
from typing import Any


@dataclass(frozen=True, slots=True)
class UsageMetrics:
    input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_tokens: int | None = None
    cached_tokens: int | None = None
    cache_creation_tokens: int | None = None
    total_tokens: int | None = None

    @property
    def billable_input_tokens(self) -> int | None:
        if self.input_tokens is None:
            return None
        cached = self.cached_tokens if isinstance(self.cached_tokens, int) else 0
        return max(0, self.input_tokens - cached)

    @property
    def cache_hit_rate(self) -> float | None:
        total = sum(
            value
            for value in (self.input_tokens, self.cached_tokens, self.cache_creation_tokens)
            if isinstance(value, int)
        )
        if total <= 0 or self.cached_tokens is None:
            return None
        return self.cached_tokens / total


@dataclass(frozen=True, slots=True)
class LatencyStats:
    values_ms: tuple[float, ...]

    @property
    def first_ms(self) -> float | None:
        return self.values_ms[0] if self.values_ms else None

    @property
    def mean_ms(self) -> float | None:
        return mean(self.values_ms) if self.values_ms else None

    @property
    def repeat_mean_ms(self) -> float | None:
        return mean(self.values_ms[1:]) if len(self.values_ms) > 1 else None

    @property
    def drop_ratio(self) -> float | None:
        if self.first_ms is None or self.repeat_mean_ms is None or self.first_ms <= 0:
            return None
        return max(0.0, (self.first_ms - self.repeat_mean_ms) / self.first_ms)

    @property
    def coefficient_of_variation(self) -> float | None:
        if len(self.values_ms) < 2:
            return None
        avg = mean(self.values_ms)
        if avg <= 0:
            return None
        return pstdev(self.values_ms) / avg


def parse_usage(usage: dict[str, Any] | None) -> UsageMetrics:
    usage = usage or {}
    input_tokens = _first_int(usage, "prompt_tokens", "input_tokens")
    output_tokens = _first_int(usage, "completion_tokens", "output_tokens")
    total_tokens = _first_int(usage, "total_tokens")

    prompt_details = usage.get("prompt_tokens_details")
    completion_details = usage.get("completion_tokens_details")
    output_details = usage.get("output_tokens_details")

    cached_tokens = _first_nested_int(prompt_details, "cached_tokens", "cache_read_tokens")
    if cached_tokens is None:
        cached_tokens = _first_int(usage, "cache_read_input_tokens", "cached_tokens")

    cache_creation_tokens = _first_int(usage, "cache_creation_input_tokens")
    if cache_creation_tokens is None:
        cache_creation_tokens = _first_nested_int(prompt_details, "cache_creation_tokens")

    reasoning_tokens = _first_nested_int(completion_details, "reasoning_tokens")
    if reasoning_tokens is None:
        reasoning_tokens = _first_nested_int(output_details, "reasoning_tokens")
    if reasoning_tokens is None:
        reasoning_tokens = _first_int(usage, "reasoning_tokens")

    return UsageMetrics(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        reasoning_tokens=reasoning_tokens,
        cached_tokens=cached_tokens,
        cache_creation_tokens=cache_creation_tokens,
        total_tokens=total_tokens,
    )


def usage_to_metrics(usage: dict[str, Any] | None) -> dict[str, Any]:
    parsed = parse_usage(usage)
    return {
        "input_tokens": parsed.input_tokens,
        "output_tokens": parsed.output_tokens,
        "reasoning_tokens": parsed.reasoning_tokens,
        "cached_tokens": parsed.cached_tokens,
        "cache_creation_tokens": parsed.cache_creation_tokens,
        "total_tokens": parsed.total_tokens,
        "billable_input_tokens": parsed.billable_input_tokens,
        "cache_hit_rate": None if parsed.cache_hit_rate is None else round(parsed.cache_hit_rate * 100, 2),
    }


def has_cache_metric(usage: dict[str, Any] | None) -> bool:
    usage = usage or {}
    if any(key in usage for key in ("cache_read_input_tokens", "cache_creation_input_tokens", "cached_tokens")):
        return True
    details = usage.get("prompt_tokens_details")
    return isinstance(details, dict) and any(
        key in details for key in ("cached_tokens", "cache_creation_tokens", "cache_read_tokens")
    )


def _first_int(source: dict[str, Any], *keys: str) -> int | None:
    for key in keys:
        value = source.get(key)
        if isinstance(value, int):
            return value
    return None


def _first_nested_int(source: Any, *keys: str) -> int | None:
    if not isinstance(source, dict):
        return None
    return _first_int(source, *keys)
