"""
Upstream client (leg 02 "upstream client", leg 04 step 2).

Forwards a chat/completion to a backend's native API with the safe model
context injected when that backend accepts it. Ollama backends speak
``/api/chat`` or ``/api/generate`` and take ``options.num_ctx``. OpenAI-shaped
backends like LiteLLM speak ``/v1/chat/completions`` and take a top-level
``num_ctx`` extension. Both normalize back to the proxy's canonical internal
shape.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, TypedDict

import httpx

from .config import get_settings
from .models import Backend
from .obs import get_tracer, log, record_error


class UpstreamError(Exception):
    """A backend call failed at the transport/HTTP level (not a bad generation)."""


class UpstreamStatusError(UpstreamError):
    """A backend answered, and its answer was a non-2xx status.

    Distinct from its base because a status is evidence about *what* failed and
    a bare transport error is not. Issue #114 measured the cost of collapsing
    the two: a deterministic 400 was bucketed as ``dispatch.transport_error``,
    retried three times with a byte-identical body, and returned as a 502
    naming a backend that was serving other requests at the time.
    """

    def __init__(self, message: str, status_code: int, body: str = "") -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body


# 4xx that a later identical attempt can still resolve: a timeout, a too-early
# send, and a rate limit. Every other 4xx is a settled fact about the body.
RETRYABLE_CLIENT_STATUSES = frozenset({408, 425, 429})

# Upstream error bodies ride into the caller's response, so they are bounded.
MAX_UPSTREAM_ERROR_BODY = 2048


def is_retryable_status(status_code: int) -> bool:
    """Whether resending the identical request could plausibly succeed."""
    return status_code >= 500 or status_code in RETRYABLE_CLIENT_STATUSES


def _status_error(backend: Backend, exc: httpx.HTTPStatusError) -> UpstreamStatusError:
    try:
        body = exc.response.text[:MAX_UPSTREAM_ERROR_BODY]
    except Exception:
        body = ""
    return UpstreamStatusError(
        f"{backend.name}: {exc}",
        status_code=exc.response.status_code,
        body=body,
    )


class RequestAuthKwargs(TypedDict, total=False):
    headers: dict[str, str]


@dataclass
class UpstreamResult:
    """A normalized non-streaming result from a backend."""

    model: str
    content: str
    thinking: str = ""  # reasoning models (qwen3 *-think) emit this separately.
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    prompt_eval_count: int = 0
    eval_count: int = 0
    total_duration: int = 0
    load_duration: int = 0
    prompt_eval_duration: int = 0
    eval_duration: int = 0
    done_reason: str = "stop"
    # Provider prompt-cache accounting (issue #101). Absence and a reported zero
    # are different facts, so the counts mean nothing without the flag.
    cache_usage_reported: bool = False
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    # Set by dispatch (issue #33) when the backend delivered a shorter context than
    # asked for - the OLLAMA_NUM_PARALLEL division. Surfaced loud, never silent.
    context_truncated: bool = False
    # Which backend served this, and its capacity state at dispatch (#109).
    # Stamped by dispatch, because only it knows which chain entry won.
    served_by: str = ""
    served_regime: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    def ollama_measurements_ms(self) -> dict[str, float]:
        """Return bounded provider timings from Ollama's final response."""

        values = {
            "ollama.total_duration_ms": self.total_duration,
            "ollama.load_duration_ms": self.load_duration,
            "ollama.prompt_eval_duration_ms": self.prompt_eval_duration,
            "ollama.eval_duration_ms": self.eval_duration,
        }
        return {name: value / 1_000_000 for name, value in values.items() if value > 0}

    @property
    def cache_miss_tokens(self) -> int:
        """Prompt tokens the provider billed at the uncached rate."""

        return max(self.prompt_eval_count - self.cache_read_tokens, 0)

    def cache_usage_attributes(self) -> dict[str, int]:
        """Return the prompt-cache span attributes, empty when unreported."""

        if not self.cache_usage_reported:
            return {}
        return {
            "gen_ai.usage.cache_read_input_tokens": self.cache_read_tokens,
            "gen_ai.usage.cache_creation_input_tokens": self.cache_write_tokens,
        }


def _first_int(source: dict[str, Any], *names: str) -> int | None:
    """Return the first present numeric field, or None when the provider is silent."""

    for name in names:
        value = source.get(name)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        return int(value)
    return None


