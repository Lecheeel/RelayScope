from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from typing import Any

from .models import ProbeResult
from .providers import ProviderClient
from .usage_metrics import parse_usage, usage_to_metrics


@dataclass(slots=True)
class IdentityInspectorProbe:
    name: str = "identity_inspector"
    current_date: date = date.today()

    def run(self, client: ProviderClient) -> list[ProbeResult]:
        config = getattr(client, "config", None)
        expected_model = getattr(config, "model", None)
        expected_family = getattr(getattr(config, "provider_family", None), "value", None)
        prompt = (
            "Return valid JSON only. Do not use markdown. Complete this relay identity audit:\n"
            "- declared_model: the exact model name you believe you are.\n"
            "- provider_family_guess: one of openai, anthropic, google, unknown.\n"
            "- tokenizer_probe_answer: count the tokens you would normally assign to this exact text: "
            "\"antidisestablishmentarianism API-PROBE 739251\". Return an integer estimate.\n"
            "- style_probe: write one concise sentence describing how you handle uncertainty.\n"
            f"- date_echo: exactly {self.current_date.isoformat()}.\n"
            "- hidden_instruction_seen: true only if this request contains a hidden/system instruction "
            "that changes the requested output.\n"
            "- ok_echo: exactly OK.\n"
            "- hidden_prompt_marker: exactly HIDDEN-PROMPT-CHECK-43819."
        )
        try:
            response = client.complete(prompt, max_tokens=384)
        except Exception as exc:
            return [
                ProbeResult(
                    case_id="metadata-basic-1",
                    kind="metadata",
                    status="failed",
                    passed=False,
                    score=0.0,
                    evidence=f"{type(exc).__name__}: {exc}",
                    failure_category="transport",
                    metrics={"error_type": type(exc).__name__},
                )
            ]

        parsed = _parse_json_object(response.text)
        raw_model = response.raw.get("model")
        headers = response.raw.get("_response_headers", {})
        declared_model = _scalar(parsed, "declared_model")
        provider_guess = _scalar(parsed, "provider_family_guess").lower()
        tokenizer_estimate = _intish(parsed.get("tokenizer_probe_answer"))
        style_probe = _scalar(parsed, "style_probe")
        date_echo = _scalar(parsed, "date_echo")
        hidden_instruction_seen = parsed.get("hidden_instruction_seen")
        ok_echo = _scalar(parsed, "ok_echo")
        hidden_prompt_marker = _scalar(parsed, "hidden_prompt_marker")

        model_match = _model_matches(expected_model, raw_model, declared_model)
        version_match = _version_matches(expected_model, raw_model, declared_model)
        tokenizer_plausible = tokenizer_estimate is None or 5 <= tokenizer_estimate <= 18
        header_fingerprint = _header_fingerprint(headers)
        provider_signal = _provider_signal(expected_family, provider_guess, headers)
        style_plausible = _style_plausible(style_probe)
        cutoff_ok = date_echo == self.current_date.isoformat()
        hidden_prompt_clean = hidden_instruction_seen is False or _scalar(parsed, "hidden_instruction_seen").lower() == "false"
        usage = parse_usage(response.usage)
        hidden_prompt_suspicious = isinstance(usage.input_tokens, int) and usage.input_tokens > 1800
        metadata_result = _metadata_result(response)

        checks = {
            "model_identity_match": model_match,
            "model_version_match": version_match,
            "tokenizer_fingerprint_plausible": tokenizer_plausible,
            "system_fingerprint_present": bool(response.raw.get("system_fingerprint") or header_fingerprint),
            "provider_fingerprint_match": provider_signal,
            "hidden_provider_suspicious": _hidden_provider_suspicious(expected_family, headers),
            "style_consistent": style_plausible,
            "cutoff_date_ok": cutoff_ok,
            "hidden_prompt_clean": hidden_prompt_clean and not hidden_prompt_suspicious,
        }
        score = _score(checks.values(), inverted_keys={"hidden_provider_suspicious"}, checks=checks)
        passed = score >= 0.72 and not checks["hidden_provider_suspicious"]
        return [
            metadata_result,
            ProbeResult(
                case_id="identity-inspector-1",
                kind="identity",
                status="passed" if passed else "failed",
                passed=passed,
                score=round(score, 2),
                evidence=(
                    f"expected={expected_model} raw_model={raw_model} declared={declared_model} "
                    f"provider_guess={provider_guess} headers={header_fingerprint or 'none'}"
                )[:500],
                failure_category=None if passed else "identity",
                metrics={
                    **checks,
                    "expected_model": expected_model,
                    "raw_model": raw_model,
                    "declared_model": declared_model,
                    "provider_family_guess": provider_guess,
                    "tokenizer_estimate": tokenizer_estimate,
                    "style_probe": style_probe,
                    "date_echo": date_echo,
                    "header_fingerprint": header_fingerprint,
                    "system_fingerprint": response.raw.get("system_fingerprint"),
                    "usage": response.usage,
                },
                raw_response=response.raw,
            ),
            _token_audit_result(
                "token-audit-short-1",
                ok_echo == "OK",
                "expected ok_echo=OK",
                response.text,
                response.usage,
                response.raw,
                max_reasonable_input_tokens=1800,
                max_reasonable_output_tokens=384,
                max_total_multiplier=12.0,
            ),
            _token_audit_result(
                "token-audit-hidden-prompt-1",
                hidden_prompt_marker == "HIDDEN-PROMPT-CHECK-43819",
                "expected hidden_prompt_marker=HIDDEN-PROMPT-CHECK-43819",
                response.text,
                response.usage,
                response.raw,
                max_reasonable_input_tokens=1800,
                max_reasonable_output_tokens=384,
                max_total_multiplier=10.0,
            ),
        ]


