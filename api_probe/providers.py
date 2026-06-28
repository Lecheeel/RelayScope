from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse
from time import perf_counter
from time import sleep
from typing import Any, Protocol
import json
import uuid

import httpx

from .config import ClientProfile, ProtocolMode, TargetConfig
from .debug_tools import log_debug_event

CLAUDE_CODE_USER_AGENT = "claude-cli/2.0.1 (external, cli)"
CLAUDE_CODE_BETA = "claude-code-20250219,oauth-2025-04-20,interleaved-thinking-2025-05-14,fine-grained-tool-streaming-2025-05-14"


@dataclass(slots=True)
class ProviderResponse:
    text: str
    raw: dict[str, Any]
    usage: dict[str, Any]
    latency_ms: float
    content_type: str = ""
    request: dict[str, Any] | None = None
    retries: int = 0
    transient_failures: list[str] | None = None


@dataclass(slots=True)
class ProviderStreamResponse:
    text: str
    raw_events: list[dict[str, Any]]
    usage: dict[str, Any]
    latency_ms: float
    first_token_ms: float | None
    chunk_count: int
    content_type: str = ""
    request: dict[str, Any] | None = None
    retries: int = 0
    transient_failures: list[str] | None = None


class ProviderClient(Protocol):
    def complete(
        self,
        prompt: str = "",
        *,
        messages: list[dict[str, Any]] | None = None,
        temperature: float = 0.0,
        max_tokens: int = 1024,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        response_format: dict[str, Any] | None = None,
        cache_control: bool = False,
        prompt_cache_key: str | None = None,
    ) -> ProviderResponse:
        ...

    def stream_complete(
        self,
        prompt: str = "",
        *,
        messages: list[dict[str, Any]] | None = None,
        temperature: float = 0.0,
        max_tokens: int = 1024,
    ) -> ProviderStreamResponse:
        ...