def parse_cache_usage(usage: dict[str, Any] | None) -> tuple[bool, int, int]:
    """Normalize one provider's prompt-cache accounting to ``(reported, read, write)``.

    Three wire shapes reach this proxy through LiteLLM and none of them agree:

    * DeepSeek reports ``prompt_cache_hit_tokens`` and ``prompt_cache_miss_tokens``
      natively, and its caching is automatic with no request-side opt-in.
    * OpenAI-compatible providers report ``prompt_tokens_details.cached_tokens``,
      which is also the field LiteLLM normalizes into.
    * Anthropic-style providers report ``cache_read_input_tokens`` beside a
      separate ``cache_creation_input_tokens`` write charge.

    A provider that reports nothing is not a cache miss, it is an unmeasured
    route, so ``reported`` stays False and the counts are never published.
    """
    if not isinstance(usage, dict):
        return (False, 0, 0)

    details = usage.get("prompt_tokens_details")
    detail_cached = _first_int(details, "cached_tokens") if isinstance(details, dict) else None
    read = _first_int(usage, "prompt_cache_hit_tokens", "cache_read_input_tokens")
    if read is None:
        read = detail_cached
    write = _first_int(usage, "cache_creation_input_tokens", "cache_creation_tokens")
    # A bare miss count still proves the provider accounts for caching, even on
    # the turn that populated the cache and read nothing back.
    miss_only = _first_int(usage, "prompt_cache_miss_tokens") is not None
    if read is None and write is None and not miss_only:
        return (False, 0, 0)
    return (True, max(read or 0, 0), max(write or 0, 0))


def _record_status_failure(span: Any | None, error: UpstreamStatusError) -> None:
    """Put the upstream status on the span, so the failure is not invisible.

    Issue #106 found a trace where litellm returned 500 and every agent-proxy
    span still read ``has_error: false`` with an empty status code, which makes
    the service's reported error rate untrustworthy.
    """
    error_type = (
        "upstream_request_rejected"
        if not is_retryable_status(error.status_code)
        else "upstream_5xx"
    )
    record_error(error_type, span)
    if span is None:
        return
    span.set_attribute("http.response.status_code", error.status_code)
    span.set_attribute("agentproxy.upstream.status_code", error.status_code)
    span.set_attribute("agentproxy.upstream.error", str(error))
    span.set_attribute("agentproxy.upstream.retryable", is_retryable_status(error.status_code))


def set_result_span_attributes(span: Any, result: UpstreamResult) -> None:
    """Attach normalized usage plus any Ollama-native final-response timings.

    Backend identity and regime ride along once dispatch has stamped them, so
    the request span can be grouped by which backend actually served it rather
    than by a client URL (#109).
    """

    if result.served_by:
        span.set_attribute("agentproxy.backend", result.served_by)
        span.set_attribute("agentproxy.backend.regime", result.served_regime)
    # agentproxy.backend names this proxy's chain entry. For an upstream-alias
    # route that is one hop, so only the response says which model answered.
    if result.model:
        span.set_attribute("gen_ai.response.model", result.model)
    span.set_attribute("gen_ai.usage.input_tokens", result.prompt_eval_count)
    span.set_attribute("gen_ai.usage.output_tokens", result.eval_count)
    span.set_attribute(
        "response.finish_reasons", [result.done_reason] if result.done_reason else []
    )
    for name, tokens in result.cache_usage_attributes().items():
        span.set_attribute(name, tokens)
    for name, duration in result.ollama_measurements_ms().items():
        span.set_attribute(name, duration)


_client: httpx.AsyncClient | None = None

_CORRELATION_METADATA_KEYS = (
    "agentproxy.logical_model",
    "agentproxy.upstream_mode",
    "agentproxy.request_kind",
    "agentproxy.request_id",
    "ward.run_id",
    "ward.container_name",
    "ward.role",
    "ward.harness",
    "ward.target_repo",
    "ward.issue_ref",
    "ward.workflow",
    "ward.context_level",
    "ward.version",
    "agent.session_id",
)


