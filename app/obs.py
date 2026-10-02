"""
Observability wiring: structlog JSON logs, prometheus metrics, OpenTelemetry
traces, Sentry errors. This module is imported before any request logic so every
path below is instrumented from line one (leg 04 step 1, leg 02 observability).

All metric objects are defined here once and imported everywhere else, so the
names in leg 04 (``llm_queue_depth``, ``llm_retries_total``,
``llm_fallbacks_total``, ``llm_circuit_state``, ``llm_truncation_avoided_total``)
have a single source of truth.
"""

from __future__ import annotations

import logging
import sys
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Iterator, MutableMapping
from urllib.parse import urlsplit

import structlog
from prometheus_client import Counter, Gauge, Histogram, generate_latest

from .config import get_settings

if TYPE_CHECKING:
    from sentry_sdk.types import Event

# Prometheus metrics - the leg 04 names plus request-level counters.

llm_requests_total = Counter(
    "llm_requests_total", "Requests accepted by the proxy", ["logical_model", "outcome"]
)
llm_route_requests_total = Counter(
    "llm_route_requests_total",
    "Requests resolved through one logical route and upstream mode",
    ["logical_model", "upstream_mode"],
)
llm_queue_depth = Gauge("llm_queue_depth", "Jobs currently waiting in the in-memory queue")
llm_backend_saturated_total = Counter(
    "llm_backend_saturated_total",
    "Attempts abandoned because a backend was too slow to be treated as available (#108)",
    ["logical_model", "backend"],
)
llm_stream_heartbeats_total = Counter(
    "llm_stream_heartbeats_total",
    "SSE heartbeat comments emitted to a streaming caller (issue #104)",
    ["logical_model", "state"],
)
llm_queue_rejected_total = Counter(
    "llm_queue_rejected_total", "Requests rejected with 429 because the queue was full"
)
llm_rate_limited_total = Counter(
    "llm_rate_limited_total",
    "Requests shed with 429 for exceeding the configured admission rate (issue #110)",
    ["logical_model"],
)
llm_retries_total = Counter(
    "llm_retries_total", "Dispatch retries against a single backend", ["logical_model", "backend"]
)
llm_fallbacks_total = Counter(
    "llm_fallbacks_total",
    "Falls to the next backend in a logical model's chain",
    ["logical_model", "backend"],
)
# 0 = closed (healthy), 1 = open (tripped), 2 = half-open (probing).
llm_circuit_state = Gauge("llm_circuit_state", "Per-backend circuit breaker state", ["backend"])
llm_truncation_avoided_total = Counter(
    "llm_truncation_avoided_total",
    "Requests trimmed to fit the safe context budget",
    ["logical_model"],
)
llm_validation_failures_total = Counter(
    "llm_validation_failures_total", "Responses rejected by validation", ["logical_model", "reason"]
)
llm_context_truncated_total = Counter(
    "llm_context_truncated_total",
    "Responses whose backend delivered a shorter context than the proxy asked for "
    "(the OLLAMA_NUM_PARALLEL division, issue #33)",
    ["logical_model", "backend"],
)
ward_skill_use_total = Counter(
    "ward_skill_use_total",
    "Ward skill-use counts observed from reap artifacts",
    ["skill", "harness"],
)
llm_prompt_tokens = Histogram(
    "llm_prompt_tokens",
    "Prompt tokens forwarded upstream (post-guard)",
    ["logical_model"],
    buckets=(1024, 4096, 8192, 16384, 32768, 49152, 65536, 98304, 131072),
)
llm_prompt_cache_hit_tokens_total = Counter(
    "llm_prompt_cache_hit_tokens_total",
    "Prompt tokens a provider served from its prompt cache (issue #101)",
    ["logical_model"],
)
llm_prompt_cache_miss_tokens_total = Counter(
    "llm_prompt_cache_miss_tokens_total",
    "Prompt tokens a provider billed at the uncached rate (issue #101)",
    ["logical_model"],
)
llm_prompt_cache_write_tokens_total = Counter(
    "llm_prompt_cache_write_tokens_total",
    "Prompt tokens a provider charged to populate its prompt cache (issue #101)",
    ["logical_model"],
)
llm_upstream_latency_seconds = Histogram(
    "llm_upstream_latency_seconds", "Upstream generation latency", ["logical_model", "backend"]
)
llm_cost_usd_total = Counter(
    "llm_cost_usd_total",
    "USD charged for served hosted decision calls, from the configured input-token price",
    ["logical_model", "backend"],
)
llm_ollama_duration_seconds = Histogram(
    "llm_ollama_duration_seconds",
    "Ollama final-response duration by generation phase",
    ["logical_model", "backend", "phase"],
)
agent_proxy_readiness_checks_total = Counter(
    "agent_proxy_readiness_checks_total",
    "Non-generating route readiness checks",
    ["check", "outcome"],
)
agent_proxy_readiness_check_duration_seconds = Histogram(
    "agent_proxy_readiness_check_duration_seconds",
    "Non-generating route readiness check duration",
    ["check"],
)
agent_proxy_route_ready = Gauge(
    "agent_proxy_route_ready",
    "Whether a governed logical route is structurally ready without inference",
    ["logical_route"],
)
agent_proxy_readiness_last_success_timestamp_seconds = Gauge(
    "agent_proxy_readiness_last_success_timestamp_seconds",
    "Unix timestamp of the last successful non-generating readiness check",
    ["check"],
)
agent_proxy_health_endpoint_requests_total = Counter(
    "agent_proxy_health_endpoint_requests_total",
    "Metrics-only health endpoint responses",
    ["endpoint", "outcome"],
)
# The origin value itself is caller-supplied, so it lives in logs and spans only.
agent_proxy_request_origin_total = Counter(
    "agent_proxy_request_origin_total",
    "Model requests by x-agent-origin state: present, unknown or invalid",
    ["state"],
)


