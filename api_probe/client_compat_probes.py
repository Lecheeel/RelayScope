from __future__ import annotations

from dataclasses import dataclass
from time import perf_counter
from typing import Any
import uuid

import httpx

from .config import ClientProfile
from .models import ProbeResult
from .probes import _categorize_exception
from .providers import ProviderClient


@dataclass(slots=True)
class ClientCompatibilityProbe:
    name: str = "client_compatibility"

    def run(self, client: ProviderClient) -> list[ProbeResult]:
        config = getattr(client, "config", None)
        profile = getattr(getattr(config, "client_profile", None), "value", None)
        if profile == ClientProfile.CLAUDE_CODE.value:
            return [
                _run_claude_count_tokens(config),
                _run_claude_model_discovery(config),
            ]
        if profile == ClientProfile.CODEX_RESPONSES.value:
            return [_run_codex_responses_stream(client)]
        return [
            ProbeResult(
                case_id="client-profile-shape-1",
                kind="client_compat",
                status="skipped",
                passed=False,
                score=0.0,
                evidence="Skipped because the current profile does not have extra client-shape checks.",
                failure_category="unsupported",
                skipped_reason=f"profile={profile or '-'}",
            )
        ]


def _run_claude_count_tokens(config: Any) -> ProbeResult:
    url = config.base_url.rstrip("/") + "/messages/count_tokens"
    payload = {
        "model": config.model,
        "messages": [
            {
                "role": "user",
                "content": "Count this short Claude Code compatibility prompt.",
            }
        ],
    }
    headers = _claude_code_headers(config)
    started = perf_counter()
    try:
        with httpx.Client(timeout=config.timeout_seconds) as client:
            response = client.post(url, headers=headers, json=payload)
            response.raise_for_status()
        latency_ms = (perf_counter() - started) * 1000
        raw = response.json()
    except Exception as exc:
        if _optional_endpoint_failure(exc):
            return ProbeResult(
                case_id="claude-code-count-tokens-1",
                kind="client_compat",
                status="skipped",
                passed=False,
                score=0.0,
                evidence=f"Optional count_tokens endpoint is not compatible: {type(exc).__name__}: {_exception_preview(exc)}",
                failure_category="unsupported",
                skipped_reason="optional count_tokens endpoint unavailable",
                metrics={"endpoint": "/messages/count_tokens", "error_type": type(exc).__name__},
                raw_response={"request": _request_snapshot("POST", url, headers, payload)},
            )
        return ProbeResult(
            case_id="claude-code-count-tokens-1",
            kind="client_compat",
            status="failed",
            passed=False,
            score=0.0,
            evidence=f"{type(exc).__name__}: {_exception_preview(exc)}",
            failure_category=_categorize_exception(exc),
            metrics={"endpoint": "/messages/count_tokens", "error_type": type(exc).__name__},
            raw_response={"request": _request_snapshot("POST", url, headers, payload)},
        )

    input_tokens = raw.get("input_tokens")
    passed = isinstance(input_tokens, int) and input_tokens > 0
    raw["_response_headers"] = dict(response.headers)
    raw["_request"] = _request_snapshot("POST", url, headers, payload)
    if not passed:
        return ProbeResult(
            case_id="claude-code-count-tokens-1",
            kind="client_compat",
            status="skipped",
            passed=False,
            score=0.0,
            evidence=f"Optional count_tokens endpoint returned unsupported shape: input_tokens={input_tokens!r}",
            failure_category="unsupported",
            skipped_reason="optional count_tokens response shape unsupported",
            metrics={
                "latency_ms": round(latency_ms, 2),
                "content_type": response.headers.get("content-type", ""),
                "endpoint": "/messages/count_tokens",
            },
            raw_response=raw,
        )
    return ProbeResult(
        case_id="claude-code-count-tokens-1",
        kind="client_compat",
        status="passed" if passed else "failed",
        passed=passed,
        score=1.0 if passed else 0.0,
        evidence=f"input_tokens={input_tokens!r}",
        failure_category=None if passed else "protocol",
        metrics={
            "latency_ms": round(latency_ms, 2),
            "content_type": response.headers.get("content-type", ""),
            "endpoint": "/messages/count_tokens",
        },
        raw_response=raw,
    )


