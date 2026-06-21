from __future__ import annotations

import base64
import struct
import zlib
from dataclasses import dataclass
from dataclasses import asdict
from time import sleep
from typing import Any

from .config import ProtocolMode, ProviderFamily, TargetConfig
from .models import ProbeResult
from .probes import _categorize_exception
from .providers import OpenAIResponsesClient, ProviderClient
from .usage_metrics import LatencyStats, has_cache_metric, parse_usage, usage_to_metrics


IMAGE_CASES = (
    ("vision-image-red-1", "red", "RED-7391", (220, 30, 30)),
    ("vision-image-green-1", "green", "GREEN-4826", (20, 160, 70)),
    ("vision-image-blue-1", "blue", "BLUE-9154", (35, 95, 220)),
)

PDF_CASES = (
    ("pdf-document-marker-1", "LIME-7391"),
    ("pdf-document-marker-2", "MANGO-4826"),
    ("pdf-document-marker-3", "AZURE-9154"),
)


@dataclass(slots=True)
class MultimodalCapabilityProbe:
    name: str = "multimodal_capability"

    def run(self, client: ProviderClient) -> list[ProbeResult]:
        config = getattr(client, "config", None)
        family = getattr(getattr(config, "provider_family", None), "value", None)
        protocol = getattr(getattr(config, "protocol_mode", None), "value", None)
        pdf_client = _pdf_client(client)
        pdf_config = getattr(pdf_client, "config", None)
        pdf_family = getattr(getattr(pdf_config, "provider_family", None), "value", None)
        pdf_protocol = getattr(getattr(pdf_config, "protocol_mode", None), "value", None)
        image_results: list[ProbeResult] = []
        pdf_results: list[ProbeResult] = []

        for case_id, color, marker, rgb in IMAGE_CASES:
            image_messages = _image_messages(family, protocol, marker, rgb)
            if image_messages is None:
                image_results.append(_skipped(case_id, "vision", family, protocol))
            else:
                image_results.append(self._run_case(client, case_id, "vision", image_messages, (color,), optional_any=(marker.lower(),)))

        for case_id, marker in PDF_CASES:
            pdf_messages = _pdf_messages(pdf_family, pdf_protocol, marker)
            if pdf_messages is None:
                pdf_results.append(_skipped(case_id, "pdf", pdf_family, pdf_protocol))
            else:
                pdf_results.append(self._run_case(pdf_client, case_id, "pdf", pdf_messages, (marker, marker.lower())))

        return [
            _aggregate_results("vision-image-suite-1", "vision", "image", image_results),
            _aggregate_results("pdf-document-suite-1", "pdf", "pdf", pdf_results),
        ]

    def _run_case(
        self,
        client: ProviderClient,
        case_id: str,
        kind: str,
        messages: list[dict[str, Any]],
        expected_any: tuple[str, ...],
        require_all: bool = False,
        optional_any: tuple[str, ...] = (),
    ) -> ProbeResult:
        try:
            response = client.complete(messages=messages, max_tokens=128)
        except Exception as exc:
            failure_category = _multimodal_failure_category(exc)
            return ProbeResult(
                case_id=case_id,
                kind=kind,
                status="failed",
                passed=False,
                score=0.0,
                evidence=f"{type(exc).__name__}: {exc}",
                failure_category=failure_category,
                metrics={"error_type": type(exc).__name__},
                raw_response={"request": _request_snapshot(client, messages), "error": f"{type(exc).__name__}: {exc}"},
            )

        normalized = response.text.strip()
        lowered = normalized.lower()
        if require_all:
            passed = all(item.lower() in lowered for item in expected_any)
        else:
            passed = any(item.lower() in lowered for item in expected_any)
        optional_matches = [item for item in optional_any if item.lower() in lowered]
        return ProbeResult(
            case_id=case_id,
            kind=kind,
            status="passed" if passed else "failed",
            passed=passed,
            score=1.0 if passed else 0.0,
            evidence=normalized[:500] if passed else f"expected one of {expected_any} | response={normalized[:350]}",
            failure_category=None if passed else "capability",
            metrics={
                "latency_ms": round(response.latency_ms, 2),
                "content_type": response.content_type,
                "usage": response.usage,
                "response_model": response.raw.get("model"),
                "retry_count": response.retries,
                "transient_failures": response.transient_failures or [],
                "optional_matches": optional_matches,
            },
            raw_response=response.raw,
        )