HEALTH_TRACE_EXCLUDED_URLS = "healthz,readyz,metrics"
_HEALTH_PATH_PREFIXES = ("/healthz", "/readyz", "/metrics")
_health_observability_suppressed: ContextVar[bool] = ContextVar(
    "agent_proxy_health_observability_suppressed", default=False
)


def _is_health_path(value: str) -> bool:
    try:
        path = urlsplit(value).path
    except ValueError:
        path = value.split("?", 1)[0]
    return any(path == prefix or path.startswith(f"{prefix}/") for prefix in _HEALTH_PATH_PREFIXES)


def _record_contains_health_path(record: logging.LogRecord) -> bool:
    values: list[str] = []
    if isinstance(record.args, tuple):
        values.extend(value for value in record.args if isinstance(value, str))
    elif isinstance(record.args, dict):
        values.extend(value for value in record.args.values() if isinstance(value, str))
    values.append(record.getMessage())
    return any(_is_health_path(value) for value in values)


class _HealthNoiseFilter(logging.Filter):
    """Drop health access records and standard-library logs inside readiness calls."""

    def filter(self, record: logging.LogRecord) -> bool:
        if _health_observability_suppressed.get():
            return False
        if record.name in {"httpx", "uvicorn.access", "hypercorn.access"}:
            return not _record_contains_health_path(record)
        return True


_health_noise_filter = _HealthNoiseFilter()


@contextmanager
def suppress_health_observability() -> Iterator[None]:
    """Suppress logs and outbound HTTP spans for one health operation."""

    token = _health_observability_suppressed.set(True)
    try:
        try:
            from opentelemetry.instrumentation.utils import suppress_http_instrumentation
        except Exception:
            yield
        else:
            with suppress_http_instrumentation():
                yield
    finally:
        _health_observability_suppressed.reset(token)


