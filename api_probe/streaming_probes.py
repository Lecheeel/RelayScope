from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .config import ClientProfile
from .models import ProbeResult
from .probes import _categorize_exception
from .providers import ProviderClient


@dataclass(slots=True)
class StreamingLatencyProbe:
    name: str = "streaming_latency"

    def run(self, client: ProviderClient) -> list[ProbeResult]:
        config = getattr(client, "config", None)
        profile = getattr(getattr(config, "client_profile", None), "value", None)
        if profile == ClientProfile.CODEX_RESPONSES.value:
            return [
                ProbeResult(
                    case_id="stream-sse-basic-1",
                    kind="streaming",
                    status="skipped",
                    passed=False,
                    score=0.0,
                    evidence="Skipped because Codex Responses streaming is covered by client compatibility.",
                    failure_category="unsupported",
                    skipped_reason="covered by codex-responses-stream-events-1",
                )
            ]
        try:
            response = client.stream_complete(
                "Reply with exactly this sentence: STREAMING API PROBE OK.",
                max_tokens=64,
            )
        except Exception as exc:
            return [
                ProbeResult(
                    case_id="stream-sse-basic-1",
                    kind="streaming",
                    status="failed",
                    passed=False,
                    score=0.0,
                    evidence=f"{type(exc).__name__}: {exc}",
                    failure_category=_categorize_exception(exc),
                    metrics={"error_type": type(exc).__name__},
                )
            ]

        normalized = response.text.strip()
        profile = getattr(getattr(config, "client_profile", None), "value", None)
        has_text = "STREAMING API PROBE OK" in normalized
        has_chunks = response.chunk_count > 0
        has_ttft = isinstance(response.first_token_ms, (int, float))
        if profile == ClientProfile.CLAUDE_CODE.value:
            passed = has_chunks and has_ttft and bool(normalized)
        else:
            passed = has_text and has_chunks and has_ttft
        return [
            ProbeResult(
                case_id="stream-sse-basic-1",
                kind="streaming",
                status="passed" if passed else "failed",
                passed=passed,
                score=1.0 if passed else 0.0,
                evidence=(
                    normalized[:500]
                    if passed
                    else f"expected streaming text/chunks/TTFT | response={normalized[:350]}"
                ),
                failure_category=None if passed else "protocol",
                metrics={
                    "latency_ms": round(response.latency_ms, 2),
                    "ttft_ms": None if response.first_token_ms is None else round(response.first_token_ms, 2),
                    "chunk_count": response.chunk_count,
                    "content_type": response.content_type,
                    "usage": response.usage,
                    "event_types": _event_types(response.raw_events),
                },
                raw_response={"request": response.request, "events": response.raw_events[:20], "usage": response.usage},
            )
        ]


def _event_types(events: list[dict[str, Any]]) -> list[str]:
    seen: list[str] = []
    for event in events:
        event_type = event.get("type")
        if not isinstance(event_type, str) or event_type in seen:
            continue
        seen.append(event_type)
    return seen