class OpenAICompatibleClient:
    def __init__(self, config: TargetConfig) -> None:
        self.config = config

    def complete(
        self,
        prompt: str = "",
        *,
        messages: list[dict[str, Any]] | None = None,
        temperature: float = 0.0,
        max_tokens: int = 1024,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        response_format: dict[str, Any] | None = None,
        cache_control: bool = False,
        prompt_cache_key: str | None = None,
    ) -> ProviderResponse:
        payload = {
            "model": self.config.model,
            "messages": messages or [{"role": "user", "content": prompt}],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if tools is not None:
            payload["tools"] = tools
        if tool_choice is not None:
            payload["tool_choice"] = tool_choice
        if response_format is not None:
            payload["response_format"] = _openai_chat_response_format(response_format)
        _ = cache_control
        if prompt_cache_key is not None:
            payload["prompt_cache_key"] = prompt_cache_key
        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
            **self.config.extra_headers,
        }
        url = self.config.base_url.rstrip("/") + "/chat/completions"
        started = perf_counter()
        response, retry_info = _post_json_with_retries(self.config, url, headers, payload)
        latency_ms = (perf_counter() - started) * 1000
        raw = _json_or_error(response)
        raw["_response_headers"] = dict(response.headers)
        request_info = _request_info("POST", url, headers, payload)
        raw["_request"] = request_info
        text = raw.get("choices", [{}])[0].get("message", {}).get("content") or ""
        log_debug_event(
            self.config,
            "provider.complete",
            {
                "method": "POST",
                "url": url,
                "status_code": response.status_code,
                "latency_ms": round(latency_ms, 2),
                "content_type": response.headers.get("content-type", ""),
                "request": request_info,
                "usage": raw.get("usage", {}),
                "response_model": raw.get("model"),
                "response_preview": text[:500],
                "retries": retry_info["retries"],
                "transient_failures": retry_info["transient_failures"],
            },
        )
        return ProviderResponse(
            text=text,
            raw=raw,
            usage=raw.get("usage", {}),
            latency_ms=latency_ms,
            content_type=response.headers.get("content-type", ""),
            request=request_info,
            retries=retry_info["retries"],
            transient_failures=retry_info["transient_failures"],
        )

    def stream_complete(
        self,
        prompt: str = "",
        *,
        messages: list[dict[str, Any]] | None = None,
        temperature: float = 0.0,
        max_tokens: int = 1024,
    ) -> ProviderStreamResponse:
        payload = {
            "model": self.config.model,
            "messages": messages or [{"role": "user", "content": prompt}],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
            **self.config.extra_headers,
        }
        url = self.config.base_url.rstrip("/") + "/chat/completions"
        started = perf_counter()
        first_token_ms: float | None = None
        chunks: list[str] = []
        events: list[dict[str, Any]] = []
        usage: dict[str, Any] = {}
        content_type = ""
        retry_info = {"retries": 0, "transient_failures": []}
        with httpx.Client(timeout=self.config.timeout_seconds) as client:
            with client.stream("POST", url, headers=headers, json=payload) as response:
                _raise_for_status(response)
                content_type = response.headers.get("content-type", "")
                for event in _iter_sse_json(response):
                    events.append(event)
                    if isinstance(event.get("usage"), dict):
                        usage = event["usage"]
                    for choice in event.get("choices", []) or []:
                        delta = choice.get("delta", {}) if isinstance(choice, dict) else {}
                        text = delta.get("content")
                        if isinstance(text, str) and text:
                            if first_token_ms is None:
                                first_token_ms = (perf_counter() - started) * 1000
                            chunks.append(text)
        latency_ms = (perf_counter() - started) * 1000
        request_info = _request_info("POST", url, headers, payload)
        log_debug_event(
            self.config,
            "provider.stream_complete",
            {
                "method": "POST",
                "url": url,
                "latency_ms": round(latency_ms, 2),
                "first_token_ms": None if first_token_ms is None else round(first_token_ms, 2),
                "chunk_count": len(events),
                "content_type": content_type,
                "request": request_info,
                "usage": usage,
                "response_preview": "".join(chunks)[:500],
            },
        )
        return ProviderStreamResponse(
            text="".join(chunks),
            raw_events=events,
            usage=usage,
            latency_ms=latency_ms,
            first_token_ms=first_token_ms,
            chunk_count=len(events),
            content_type=content_type,
            request=request_info,
            retries=retry_info["retries"],
            transient_failures=retry_info["transient_failures"],
        )