def _drop_suppressed_health_log(
    _logger: Any, _method_name: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    if _health_observability_suppressed.get():
        raise structlog.DropEvent
    return event_dict


def metrics_text() -> bytes:
    """Prometheus exposition bytes for the ``/metrics`` route (default registry)."""
    return generate_latest()


def _add_trace_context(
    _logger: Any, _method_name: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """Add the active OTel trace and span ids without risking log delivery."""
    try:
        from opentelemetry import trace

        span_context = trace.get_current_span().get_span_context()
    except Exception:
        return event_dict

    if not span_context.is_valid:
        return event_dict

    event_dict["trace_id"] = f"{span_context.trace_id:032x}"
    event_dict["span_id"] = f"{span_context.span_id:016x}"
    return event_dict


def _configure_structlog(log_level: str) -> None:
    """JSON logs to stdout, shared processor chain, level from settings."""
    level = getattr(logging, log_level.upper(), logging.INFO)
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level)
    for logger_name in ("httpx", "uvicorn.access", "hypercorn.access"):
        logger = logging.getLogger(logger_name)
        if _health_noise_filter not in logger.filters:
            logger.addFilter(_health_noise_filter)
    structlog.configure(
        processors=[
            _drop_suppressed_health_log,
            structlog.contextvars.merge_contextvars,
            _add_trace_context,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


_tracer = None


def _otlp_http_traces_url(endpoint: str) -> str:
    """Resolve the OTLP/HTTP traces URL from a configured base endpoint.

    The config value is the OTLP base (e.g. ``http://host.docker.internal:4318``),
    per OTEL_EXPORTER_OTLP_ENDPOINT convention. But the Python OTLP/HTTP
    ``OTLPSpanExporter(endpoint=...)`` kwarg is taken VERBATIM and does NOT append
    the ``/v1/traces`` signal path the way the env var does, so a base value posts
    to the collector root and gets a 404. Append it here, idempotently."""
    base = endpoint.rstrip("/")
    if base.endswith("/v1/traces"):
        return base
    return base + "/v1/traces"


def _configure_otel(service_name: str, endpoint: str):
    """Best-effort OTel tracer. Degrades to a no-op tracer if the SDK/exporter
    is unavailable, so obs never blocks startup."""
    global _tracer
    try:
        from opentelemetry import trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
        if endpoint:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

            provider.add_span_processor(
                BatchSpanProcessor(OTLPSpanExporter(endpoint=_otlp_http_traces_url(endpoint)))
            )
        trace.set_tracer_provider(provider)
        _tracer = trace.get_tracer(service_name)
    except Exception:
        _tracer = None
    return _tracer


def get_tracer():
    """The configured tracer, or None if OTel is not wired."""
    return _tracer


def is_trace_bodies_enabled() -> bool:
    return get_settings().trace_bodies


@dataclass(frozen=True)
class RequestTraceContext:
    logical_model: str
    request_model: str
    request_kind: str
    upstream_mode: str = ""
    trace_bodies: bool = False
    request_id: str = ""
    extra: dict[str, object] = field(default_factory=dict)

    def attrs(self) -> dict[str, object]:
        data: dict[str, object] = {
            "agentproxy.logical_model": self.logical_model,
            "gen_ai.request.model": self.request_model,
            "agentproxy.request_kind": self.request_kind,
        }
        if self.upstream_mode:
            data["agentproxy.upstream_mode"] = self.upstream_mode
        if self.request_id:
            data["agentproxy.request_id"] = self.request_id
        data.update(self.extra)
        return data


@dataclass(frozen=True)
class InstrumentedAction:
    log_event: str
    metric: Callable[[], None]
    span_event: str
    fields: dict[str, object]
    level: str = "info"


def request_log_fields(ctx: RequestTraceContext | None, **fields: object) -> dict[str, object]:
    # ctx is optional at the call sites (dispatch/dispatch_stream default it to
    # None); when absent, emit just the ad-hoc fields rather than crashing.
    out: dict[str, object] = {}
    if ctx is not None:
        out["logical_model"] = ctx.logical_model
        out["request_model"] = ctx.request_model
        out["request_kind"] = ctx.request_kind
        if ctx.upstream_mode:
            out["upstream_mode"] = ctx.upstream_mode
        if ctx.request_id:
            out["request_id"] = ctx.request_id
        out.update(ctx.extra)
    out.update(fields)
    return out


def _current_span():
    try:
        from opentelemetry import trace
    except Exception:
        return None
    try:
        span = trace.get_current_span()
    except Exception:
        return None
    if span is None:
        return None
    if not getattr(span, "is_recording", lambda: False)():
        return None
    return span


class _RecordedError(Exception):
    """Static exception type whose message is always a closed-set summary."""


# The closed exception taxonomy (agent-proxy#74). Grouping cardinality in SigNoz
# is bounded by this table. See docs/exception-taxonomy.md.
ERROR_TAXONOMY: dict[str, tuple[str, str]] = {
    "upstream_transport_failed": ("upstream", "Upstream backend transport failed"),
    "response_validation_failed": ("dispatch", "Upstream response failed validation"),
    "context_truncated": ("dispatch", "Backend delivered less context than requested"),
    "stream_failed": ("stream", "Streaming response failed before completion"),
    "queue_worker_failed": ("queue", "Queue worker failed while dispatching"),
    "body_capture_failed": ("capture", "Model body capture failed"),
    "trajectory_event_dropped": ("trajectory", "Trajectory event dropped before storage"),
    "trajectory_event_persist_failed": ("trajectory", "Trajectory event failed to persist"),
    "invalid_request_error": ("request", "Client request was malformed"),
    "model_not_found": ("request", "Requested logical route is unknown"),
    "model_unavailable": ("request", "Requested logical route is disabled"),
    "rate_limit_error": ("request", "Request rejected by queue backpressure"),
    "rate_limited": ("request", "Request shed for exceeding the admission rate"),
    "upstream_error": ("upstream", "All backends failed for the request"),
    "upstream_5xx": ("upstream", "Upstream backend returned a server error"),
    "backend_saturated": ("upstream", "Backend was too slow to be treated as available"),
    "request_deadline_exceeded": ("request", "Request exceeded its total wall-clock budget"),
    "upstream_request_rejected": ("upstream", "Upstream rejected the request as invalid"),
}

# Fallback for a code outside the table. Unknown codes must never widen
# cardinality, so the offending value is discarded rather than recorded.
UNCLASSIFIED_ERROR = "unclassified_error"
_UNCLASSIFIED = ("unknown", "Unclassified runtime error")

ERROR_STAGES: frozenset[str] = frozenset(
    {stage for stage, _ in ERROR_TAXONOMY.values()} | {_UNCLASSIFIED[0]}
)


def classify_error(error_type: str) -> tuple[str, str, str]:
    """Return ``(code, stage, summary)`` for a requested error type.

    A code outside :data:`ERROR_TAXONOMY` collapses to
    :data:`UNCLASSIFIED_ERROR`. The requested value is deliberately dropped
    instead of being passed through, because an unbounded string reaching a
    span attribute is exactly the cardinality and redaction failure this
    taxonomy exists to prevent.
    """
    entry = ERROR_TAXONOMY.get(error_type)
    if entry is None:
        return (UNCLASSIFIED_ERROR, _UNCLASSIFIED[0], _UNCLASSIFIED[1])
    return (error_type, entry[0], entry[1])


def _record_error_on_span(span: Any, error_type: str) -> None:
    from opentelemetry.trace import Status, StatusCode

    code, stage, summary = classify_error(error_type)
    span.record_exception(
        _RecordedError(summary),
        attributes={"error.type": code, "error.stage": stage},
        escaped=False,
    )
    span.set_attribute("error.type", code)
    span.set_attribute("error.stage", stage)
    span.set_status(Status(StatusCode.ERROR, summary))


def record_error(error_type: str, span: Any | None = None) -> None:
    """Project one closed-set runtime error onto the SigNoz Exceptions surface."""

    target = span
    if target is None or not getattr(target, "is_recording", lambda: False)():
        target = _current_span()
    if target is not None:
        _record_error_on_span(target, error_type)
        return
    if _tracer is None:
        return
    with _tracer.start_as_current_span("error.recorded") as error_span:
        _record_error_on_span(error_span, error_type)


def record_response_status(status_code: int, span: Any | None = None) -> None:
    """Stamp the status the caller actually received onto the request span.

    Issue #106 found a 240-second trace whose root span carried an empty
    ``response_status_code`` and ``has_error: false`` while the upstream had
    returned 500, which makes any alert built on the service error rate
    untrustworthy.
    """
    target = span
    if target is None or not getattr(target, "is_recording", lambda: False)():
        target = _current_span()
    if target is None:
        return
    target.set_attribute("http.response.status_code", status_code)
    target.set_attribute("agentproxy.response.status_code", status_code)


def get_current_trace_span():
    """Return the active span when it carries a valid correlation context."""
    try:
        from opentelemetry import trace

        span = trace.get_current_span()
        span_context = span.get_span_context()
    except Exception:
        return None
    if not span_context.is_valid:
        return None
    return span


def log_on_span(
    span: Any | None,
    event: str,
    level: str = "info",
    /,
    **fields: object,
) -> None:
    """Emit one structured record against an explicitly selected span."""
    logger = getattr(log, level)
    if span is None:
        logger(event, **fields)
        return

    try:
        span_context = span.get_span_context()
        if span_context.is_valid:
            fields["trace_id"] = f"{span_context.trace_id:032x}"
            fields["span_id"] = f"{span_context.span_id:016x}"
    except Exception:
        pass

    try:
        from opentelemetry import trace

        span_scope = trace.use_span(span, end_on_exit=False)
    except Exception:
        logger(event, **fields)
        return

    emitted = False
    try:
        with span_scope:
            logger(event, **fields)
            emitted = True
    except Exception:
        if not emitted:
            logger(event, **fields)


def record_prompt_cache_usage(
    logical_model: str,
    *,
    reported: bool,
    read_tokens: int,
    miss_tokens: int,
    write_tokens: int,
) -> None:
    """Publish one served response's prompt-cache accounting (issue #101).

    Called once per delivered response rather than per upstream attempt, so a
    retried turn counts the tokens the caller was actually billed for. A
    provider that reports no cache accounting publishes nothing at all: an
    Ollama backend reuses its KV cache without saying so, and recording that
    silence as a 100% miss would invent a regression that never happened.
    """
    if not reported:
        return
    llm_prompt_cache_hit_tokens_total.labels(logical_model=logical_model).inc(max(read_tokens, 0))
    llm_prompt_cache_miss_tokens_total.labels(logical_model=logical_model).inc(max(miss_tokens, 0))
    llm_prompt_cache_write_tokens_total.labels(logical_model=logical_model).inc(
        max(write_tokens, 0)
    )


def emit_instrumented_action(action: InstrumentedAction) -> None:
    """Emit the log, metric, and current-span event for one instrumented action."""
    getattr(log, action.level)(action.log_event, **action.fields)
    action.metric()

    span = _current_span()
    if span is None:
        return

    span.add_event(action.span_event, action.fields)
    for key, value in action.fields.items():
        span.set_attribute(key, value)


SENTRY_EVENTS_PER_MINUTE = 20
_sentry_window: list[float] = []


def _sentry_within_budget(now: float) -> bool:
    """Cap events per process so one hot loop cannot spend the monthly quota."""

    cutoff = now - 60.0
    while _sentry_window and _sentry_window[0] < cutoff:
        _sentry_window.pop(0)
    if len(_sentry_window) >= SENTRY_EVENTS_PER_MINUTE:
        return False
    _sentry_window.append(now)
    return True


def _active_trace_id() -> str | None:
    """The active OTel trace id as 32 hex, or None. Never raises into Sentry."""
    try:
        from opentelemetry import trace

        span_context = trace.get_current_span().get_span_context()
    except Exception:
        return None
    return f"{span_context.trace_id:032x}" if span_context.is_valid else None


def tag_sentry_with_trace(span: Any, _scope: Any = None) -> None:
    """Server request hook: put the trace id on this request's Sentry scope while
    the server span is open. Starlette's error middleware captures a crash after
    that span has ended, so before_send alone finds no active span then."""
    try:
        import sentry_sdk

        span_context = span.get_span_context()
        if span_context.is_valid:
            sentry_sdk.get_isolation_scope().set_tag("trace_id", f"{span_context.trace_id:032x}")
    except Exception:
        pass


def _sentry_before_send(event: Event, _hint: dict[str, Any]) -> Event | None:
    if _health_observability_suppressed.get():
        return None
    request = event.get("request") or {}
    url = request.get("url", "") if isinstance(request, dict) else ""
    if isinstance(url, str) and _is_health_path(url):
        return None
    if not _sentry_within_budget(time.monotonic()):
        return None
    # So a Sentry issue opens the trace it came from. Sentry's own trace id is
    # a different one, because traces_sample_rate is 0.
    if (trace_id := _active_trace_id()) is not None:
        event.setdefault("tags", {})["trace_id"] = trace_id
    return event


def _sentry_before_breadcrumb(
    breadcrumb: dict[str, Any], _hint: dict[str, Any]
) -> dict[str, Any] | None:
    if _health_observability_suppressed.get():
        return None
    data = breadcrumb.get("data") or {}
    url = data.get("url", "") if isinstance(data, dict) else ""
    return None if isinstance(url, str) and _is_health_path(url) else breadcrumb


# Frame locals and request bodies stay on, because they make a trace readable.
# These keys hold member or model text and are scrubbed wherever they appear.
SENTRY_USER_DATA_KEYS = [
    "messages",
    "message",
    "prompt",
    "input",
    "content",
    "text",
    "system",
    "instructions",
    "tools",
    "tool_calls",
    "arguments",
    "choices",
    "completion",
    "reply",
    "response_body",
    "request_body",
    "raw_body",
    # FastAPI's raw request bytes, held in its own routing frame.
    "body_bytes",
    "body",
    "payload",
    "state",
    "questions",
    "answers",
]


def _configure_sentry(dsn: str, service_name: str) -> None:
    if not dsn:
        return
    try:
        import sentry_sdk
        from sentry_sdk.integrations.fastapi import FastApiIntegration
        from sentry_sdk.integrations.logging import LoggingIntegration
        from sentry_sdk.integrations.starlette import StarletteIntegration
        from sentry_sdk.scrubber import DEFAULT_DENYLIST, EventScrubber

        # Crashes only (teable:coilyco/deploy#8347), fully annotated: every
        # integration stays on, and only these three are kept from raising events.
        sentry_sdk.init(
            dsn=dsn,
            traces_sample_rate=0.0,
            environment=service_name,
            before_send=_sentry_before_send,
            before_breadcrumb=_sentry_before_breadcrumb,
            send_default_pii=False,
            event_scrubber=EventScrubber(
                denylist=DEFAULT_DENYLIST + SENTRY_USER_DATA_KEYS, recursive=True
            ),
            integrations=[
                LoggingIntegration(event_level=None),
                StarletteIntegration(failed_request_status_codes=set()),
                FastApiIntegration(failed_request_status_codes=set()),
            ],
        )
    except Exception as exc:
        # The class only: a BadDsn message can carry the DSN itself.
        structlog.get_logger(service_name).warning(
            "sentry.init_failed", error_class=type(exc).__name__
        )


def get_logger(name: str) -> structlog.BoundLogger:
    """A JSON structlog bound logger. structlog is configured on first obs setup,
    but log lines emit even before that with structlog's own defaults."""
    return structlog.get_logger(name)


def init_sentry() -> None:
    """Initialize Sentry from settings, only when a DSN is configured."""
    _configure_sentry(get_settings().resolved_sentry_dsn(), get_settings().service_name)


_initialized = False


def setup_observability() -> structlog.BoundLogger:
    """Idempotent obs bring-up. Call once at import/startup, before any logic."""
    global _initialized
    settings = get_settings()
    if not _initialized:
        _configure_structlog(settings.log_level)
        _configure_otel(settings.service_name, settings.otel_exporter_otlp_endpoint)
        _configure_sentry(settings.resolved_sentry_dsn(), settings.service_name)
        _initialized = True
    return structlog.get_logger(settings.service_name)


# Wire obs at import time so importing any app module gives instrumented logs.
log = setup_observability()
