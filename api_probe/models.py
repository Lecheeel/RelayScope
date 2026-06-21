from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class ProbeCase:
    id: str
    kind: str
    prompt: str = ""
    messages: list[dict[str, Any]] = field(default_factory=list)
    expected: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    request_options: dict[str, Any] = field(default_factory=dict)
    repeat: int = 1


@dataclass(slots=True)
class ProbeResult:
    case_id: str
    kind: str
    score: float
    evidence: str
    status: str = ""
    passed: bool = False
    failure_category: str | None = None
    skipped_reason: str | None = None
    metrics: dict[str, Any] = field(default_factory=dict)
    raw_response: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class RunReport:
    target_name: str
    baseline_name: str | None
    profile: str | None
    results: list[ProbeResult]
    summary: dict[str, Any] = field(default_factory=dict)