def _run_claude_model_discovery(config: Any) -> ProbeResult:
    url = config.base_url.rstrip("/") + "/models"
    headers = _claude_code_headers(config)
    started = perf_counter()
    try:
        with httpx.Client(timeout=config.timeout_seconds) as client:
            response = client.get(url, headers=headers)
            response.raise_for_status()
        latency_ms = (perf_counter() - started) * 1000
        raw = response.json()
    except Exception as exc:
        return ProbeResult(
            case_id="claude-code-model-discovery-1",
            kind="client_compat",
            status="failed",
            passed=False,
            score=0.0,
            evidence=f"{type(exc).__name__}: {_exception_preview(exc)}",
            failure_category=_categorize_exception(exc),
            metrics={"endpoint": "/models", "error_type": type(exc).__name__},
            raw_response={"request": _request_snapshot("GET", url, headers, {})},
        )

    models = raw.get("data") if isinstance(raw, dict) else None
    passed = isinstance(models, list)
    raw["_response_headers"] = dict(response.headers)
    raw["_request"] = _request_snapshot("GET", url, headers, {})
    return ProbeResult(
        case_id="claude-code-model-discovery-1",
        kind="client_compat",
        status="passed" if passed else "failed",
        passed=passed,
        score=1.0 if passed else 0.0,
        evidence=f"model_count={len(models) if isinstance(models, list) else 'unknown'}",
        failure_category=None if passed else "protocol",
        metrics={
            "latency_ms": round(latency_ms, 2),
            "content_type": response.headers.get("content-type", ""),
            "endpoint": "/models",
        },
        raw_response=raw,
    )


def _run_codex_responses_stream(client: ProviderClient) -> ProbeResult:
    try:
        response = client.stream_complete(
            "Reply with exactly: CODEX RESPONSES STREAM OK",
            max_tokens=64,
        )
    except Exception as exc:
        return ProbeResult(
            case_id="codex-responses-stream-events-1",
            kind="client_compat",
            status="failed",
            passed=False,
            score=0.0,
            evidence=f"{type(exc).__name__}: {exc}",
            failure_category=_categorize_exception(exc),
            metrics={"error_type": type(exc).__name__},
        )

    event_types = _event_types(response.raw_events)
    has_text = "CODEX RESPONSES STREAM OK" in response.text.strip()
    has_responses_event = any(event_type.startswith("response.") for event_type in event_types)
    passed = has_text and response.chunk_count > 0 and has_responses_event
    return ProbeResult(
        case_id="codex-responses-stream-events-1",
        kind="client_compat",
        status="passed" if passed else "failed",
        passed=passed,
        score=1.0 if passed else 0.0,
        evidence=response.text.strip()[:500] if has_text else f"event_types={event_types} text={response.text[:200]!r}",
        failure_category=None if passed else "protocol",
        metrics={
            "latency_ms": round(response.latency_ms, 2),
            "ttft_ms": None if response.first_token_ms is None else round(response.first_token_ms, 2),
            "chunk_count": response.chunk_count,
            "content_type": response.content_type,
            "event_types": event_types,
            "usage": response.usage,
        },
        raw_response={"request": response.request, "events": response.raw_events[:20], "usage": response.usage},
    )


def _claude_code_headers(config: Any) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {config.api_key}",
        "Content-Type": "application/json",
        "anthropic-version": config.extra_headers.get("anthropic-version", "2023-06-01"),
        "User-Agent": "api-probe claude-code profile",
        "X-Claude-Code-Session-Id": str(uuid.uuid4()),
        "X-Claude-Code-Agent-Id": str(uuid.uuid4()),
        **config.extra_headers,
    }
    return headers


def _request_snapshot(method: str, url: str, headers: dict[str, str], payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "method": method,
        "url": url,
        "headers": {
            key: ("***REDACTED***" if key.lower() in {"authorization", "x-api-key", "api-key"} else value)
            for key, value in headers.items()
        },
        "json": payload,
    }


def _event_types(events: list[dict[str, Any]]) -> list[str]:
    seen: list[str] = []
    for event in events:
        event_type = event.get("type")
        if isinstance(event_type, str) and event_type not in seen:
            seen.append(event_type)
    return seen


def _exception_preview(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        preview = exc.response.text[:500].replace("\n", " ")
        return f"{exc} | body_preview={preview!r}"
    return str(exc)


def _optional_endpoint_failure(exc: Exception) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in {404, 405, 501}
    message = str(exc).lower()
    return "count_tokens" in message and ("not found" in message or "method not allowed" in message or "unsupported" in message)
