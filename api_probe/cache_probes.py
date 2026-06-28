from __future__ import annotations

from dataclasses import dataclass
from itertools import count
from time import sleep
from typing import Any

from .models import ProbeCase, ProbeResult
from .providers import ProviderClient
from .usage_metrics import LatencyStats, has_cache_metric, parse_usage, usage_to_metrics


@dataclass(slots=True)
class CacheIntegrityProbe:
    name: str = "cache_integrity"
    repeats: int = 4

    def cases(self) -> list[ProbeCase]:
        return [
            ProbeCase(
                id="cache-integrity-repeat-1",
                kind="cache",
                prompt="",
                expected={"equals": "API-PROBE-CACHE-739251"},
                request_options={"cache_control": True, "max_tokens": 64},
                repeat=self.repeats,
            )
        ]

    def run(self, client: ProviderClient) -> list[ProbeResult]:
        case = self.cases()[0]
        cached = _run_attempts(client, case, request_options=case.request_options, nonce_mode=False)
        nonce = _run_attempts(
            client,
            case,
            request_options={k: v for k, v in case.request_options.items() if k != "cache_control"},
            nonce_mode=True,
        )
        return [_merge_cache_results(case.id, cached, nonce)]

    def grade(self, case: ProbeCase, response_text: str, raw: dict[str, Any]) -> ProbeResult:
        normalized = response_text.strip()
        passed = normalized == case.expected["equals"]
        usage = raw.get("usage", {}) if isinstance(raw, dict) else {}
        parsed = parse_usage(usage)
        return ProbeResult(
            case_id=case.id,
            kind=case.kind,
            passed=passed,
            score=1.0 if passed else 0.0,
            evidence=normalized[:500],
            metrics={
                "cached_tokens": parsed.cached_tokens,
                "cache_creation_tokens": parsed.cache_creation_tokens,
                "cache_hit_rate": None if parsed.cache_hit_rate is None else round(parsed.cache_hit_rate * 100, 2),
                "usage": usage,
            },
            raw_response=raw,
        )


@dataclass(slots=True)
class CacheNonceProbe(CacheIntegrityProbe):
    name: str = "cache_nonce"

    def cases(self) -> list[ProbeCase]:
        return [
            ProbeCase(
                id="cache-nonce-1",
                kind="cache",
                prompt="",
                expected={"equals": "API-PROBE-739251"},
                request_options={"cache_control": True},
                repeat=3,
            )
        ]


def _pct(value: float | None) -> str:
    return "unknown" if value is None else f"{value * 100:.1f}%"


def _request_snapshot(client: ProviderClient, prompt: str, request_options: dict[str, Any]) -> dict[str, Any]:
    config = getattr(client, "config", None)
    base_url = getattr(config, "base_url", "")
    protocol = getattr(getattr(config, "protocol_mode", None), "value", None)
    endpoint = "/responses" if protocol == "openai_responses" else "/chat/completions"
    return {
        "method": "POST",
        "url": base_url.rstrip("/") + endpoint,
        "json": {
            "model": getattr(config, "model", None),
            "prompt": prompt,
            **request_options,
        },
    }


def _run_attempts(
    client: ProviderClient,
    case: ProbeCase,
    *,
    request_options: dict[str, Any],
    nonce_mode: bool,
) -> dict[str, Any]:
    stable_prefix = "\n".join(
        f"REFERENCE-{i:04d}: This repeated document line is stable for prompt-cache measurement."
        for i in range(1000)
    )
    responses = []
    failures = []
    nonce_seed = count(1)
    prompt = _build_prompt(stable_prefix, next(nonce_seed) if nonce_mode else None)
    for attempt in range(max(2, case.repeat)):
        if attempt > 0:
            sleep(_cache_delay(client))
        current_prompt = _build_prompt(stable_prefix, next(nonce_seed) if nonce_mode else None)
        current_options = dict(request_options)
        if nonce_mode:
            current_options["prompt_cache_key"] = f"cache-nonce-{attempt + 1}"
            current_prompt = f"{current_prompt}\n\nNONCE: {attempt + 1}"
        try:
            response = client.complete(current_prompt, **current_options)
        except Exception as exc:
            failures.append({"attempt": attempt + 1, "error": f"{type(exc).__name__}: {exc}"})
            continue
        responses.append(response)

    return {"responses": responses, "failures": failures, "nonce_mode": nonce_mode}


def _build_prompt(stable_prefix: str, nonce: int | None) -> str:
    parts = [
        "Use the reference document below only.",
        "",
        stable_prefix,
    ]
    if nonce is not None:
        parts.extend(["", f"NONCE-{nonce:04d}: keep this block unique for the control run."])
    parts.extend(
        [
            "",
            "The verification marker is API-PROBE-CACHE-739251.",
            "Reply with the verification marker only.",
        ]
    )
    return "\n".join(parts)