class OpenAIResponsesClient:
    def __init__(self, config: TargetConfig) -> None:
        self.config = config

    def complete(
        self,
        prompt: str = "",
        *,
        messages: list[dict[str, Any]] | None = None,
        temperature: float = 0.0,
        max_tokens: int = 1024,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        response_format: dict[str, Any] | None = None,
        cache_control: bool = False,
        prompt_cache_key: str | None = None,
    ) -> ProviderResponse:
        payload = {
            "model": self.config.model,
            "input": messages or prompt,
            "temperature": temperature,
            "max_output_tokens": max_tokens,
        }
        if tools is not None:
            payload["tools"] = _responses_tools(tools)
        if tool_choice is not None:
            payload["tool_choice"] = tool_choice
        if response_format is not None:
            payload["text"] = {"format": _responses_text_format(response_format)}
        _ = cache_control
        _ = prompt_cache_key
        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
            "User-Agent": "api-probe codex-responses profile",
            **self.config.extra_headers,
        }
        url = self.config.base_url.rstrip("/") + "/responses"
        started = perf_counter()
        response, retry_info = _post_json_with_retries(self.config, url, headers, payload)
        latency_ms = (perf_counter() - started) * 1000
        raw = _json_or_error(response)
        raw["_response_headers"] = dict(response.headers)
        request_info = _request_info("POST", url, headers, payload)
        raw["_request"] = request_info
        text = _extract_responses_text(raw)
        log_debug_event(
            self.config,
            "provider.complete",
            {
                "method": "POST",
                "url": url,
                "status_code": response.status_code,
                "latency_ms": round(latency_ms, 2),
                "content_type": response.headers.get("content-type", ""),
                "request": request_info,
                "usage": raw.get("usage", {}),
                "response_model": raw.get("model"),
                "response_preview": text[:500],
                "retries": retry_info["retries"],
                "transient_failures": retry_info["transient_failures"],
            },
        )
        return ProviderResponse(
            text=text,
            raw=raw,
            usage=raw.get("usage", {}),
            latency_ms=latency_ms,
            content_type=response.headers.get("content-type", ""),
            request=request_info,
            retries=retry_info["retries"],
            transient_failures=retry_info["transient_failures"],
        )

    def stream_complete(
        self,
        prompt: str = "",
        *,
        messages: list[dict[str, Any]] | None = None,
        temperature: float = 0.0,
        max_tokens: int = 1024,
    ) -> ProviderStreamResponse:
        payload = {
            "model": self.config.model,
            "input": messages or prompt,
            "temperature": temperature,
            "max_output_tokens": max_tokens,
            "stream": True,
        }
        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
            "User-Agent": "api-probe codex-responses profile",
            **self.config.extra_headers,
        }
        url = self.config.base_url.rstrip("/") + "/responses"
        started = perf_counter()
        first_token_ms: float | None = None
        chunks: list[str] = []
        events: list[dict[str, Any]] = []
        usage: dict[str, Any] = {}
        content_type = ""
        with httpx.Client(timeout=self.config.timeout_seconds) as client:
            with client.stream("POST", url, headers=headers, json=payload) as response:
                _raise_for_status(response)
                content_type = response.headers.get("content-type", "")
                for event in _iter_sse_json(response):
                    events.append(event)
                    if isinstance(event.get("response"), dict) and isinstance(event["response"].get("usage"), dict):
                        usage = event["response"]["usage"]
                    if isinstance(event.get("usage"), dict):
                        usage = event["usage"]
                    delta = _responses_stream_text(event)
                    if isinstance(delta, str) and delta:
                        if first_token_ms is None:
                            first_token_ms = (perf_counter() - started) * 1000
                        chunks.append(delta)
        latency_ms = (perf_counter() - started) * 1000
        request_info = _request_info("POST", url, headers, payload)
        log_debug_event(
            self.config,
            "provider.stream_complete",
            {
                "method": "POST",
                "url": url,
                "latency_ms": round(latency_ms, 2),
                "first_token_ms": None if first_token_ms is None else round(first_token_ms, 2),
                "chunk_count": len(events),
                "content_type": content_type,
                "request": request_info,
                "usage": usage,
                "response_preview": "".join(chunks)[:500],
            },
        )
        return ProviderStreamResponse(
            text="".join(chunks),
            raw_events=events,
            usage=usage,
            latency_ms=latency_ms,
            first_token_ms=first_token_ms,
            chunk_count=len(events),
            content_type=content_type,
            request=request_info,
        )


