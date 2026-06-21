from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .models import ProbeCase, ProbeResult
from .providers import ProviderClient
from .usage_metrics import LatencyStats, has_cache_metric, parse_usage, usage_to_metrics


@dataclass(slots=True)
class CacheIntegrityProbe:
    name: str = "cache_integrity"
    repeats: int = 4

    def cases(self) -> list[ProbeCase]:
        stable_prefix = "\n".join(
            f"REFERENCE-{i:04d}: This repeated document line is stable for prompt-cache measurement."
            for i in range(1000)
        )
        return [
            ProbeCase(
                id="cache-integrity-repeat-1",
                kind="cache",
                prompt=(
                    "Use the reference document below only.\n\n"
                    f"{stable_prefix}\n\n"
                    "The verification marker is API-PROBE-CACHE-739251. "
                    "Reply with the verification marker only."
                ),
                expected={"equals": "API-PROBE-CACHE-739251"},
                request_options={"cache_control": True, "max_tokens": 64},
                repeat=self.repeats,
            )
        ]

    def run(self, client: ProviderClient) -> list[ProbeResult]:
        case = self.cases()[0]
        responses = []
        failures = []
        for attempt in range(max(2, case.repeat)):
            try:
                response = client.complete(case.prompt, **case.request_options)
            except Exception as exc:
                failures.append({"attempt": attempt + 1, "error": f"{type(exc).__name__}: {exc}"})
                continue
            responses.append(response)

        if failures and not responses:
            return [
                ProbeResult(
                    case_id=case.id,
                    kind=case.kind,
                    status="failed",
                    passed=False,
                    score=0.0,
                    evidence=failures[0]["error"],
                    failure_category="transport",
                    metrics={"failures": failures},
                    raw_response={"request": _request_snapshot(client, case.prompt, case.request_options), "failures": failures},
                )
            ]

        expected = case.expected["equals"]
        texts = [response.text.strip() for response in responses]
        answer_ok = all(text == expected for text in texts)
        consistent = len(set(texts)) <= 1
        usage_items = [parse_usage(response.usage) for response in responses]
        cached_values = [item.cached_tokens or 0 for item in usage_items]
        creation_values = [item.cache_creation_tokens or 0 for item in usage_items]
        cache_metric_seen = any(has_cache_metric(response.usage) for response in responses)
        cache_hit_seen = any(value > 0 for value in cached_values[1:])
        latency = LatencyStats(tuple(response.latency_ms for response in responses))
        latency_drop = latency.drop_ratio
        latency_supports_cache = latency_drop is not None and latency_drop >= 0.10
        passed = answer_ok and consistent and (cache_hit_seen or latency_supports_cache)
        score = sum(
            [
                0.35 if answer_ok else 0.0,
                0.20 if consistent else 0.0,
                0.30 if cache_hit_seen else 0.0,
                0.15 if latency_supports_cache else 0.0,
            ]
        )

        return [
            ProbeResult(
                case_id=case.id,
                kind=case.kind,
                status="passed" if passed else "failed",
                passed=passed,
                score=round(score, 2),
                evidence=(
                    f"answers_ok={answer_ok} consistent={consistent} "
                    f"cached_tokens={cached_values} latency_drop={_pct(latency_drop)}"
                ),
                failure_category=None if passed else "cache",
                metrics={
                    "attempts": len(responses),
                    "failures": failures,
                    "cached_tokens_by_attempt": cached_values,
                    "cache_creation_tokens_by_attempt": creation_values,
                    "cache_metric_seen": cache_metric_seen,
                    "cache_hit_seen": cache_hit_seen,
                    "latencies_ms": [round(response.latency_ms, 2) for response in responses],
                    "latency_drop_ratio": latency_drop,
                    "latency_cv": latency.coefficient_of_variation,
                    "usage": responses[-1].usage if responses else {},
                    "usage_by_attempt": [usage_to_metrics(response.usage) for response in responses],
                    "response_model": responses[-1].raw.get("model") if responses else None,
                },
                raw_response=responses[-1].raw if responses else {},
            )
        ]

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
        stable_prefix = "\n".join(
            f"REFERENCE-{i:04d}: This repeated document line is stable for prompt-cache measurement."
            for i in range(900)
        )
        return [
            ProbeCase(
                id="cache-nonce-1",
                kind="cache",
                prompt=(
                    "Use the reference document below only.\n\n"
                    f"{stable_prefix}\n\n"
                    "The verification marker is API-PROBE-739251. "
                    "Reply with the verification marker only."
                ),
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