def _correlation_log_fields(span_attrs: dict[str, Any] | None) -> dict[str, Any]:
    """Keep request joins in upstream logs without copying opt-in body fields."""
    if not span_attrs:
        return {}

    fields: dict[str, Any] = {}
    mapped_keys = {
        "agentproxy.logical_model": "logical_model",
        "gen_ai.request.model": "request_model",
        "agentproxy.request_kind": "request_kind",
        "agentproxy.request_id": "request_id",
    }
    for source, target in mapped_keys.items():
        if source in span_attrs:
            fields[target] = span_attrs[source]

    for key in (
        "ward.run_id",
        "ward.container_name",
        "ward.role",
        "ward.harness",
        "ward.target_repo",
        "ward.issue_ref",
        "ward.workflow",
        "ward.context_level",
        "ward.version",
        "agent.session_id",
    ):
        if key in span_attrs:
            fields[key] = span_attrs[key]
    return fields


def _gateway_metadata(span_attrs: dict[str, Any] | None) -> dict[str, Any]:
    """Return the body-safe correlation fields forwarded through LiteLLM."""
    if not span_attrs:
        return {}
    return {key: span_attrs[key] for key in _CORRELATION_METADATA_KEYS if key in span_attrs}


def request_auth_kwargs(backend: Backend) -> RequestAuthKwargs:
    """Build an authenticated request kwarg without exposing the key.

    The backend spec contains only a mounted file path. The secret value never
    enters tracked configuration, argv, logs, exception text, or span
    attributes.
    """
    if not backend.api_key_file:
        return {}
    try:
        key = Path(backend.api_key_file).read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise UpstreamError(f"{backend.name}: authentication key unavailable") from exc
    if not key:
        raise UpstreamError(f"{backend.name}: authentication key unavailable")
    return {"headers": {"Authorization": f"Bearer {key}"}}


def get_client() -> httpx.AsyncClient:
    """Shared async httpx client. Instrumented by OTel's httpx integration when wired."""
    global _client
    if _client is None:
        _client = httpx.AsyncClient()
    return _client


async def aclose() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def _inject_options(
    base: dict[str, Any] | None, num_ctx: int, num_parallel: int = 1
) -> dict[str, Any]:
    """Return a fresh options dict with the model's safe ``num_ctx`` injected.

    The injected ``num_ctx`` always wins - it is the whole point of the proxy, so
    a caller-supplied value never overrides the model's proven-safe ceiling.

    ollama loads ``num_ctx`` as the model's *total* context and splits it across
    ``OLLAMA_NUM_PARALLEL`` slots, so the usable per-request window is
    ``num_ctx / NUM_PARALLEL`` (issue #33). To keep each request's window equal to
    the intended ``num_ctx``, the injected value is scaled by the backend's
    ``num_parallel``; with the default (1) the injected value is unchanged.
    """
    opts = dict(base or {})
    opts["num_ctx"] = num_ctx * max(num_parallel, 1)
    return opts


def _openai_options(options: dict[str, Any] | None) -> dict[str, Any]:
    """Translate the proxy's Ollama-shaped sampling options back to OpenAI."""
    mapped = dict(options or {})
    if "num_predict" in mapped:
        mapped["max_tokens"] = mapped.pop("num_predict")
    mapped.pop("num_ctx", None)
    return mapped


@dataclass(frozen=True)
class ToolPolicy:
    """The caller's tool-calling constraints, carried beside ``tools``.

    ``tools`` alone says what the model may call. These say whether it must, and
    how many at once, which is the difference between measuring a roster and
    measuring a model's willingness to use one. New OpenAI tool-contract fields
    arrive here rather than as another parameter through nine signatures.
    """

    choice: Any | None = None
    parallel: bool | None = None

    def is_empty(self) -> bool:
        return self.choice is None and self.parallel is None


