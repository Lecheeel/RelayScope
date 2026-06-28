from __future__ import annotations

from api_probe.cache_probes import _build_prompt
from api_probe.usage_metrics import UsageMetrics
from api_probe.web import _cache_usage_note, _cache_usage_status


def test_usage_metrics_cache_hit_rate_uses_input_tokens() -> None:
    metrics = UsageMetrics(input_tokens=100, cached_tokens=25, cache_creation_tokens=40)
    assert metrics.cache_hit_rate == 0.25


def test_build_prompt_includes_nonce_only_for_control_run() -> None:
    cached_prompt = _build_prompt("BASE", None)
    nonce_prompt = _build_prompt("BASE", 7)

    assert "NONCE-" not in cached_prompt
    assert "NONCE-0007" in nonce_prompt
    assert "BASE" in cached_prompt
    assert "BASE" in nonce_prompt


def test_cache_usage_status_distinguishes_missing_cache_metrics() -> None:
    assert _cache_usage_status(cache_samples=2, cache_metric_samples=0) == "not_reported"
    assert "没有透传" in _cache_usage_note("not_reported")


def test_cache_usage_status_reports_partial_and_full_metrics() -> None:
    assert _cache_usage_status(cache_samples=3, cache_metric_samples=1) == "partial"
    assert _cache_usage_status(cache_samples=3, cache_metric_samples=3) == "reported"
    assert _cache_usage_status(cache_samples=0, cache_metric_samples=0) == "no_usage_samples"