class AnthropicClient:
    def __init__(self, config: TargetConfig) -> None:
        self.config = config

    def complete(
        self,
        prompt: str = "",
        *,
        messages: list[dict[str, Any]] | None = None,
        temperature: float = 0.0,
        max_tokens: int = 1024,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        response_format: dict[str, Any] | None = None,
        cache_control: bool = False,
        prompt_cache_key: str | None = None,
    ) -> ProviderResponse:
        payload = {
            "model": self.config.model,
            "messages": _anthropic_messages(messages or [{"role": "user", "content": prompt}], cache_control=cache_control),
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools is not None:
            payload["tools"] = _anthropic_tools(tools)
        if tool_choice is not None:
            payload["tool_choice"] = _anthropic_tool_choice(tool_choice)
        # Anthropic Messages API does not use OpenAI-style response_format.
        _ = response_format
        _ = prompt_cache_key
        headers = {
            "anthropic-version": self.config.extra_headers.get("anthropic-version", "2023-06-01"),
            "Content-Type": "application/json",
            **self.config.extra_headers,
        }
        if self.config.client_profile == ClientProfile.CLAUDE_CODE:
            headers.update(_claude_code_headers(self.config))
        else:
            headers["x-api-key"] = self.config.api_key
        url = self.config.base_url.rstrip("/") + "/messages"
        started = perf_counter()
        response, retry_info = _post_json_with_retries(self.config, url, headers, payload)
        latency_ms = (perf_counter() - started) * 1000
        raw = _json_or_error(response)
        raw["_response_headers"] = dict(response.headers)
        request_info = _request_info("POST", url, headers, payload)
        raw["_request"] = request_info
        text_parts = [
            part.get("text", "")
            for part in raw.get("content", [])
            if isinstance(part, dict) and part.get("type") == "text"
        ]
        text = "".join(text_parts)
        log_debug_event(
            self.config,
            "provider.complete",
            {
                "method": "POST",
                "url": url,
                "status_code": response.status_code,
                "latency_ms": round(latency_ms, 2),
                "content_type": response.headers.get("content-type", ""),
                "request": request_info,
                "usage": raw.get("usage", {}),
                "response_model": raw.get("model"),
                "response_preview": text[:500],
                "retries": retry_info["retries"],
                "transient_failures": retry_info["transient_failures"],
            },
        )
        return ProviderResponse(
            text=text,
            raw=raw,
            usage=raw.get("usage", {}),
            latency_ms=latency_ms,
            content_type=response.headers.get("content-type", ""),
            request=request_info,
            retries=retry_info["retries"],
            transient_failures=retry_info["transient_failures"],
        )

    def stream_complete(
        self,
        prompt: str = "",
        *,
        messages: list[dict[str, Any]] | None = None,
        temperature: float = 0.0,
        max_tokens: int = 1024,
    ) -> ProviderStreamResponse:
        payload = {
            "model": self.config.model,
            "messages": _anthropic_messages(messages or [{"role": "user", "content": prompt}]),
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
        }
        headers = {
            "anthropic-version": self.config.extra_headers.get("anthropic-version", "2023-06-01"),
            "Content-Type": "application/json",
            **self.config.extra_headers,
        }
        if self.config.client_profile == ClientProfile.CLAUDE_CODE:
            headers.update(_claude_code_headers(self.config))
        else:
            headers["x-api-key"] = self.config.api_key
        url = self.config.base_url.rstrip("/") + "/messages"
        started = perf_counter()
        first_token_ms: float | None = None
        chunks: list[str] = []
        events: list[dict[str, Any]] = []
        usage: dict[str, Any] = {}
        content_type = ""
        with httpx.Client(timeout=self.config.timeout_seconds) as client:
            with client.stream("POST", url, headers=headers, json=payload) as response:
                _raise_for_status(response)
                content_type = response.headers.get("content-type", "")
                for event in _iter_sse_json(response):
                    events.append(event)
                    if isinstance(event.get("usage"), dict):
                        usage.update(event["usage"])
                    if isinstance(event.get("message"), dict) and isinstance(event["message"].get("usage"), dict):
                        usage.update(event["message"]["usage"])
                    delta = event.get("delta", {})
                    text = delta.get("text") if isinstance(delta, dict) else None
                    if text is None and isinstance(event.get("content_block"), dict):
                        text = event["content_block"].get("text")
                    if isinstance(text, str) and text:
                        if first_token_ms is None:
                            first_token_ms = (perf_counter() - started) * 1000
                        chunks.append(text)
        latency_ms = (perf_counter() - started) * 1000
        request_info = _request_info("POST", url, headers, payload)
        log_debug_event(
            self.config,
            "provider.stream_complete",
            {
                "method": "POST",
                "url": url,
                "latency_ms": round(latency_ms, 2),
                "first_token_ms": None if first_token_ms is None else round(first_token_ms, 2),
                "chunk_count": len(events),
                "content_type": content_type,
                "request": request_info,
                "usage": usage,
                "response_preview": "".join(chunks)[:500],
            },
        )
        return ProviderStreamResponse(
            text="".join(chunks),
            raw_events=events,
            usage=usage,
            latency_ms=latency_ms,
            first_token_ms=first_token_ms,
            chunk_count=len(events),
            content_type=content_type,
            request=request_info,
        )


def build_client(config: TargetConfig) -> ProviderClient:
    if config.protocol_mode == ProtocolMode.OPENAI_RESPONSES:
        return OpenAIResponsesClient(config)
    if config.provider_family.value == "openai":
        return OpenAICompatibleClient(config)
    if config.provider_family.value == "anthropic":
        return AnthropicClient(config)
    raise ValueError(f"Unsupported provider family: {config.provider_family}")


def _claude_code_headers(config: TargetConfig) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {config.api_key}",
        "User-Agent": config.extra_headers.get("User-Agent", CLAUDE_CODE_USER_AGENT),
        "x-app": config.extra_headers.get("x-app", "cli"),
        "anthropic-beta": config.extra_headers.get("anthropic-beta", CLAUDE_CODE_BETA),
        "x-claude-code-session-id": config.metadata.setdefault("claude_code_session_id", str(uuid.uuid4())),
    }