@dataclass(slots=True)
class PdfCacheProbe:
    name: str = "pdf_cache"
    repeats: int = 3

    def run(self, client: ProviderClient) -> list[ProbeResult]:
        pdf_client = _pdf_client(client)
        config = getattr(pdf_client, "config", None)
        family = getattr(getattr(config, "provider_family", None), "value", None)
        protocol = getattr(getattr(config, "protocol_mode", None), "value", None)
        marker = PDF_CASES[0][1]
        messages = _pdf_messages(family, protocol, marker)
        if messages is None:
            return [_skipped("pdf-cache-repeat-1", "pdf_cache", family, protocol)]

        responses = []
        failures = []
        for attempt in range(max(2, self.repeats)):
            if attempt > 0:
                sleep(_cache_delay(pdf_client))
            try:
                response = pdf_client.complete(messages=messages, max_tokens=128, cache_control=True)
            except Exception as exc:
                failures.append({"attempt": attempt + 1, "error": f"{type(exc).__name__}: {exc}"})
                continue
            responses.append(response)

        if failures and not responses:
            return [
                ProbeResult(
                    case_id="pdf-cache-repeat-1",
                    kind="pdf_cache",
                    status="failed",
                    passed=False,
                    score=0.0,
                    evidence=failures[0]["error"],
                    failure_category="transport",
                    metrics={"failures": failures},
                    raw_response={"request": _request_snapshot(client, messages), "failures": failures},
                )
            ]

        texts = [response.text.strip() for response in responses]
        answer_ok = all(marker.lower() in text.lower() for text in texts)
        consistent = len(set(texts)) <= 1
        usage_items = [parse_usage(response.usage) for response in responses]
        cached_values = [item.cached_tokens or 0 for item in usage_items]
        creation_values = [item.cache_creation_tokens or 0 for item in usage_items]
        cache_metric_seen = any(has_cache_metric(response.usage) for response in responses)
        cache_hit_seen = any(value > 0 for value in cached_values[1:])
        latency = LatencyStats(tuple(response.latency_ms for response in responses))
        latency_drop = latency.drop_ratio
        latency_supports_cache = latency_drop is not None and latency_drop >= 0.10
        input_values = [item.input_tokens for item in usage_items]
        passed = answer_ok and consistent and (cache_hit_seen or latency_supports_cache)
        score = sum(
            [
                0.35 if answer_ok else 0.0,
                0.15 if consistent else 0.0,
                0.30 if cache_hit_seen else 0.0,
                0.10 if cache_metric_seen else 0.0,
                0.10 if latency_supports_cache else 0.0,
            ]
        )

        return [
            ProbeResult(
                case_id="pdf-cache-repeat-1",
                kind="pdf_cache",
                status="passed" if passed else "failed",
                passed=passed,
                score=round(score, 2),
                evidence=(
                    f"answers_ok={answer_ok} consistent={consistent} "
                    f"cached_tokens={cached_values} cache_metric_seen={cache_metric_seen} "
                    f"latency_drop={_pct(latency_drop)}"
                ),
                failure_category=None if passed else "cache",
                metrics={
                    "attempts": len(responses),
                    "failures": failures,
                    "input_tokens_by_attempt": input_values,
                    "cached_tokens_by_attempt": cached_values,
                    "cache_creation_tokens_by_attempt": creation_values,
                    "cache_metric_seen": cache_metric_seen,
                    "cache_hit_seen": cache_hit_seen,
                    "latencies_ms": [round(response.latency_ms, 2) for response in responses],
                    "retry_counts": [response.retries for response in responses],
                    "transient_failures": [
                        failure
                        for response in responses
                        for failure in (response.transient_failures or [])
                    ],
                    "latency_drop_ratio": latency_drop,
                    "latency_cv": latency.coefficient_of_variation,
                    "usage": responses[-1].usage if responses else {},
                    "usage_by_attempt": [usage_to_metrics(response.usage) for response in responses],
                    "response_model": responses[-1].raw.get("model") if responses else None,
                },
                raw_response=responses[-1].raw if responses else {},
            )
        ]


