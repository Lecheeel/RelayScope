from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from .models import RunReport


def write_report(report: RunReport, output_dir: str) -> Path:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{report.target_name}.json"
    path.write_text(json.dumps(asdict(report), ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def summarize_report(report: RunReport) -> str:
    total = len(report.results)
    passed = sum(1 for item in report.results if item.status == "passed")
    skipped = sum(1 for item in report.results if item.status == "skipped")
    failed = total - passed - skipped
    by_kind: dict[str, dict[str, int]] = {}
    for item in report.results:
        bucket = by_kind.setdefault(item.kind, {"total": 0, "passed": 0, "skipped": 0})
        bucket["total"] += 1
        bucket["passed"] += int(item.status == "passed")
        bucket["skipped"] += int(item.status == "skipped")
    lines = [
        f"target: {report.target_name}",
        f"profile: {report.profile}",
        f"total: {total}",
        f"passed: {passed}",
        f"failed: {failed}",
        f"skipped: {skipped}",
    ]
    for kind, stats in sorted(by_kind.items()):
        suffix = f" ({stats['skipped']} skipped)" if stats["skipped"] else ""
        lines.append(f"{kind}: {stats['passed']}/{stats['total']}{suffix}")
    return "\n".join(lines)
