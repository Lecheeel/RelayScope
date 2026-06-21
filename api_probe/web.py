from __future__ import annotations

import json
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

from .cli import build_default_target, build_probes
from .config import normalize_base_url
from .providers import build_client
from .probes import run_probe
from .usage_metrics import LatencyStats, has_cache_metric, parse_usage


MODEL_OPTIONS = {
    "sonnet4.6": {"api_model": "claude-sonnet-4-6", "provider": "anthropic", "profile": "anthropic-messages"},
    "sonnet4.7": {"api_model": "claude-sonnet-4-7", "provider": "anthropic", "profile": "anthropic-messages"},
    "gpt5.4": {"api_model": "gpt-5.4", "provider": "openai", "profile": "openai-chat"},
    "gpt5.5": {"api_model": "gpt-5.5", "provider": "openai", "profile": "openai-chat"},
}

JOBS: dict[str, dict[str, Any]] = {}
JOBS_LOCK = threading.Lock()


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="api-probe-web")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), ProbeRequestHandler)
    print(f"API Probe UI running at http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down API Probe UI")
    finally:
        server.server_close()
    return 0


class ProbeRequestHandler(BaseHTTPRequestHandler):
    server_version = "APIProbeWeb/0.1"

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path in {"/", "/index.html"}:
            self._send_static("index.html", "text/html; charset=utf-8")
            return
        if parsed.path == "/advanced.html":
            self._send_static("advanced.html", "text/html; charset=utf-8")
            return
        if parsed.path == "/settings":
            self._send_static("advanced.html", "text/html; charset=utf-8")
            return
        if parsed.path == "/styles.css":
            self._send_static("styles.css", "text/css; charset=utf-8")
            return
        if parsed.path == "/app.js":
            self._send_static("app.js", "application/javascript; charset=utf-8")
            return
        if parsed.path in {"/api.js", "/utils.js"}:
            self._send_static(parsed.path.lstrip("/"), "application/javascript; charset=utf-8")
            return
        if parsed.path == "/api/probe/status":
            query = parse_qs(parsed.query)
            job_id = query.get("job_id", [""])[0]
            job = get_probe_job(job_id)
            if job is None:
                self._send_json({"error": "Probe job not found."}, HTTPStatus.NOT_FOUND)
                return
            self._send_json(job)
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        if self.path not in {"/api/probe", "/api/probe/start", "/api/models"}:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            payload = self._read_json()
            if self.path == "/api/probe/start":
                response = start_probe_job(payload)
            elif self.path == "/api/models":
                response = fetch_model_names(payload)
            else:
                response = run_web_probe(payload)
        except ValueError as exc:
            self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        except Exception as exc:
            self._send_json({"error": f"{type(exc).__name__}: {exc}"}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        self._send_json(response)

    def log_message(self, format: str, *args: Any) -> None:
        if self.path in {"/api/probe", "/api/probe/start", "/api/models"} or self.path.startswith("/api/probe/status"):
            return
        super().log_message(format, *args)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("content-length", "0"))
        if length <= 0:
            raise ValueError("Request body is required.")
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError("Request body must be valid JSON.") from exc
        if not isinstance(data, dict):
            raise ValueError("Request body must be a JSON object.")
        return data

    def _send_static(self, filename: str, content_type: str) -> None:
        data = resources.files("api_probe.static").joinpath(filename).read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("content-type", content_type)
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def run_web_probe(payload: dict[str, Any]) -> dict[str, Any]:
    targets, selected_model = build_web_targets(payload)
    max_concurrency = _max_concurrency(payload)
    results = run_targets_probes(targets, None, max_concurrency=max_concurrency)
    summary = summarize_web_results(results)
    summary["stop_reason"] = _stop_reason(results)
    summary["stopped_early"] = summary["stop_reason"] is not None
    return build_probe_response(targets, selected_model, summary, results)


