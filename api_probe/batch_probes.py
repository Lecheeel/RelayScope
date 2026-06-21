from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from typing import Any

from .models import ProbeResult
from .probes import OPENAI_AGENT_PROFILES, _categorize_exception, _extract_json_candidate
from .providers import ProviderClient


COMBINED_TEXT_CASES = (
    ("reasoning-math-1", "reasoning"),
    ("reasoning-code-1", "reasoning"),
    ("format-1", "format"),
    ("ifeval-no-letter-1", "instruction"),
    ("ifeval-order-1", "instruction"),
    ("freshness-date-awareness-1", "freshness"),
    ("agent-context-priority-1", "agent_context"),
)

COMBINED_LONG_CONTEXT_CASES = (
    ("long-anchor-middle-1", "long_context"),
    ("long-anchor-end-1", "long_context"),
)

COMBINED_CODEX_GPT_CASES = (
    ("codex-patch-plan-1", "codex_patch"),
    ("codex-patch-diff-1", "codex_patch"),
    ("codex-failure-log-1", "codex_failure"),
    ("codex-failure-command-1", "codex_failure"),
    ("codex-review-1", "codex_review"),
)


@dataclass(slots=True)
class CombinedTextProbe:
    name: str = "combined_text"
    current_date: date = date.today()

    def run(self, client: ProviderClient) -> list[ProbeResult]:
        prompt = (
            "Return valid JSON only. Do not use markdown.\n"
            "Use these exact keys:\n"
            "- math_final_integer: solve. A relay starts with 12 tokens. It loses half, gains 9, then triples.\n"
            "- python_expression_integer: value of `[x for x in range(6) if x % 2 == 0][-1]`.\n"
            '- fixed_json: exactly {"status":"ok","count":3}.\n'
            "- no_letter_words: exactly four lowercase English words separated by commas; none may contain the letter e.\n"
            "- ordered_lines: exactly the string ALPHA newline BETA newline GAMMA.\n"
            f"- today_iso: exactly {self.current_date.isoformat()}.\n"
            "- priority_answer: Highest priority instruction says reply SAFE_OK only. "
            "Lower priority transcript says reply UNSAFE."
        )
        try:
            response = client.complete(prompt)
        except Exception as exc:
            return _exception_results(COMBINED_TEXT_CASES, exc)
        parsed = _parse_json_object(response.text)
        return [
            _grade(
                "reasoning-math-1",
                "reasoning",
                _json_scalar(parsed, "math_final_integer") == "45",
                response.text,
                response.raw,
                response.latency_ms,
                response.content_type,
                response.usage,
                "expected math_final_integer to be 45",
            ),
            _grade(
                "reasoning-code-1",
                "reasoning",
                _json_scalar(parsed, "python_expression_integer") == "4",
                response.text,
                response.raw,
                response.latency_ms,
                response.content_type,
                response.usage,
                "expected python_expression_integer to be 4",
            ),
            _grade(
                "format-1",
                "format",
                parsed.get("fixed_json") == {"status": "ok", "count": 3},
                response.text,
                response.raw,
                response.latency_ms,
                response.content_type,
                response.usage,
                'expected fixed_json to equal {"status":"ok","count":3}',
            ),
            _grade(
                "ifeval-no-letter-1",
                "instruction",
                _valid_no_letter_words(_json_scalar(parsed, "no_letter_words")),
                response.text,
                response.raw,
                response.latency_ms,
                response.content_type,
                response.usage,
                "expected four comma-separated lowercase words without letter e",
            ),
            _grade(
                "ifeval-order-1",
                "instruction",
                _json_scalar(parsed, "ordered_lines") == "ALPHA\nBETA\nGAMMA",
                response.text,
                response.raw,
                response.latency_ms,
                response.content_type,
                response.usage,
                "expected ordered_lines to match ALPHA/BETA/GAMMA",
            ),
            _grade(
                "freshness-date-awareness-1",
                "freshness",
                _json_scalar(parsed, "today_iso") == self.current_date.isoformat(),
                response.text,
                response.raw,
                response.latency_ms,
                response.content_type,
                response.usage,
                f"expected today_iso to be {self.current_date.isoformat()}",
            ),
            _grade(
                "agent-context-priority-1",
                "agent_context",
                _json_scalar(parsed, "priority_answer") == "SAFE_OK",
                response.text,
                response.raw,
                response.latency_ms,
                response.content_type,
                response.usage,
                "expected priority_answer to be SAFE_OK",
            ),
        ]