def _image_messages(
    family: str | None,
    protocol: str | None,
    marker: str,
    rgb: tuple[int, int, int],
) -> list[dict[str, Any]] | None:
    image_base64 = _marker_png_base64(rgb, marker)
    prompt = (
        "Read this image. Reply with valid JSON only: "
        '{"color":"<dominant lowercase color>","marker":"<visible marker text>"}'
    )
    if family == ProviderFamily.ANTHROPIC.value:
        return [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {"type": "base64", "media_type": "image/png", "data": image_base64},
                    },
                    {"type": "text", "text": prompt},
                ],
            }
        ]
    if family == ProviderFamily.OPENAI.value:
        return [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_base64}"}},
                ],
            }
        ]
    return None


def _pdf_messages(family: str | None, protocol: str | None, marker: str) -> list[dict[str, Any]] | None:
    pdf_data = base64.b64encode(_minimal_pdf_bytes(f"API Probe PDF marker: {marker}")).decode("ascii")
    prompt = "Read the attached PDF and reply with the marker value only."
    if family == ProviderFamily.ANTHROPIC.value:
        return [
            {
                "role": "user",
                "content": [
                    {
                        "type": "document",
                        "source": {"type": "base64", "media_type": "application/pdf", "data": pdf_data},
                    },
                    {"type": "text", "text": prompt},
                ],
            }
        ]
    if family == ProviderFamily.OPENAI.value and protocol == ProtocolMode.OPENAI_RESPONSES.value:
        return [
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": prompt},
                    {
                        "type": "input_file",
                        "filename": "api-probe-marker.pdf",
                        "file_data": f"data:application/pdf;base64,{pdf_data}",
                    },
                ],
            }
        ]
    return None


def _pdf_client(client: ProviderClient) -> ProviderClient:
    config = getattr(client, "config", None)
    if config is None:
        return client
    family = getattr(getattr(config, "provider_family", None), "value", None)
    protocol = getattr(getattr(config, "protocol_mode", None), "value", None)
    if family == ProviderFamily.OPENAI.value and protocol == ProtocolMode.OPENAI_COMPAT.value:
        responses_config = TargetConfig(
            name=getattr(config, "name", "target"),
            provider_family=config.provider_family,
            protocol_mode=ProtocolMode.OPENAI_RESPONSES,
            base_url=config.base_url,
            api_key=config.api_key,
            model=config.model,
            client_profile=config.client_profile,
            extra_headers=dict(config.extra_headers),
            timeout_seconds=config.timeout_seconds,
            max_retries=config.max_retries,
            retry_backoff_seconds=config.retry_backoff_seconds,
            cache_probe_delay_seconds=config.cache_probe_delay_seconds,
            stream=config.stream,
            metadata=dict(config.metadata),
        )
        return OpenAIResponsesClient(responses_config)
    return client