def start_probe_job(payload: dict[str, Any]) -> dict[str, Any]:
    targets, selected_model = build_web_targets(payload)
    max_concurrency = _max_concurrency(payload)
    probes = build_probes()
    total_probes = len(probes) * len(targets)
    job_id = uuid.uuid4().hex
    job = {
        "job_id": job_id,
        "status": "running",
        "target": _targets_payload(targets, selected_model),
        "progress": {
            "current_probe": None,
            "completed_probes": 0,
            "total_probes": total_probes,
            "completed_results": 0,
        },
        "summary": None,
        "results": [],
        "error": None,
        "settings": {"max_concurrency": max_concurrency, "timeout_seconds": targets[0].timeout_seconds},
    }
    with JOBS_LOCK:
        JOBS[job_id] = job
    thread = threading.Thread(target=_run_probe_job, args=(job_id, targets, selected_model, probes, max_concurrency), daemon=True)
    thread.start()
    return {"job_id": job_id}


def get_probe_job(job_id: str) -> dict[str, Any] | None:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job is None:
            return None
        return json.loads(json.dumps(job, ensure_ascii=False))


def build_web_targets(payload: dict[str, Any]) -> tuple[list[Any], str]:
    base_url = _required_string(payload, "base_url")
    api_key = _required_string(payload, "api_key")
    model = _required_string(payload, "model")
    timeout = float(payload.get("timeout_seconds", 60))
    max_retries = _max_retries(payload)
    retry_backoff = _retry_backoff(payload)
    cache_probe_delay = _cache_probe_delay(payload)
    provider_family = _optional_provider_family(payload.get("provider_family", payload.get("channel")))
    client_profile = _optional_client_profile(payload.get("client_profile", payload.get("profile")))
    target_profiles = _target_profiles_for_provider(provider_family, client_profile)
    if not target_profiles:
        target_profiles = ["codex-responses"]
    targets = []
    for target_profile in target_profiles:
        model_config = _resolve_model_config(model, target_profile)
        api_model = model_config["api_model"]
        target = build_default_target(
            name=f"web-{model}-{target_profile}",
            base_url=base_url,
            api_key=api_key,
            model=api_model,
            provider=model_config["provider"],
            profile=model_config["profile"],
        )
        target.timeout_seconds = timeout
        target.max_retries = max_retries
        target.retry_backoff_seconds = retry_backoff
        target.cache_probe_delay_seconds = cache_probe_delay
        targets.append(target)
    return targets, model