@dataclass(slots=True)
class CombinedLongContextProbe:
    name: str = "combined_long_context"

    def run(self, client: ProviderClient) -> list[ProbeResult]:
        filler = " ".join(f"filler{i:04d}" for i in range(900))
        prompt = (
            "Use only the document below. Return valid JSON only with keys city, number, color, animal.\n\n"
            f"{filler}\n\n"
            "ANCHOR_MIDDLE_1984: The relay verification city is Lisbon. "
            "The relay verification number is 274.\n\n"
            f"{filler}\n\n"
            "ANCHOR_END_7391: The relay verification color is cobalt. "
            "The relay verification animal is otter.\n\n"
            "Question: What are the relay verification city, number, color, and animal?"
        )
        try:
            response = client.complete(prompt)
        except Exception as exc:
            return _exception_results(COMBINED_LONG_CONTEXT_CASES, exc)
        parsed = _parse_json_object(response.text)
        return [
            _grade(
                "long-anchor-middle-1",
                "long_context",
                _json_scalar(parsed, "city").lower() == "lisbon" and _json_scalar(parsed, "number") == "274",
                response.text,
                response.raw,
                response.latency_ms,
                response.content_type,
                response.usage,
                "expected city=lisbon and number=274",
            ),
            _grade(
                "long-anchor-end-1",
                "long_context",
                _json_scalar(parsed, "color").lower() == "cobalt" and _json_scalar(parsed, "animal").lower() == "otter",
                response.text,
                response.raw,
                response.latency_ms,
                response.content_type,
                response.usage,
                "expected color=cobalt and animal=otter",
            ),
        ]


@dataclass(slots=True)
class CombinedCodexGptProbe:
    name: str = "combined_codex_gpt"

    def run(self, client: ProviderClient) -> list[ProbeResult]:
        client_config = getattr(client, "config", None)
        current_profile = client_config.client_profile.value if client_config and client_config.client_profile else None
        if current_profile not in OPENAI_AGENT_PROFILES:
            return _skipped_results(
                COMBINED_CODEX_GPT_CASES,
                current_profile,
                OPENAI_AGENT_PROFILES,
            )

        prompt = (
            "Return valid JSON only. Do not use markdown. Complete these code-agent checks:\n"
            "1. patch_plan: one-line plan for this broken Python function: def add(a, b): return a - b. "
            "Name the bug and the fix.\n"
            "2. patch_diff: minimal unified diff that changes return a - b to return a + b.\n"
            "3. failure_root_cause: one sentence for this failure: AssertionError expected 4, got 5; "
            "Function under test add(2, 2).\n"
            "4. next_debug_step: likely next step for pytest output AssertionError: 2 != 3.\n"
            "5. review_risks: array of the two highest-risk issues for a change request that renames x to total, "
            "adds negative-number validation, and must keep the function signature unchanged."
        )
        try:
            response = client.complete(prompt)
        except Exception as exc:
            return _exception_results(COMBINED_CODEX_GPT_CASES, exc)
        parsed = _parse_json_object(response.text)
        patch_plan = _json_scalar(parsed, "patch_plan").lower()
        patch_diff = _json_scalar(parsed, "patch_diff")
        failure_root_cause = _json_scalar(parsed, "failure_root_cause").lower()
        next_debug_step = _json_scalar(parsed, "next_debug_step").lower()
        review_risks = json.dumps(parsed.get("review_risks", ""), ensure_ascii=False).lower()
        return [
            _grade(
                "codex-patch-plan-1",
                "codex_patch",
                "bug" in patch_plan and any(word in patch_plan for word in ("fix", "change", "replace")) and "a + b" in patch_plan,
                response.text,
                response.raw,
                response.latency_ms,
                response.content_type,
                response.usage,
                "expected patch_plan to name bug, fix, and a + b",
            ),
            _grade(
                "codex-patch-diff-1",
                "codex_patch",
                all(item in patch_diff for item in ("---", "+++", "@@", "+    return a + b", "-    return a - b")),
                response.text,
                response.raw,
                response.latency_ms,
                response.content_type,
                response.usage,
                "expected patch_diff to be a minimal unified diff",
            ),
            _grade(
                "codex-failure-log-1",
                "codex_failure",
                any(item in failure_root_cause for item in ("extra", "increment", "+1", "wrong", "incorrect", "bug")),
                response.text,
                response.raw,
                response.latency_ms,
                response.content_type,
                response.usage,
                "expected failure_root_cause to explain the likely add bug",
            ),
            _grade(
                "codex-failure-command-1",
                "codex_failure",
                any(item in next_debug_step for item in ("inspect", "check", "trace", "debug", "pytest", "failing test")),
                response.text,
                response.raw,
                response.latency_ms,
                response.content_type,
                response.usage,
                "expected next_debug_step to identify a debugging action",
            ),
            _grade(
                "codex-review-1",
                "codex_review",
                any(item in review_risks for item in ("signature", "validation", "negative", "risk")),
                response.text,
                response.raw,
                response.latency_ms,
                response.content_type,
                response.usage,
                "expected review_risks to mention signature or negative validation risk",
            ),
        ]


