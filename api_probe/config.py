from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse

import yaml


class ProviderFamily(str, Enum):
    OPENAI = "openai"
    ANTHROPIC = "anthropic"


class ProtocolMode(str, Enum):
    OPENAI_COMPAT = "openai_compat"
    OPENAI_RESPONSES = "openai_responses"
    ANTHROPIC_NATIVE = "anthropic_native"
    ANTHROPIC_COMPAT = "anthropic_compat"


class ClientProfile(str, Enum):
    OPENAI_CHAT = "openai-chat"
    CODEX_RESPONSES = "codex-responses"
    ANTHROPIC_MESSAGES = "anthropic-messages"
    CLAUDE_CODE = "claude-code"


@dataclass(slots=True)
class TargetConfig:
    name: str
    provider_family: ProviderFamily
    protocol_mode: ProtocolMode
    base_url: str
    api_key: str
    model: str
    client_profile: ClientProfile | None = None
    extra_headers: dict[str, str] = field(default_factory=dict)
    timeout_seconds: float = 60.0
    max_retries: int = 2
    retry_backoff_seconds: float = 0.8
    cache_probe_delay_seconds: float = 1.2
    stream: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class RunConfig:
    target: TargetConfig
    baseline: TargetConfig | None = None
    output_dir: str = "runs"
    sample_limit: int = 20


def load_run_configs(path: str) -> list[RunConfig]:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    targets = data.get("targets", [])
    output_dir = data.get("output_dir", "runs")
    run_configs: list[RunConfig] = []
    for item in targets:
        family = ProviderFamily(item["provider_family"])
        client_profile = ClientProfile(item["client_profile"]) if item.get("client_profile") else None
        protocol_mode = ProtocolMode(item["protocol_mode"])
        target = TargetConfig(
            name=item["name"],
            provider_family=family,
            protocol_mode=protocol_mode,
            base_url=normalize_base_url(item["base_url"]),
            api_key=_expand_env(item["api_key"]),
            model=item["model"],
            client_profile=client_profile,
            extra_headers=item.get("extra_headers", {}),
            timeout_seconds=float(item.get("timeout_seconds", 60.0)),
            max_retries=int(item.get("max_retries", 2)),
            retry_backoff_seconds=float(item.get("retry_backoff_seconds", 0.8)),
            cache_probe_delay_seconds=float(item.get("cache_probe_delay_seconds", 1.2)),
            metadata=_debug_metadata(item),
        )
        profiles = item.get("profiles")
        if profiles:
            for profile_name in profiles:
                profile = ClientProfile(profile_name)
                profiled = TargetConfig(
                    name=f"{target.name}-{profile.value}",
                    provider_family=target.provider_family,
                    protocol_mode=_protocol_mode_for_profile(target.provider_family, profile),
                    base_url=target.base_url,
                    api_key=target.api_key,
                    model=target.model,
                    client_profile=profile,
                    extra_headers=dict(target.extra_headers),
                    timeout_seconds=target.timeout_seconds,
                    max_retries=target.max_retries,
                    retry_backoff_seconds=target.retry_backoff_seconds,
                    cache_probe_delay_seconds=target.cache_probe_delay_seconds,
                    metadata=dict(target.metadata),
                )
                run_configs.append(RunConfig(target=profiled, output_dir=output_dir))
            continue
        run_configs.append(RunConfig(target=target, output_dir=output_dir))
    return run_configs


def _expand_env(value: str) -> str:
    if value.startswith("${") and value.endswith("}"):
        import os

        return os.environ[value[2:-1]]
    return value


def _debug_metadata(item: dict[str, Any]) -> dict[str, Any]:
    metadata = dict(item.get("metadata", {}))
    if bool(item.get("debug_mode", False)):
        metadata["debug_mode"] = True
        if isinstance(item.get("debug_log_path"), str):
            metadata["debug_log_path"] = item["debug_log_path"]
    return metadata


def _protocol_mode_for_profile(family: ProviderFamily, profile: ClientProfile) -> ProtocolMode:
    if profile == ClientProfile.CODEX_RESPONSES:
        return ProtocolMode.OPENAI_RESPONSES
    if family == ProviderFamily.OPENAI:
        return ProtocolMode.OPENAI_COMPAT
    return ProtocolMode.ANTHROPIC_NATIVE


def normalize_base_url(base_url: str) -> str:
    parsed = urlparse(base_url)
    path = parsed.path.rstrip("/")
    if path in {"", "/"}:
        parsed = parsed._replace(path="/v1")
        return urlunparse(parsed)
    return base_url.rstrip("/")