def _optional_provider_family(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    if not normalized or normalized == "auto":
        return None
    allowed = {"gpt", "anthropic"}
    if normalized not in allowed:
        raise ValueError(f"Unsupported provider family: {normalized}")
    return normalized


def _optional_client_profile(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if not normalized or normalized == "auto":
        return None
    allowed = {"openai-chat", "codex-responses", "anthropic-messages", "claude-code"}
    if normalized not in allowed:
        raise ValueError(f"Unsupported client profile: {normalized}")
    return normalized


def _target_profiles_for_provider(provider_family: str | None, profile: str | None) -> list[str]:
    if profile:
        return [profile]
    if provider_family == "anthropic":
        return ["claude-code"]
    if provider_family == "gpt":
        return ["codex-responses"]
    return ["codex-responses"]


def run_targets_probes(targets: list[Any], job_id: str | None, *, max_concurrency: int = 1) -> list[Any]:
    results = []
    completed_offset = 0
    total_per_target = len(build_probes())
    for target in targets:
        target_results = run_target_probes(
            target,
            job_id,
            max_concurrency=max_concurrency,
            completed_offset=completed_offset,
            total_per_target=total_per_target,
            prior_results=list(results),
        )
        results.extend(target_results)
        if _should_stop_early(target_results) or _stop_reason(target_results) == "connectivity":
            pass
        completed_offset += total_per_target
    return results


def run_target_probes(
    target: Any,
    job_id: str | None,
    *,
    max_concurrency: int = 1,
    completed_offset: int = 0,
    total_per_target: int | None = None,
    prior_results: list[Any] | None = None,
) -> list[Any]:
    client = build_client(target)
    prior_results = prior_results or []
    results = []
    probes = build_probes()
    if not probes:
        return results

    first_probe = probes[0]
    if job_id is not None:
        _update_probe_job(job_id, current_probe=_probe_label(target, first_probe.name))
    first_results = run_probe(first_probe, client)
    _tag_results(first_results, target)
    results.extend(first_results)
    if job_id is not None:
        _update_probe_job(
            job_id,
            current_probe=_probe_label(target, first_probe.name),
            completed_probes=completed_offset + 1,
            completed_results=len(results),
            results=[asdict(result) for result in prior_results + results],
        )
    if _should_stop_early(results) or _should_stop_after_probe(first_results):
        return results

    remaining = list(enumerate(probes[1:], start=1))
    parallel_remaining = [(index, probe) for index, probe in remaining if not _is_cache_probe(probe)]
    serial_remaining = [(index, probe) for index, probe in remaining if _is_cache_probe(probe)]
    if max_concurrency <= 1:
        for index, probe in remaining:
            if job_id is not None:
                _update_probe_job(job_id, current_probe=_probe_label(target, probe.name))
            probe_results = run_probe(probe, client)
            _tag_results(probe_results, target)
            results.extend(probe_results)
            if job_id is not None:
                _update_probe_job(
                    job_id,
                    current_probe=_probe_label(target, probe.name),
                    completed_probes=completed_offset + index + 1,
                    completed_results=len(results),
                    results=[asdict(result) for result in prior_results + results],
                )
            if _should_stop_early(results) or _should_stop_after_probe(probe_results):
                break
        return results

    ordered_results: dict[int, list[Any]] = {0: first_results}
    completed = 1
    with ThreadPoolExecutor(max_workers=max_concurrency) as executor:
        future_to_probe = {
            executor.submit(run_probe, probe, client): (index, probe)
            for index, probe in parallel_remaining
        }
        for future in as_completed(future_to_probe):
            index, probe = future_to_probe[future]
            probe_results = future.result()
            _tag_results(probe_results, target)
            ordered_results[index] = probe_results
            completed += 1
            results = [
                result
                for result_index in sorted(ordered_results)
                for result in ordered_results[result_index]
            ]
            if job_id is not None:
                _update_probe_job(
                    job_id,
                    current_probe=_probe_label(target, probe.name),
                    completed_probes=completed_offset + completed,
                    completed_results=len(results),
                    results=[asdict(result) for result in prior_results + results],
                )
    for index, probe in serial_remaining:
        if job_id is not None:
            _update_probe_job(job_id, current_probe=_probe_label(target, probe.name))
        probe_results = run_probe(probe, client)
        _tag_results(probe_results, target)
        ordered_results[index] = probe_results
        completed += 1
        results = [
            result
            for result_index in sorted(ordered_results)
            for result in ordered_results[result_index]
        ]
        if job_id is not None:
            _update_probe_job(
                job_id,
                current_probe=_probe_label(target, probe.name),
                completed_probes=completed_offset + completed,
                completed_results=len(results),
                results=[asdict(result) for result in prior_results + results],
            )
    return results


def _is_cache_probe(probe: Any) -> bool:
    return getattr(probe, "name", "") in {"pdf_cache", "cache_integrity", "cache_nonce"}


def build_probe_response(targets: list[Any], selected_model: str, summary: dict[str, Any], results: list[Any]) -> dict[str, Any]:
    return {
        "target": _targets_payload(targets, selected_model),
        "summary": summary,
        "results": [asdict(result) for result in results],
    }


def _run_probe_job(job_id: str, targets: list[Any], selected_model: str, probes: list[Any], max_concurrency: int) -> None:
    try:
        _ = probes
        results = run_targets_probes(targets, job_id, max_concurrency=max_concurrency)
        summary = summarize_web_results(results)
        summary["stop_reason"] = _stop_reason(results)
        summary["stopped_early"] = summary["stop_reason"] is not None
        with JOBS_LOCK:
            JOBS[job_id].update(
                {
                    "status": "completed",
                    "target": _targets_payload(targets, selected_model),
                    "summary": summary,
                    "results": [asdict(result) for result in results],
                }
            )
    except Exception as exc:
        with JOBS_LOCK:
            JOBS[job_id].update({"status": "failed", "error": f"{type(exc).__name__}: {exc}"})


def _update_probe_job(job_id: str, **updates: Any) -> None:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job is None:
            return
        progress_keys = {"current_probe", "completed_probes", "total_probes", "completed_results"}
        for key, value in updates.items():
            if key in progress_keys:
                job["progress"][key] = value
            else:
                job[key] = value


def _target_payload(target: Any, selected_model: str) -> dict[str, Any]:
    return {
        "base_url": target.base_url,
        "selected_model": selected_model,
        "model": target.model,
        "provider": target.provider_family.value,
        "profile": target.client_profile.value if target.client_profile else None,
    }


def _targets_payload(targets: list[Any], selected_model: str) -> dict[str, Any]:
    primary = _target_payload(targets[0], selected_model)
    primary["profiles"] = [
        target.client_profile.value if target.client_profile else None
        for target in targets
    ]
    primary["targets"] = [_target_payload(target, selected_model) for target in targets]
    if len(targets) > 1:
        primary["provider"] = "client-matrix"
        primary["profile"] = "codex-responses + claude-code"
    return primary


def _tag_results(results: list[Any], target: Any) -> None:
    profile = target.client_profile.value if target.client_profile else None
    for result in results:
        result.metrics.setdefault("client_profile", profile)
        result.metrics.setdefault("provider", target.provider_family.value)
        result.metrics.setdefault("target_name", target.name)


def _probe_label(target: Any, probe_name: str) -> str:
    profile = target.client_profile.value if target.client_profile else "-"
    return f"{profile}: {probe_name}"




def fetch_model_names(payload: dict[str, Any]) -> dict[str, Any]:
    base_url = normalize_base_url(_required_string(payload, "base_url"))
    api_key = _required_string(payload, "api_key")
    selected_model = payload.get("model")
    selected_profile = _optional_client_profile(payload.get("client_profile", payload.get("profile")))
    selected_provider_family = _optional_provider_family(payload.get("provider_family", payload.get("channel")))
    provider = (
        _provider_for_profile(selected_profile)
        or _provider_for_provider_family(selected_provider_family)
        or (_provider_for_model(selected_model) if isinstance(selected_model, str) else "openai")
    )
    attempts = [provider, "anthropic" if provider == "openai" else "openai"]
    last_error = ""

    for attempt_provider in attempts:
        url = base_url.rstrip("/") + "/models"
        headers = _model_list_headers(attempt_provider, api_key, selected_profile)
        try:
            with httpx.Client(timeout=30.0) as client:
                response = client.get(url, headers=headers)
            if response.status_code >= 400:
                last_error = _http_error_preview(response)
                continue
            data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            continue
        models = _extract_model_names(data)
        if models:
            return {"base_url": base_url, "provider": attempt_provider, "models": models}
        last_error = "Model list response did not contain any model ids."

    raise ValueError(f"Unable to fetch model list. {last_error}")


def summarize_web_results(results: list[Any]) -> dict[str, Any]:
    total = len(results)
    passed = sum(1 for result in results if result.status == "passed")
    skipped = sum(1 for result in results if result.status == "skipped")
    failed = total - passed - skipped
    scored_total = max(1, total - skipped)
    score = round((passed / scored_total) * 100)

    latencies = [
        result.metrics.get("latency_ms")
        for result in results
        if isinstance(result.metrics.get("latency_ms"), (int, float))
    ]
    avg_latency_ms = round(sum(latencies) / len(latencies), 2) if latencies else None
    total_latency_seconds = sum(latencies) / 1000 if latencies else 0
    latency_stats = LatencyStats(tuple(float(value) for value in latencies))
    first_token_ms = round(latency_stats.first_ms, 2) if latency_stats.first_ms is not None else None
    latency_variation = (
        round(latency_stats.coefficient_of_variation * 100, 2)
        if latency_stats.coefficient_of_variation is not None
        else None
    )

    cached_tokens = 0
    cache_creation_tokens = 0
    input_tokens = 0
    output_tokens = 0
    reasoning_tokens = 0
    total_tokens = 0
    usage_samples = 0
    cache_samples = 0
    cache_metric_samples = 0
    for result in results:
        usage = result.metrics.get("usage")
        if not isinstance(usage, dict):
            continue
        parsed_usage = parse_usage(usage)
        if isinstance(parsed_usage.input_tokens, int):
            input_tokens += parsed_usage.input_tokens
        if isinstance(parsed_usage.output_tokens, int):
            output_tokens += parsed_usage.output_tokens
        if isinstance(parsed_usage.reasoning_tokens, int):
            reasoning_tokens += parsed_usage.reasoning_tokens
        if isinstance(parsed_usage.total_tokens, int):
            total_tokens += parsed_usage.total_tokens
        usage_samples += 1

    cache_relevant_results = [result for result in results if result.kind == "cache" or "cache" in result.case_id]
    usage_source = cache_relevant_results or results
    cache_input_tokens = 0
    for result in usage_source:
        usage = result.metrics.get("usage")
        if not isinstance(usage, dict):
            continue
        parsed_usage = parse_usage(usage)
        has_cache = has_cache_metric(usage)
        prompt_tokens = parsed_usage.input_tokens
        current_cached = parsed_usage.cached_tokens
        current_cache_creation = parsed_usage.cache_creation_tokens
        if isinstance(prompt_tokens, int) or isinstance(current_cached, int) or isinstance(current_cache_creation, int):
            cache_input_tokens += prompt_tokens if isinstance(prompt_tokens, int) else 0
            cached_tokens += current_cached if isinstance(current_cached, int) else 0
            cache_creation_tokens += current_cache_creation if isinstance(current_cache_creation, int) else 0
            cache_samples += 1
            if has_cache:
                cache_metric_samples += 1

    cache_hit_rate = None
    cache_total_tokens = cache_input_tokens + cached_tokens + cache_creation_tokens
    if cache_total_tokens > 0 and cache_metric_samples > 0:
        cache_hit_rate = round((cached_tokens / cache_total_tokens) * 100, 2)
    reference_tokens = input_tokens + output_tokens
    weighted_tokens = input_tokens + (output_tokens * 3) + reasoning_tokens
    effective_total_tokens = total_tokens if total_tokens > 0 else reference_tokens + reasoning_tokens
    tokens_per_second = (
        round(output_tokens / total_latency_seconds, 2)
        if output_tokens > 0 and total_latency_seconds > 0
        else None
    )
    composite_multiplier = round(weighted_tokens / reference_tokens, 2) if reference_tokens > 0 else None

    return {
        "score": score,
        "risk_level": _risk_level(score, failed),
        "probe_count": total,
        "scored_count": total - skipped,
        "passed": passed,
        "failed": failed,
        "skipped": skipped,
        "avg_latency_ms": avg_latency_ms,
        "first_token_ms": first_token_ms,
        "latency_variation": latency_variation,
        "tokens_per_second": tokens_per_second,
        "cache_hit_rate": cache_hit_rate,
        "cached_tokens": cached_tokens if cache_samples else None,
        "cache_creation_tokens": cache_creation_tokens if cache_samples else None,
        "input_tokens": input_tokens if usage_samples else None,
        "output_tokens": output_tokens if usage_samples else None,
        "reasoning_tokens": reasoning_tokens if usage_samples else None,
        "total_tokens": effective_total_tokens if usage_samples else None,
        "cache_input_tokens": cache_input_tokens if cache_samples else None,
        "reference_tokens": reference_tokens if usage_samples else None,
        "weighted_tokens": weighted_tokens if usage_samples else None,
        "composite_multiplier": composite_multiplier,
        "cache_sample_count": cache_samples,
        "cache_metric_sample_count": cache_metric_samples,
    }


def _required_string(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} is required.")
    return value.strip()


def _max_concurrency(payload: dict[str, Any]) -> int:
    try:
        value = int(payload.get("max_concurrency", 3))
    except (TypeError, ValueError):
        value = 3
    return max(1, min(8, value))


def _max_retries(payload: dict[str, Any]) -> int:
    try:
        value = int(payload.get("max_retries", 2))
    except (TypeError, ValueError):
        value = 2
    return max(0, min(5, value))


def _retry_backoff(payload: dict[str, Any]) -> float:
    try:
        value = float(payload.get("retry_backoff_seconds", 0.8))
    except (TypeError, ValueError):
        value = 0.8
    return max(0.1, min(5.0, value))


def _cache_probe_delay(payload: dict[str, Any]) -> float:
    try:
        value = float(payload.get("cache_probe_delay_seconds", 1.2))
    except (TypeError, ValueError):
        value = 1.2
    return max(0.0, min(5.0, value))


def _resolve_model_config(model: str, profile: str | None = None) -> dict[str, str]:
    known = MODEL_OPTIONS.get(model)
    if known is not None and profile is None:
        return known
    provider = _provider_for_profile(profile) or _provider_for_model(model)
    return {
        "api_model": model,
        "provider": provider,
        "profile": profile or ("anthropic-messages" if provider == "anthropic" else "openai-chat"),
    }


def _provider_for_profile(profile: str | None) -> str | None:
    if profile in {"openai-chat", "codex-responses"}:
        return "openai"
    if profile in {"anthropic-messages", "claude-code"}:
        return "anthropic"
    return None


def _provider_for_provider_family(provider_family: str | None) -> str | None:
    if provider_family == "gpt":
        return "openai"
    if provider_family == "anthropic":
        return "anthropic"
    return None


def _provider_for_model(model: str | None) -> str:
    normalized = (model or "").lower()
    if normalized.startswith(("claude", "sonnet")) or "sonnet" in normalized:
        return "anthropic"
    return "openai"


def _model_list_headers(provider: str, api_key: str, profile: str | None = None) -> dict[str, str]:
    if provider == "anthropic":
        if profile == "claude-code":
            return {
                "Authorization": f"Bearer {api_key}",
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
                "User-Agent": "api-probe claude-code profile",
            }
        return {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
    return {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}


def _extract_model_names(data: Any) -> list[str]:
    items = data.get("data") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return []
    names: list[str] = []
    seen: set[str] = set()
    for item in items:
        if isinstance(item, str):
            model_id = item
        elif isinstance(item, dict) and isinstance(item.get("id"), str):
            model_id = item["id"]
        else:
            continue
        if model_id not in seen:
            seen.add(model_id)
            names.append(model_id)
    return names


def _http_error_preview(response: httpx.Response) -> str:
    preview = response.text[:500].replace("\n", " ")
    return f"HTTP {response.status_code} from {response.request.url}: {preview}"


def _risk_level(score: int, failed: int) -> str:
    if failed == 0 and score >= 90:
        return "normal"
    if score >= 70:
        return "suspicious"
    if score >= 40:
        return "high_risk"
    return "inconclusive"


def _should_stop_early(results: list[Any]) -> bool:
    if not results:
        return False
    if any(result.status != "failed" for result in results):
        return False
    evidence = " ".join(str(result.evidence).lower() for result in results)
    return (
        "401 unauthorized" in evidence
        or "403 forbidden" in evidence
        or "model_not_found" in evidence
        or "no available channel for model" in evidence
    )


def _should_stop_after_probe(probe_results: list[Any]) -> bool:
    if not probe_results:
        return False
    if any(result.status == "passed" for result in probe_results):
        return False
    evidence = " ".join(str(result.evidence).lower() for result in probe_results)
    return _is_connectivity_failure(evidence)


def _stop_reason(results: list[Any]) -> str | None:
    evidence = " ".join(str(result.evidence).lower() for result in results)
    if _is_connectivity_failure(evidence):
        return "connectivity"
    if _should_stop_early(results):
        return "auth_or_model"
    return None


def _is_connectivity_failure(evidence: str) -> bool:
    return (
        "connecterror" in evidence
        or "getaddrinfo failed" in evidence
        or "readtimeout" in evidence
        or "connecttimeout" in evidence
        or "connection refused" in evidence
    )


if __name__ == "__main__":
    raise SystemExit(main())