def _chat_body(
    backend: Backend,
    num_ctx: int,
    messages: list[dict[str, Any]],
    *,
    stream: bool,
    tools: list[dict[str, Any]] | None,
    tool_policy: ToolPolicy | None = None,
    options: dict[str, Any] | None,
    span_attrs: dict[str, Any] | None,
) -> dict[str, Any]:
    """Shape one request while preserving Agent Proxy's safe-context policy."""
    body: dict[str, Any] = {
        "model": backend.ollama_tag,
        "messages": messages,
        "stream": stream,
    }
    # A non-positive num_ctx means no locally-derived window applies, so there is
    # nothing to inject and the provider's own limit stands (issue #115).
    inject = backend.injects_num_ctx and num_ctx > 0
    if backend.dialect == "ollama":
        if inject:
            body["options"] = _inject_options(options, num_ctx, backend.num_parallel)
        elif options:
            body["options"] = dict(options)
    else:
        body.update(_openai_options(options))
        # OpenAI-compatible streaming omits the usage block unless asked, and the
        # cache attributes derive from it. See docs/proxy-prompt-cache.md.
        if stream:
            body["stream_options"] = {"include_usage": True}
        if inject:
            body["num_ctx"] = num_ctx
        if metadata := _gateway_metadata(span_attrs):
            body["metadata"] = metadata
    if tools:
        body["tools"] = tools
        # Ollama's /api/chat has neither field and ignores an unknown key, so
        # the request path refuses. See docs/proxy-request-path.md.
        if backend.dialect != "ollama" and tool_policy is not None:
            if tool_policy.choice is not None:
                body["tool_choice"] = tool_policy.choice
            if tool_policy.parallel is not None:
                body["parallel_tool_calls"] = tool_policy.parallel
    return body


def _fold_stream_usage(
    result: UpstreamResult | None,
    payload: dict[str, Any],
    fallback_model: str,
) -> UpstreamResult:
    """Carry a usage-only trailing chunk onto the terminal result.

    An OpenAI-dialect stream asked for usage answers with a final chunk whose
    choice has an empty delta and no ``finish_reason``, so the accounting arrives
    after the chunk that ends the message. Keyed on ``finish_reason`` alone it is
    dropped, which is what left the streaming route unmeasured in issue #138.
    """

    usage = payload.get("usage") or {}
    reported, read, write = parse_cache_usage(usage)
    return UpstreamResult(
        model=str((result.model if result else "") or payload.get("model") or fallback_model),
        content="",
        prompt_eval_count=int(usage.get("prompt_tokens", 0) or 0),
        eval_count=int(usage.get("completion_tokens", 0) or 0),
        done_reason=result.done_reason if result else "stop",
        cache_usage_reported=reported,
        cache_read_tokens=read,
        cache_write_tokens=write,
        raw=payload,
    )


def _chat_path(backend: Backend) -> str:
    if backend.chat_path:
        return backend.chat_path
    return "/api/chat" if backend.dialect == "ollama" else "/v1/chat/completions"


def _health_path(backend: Backend) -> str:
    if backend.health_path:
        return backend.health_path
    return "/api/version" if backend.dialect == "ollama" else "/health"


def _timeout(backend: Backend) -> httpx.Timeout:
    t = backend.timeout if backend.timeout is not None else get_settings().request_timeout
    # No read timeout cap beyond t; connect kept short so a dead backend fails fast.
    return httpx.Timeout(t, connect=5.0)


def _parse_chat_response(data: dict[str, Any]) -> UpstreamResult:
    """Normalize an Ollama-shaped response, or this module's canonical chunk.

    The three ``cache_*`` keys never appear on Ollama's own wire format. They
    exist on the canonical chunk :func:`chat_stream` normalizes an OpenAI-dialect
    stream into, which is the only path by which a caching provider's accounting
    reaches the terminal result the streaming surface reads back.
    """
    message = data.get("message") or {}
    return UpstreamResult(
        model=data.get("model", ""),
        content=message.get("content", "") or "",
        thinking=message.get("thinking", "") or "",
        tool_calls=message.get("tool_calls", []) or [],
        prompt_eval_count=int(data.get("prompt_eval_count", 0) or 0),
        eval_count=int(data.get("eval_count", 0) or 0),
        total_duration=int(data.get("total_duration", 0) or 0),
        load_duration=int(data.get("load_duration", 0) or 0),
        prompt_eval_duration=int(data.get("prompt_eval_duration", 0) or 0),
        eval_duration=int(data.get("eval_duration", 0) or 0),
        done_reason=data.get("done_reason", "stop") or "stop",
        cache_usage_reported=bool(data.get("cache_usage_reported", False)),
        cache_read_tokens=int(data.get("cache_read_tokens", 0) or 0),
        cache_write_tokens=int(data.get("cache_write_tokens", 0) or 0),
        raw=data,
    )


def parse_stream_result(data: dict[str, Any], fallback_model: str = "") -> UpstreamResult:
    """Normalize one terminal stream chunk without requiring response content."""

    result = _parse_chat_response(data)
    if not result.model:
        result.model = fallback_model
    return result


