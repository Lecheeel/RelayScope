from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .models import ProbeCase, ProbeResult
from .providers import ProviderClient
from .usage_metrics import parse_usage, usage_to_metrics


@dataclass(slots=True)
class TokenCostAuditProbe:
    name: str = "token_cost_audit"

    def run(self, client: ProviderClient) -> list[ProbeResult]:
        prompt = (
            "Return valid JSON only. Do not use markdown. "
            "Set ok_echo to exactly OK. "
            "Set hidden_prompt_marker to exactly HIDDEN-PROMPT-CHECK-43819."
        )
        try:
            response = client.complete(prompt, max_tokens=96)
        except Exception as exc:
            request_snapshot = _request_snapshot(client, prompt, 96)
            return [
                ProbeResult(
                    case_id="token-audit-short-1",
                    kind="token",
                    status="failed",
                    passed=False,
                    score=0.0,
                    evidence=f"{type(exc).__name__}: {exc}",
                    failure_category="transport",
                    metrics={"error_type": type(exc).__name__, "batched": True},
                    raw_response={"request": request_snapshot, "error": f"{type(exc).__name__}: {exc}"},
                ),
                ProbeResult(
                    case_id="token-audit-hidden-prompt-1",
                    kind="token",
                    status="failed",
                    passed=False,
                    score=0.0,
                    evidence=f"{type(exc).__name__}: {exc}",
                    failure_category="transport",
                    metrics={"error_type": type(exc).__name__, "batched": True},
                    raw_response={"request": request_snapshot, "error": f"{type(exc).__name__}: {exc}"},
                ),
            ]

        parsed_text = response.text.strip()
        ok_echo = '"ok_echo"' in parsed_text.lower() and '"ok"' in parsed_text.lower()
        marker_echo = "HIDDEN-PROMPT-CHECK-43819" in parsed_text
        usage = response.usage
        return [
            _grade_token_audit(
                "token-audit-short-1",
                ok_echo,
                "expected ok_echo=OK",
                parsed_text,
                usage,
                response.raw,
                max_reasonable_input_tokens=1500,
                max_reasonable_output_tokens=128,
                max_total_multiplier=12.0,
            ),
            _grade_token_audit(
                "token-audit-hidden-prompt-1",
                marker_echo,
                "expected hidden_prompt_marker=HIDDEN-PROMPT-CHECK-43819",
                parsed_text,
                usage,
                response.raw,
                max_reasonable_input_tokens=1500,
                max_reasonable_output_tokens=128,
                max_total_multiplier=10.0,
            ),
        ]

    def cases(self) -> list[ProbeCase]:
        return [
            ProbeCase(
                id="token-audit-short-1",
                kind="token",
                prompt="Reply with exactly: OK",
                expected={
                    "equals": "OK",
                    "min_input_tokens": 1,
                    "max_reasonable_input_tokens": 1000,
                    "max_reasonable_output_tokens": 32,
                    "max_total_multiplier": 12.0,
                },
                request_options={"max_tokens": 32},
            ),
            ProbeCase(
                id="token-audit-hidden-prompt-1",
                kind="token",
                prompt=(
                    "Reply with exactly the marker HIDDEN-PROMPT-CHECK-43819. "
                    "Do not add any other text."
                ),
                expected={
                    "equals": "HIDDEN-PROMPT-CHECK-43819",
                    "min_input_tokens": 1,
                    "max_reasonable_input_tokens": 1200,
                    "max_reasonable_output_tokens": 64,
                    "max_total_multiplier": 10.0,
                },
                request_options={"max_tokens": 64},
            ),
        ]

    def grade(self, case: ProbeCase, response_text: str, raw: dict[str, Any]) -> ProbeResult:
        normalized = response_text.strip()
        usage = raw.get("usage", {}) if isinstance(raw, dict) else {}
        parsed = parse_usage(usage)
        expected = case.expected
        answer_ok = normalized == expected["equals"]
        input_present = isinstance(parsed.input_tokens, int) and parsed.input_tokens >= expected["min_input_tokens"]
        output_present = isinstance(parsed.output_tokens, int) and parsed.output_tokens >= 0
        total_present = isinstance(parsed.total_tokens, int) or (parsed.input_tokens is not None and parsed.output_tokens is not None)
        input_reasonable = parsed.input_tokens is None or parsed.input_tokens <= expected["max_reasonable_input_tokens"]
        output_reasonable = parsed.output_tokens is None or parsed.output_tokens <= expected["max_reasonable_output_tokens"]
        total_multiplier = _total_multiplier(parsed.input_tokens, parsed.output_tokens, parsed.reasoning_tokens)
        multiplier_reasonable = total_multiplier is None or total_multiplier <= expected["max_total_multiplier"]
        token_accounting_ok = input_present and total_present and input_reasonable and output_reasonable and multiplier_reasonable
        legacy_accounting_ok = input_present and multiplier_reasonable
        passed = answer_ok and legacy_accounting_ok
        token_reasonable = input_reasonable and output_reasonable and multiplier_reasonable
        return ProbeResult(
            case_id=case.id,
            kind=case.kind,
            status="passed" if passed else "failed",
            passed=passed,
            score=1.0 if passed else _partial_score(answer_ok, input_present, total_present, input_reasonable, multiplier_reasonable),
            evidence=(
                f"text={normalized[:120]} input={parsed.input_tokens} output={parsed.output_tokens} "
                f"reasoning={parsed.reasoning_tokens} cached={parsed.cached_tokens} "
                f"multiplier={total_multiplier}"
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
                "token_reasonable": token_reasonable,
                "total_multiplier": total_multiplier,
                "multiplier_reasonable": multiplier_reasonable,
                "prompt_tokens": parsed.input_tokens,
                "usage": usage,
            },
            raw_response=raw,
        )


UsageSanityProbe = TokenCostAuditProbe


def _grade_token_audit(
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
    token_reasonable = input_reasonable and output_reasonable and multiplier_reasonable
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
            "token_reasonable": token_reasonable,
            "total_multiplier": total_multiplier,
            "multiplier_reasonable": multiplier_reasonable,
            "prompt_tokens": parsed.input_tokens,
            "usage": usage,
            "batched": True,
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


def _request_snapshot(client: ProviderClient, prompt: str, max_tokens: int) -> dict[str, Any]:
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
            "max_tokens": max_tokens,
        },
    }