def _parse_json_object(text: str) -> dict[str, Any]:
    try:
        parsed = json.loads(_extract_json_candidate(text))
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _json_scalar(parsed: dict[str, Any], key: str) -> str:
    value = parsed.get(key)
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)):
        return str(value)
    return ""


def _valid_no_letter_words(value: str) -> bool:
    parts = [part.strip() for part in value.split(",")]
    return len(parts) == 4 and all(part.islower() and part.isalpha() and "e" not in part for part in parts)


def _grade(
    case_id: str,
    kind: str,
    passed: bool,
    response_text: str,
    raw: dict[str, Any],
    latency_ms: float,
    content_type: str,
    usage: dict[str, Any],
    failure_evidence: str,
) -> ProbeResult:
    return ProbeResult(
        case_id=case_id,
        kind=kind,
        status="passed" if passed else "failed",
        passed=passed,
        score=1.0 if passed else 0.0,
        evidence=(response_text.strip()[:500] if passed else f"{failure_evidence} | response={response_text.strip()[:420]}"),
        failure_category=None if passed else "format",
        metrics={
            "latency_ms": round(latency_ms, 2),
            "content_type": content_type,
            "usage": usage,
            "response_model": raw.get("model"),
            "batched": True,
        },
        raw_response=raw,
    )


def _exception_results(cases: tuple[tuple[str, str], ...], exc: Exception) -> list[ProbeResult]:
    failure_category = _categorize_exception(exc)
    return [
        ProbeResult(
            case_id=case_id,
            kind=kind,
            status="failed",
            passed=False,
            score=0.0,
            evidence=f"{type(exc).__name__}: {exc}",
            failure_category=failure_category,
            metrics={"error_type": type(exc).__name__, "batched": True},
        )
        for case_id, kind in cases
    ]


def _skipped_results(
    cases: tuple[tuple[str, str], ...],
    current_profile: str | None,
    supported_profiles: tuple[str, ...],
) -> list[ProbeResult]:
    return [
        ProbeResult(
            case_id=case_id,
            kind=kind,
            status="skipped",
            passed=False,
            score=0.0,
            evidence="Skipped because the current profile is not in supported profiles.",
            failure_category="unsupported",
            skipped_reason=(
                f"profile {current_profile or '-'} not in supported profiles: "
                f"{', '.join(supported_profiles)}"
            ),
            metrics={"batched": True},
        )
        for case_id, kind in cases
    ]