def _merge_cache_results(case_id: str, cached_run: dict[str, Any], nonce_run: dict[str, Any]) -> ProbeResult:
    cached_results = cached_run["responses"]
    nonce_results = nonce_run["responses"]
    all_responses = cached_results + nonce_results
    if not all_responses:
        failure = (cached_run["failures"] or nonce_run["failures"] or [{"error": "No successful responses."}])[0]
        return ProbeResult(
            case_id=case_id,
            kind="cache",
            status="failed",
            passed=False,
            score=0.0,
            evidence=failure.get("error", "No successful responses."),
            failure_category="transport",
            metrics={"failures": cached_run["failures"] + nonce_run["failures"]},
            raw_response={"cached": cached_run, "nonce": nonce_run},
        )

    def _summarize(responses: list[Any]) -> dict[str, Any]:
        texts = [response.text.strip() for response in responses]
        usage_items = [parse_usage(response.usage) for response in responses]
        cached_values = [item.cached_tokens or 0 for item in usage_items]
        creation_values = [item.cache_creation_tokens or 0 for item in usage_items]
        input_values = [item.input_tokens or 0 for item in usage_items]
        cache_metric_seen = any(has_cache_metric(response.usage) for response in responses)
        cache_hit_seen = any(value > 0 for value in cached_values[1:])
        cached_input_total = sum(cached_values)
        prompt_input_total = sum(input_values)
        cache_hit_rate = (cached_input_total / prompt_input_total * 100) if prompt_input_total > 0 else None
        latency = LatencyStats(tuple(response.latency_ms for response in responses))
        return {
            "answer_ok": all(text == "API-PROBE-CACHE-739251" for text in texts),
            "consistent": len(set(texts)) <= 1,
            "cached_values": cached_values,
            "creation_values": creation_values,
            "input_values": input_values,
            "cache_metric_seen": cache_metric_seen,
            "cache_hit_seen": cache_hit_seen,
            "cache_hit_rate": None if cache_hit_rate is None else round(cache_hit_rate, 2),
            "latency_drop": latency.drop_ratio,
            "latency_cv": latency.coefficient_of_variation,
            "latencies_ms": [round(response.latency_ms, 2) for response in responses],
            "retry_counts": [response.retries for response in responses],
            "transient_failures": [failure for response in responses for failure in (response.transient_failures or [])],
            "usage": responses[-1].usage if responses else {},
            "usage_by_attempt": [usage_to_metrics(response.usage) for response in responses],
            "response_model": responses[-1].raw.get("model") if responses else None,
        }

    cached_summary = _summarize(cached_results)
    nonce_summary = _summarize(nonce_results) if nonce_results else {}
    cache_strength = 0.0
    cache_strength += 0.45 if cached_summary["cache_hit_seen"] else 0.0
    cache_strength += 0.25 if cached_summary["cache_metric_seen"] else 0.0
    cache_strength += 0.15 if cached_summary["latency_drop"] is not None and cached_summary["latency_drop"] >= 0.10 else 0.0
    cache_strength += 0.15 if nonce_summary and not nonce_summary["cache_hit_seen"] and (nonce_summary["latency_drop"] is None or nonce_summary["latency_drop"] < 0.10) else 0.0
    passed = cached_summary["answer_ok"] and cached_summary["consistent"] and cache_strength >= 0.55
    evidence = (
        f"cached_hit={cached_summary['cache_hit_seen']} cached_rate={cached_summary['cache_hit_rate']} "
        f"nonce_hit={nonce_summary.get('cache_hit_seen')} nonce_rate={nonce_summary.get('cache_hit_rate')} "
        f"cached_drop={_pct(cached_summary['latency_drop'])} nonce_drop={_pct(nonce_summary.get('latency_drop'))}"
    )
    return ProbeResult(
        case_id=case_id,
        kind="cache",
        status="passed" if passed else "failed",
        passed=passed,
        score=round(cache_strength, 2),
        evidence=evidence,
        failure_category=None if passed else "cache",
        metrics={
            "cached": cached_summary,
            "nonce": nonce_summary,
            "cache_strength": round(cache_strength, 2),
        },
        raw_response={"cached": cached_run, "nonce": nonce_run},
    )


def _cache_delay(client: ProviderClient) -> float:
    config = getattr(client, "config", None)
    value = getattr(config, "cache_probe_delay_seconds", 1.2)
    try:
        return max(0.0, min(5.0, float(value)))
    except (TypeError, ValueError):
        return 1.2