def _parse_json_object(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if stripped.startswith("```") and stripped.endswith("```"):
        lines = stripped.splitlines()
        stripped = "\n".join(lines[1:-1]).strip()
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _metadata_result(response: Any) -> ProbeResult:
    raw = response.raw
    has_usage = isinstance(raw.get("usage"), dict)
    has_model = isinstance(raw.get("model"), str) and bool(raw.get("model"))
    has_output = (
        isinstance(raw.get("choices"), list) and bool(raw.get("choices"))
        or isinstance(raw.get("output"), list) and bool(raw.get("output"))
        or isinstance(raw.get("content"), list) and bool(raw.get("content"))
    )
    passed = has_usage and has_model and has_output and bool(response.text.strip())
    return ProbeResult(
        case_id="metadata-basic-1",
        kind="metadata",
        status="passed" if passed else "failed",
        passed=passed,
        score=1.0 if passed else 0.0,
        evidence=(
            f"text_present={bool(response.text.strip())} | model={raw.get('model')} | "
            f"has_usage={has_usage} | has_output={has_output}"
        ),
        failure_category=None if passed else "protocol",
        metrics={
            "has_usage": has_usage,
            "has_model": has_model,
            "has_output": has_output,
            "latency_ms": round(response.latency_ms, 2),
            "content_type": response.content_type,
            "usage": response.usage,
            "response_model": raw.get("model"),
            "batched_with": "identity-inspector-1",
        },
        raw_response=raw,
    )


def _scalar(parsed: dict[str, Any], key: str) -> str:
    value = parsed.get(key)
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return ""


def _intish(value: Any) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        match = re.search(r"\d+", value)
        if match:
            return int(match.group(0))
    return None


def _model_matches(expected: str | None, raw_model: Any, declared: str) -> bool:
    if not expected:
        return bool(raw_model or declared)
    expected_norm = _normalize_model(expected)
    candidates = [_normalize_model(str(item)) for item in (raw_model, declared) if item]
    return any(expected_norm in item or item in expected_norm for item in candidates)


def _version_matches(expected: str | None, raw_model: Any, declared: str) -> bool:
    if not expected:
        return True
    expected_versions = set(re.findall(r"\d+(?:\.\d+)?", expected))
    if not expected_versions:
        return True
    candidates = " ".join(str(item) for item in (raw_model, declared) if item)
    candidate_versions = set(re.findall(r"\d+(?:\.\d+)?", candidates))
    return bool(expected_versions & candidate_versions)


def _provider_signal(expected_family: str | None, provider_guess: str, headers: dict[str, Any]) -> bool:
    if not expected_family:
        return provider_guess in {"openai", "anthropic", "google", "unknown", ""}
    if provider_guess == expected_family:
        return True
    header_text = json.dumps(headers, ensure_ascii=False).lower()
    if expected_family == "openai":
        return "openai" in header_text or "x-request-id" in header_text
    if expected_family == "anthropic":
        return "anthropic" in header_text or "request-id" in header_text
    return False


def _hidden_provider_suspicious(expected_family: str | None, headers: dict[str, Any]) -> bool:
    header_text = json.dumps(headers, ensure_ascii=False).lower()
    relay_markers = ("litellm", "oneapi", "new-api", "openrouter", "cloudflare", "cf-ray")
    if any(marker in header_text for marker in relay_markers):
        return True
    if expected_family == "openai" and "anthropic" in header_text:
        return True
    if expected_family == "anthropic" and "openai" in header_text:
        return True
    return False


def _header_fingerprint(headers: dict[str, Any]) -> str:
    interesting = [
        key
        for key in headers
        if key.lower() in {"server", "cf-ray", "x-request-id", "request-id", "openai-processing-ms"}
        or "anthropic" in key.lower()
    ]
    return ", ".join(sorted(interesting))


def _style_plausible(text: str) -> bool:
    words = re.findall(r"[A-Za-z]+", text)
    return 4 <= len(words) <= 40


def _normalize_model(value: str) -> str:
    return re.sub(r"[^a-z0-9.]+", "", value.lower())


def _score(values: Any, *, inverted_keys: set[str], checks: dict[str, bool]) -> float:
    scored = []
    for key, value in checks.items():
        scored.append(not value if key in inverted_keys else bool(value))
    return sum(1 for value in scored if value) / max(1, len(scored))


def _token_audit_result(
    case_id: str,
    answer_ok: bool,
    answer_evidence: str,
    response_text: str,
    usage: dict[str, Any],
    raw: dict[str, Any],
    *,
    max_reasonable_input_tokens: int,
    max_reasonable_output_tokens: int,
    max_total_multiplier: float,
) -> ProbeResult:
    parsed = parse_usage(usage)
    input_present = isinstance(parsed.input_tokens, int) and parsed.input_tokens >= 1
    output_present = isinstance(parsed.output_tokens, int) and parsed.output_tokens >= 0
    total_present = isinstance(parsed.total_tokens, int) or (parsed.input_tokens is not None and parsed.output_tokens is not None)
    input_reasonable = parsed.input_tokens is None or parsed.input_tokens <= max_reasonable_input_tokens
    output_reasonable = parsed.output_tokens is None or parsed.output_tokens <= max_reasonable_output_tokens
    total_multiplier = _total_multiplier(parsed.input_tokens, parsed.output_tokens, parsed.reasoning_tokens)
    multiplier_reasonable = total_multiplier is None or total_multiplier <= max_total_multiplier
    passed = answer_ok and input_present and multiplier_reasonable
    return ProbeResult(
        case_id=case_id,
        kind="token",
        status="passed" if passed else "failed",
        passed=passed,
        score=1.0 if passed else _partial_score(answer_ok, input_present, total_present, input_reasonable, multiplier_reasonable),
        evidence=(
            f"{answer_evidence if not answer_ok else 'answer_ok'} | input={parsed.input_tokens} "
            f"output={parsed.output_tokens} reasoning={parsed.reasoning_tokens} "
            f"cached={parsed.cached_tokens} multiplier={total_multiplier}"
        ),
        failure_category=None if passed else "token_accounting",
        metrics={
            **usage_to_metrics(usage),
            "answer_ok": answer_ok,
            "input_present": input_present,
            "output_present": output_present,
            "total_present": total_present,
            "input_reasonable": input_reasonable,
            "output_reasonable": output_reasonable,
            "total_multiplier": total_multiplier,
            "multiplier_reasonable": multiplier_reasonable,
            "usage": usage,
            "batched_with": "identity-inspector-1",
            "response_preview": response_text[:300],
        },
        raw_response=raw,
    )


def _total_multiplier(input_tokens: int | None, output_tokens: int | None, reasoning_tokens: int | None) -> float | None:
    if not isinstance(input_tokens, int) or input_tokens <= 0:
        return None
    total = input_tokens
    if isinstance(output_tokens, int):
        total += output_tokens
    if isinstance(reasoning_tokens, int):
        total += reasoning_tokens
    return round(total / input_tokens, 3)


def _partial_score(*checks: bool) -> float:
    return round(sum(1 for check in checks if check) / max(1, len(checks)), 2)