def _json_or_error(response: httpx.Response) -> dict[str, Any]:
    content_type = response.headers.get("content-type", "")
    try:
        return response.json()
    except ValueError as exc:
        preview = response.text[:300].replace("\n", " ")
        hint = ""
        if "text/html" in content_type.lower():
            parsed = urlparse(str(response.request.url))
            if parsed.path.rstrip("/") in {"", "/chat/completions", "/messages", "/responses"}:
                hint = " This endpoint returned HTML; the base_url may point at the site root instead of the API root. Try adding /v1."
        raise RuntimeError(
            f"Expected JSON response, got content-type={content_type!r}, "
            f"status={response.status_code}, body_preview={preview!r}{hint}"
        ) from exc


def _raise_for_status(response: httpx.Response) -> None:
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        preview = response.text[:500].replace("\n", " ")
        raise httpx.HTTPStatusError(
            f"{exc} | body_preview={preview!r}",
            request=exc.request,
            response=exc.response,
        ) from exc


def _post_json_with_retries(
    config: TargetConfig,
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
) -> tuple[httpx.Response, dict[str, Any]]:
    failures: list[str] = []
    max_retries = max(0, int(getattr(config, "max_retries", 2)))
    backoff = max(0.0, float(getattr(config, "retry_backoff_seconds", 0.8)))
    for attempt in range(max_retries + 1):
        try:
            with httpx.Client(timeout=config.timeout_seconds) as client:
                response = client.post(url, headers=headers, json=payload)
                _raise_for_status(response)
                return response, {"retries": attempt, "transient_failures": failures}
        except Exception as exc:
            if not _is_retryable_exception(exc) or attempt >= max_retries:
                raise
            failures.append(f"{type(exc).__name__}: {str(exc)[:300]}")
            sleep(backoff * (2 ** attempt))
    raise RuntimeError("unreachable retry state")


def _is_retryable_exception(exc: Exception) -> bool:
    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.RemoteProtocolError, httpx.PoolTimeout)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status in {408, 429, 500, 502, 503, 504}:
            return True
        return False
    message = str(exc).lower()
    retry_markers = (
        "connection reset",
        "connection aborted",
        "temporarily unavailable",
        "timed out",
        "timeout",
        "bad gateway",
        "service unavailable",
        "gateway timeout",
        "remote protocol",
    )
    non_retry_markers = (
        "401",
        "403",
        "model_not_found",
        "no available channel for model",
        "invalid_request",
        "unsupported",
    )
    if any(marker in message for marker in non_retry_markers):
        return False
    return any(marker in message for marker in retry_markers)


def _request_info(method: str, url: str, headers: dict[str, str], payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "method": method,
        "url": url,
        "headers": _redact_headers(headers),
        "json": payload,
    }


def _redact_headers(headers: dict[str, str]) -> dict[str, str]:
    redacted = {}
    for key, value in headers.items():
        lowered = key.lower()
        if lowered in {"authorization", "x-api-key", "api-key"}:
            redacted[key] = "***REDACTED***"
        else:
            redacted[key] = value
    return redacted


def _iter_sse_json(response: httpx.Response) -> list[dict[str, Any]]:
    for line in response.iter_lines():
        if not line.startswith("data:"):
            continue
        data = line.removeprefix("data:").strip()
        if not data or data == "[DONE]":
            continue
        try:
            parsed = json.loads(data)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            yield parsed


