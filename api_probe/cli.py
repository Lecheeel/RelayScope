from __future__ import annotations

import argparse

from .client_compat_probes import ClientCompatibilityProbe
from .config import ClientProfile, ProviderFamily, ProtocolMode, RunConfig, TargetConfig, load_run_configs, normalize_base_url
from .models import RunReport
from .providers import build_client
from .probes import (
    StructuredOutputProbe,
    ToolCompatibilityProbe,
    run_probe,
)
from .cache_probes import CacheIntegrityProbe
from .identity_probes import IdentityInspectorProbe
from .multimodal_probes import MultimodalCapabilityProbe, PdfCacheProbe
from .streaming_probes import StreamingLatencyProbe
from .reporting import summarize_report, write_report


def build_default_target(
    name: str,
    base_url: str,
    api_key: str,
    model: str,
    provider: str,
    profile: str | None,
) -> TargetConfig:
    family = ProviderFamily(provider)
    client_profile = ClientProfile(profile) if profile else None
    if client_profile == ClientProfile.CODEX_RESPONSES:
        mode = ProtocolMode.OPENAI_RESPONSES
    elif family == ProviderFamily.OPENAI:
        mode = ProtocolMode.OPENAI_COMPAT
    else:
        mode = ProtocolMode.ANTHROPIC_NATIVE
    return TargetConfig(
        name=name,
        provider_family=family,
        protocol_mode=mode,
        base_url=normalize_base_url(base_url),
        api_key=api_key,
        model=model,
        client_profile=client_profile,
    )


def main() -> int:
    parser = argparse.ArgumentParser(prog="api-probe")
    parser.add_argument("--provider", choices=["openai", "anthropic"])
    parser.add_argument("--base-url")
    parser.add_argument("--api-key")
    parser.add_argument("--model")
    parser.add_argument(
        "--profile",
        choices=["openai-chat", "codex-responses", "anthropic-messages", "claude-code"],
    )
    parser.add_argument("--name", default="target")
    parser.add_argument("--output-dir", default="runs")
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--config")
    args = parser.parse_args()

    run_configs = _resolve_run_configs(args)
    for run_config in run_configs:
        client = build_client(run_config.target)
        results = []
        for probe in build_probes():
            results.extend(run_probe(probe, client))
        report = RunReport(
            target_name=run_config.target.name,
            baseline_name=None,
            profile=run_config.target.client_profile.value if run_config.target.client_profile else None,
            results=results,
            summary={
                "probe_count": len(results),
                "passed": sum(1 for r in results if r.status == "passed"),
                "failed": sum(1 for r in results if r.status == "failed"),
                "skipped": sum(1 for r in results if r.status == "skipped"),
            },
        )
        path = write_report(report, run_config.output_dir)
        print(summarize_report(report))
        print(path)
    return 0


def build_probes() -> list[object]:
    return [
        IdentityInspectorProbe(),
        StructuredOutputProbe(),
        ClientCompatibilityProbe(),
        ToolCompatibilityProbe(),
        StreamingLatencyProbe(),
        MultimodalCapabilityProbe(),
        PdfCacheProbe(),
        CacheIntegrityProbe(),
    ]


def _resolve_run_configs(args: argparse.Namespace) -> list[RunConfig]:
    if args.config:
        return load_run_configs(args.config)
    missing = [name for name in ("provider", "base_url", "api_key", "model") if getattr(args, name) is None]
    if missing:
        raise SystemExit(f"Missing required args without --config: {', '.join(missing)}")
    target = build_default_target(args.name, args.base_url, args.api_key, args.model, args.provider, args.profile)
    target.timeout_seconds = args.timeout
    return [RunConfig(target=target, output_dir=args.output_dir)]


if __name__ == "__main__":
    raise SystemExit(main())