def _parse_openai_chat_response(data: dict[str, Any]) -> UpstreamResult:
    choice = (data.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    usage = data.get("usage") or {}
    cache_reported, cache_read, cache_write = parse_cache_usage(usage)
    return UpstreamResult(
        model=data.get("model", ""),
        content=message.get("content", "") or "",
        thinking=message.get("reasoning_content", "") or message.get("thinking", "") or "",
        tool_calls=message.get("tool_calls", []) or [],
        prompt_eval_count=int(usage.get("prompt_tokens", 0) or 0),
        eval_count=int(usage.get("completion_tokens", 0) or 0),
        done_reason=choice.get("finish_reason", "stop") or "stop",
        cache_usage_reported=cache_reported,
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
        raw=data,
    )


async def chat(
    backend: Backend,
    num_ctx: int,
    messages: list[dict[str, Any]],
    *,
    tools: list[dict[str, Any]] | None = None,
    tool_policy: ToolPolicy | None = None,
    options: dict[str, Any] | None = None,
    span_attrs: dict[str, Any] | None = None,
) -> UpstreamResult:
    """Non-streaming upstream call with the safe context applied where valid."""
    body = _chat_body(
        backend,
        num_ctx,
        messages,
        stream=False,
        tools=tools,
        tool_policy=tool_policy,
        options=options,
        span_attrs=span_attrs,
    )
    auth_kwargs = request_auth_kwargs(backend)
    tracer = get_tracer()
    attrs = {
        "agentproxy.backend": backend.name,
        "agentproxy.backend_dialect": backend.dialect,
        "agentproxy.backend.regime": backend.regime,
        "agentproxy.resolved_backend": backend.url,
        "agentproxy.logical_num_ctx": num_ctx,
    }
    if span_attrs:
        attrs.update(span_attrs)
    if tracer is None:
        try:
            resp = await get_client().post(
                f"{backend.url}{_chat_path(backend)}",
                json=body,
                timeout=_timeout(backend),
                **auth_kwargs,
            )
            resp.raise_for_status()
        except asyncio.CancelledError:
            log.info(
                "upstream.completed",
                **_correlation_log_fields(span_attrs),
                backend=backend.name,
                backend_dialect=backend.dialect,
                outcome="cancelled",
            )
            raise
        except httpx.HTTPStatusError as exc:
            raise _status_error(backend, exc) from exc
        except httpx.HTTPError as exc:
            raise UpstreamError(f"{backend.name}: {exc}") from exc
        return (
            _parse_chat_response(resp.json())
            if backend.dialect == "ollama"
            else _parse_openai_chat_response(resp.json())
        )
    with tracer.start_as_current_span("upstream.chat") as span:
        for key, value in attrs.items():
            span.set_attribute(key, value)
        try:
            resp = await get_client().post(
                f"{backend.url}{_chat_path(backend)}",
                json=body,
                timeout=_timeout(backend),
                **auth_kwargs,
            )
            resp.raise_for_status()
        except asyncio.CancelledError:
            cancellation_fields = {
                "backend": backend.name,
                "backend_dialect": backend.dialect,
                "outcome": "cancelled",
            }
            span.set_attribute("agentproxy.upstream.outcome", "cancelled")
            span.add_event("upstream.cancelled", cancellation_fields)
            log.info(
                "upstream.completed",
                **_correlation_log_fields(span_attrs),
                **cancellation_fields,
            )
            raise
        except httpx.HTTPStatusError as exc:
            status_error = _status_error(backend, exc)
            _record_status_failure(span, status_error)
            log.warning(
                "upstream.completed",
                **_correlation_log_fields(span_attrs),
                backend=backend.name,
                backend_dialect=backend.dialect,
                outcome="failed",
                upstream_status=status_error.status_code,
            )
            raise status_error from exc
        except httpx.HTTPError as exc:
            record_error("upstream_transport_failed", span)
            span.set_attribute("agentproxy.upstream.error", str(exc))
            log.warning(
                "upstream.completed",
                **_correlation_log_fields(span_attrs),
                backend=backend.name,
                backend_dialect=backend.dialect,
                outcome="failed",
            )
            raise UpstreamError(f"{backend.name}: {exc}") from exc
        result = (
            _parse_chat_response(resp.json())
            if backend.dialect == "ollama"
            else _parse_openai_chat_response(resp.json())
        )
        set_result_span_attributes(span, result)
        log.info(
            "upstream.completed",
            **_correlation_log_fields(span_attrs),
            backend=backend.name,
            backend_dialect=backend.dialect,
            outcome="ok",
        )
        return result


async def chat_stream(
    backend: Backend,
    num_ctx: int,
    messages: list[dict[str, Any]],
    *,
    tools: list[dict[str, Any]] | None = None,
    tool_policy: ToolPolicy | None = None,
    options: dict[str, Any] | None = None,
    span_attrs: dict[str, Any] | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Streaming upstream call, normalized to the proxy's internal chunk shape."""
    body = _chat_body(
        backend,
        num_ctx,
        messages,
        stream=True,
        tools=tools,
        tool_policy=tool_policy,
        options=options,
        span_attrs=span_attrs,
    )
    auth_kwargs = request_auth_kwargs(backend)
    tracer = get_tracer()
    attrs = {
        "agentproxy.backend": backend.name,
        "agentproxy.backend_dialect": backend.dialect,
        "agentproxy.backend.regime": backend.regime,
        "agentproxy.resolved_backend": backend.url,
        "agentproxy.logical_num_ctx": num_ctx,
    }
    if span_attrs:
        attrs.update(span_attrs)
    span_cm = tracer.start_as_current_span("upstream.chat_stream") if tracer else None
    terminal_result: UpstreamResult | None = None
    try:
        if span_cm is not None:
            span = span_cm.__enter__()
            for key, value in attrs.items():
                span.set_attribute(key, value)
        async with get_client().stream(
            "POST",
            f"{backend.url}{_chat_path(backend)}",
            json=body,
            timeout=_timeout(backend),
            **auth_kwargs,
        ) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line.strip():
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    try:
                        payload = json.loads(line.removeprefix("data:").strip())
                    except json.JSONDecodeError:
                        continue
                if backend.dialect == "ollama":
                    if payload.get("done"):
                        terminal_result = parse_stream_result(payload, backend.ollama_tag)
                    yield payload
                    continue
                choice = (payload.get("choices") or [{}])[0]
                delta = choice.get("delta") or {}
                message: dict[str, Any] = {}
                if content := delta.get("content"):
                    message["content"] = content
                if reasoning := delta.get("reasoning_content"):
                    message["thinking"] = reasoning
                if tool_calls := delta.get("tool_calls"):
                    message["tool_calls"] = tool_calls
                out: dict[str, Any] = {
                    "message": message,
                    "done": choice.get("finish_reason") is not None,
                }
                if choice.get("finish_reason"):
                    out["done_reason"] = choice.get("finish_reason")
                    usage = payload.get("usage") or {}
                    cache_reported, cache_read, cache_write = parse_cache_usage(usage)
                    if payload.get("model"):
                        out["model"] = payload["model"]
                    if usage:
                        out["prompt_eval_count"] = int(usage.get("prompt_tokens", 0) or 0)
                        out["eval_count"] = int(usage.get("completion_tokens", 0) or 0)
                    if cache_reported:
                        out["cache_usage_reported"] = True
                        out["cache_read_tokens"] = cache_read
                        out["cache_write_tokens"] = cache_write
                    terminal_result = UpstreamResult(
                        model=str(payload.get("model") or backend.ollama_tag),
                        content="",
                        prompt_eval_count=int(usage.get("prompt_tokens", 0) or 0),
                        eval_count=int(usage.get("completion_tokens", 0) or 0),
                        done_reason=str(out["done_reason"]),
                        cache_usage_reported=cache_reported,
                        cache_read_tokens=cache_read,
                        cache_write_tokens=cache_write,
                        raw=payload,
                    )
                elif payload.get("usage"):
                    # Usage arrives in a trailing chunk of its own, after the
                    # finish_reason one. See docs/proxy-prompt-cache.md.
                    terminal_result = _fold_stream_usage(
                        terminal_result, payload, backend.ollama_tag
                    )
                yield out
        if span_cm is not None and terminal_result is not None:
            set_result_span_attributes(span, terminal_result)
        log.info(
            "upstream.completed",
            **_correlation_log_fields(span_attrs),
            backend=backend.name,
            backend_dialect=backend.dialect,
            outcome="ok",
        )
    except asyncio.CancelledError:
        cancellation_fields = {
            "backend": backend.name,
            "backend_dialect": backend.dialect,
            "outcome": "cancelled",
        }
        if span_cm is not None:
            span.set_attribute("agentproxy.upstream.outcome", "cancelled")
            span.add_event("upstream.cancelled", cancellation_fields)
        log.info(
            "upstream.completed",
            **_correlation_log_fields(span_attrs),
            **cancellation_fields,
        )
        raise
    except httpx.HTTPStatusError as exc:
        status_error = _status_error(backend, exc)
        if span_cm is not None:
            _record_status_failure(span, status_error)
        log.warning(
            "upstream.completed",
            **_correlation_log_fields(span_attrs),
            backend=backend.name,
            backend_dialect=backend.dialect,
            outcome="failed",
            upstream_status=status_error.status_code,
        )
        raise status_error from exc
    except httpx.HTTPError as exc:
        if span_cm is not None:
            record_error("upstream_transport_failed", span)
            span.set_attribute("agentproxy.upstream.error", str(exc))
        log.warning(
            "upstream.completed",
            **_correlation_log_fields(span_attrs),
            backend=backend.name,
            backend_dialect=backend.dialect,
            outcome="failed",
        )
        raise UpstreamError(f"{backend.name}: {exc}") from exc
    finally:
        if span_cm is not None:
            span_cm.__exit__(None, None, None)


async def generate(
    backend: Backend,
    num_ctx: int,
    prompt: str,
    *,
    options: dict[str, Any] | None = None,
) -> UpstreamResult:
    """Non-streaming native ``/api/generate`` for the ``/v1/completions`` surface."""
    body: dict[str, Any] = {"model": backend.ollama_tag, "prompt": prompt, "stream": False}
    if backend.dialect == "ollama":
        if backend.injects_num_ctx:
            body["options"] = _inject_options(options, num_ctx, backend.num_parallel)
        elif options:
            body["options"] = dict(options)
        path = "/api/generate"
    else:
        body.update(_openai_options(options))
        if backend.injects_num_ctx:
            body["num_ctx"] = num_ctx
        path = "/v1/completions"
    try:
        resp = await get_client().post(
            f"{backend.url}{path}",
            json=body,
            timeout=_timeout(backend),
            **request_auth_kwargs(backend),
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        status_error = _status_error(backend, exc)
        _record_status_failure(None, status_error)
        raise status_error from exc
    except httpx.HTTPError as exc:
        record_error("upstream_transport_failed")
        raise UpstreamError(f"{backend.name}: {exc}") from exc
    data = resp.json()
    if backend.dialect == "ollama":
        return UpstreamResult(
            model=data.get("model", ""),
            content=data.get("response", "") or "",
            prompt_eval_count=int(data.get("prompt_eval_count", 0) or 0),
            eval_count=int(data.get("eval_count", 0) or 0),
            total_duration=int(data.get("total_duration", 0) or 0),
            load_duration=int(data.get("load_duration", 0) or 0),
            prompt_eval_duration=int(data.get("prompt_eval_duration", 0) or 0),
            eval_duration=int(data.get("eval_duration", 0) or 0),
            done_reason=data.get("done_reason", "stop") or "stop",
            raw=data,
        )
    choice = (data.get("choices") or [{}])[0]
    usage = data.get("usage") or {}
    cache_reported, cache_read, cache_write = parse_cache_usage(usage)
    return UpstreamResult(
        model=data.get("model", ""),
        content=choice.get("text", "") or "",
        prompt_eval_count=int(usage.get("prompt_tokens", 0) or 0),
        eval_count=int(usage.get("completion_tokens", 0) or 0),
        done_reason=choice.get("finish_reason", "stop") or "stop",
        cache_usage_reported=cache_reported,
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
        raw=data,
    )


async def health(backend: Backend) -> bool:
    """Cheap liveness probe used by the circuit breaker's half-open recovery."""
    try:
        resp = await get_client().get(
            f"{backend.url}{_health_path(backend)}",
            timeout=httpx.Timeout(5.0),
            **request_auth_kwargs(backend),
        )
        return resp.status_code == 200
    except (httpx.HTTPError, UpstreamError):
        return False