def _extract_responses_text(raw: dict[str, Any]) -> str:
    if isinstance(raw.get("output_text"), str):
        return raw["output_text"]
    chunks: list[str] = []
    for item in raw.get("output", []):
        if not isinstance(item, dict):
            continue
        for part in item.get("content", []):
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                chunks.append(part["text"])
    return "".join(chunks)


def _responses_stream_text(event: dict[str, Any]) -> str | None:
    delta = event.get("delta")
    if isinstance(delta, str):
        return delta
    if event.get("type") == "response.output_text.delta" and isinstance(event.get("text"), str):
        return event["text"]
    output_text = event.get("output_text")
    if isinstance(output_text, str):
        return output_text
    return None


def _responses_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    converted: list[dict[str, Any]] = []
    for tool in tools:
        if tool.get("type") != "function":
            converted.append(tool)
            continue
        function = tool.get("function", {})
        converted.append(
            {
                "type": "function",
                "name": function.get("name"),
                "description": function.get("description", ""),
                "parameters": function.get("parameters", {}),
            }
        )
    return converted


def _openai_chat_response_format(response_format: dict[str, Any]) -> dict[str, Any]:
    if response_format.get("type") != "json_schema":
        return response_format
    if "json_schema" in response_format:
        return response_format
    return {
        "type": "json_schema",
        "json_schema": {
            "name": response_format.get("name", "api_probe_result"),
            "schema": response_format.get("schema", {}),
            "strict": response_format.get("strict", True),
        },
    }


def _responses_text_format(response_format: dict[str, Any]) -> dict[str, Any]:
    if response_format.get("type") != "json_schema":
        return response_format
    if "json_schema" in response_format:
        json_schema = response_format["json_schema"]
        return {
            "type": "json_schema",
            "name": json_schema.get("name", "api_probe_result"),
            "schema": json_schema.get("schema", {}),
            "strict": json_schema.get("strict", response_format.get("strict", True)),
        }
    return {
        "type": "json_schema",
        "name": response_format.get("name", "api_probe_result"),
        "schema": response_format.get("schema", {}),
        "strict": response_format.get("strict", True),
    }


def _anthropic_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    converted: list[dict[str, Any]] = []
    for tool in tools:
        if tool.get("type") != "function":
            converted.append(tool)
            continue
        function = tool.get("function", {})
        converted.append(
            {
                "name": function.get("name"),
                "description": function.get("description", ""),
                "input_schema": function.get("parameters", {}),
            }
        )
    return converted


def _anthropic_tool_choice(tool_choice: str | dict[str, Any]) -> dict[str, Any] | str:
    if isinstance(tool_choice, dict):
        function = tool_choice.get("function", {})
        if tool_choice.get("type") == "function" and function.get("name"):
            return {"type": "tool", "name": function["name"]}
        return tool_choice
    if tool_choice == "auto":
        return {"type": "auto"}
    if tool_choice == "required":
        return {"type": "any"}
    return tool_choice


def _anthropic_messages(messages: list[dict[str, Any]], *, cache_control: bool = False) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    last_index = len(messages) - 1
    for index, message in enumerate(messages):
        role = message.get("role", "user")
        content = message.get("content", "")
        if cache_control and index == last_index and role == "user" and isinstance(content, str):
            normalized.append(
                {
                    "role": role,
                    "content": [
                        {
                            "type": "text",
                            "text": content,
                            "cache_control": {"type": "ephemeral"},
                        }
                    ],
                }
            )
        elif cache_control and index == last_index and role == "user" and isinstance(content, list):
            normalized.append({"role": role, "content": _with_anthropic_cache_control(content)})
        elif isinstance(content, str):
            normalized.append({"role": role, "content": content})
        else:
            normalized.append({"role": role, "content": content})
    return normalized


def _with_anthropic_cache_control(content: list[Any]) -> list[Any]:
    copied = [dict(part) if isinstance(part, dict) else part for part in content]
    for part in reversed(copied):
        if isinstance(part, dict) and part.get("type") in {"text", "document", "image"}:
            part.setdefault("cache_control", {"type": "ephemeral"})
            break
    return copied