def _marker_png_base64(rgb: tuple[int, int, int], marker: str, width: int = 256, height: int = 256) -> str:
    pixels = _solid_pixels(rgb, width, height)
    text_width = len(marker) * 6 - 1
    scale = 5
    x = max(8, (width - text_width * scale) // 2)
    y = max(8, (height - 7 * scale) // 2)
    _draw_text(pixels, width, height, x, y, marker, scale, (255, 255, 255))
    return _png_base64(pixels, width, height)


def _solid_png_base64(rgb: tuple[int, int, int], width: int = 256, height: int = 256) -> str:
    return _png_base64(_solid_pixels(rgb, width, height), width, height)


def _solid_pixels(rgb: tuple[int, int, int], width: int, height: int) -> bytearray:
    r, g, b = rgb
    return bytearray(bytes((r, g, b)) * width * height)


def _png_base64(pixels: bytearray, width: int, height: int) -> str:
    rows = b"".join(
        b"\x00" + bytes(pixels[row * width * 3 : (row + 1) * width * 3])
        for row in range(height)
    )
    compressed = zlib.compress(rows)
    png = (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + _png_chunk(b"IDAT", compressed)
        + _png_chunk(b"IEND", b"")
    )
    return base64.b64encode(png).decode("ascii")


def _draw_text(
    pixels: bytearray,
    width: int,
    height: int,
    x: int,
    y: int,
    text: str,
    scale: int,
    color: tuple[int, int, int],
) -> None:
    cursor_x = x
    for char in text.upper():
        glyph = FONT_5X7.get(char)
        if glyph is None:
            cursor_x += 6 * scale
            continue
        _draw_glyph(pixels, width, height, cursor_x, y, glyph, scale, color)
        cursor_x += 6 * scale


def _draw_glyph(
    pixels: bytearray,
    width: int,
    height: int,
    x: int,
    y: int,
    glyph: tuple[str, ...],
    scale: int,
    color: tuple[int, int, int],
) -> None:
    r, g, b = color
    for row_index, row in enumerate(glyph):
        for col_index, bit in enumerate(row):
            if bit != "1":
                continue
            for dy in range(scale):
                py = y + row_index * scale + dy
                if py < 0 or py >= height:
                    continue
                for dx in range(scale):
                    px = x + col_index * scale + dx
                    if px < 0 or px >= width:
                        continue
                    offset = (py * width + px) * 3
                    pixels[offset : offset + 3] = bytes((r, g, b))


def _png_chunk(chunk_type: bytes, data: bytes) -> bytes:
    checksum = zlib.crc32(chunk_type + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + chunk_type + data + struct.pack(">I", checksum)


FONT_5X7: dict[str, tuple[str, ...]] = {
    "-": (
        "00000",
        "00000",
        "00000",
        "11111",
        "00000",
        "00000",
        "00000",
    ),
    "0": (
        "01110",
        "10001",
        "10011",
        "10101",
        "11001",
        "10001",
        "01110",
    ),
    "1": (
        "00100",
        "01100",
        "00100",
        "00100",
        "00100",
        "00100",
        "01110",
    ),
    "2": (
        "01110",
        "10001",
        "00001",
        "00010",
        "00100",
        "01000",
        "11111",
    ),
    "3": (
        "11110",
        "00001",
        "00001",
        "01110",
        "00001",
        "00001",
        "11110",
    ),
    "4": (
        "00010",
        "00110",
        "01010",
        "10010",
        "11111",
        "00010",
        "00010",
    ),
    "5": (
        "11111",
        "10000",
        "10000",
        "11110",
        "00001",
        "00001",
        "11110",
    ),
    "6": (
        "01110",
        "10000",
        "10000",
        "11110",
        "10001",
        "10001",
        "01110",
    ),
    "7": (
        "11111",
        "00001",
        "00010",
        "00100",
        "01000",
        "01000",
        "01000",
    ),
    "8": (
        "01110",
        "10001",
        "10001",
        "01110",
        "10001",
        "10001",
        "01110",
    ),
    "9": (
        "01110",
        "10001",
        "10001",
        "01111",
        "00001",
        "00001",
        "01110",
    ),
    "A": (
        "01110",
        "10001",
        "10001",
        "11111",
        "10001",
        "10001",
        "10001",
    ),
    "B": (
        "11110",
        "10001",
        "10001",
        "11110",
        "10001",
        "10001",
        "11110",
    ),
    "D": (
        "11110",
        "10001",
        "10001",
        "10001",
        "10001",
        "10001",
        "11110",
    ),
    "E": (
        "11111",
        "10000",
        "10000",
        "11110",
        "10000",
        "10000",
        "11111",
    ),
    "G": (
        "01110",
        "10001",
        "10000",
        "10111",
        "10001",
        "10001",
        "01110",
    ),
    "L": (
        "10000",
        "10000",
        "10000",
        "10000",
        "10000",
        "10000",
        "11111",
    ),
    "N": (
        "10001",
        "11001",
        "10101",
        "10011",
        "10001",
        "10001",
        "10001",
    ),
    "R": (
        "11110",
        "10001",
        "10001",
        "11110",
        "10100",
        "10010",
        "10001",
    ),
    "U": (
        "10001",
        "10001",
        "10001",
        "10001",
        "10001",
        "10001",
        "01110",
    ),
}


def _skipped(case_id: str, kind: str, family: str | None, protocol: str | None) -> ProbeResult:
    return ProbeResult(
        case_id=case_id,
        kind=kind,
        status="skipped",
        passed=False,
        score=0.0,
        evidence="Skipped because this protocol shape is not supported by the probe yet.",
        failure_category="unsupported",
        skipped_reason=f"family={family or '-'}, protocol={protocol or '-'}",
    )


def _aggregate_results(case_id: str, kind: str, group: str, results: list[ProbeResult]) -> ProbeResult:
    if not results:
        return ProbeResult(
            case_id=case_id,
            kind=kind,
            status="skipped",
            passed=False,
            score=0.0,
            evidence="No subtests were generated.",
            failure_category="unsupported",
            skipped_reason="no subtests",
        )
    passed_count = sum(1 for result in results if result.status == "passed")
    failed_count = sum(1 for result in results if result.status == "failed")
    skipped_count = sum(1 for result in results if result.status == "skipped")
    scored_count = max(1, len(results) - skipped_count)
    passed = failed_count == 0 and passed_count > 0
    status = "passed" if passed else "failed" if failed_count else "skipped"
    failure_category = None if passed else ("unsupported" if skipped_count == len(results) else "capability")
    evidence = f"{group}_passed={passed_count}, failed={failed_count}, skipped={skipped_count}"
    if failed_count:
        first_failed = next((result for result in results if result.status == "failed"), None)
        if first_failed is not None:
            evidence += f" | first_failed={first_failed.case_id}: {first_failed.evidence[:220]}"
    return ProbeResult(
        case_id=case_id,
        kind=kind,
        status=status,
        passed=passed,
        score=round(passed_count / scored_count, 2),
        evidence=evidence[:500],
        failure_category=failure_category,
        skipped_reason="all subtests skipped" if status == "skipped" else None,
        metrics={
            "subtest_count": len(results),
            "passed": passed_count,
            "failed": failed_count,
            "skipped": skipped_count,
            "subtests": [asdict(result) for result in results],
        },
        raw_response={"subtests": [asdict(result) for result in results]},
    )


def _multimodal_failure_category(exc: Exception) -> str:
    message = str(exc).lower()
    if "upstream access forbidden" in message or "unsupported" in message or "image" in message or "pdf" in message:
        return "unsupported"
    return _categorize_exception(exc)


def _pct(value: float | None) -> str:
    return "unknown" if value is None else f"{value * 100:.1f}%"


def _request_snapshot(client: ProviderClient, messages: list[dict[str, Any]]) -> dict[str, Any]:
    config = getattr(client, "config", None)
    base_url = getattr(config, "base_url", "")
    family = getattr(getattr(config, "provider_family", None), "value", None)
    protocol = getattr(getattr(config, "protocol_mode", None), "value", None)
    if family == ProviderFamily.ANTHROPIC.value:
        endpoint = "/messages"
    elif protocol == ProtocolMode.OPENAI_RESPONSES.value:
        endpoint = "/responses"
    else:
        endpoint = "/chat/completions"
    return {
        "method": "POST",
        "url": base_url.rstrip("/") + endpoint,
        "json": {
            "model": getattr(config, "model", None),
            "messages": messages,
            "max_tokens": 128,
        },
    }


def _cache_delay(client: ProviderClient) -> float:
    config = getattr(client, "config", None)
    value = getattr(config, "cache_probe_delay_seconds", 1.2)
    try:
        return max(0.0, min(5.0, float(value)))
    except (TypeError, ValueError):
        return 1.2


def _minimal_pdf_bytes(text: str) -> bytes:
    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    objects = [
        b"1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n",
        b"2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj\n",
        b"3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >> endobj\n",
        b"4 0 obj << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> endobj\n",
    ]
    stream = f"BT /F1 18 Tf 72 720 Td ({escaped}) Tj ET".encode("ascii")
    objects.append(b"5 0 obj << /Length " + str(len(stream)).encode("ascii") + b" >> stream\n" + stream + b"\nendstream endobj\n")

    output = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for obj in objects:
        offsets.append(len(output))
        output.extend(obj)
    xref_start = len(output)
    output.extend(f"xref\n0 {len(offsets)}\n".encode("ascii"))
    output.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    output.extend(
        f"trailer << /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref_start}\n%%EOF\n".encode("ascii")
    )
    return bytes(output)
