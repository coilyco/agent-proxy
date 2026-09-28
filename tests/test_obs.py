"""Tests for observability wiring helpers."""

import json
import logging

import pytest

from app.obs import (
    InstrumentedAction,
    _HealthNoiseFilter,
    _add_trace_context,
    _otlp_http_traces_url,
    _sentry_before_breadcrumb,
    _sentry_before_send,
    emit_instrumented_action,
    get_current_trace_span,
    get_logger,
    init_sentry,
    log_on_span,
    metrics_text,
    record_error,
    suppress_health_observability,
)


@pytest.mark.parametrize(
    "endpoint,expected",
    [
        # Base endpoint (the documented convention) gets the signal path appended.
        ("http://host.docker.internal:4318", "http://host.docker.internal:4318/v1/traces"),
        # Trailing slash is normalized, not doubled.
        ("http://host.docker.internal:4318/", "http://host.docker.internal:4318/v1/traces"),
        ("http://localhost:4318", "http://localhost:4318/v1/traces"),
        # Already-full traces URL is left alone (idempotent), no double-append.
        (
            "http://host.docker.internal:4318/v1/traces",
            "http://host.docker.internal:4318/v1/traces",
        ),
        (
            "http://host.docker.internal:4318/v1/traces/",
            "http://host.docker.internal:4318/v1/traces",
        ),
    ],
)
def test_otlp_http_traces_url(endpoint, expected):
    assert _otlp_http_traces_url(endpoint) == expected


def test_get_logger_emits_json(capsys):
    get_logger("t").info("hello", k=1)
    line = capsys.readouterr().out.strip().splitlines()[-1]
    payload = json.loads(line)
    assert payload["event"] == "hello"
    assert payload["k"] == 1


def test_health_suppression_drops_structured_logs(capsys):
    with suppress_health_observability():
        get_logger("t").info("must-not-emit")

    assert capsys.readouterr().out == ""


def test_health_noise_filter_drops_access_records():
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1", "GET", "/readyz/community/route", "1.1", 200),
        None,
    )

    assert _HealthNoiseFilter().filter(record) is False


def test_sentry_filters_health_urls_and_suppressed_dependencies():
    assert _sentry_before_send({"request": {"url": "http://proxy/healthz"}}, {}) is None
    assert (
        _sentry_before_breadcrumb({"data": {"url": "http://proxy/readyz/community/route"}}, {})
        is None
    )
    with suppress_health_observability():
        assert _sentry_before_send({"request": {"url": "http://litellm/v1/models"}}, {}) is None


def test_get_logger_adds_active_trace_context(monkeypatch, capsys):
    class SpanContext:
        is_valid = True
        trace_id = 0x123
        span_id = 0x456

    class Span:
        def get_span_context(self):
            return SpanContext()

    monkeypatch.setattr("opentelemetry.trace.get_current_span", lambda: Span())

    get_logger("t").info("correlated")

    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["trace_id"] == "00000000000000000000000000000123"
    assert payload["span_id"] == "0000000000000456"


def test_trace_context_processor_leaves_invalid_context_unchanged(monkeypatch):
    class SpanContext:
        is_valid = False

    class Span:
        def get_span_context(self):
            return SpanContext()

    monkeypatch.setattr("opentelemetry.trace.get_current_span", lambda: Span())
    event = {"event": "uncorrelated"}

    assert _add_trace_context(None, "info", event) == {"event": "uncorrelated"}


def test_trace_context_processor_is_failure_safe(monkeypatch):
    def fail():
        raise RuntimeError("trace context unavailable")

    monkeypatch.setattr("opentelemetry.trace.get_current_span", fail)
    event = {"event": "still-logged"}

    assert _add_trace_context(None, "info", event) == {"event": "still-logged"}


def test_log_on_span_activates_requested_trace_context(capsys):
    from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags

    span = NonRecordingSpan(
        SpanContext(
            trace_id=0x123,
            span_id=0x456,
            is_remote=False,
            trace_flags=TraceFlags(0x01),
        )
    )

    log_on_span(span, "root.completed")

    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["event"] == "root.completed"
    assert payload["trace_id"] == "00000000000000000000000000000123"
    assert payload["span_id"] == "0000000000000456"


def test_get_current_trace_span_accepts_valid_nonrecording_span():
    from opentelemetry import trace
    from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags

    span = NonRecordingSpan(
        SpanContext(
            trace_id=0x123,
            span_id=0x456,
            is_remote=False,
            trace_flags=TraceFlags(0x01),
        )
    )

    with trace.use_span(span, end_on_exit=False):
        assert get_current_trace_span() is span


def test_emit_instrumented_action_hits_log_metric_and_span(monkeypatch, capsys):
    events = []
    attributes = {}
    metric_calls = []

    class Span:
        def is_recording(self):
            return True

        def add_event(self, name, attrs):
            events.append((name, attrs))

        def set_attribute(self, key, value):
            attributes[key] = value

    monkeypatch.setattr("app.obs._current_span", lambda: Span())

    emit_instrumented_action(
        InstrumentedAction(
            log_event="request.prompt_trimmed",
            metric=lambda: metric_calls.append(1),
            span_event="request.prompt_trimmed",
            fields={"logical_model": "m", "dropped_message_count": 2},
        )
    )

    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["event"] == "request.prompt_trimmed"
    assert payload["logical_model"] == "m"
    assert metric_calls == [1]
    assert events == [
        ("request.prompt_trimmed", {"logical_model": "m", "dropped_message_count": 2})
    ]
    assert attributes["logical_model"] == "m"
    assert attributes["dropped_message_count"] == 2


def test_record_error_emits_closed_set_exception_and_error_status():
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    from opentelemetry.trace import StatusCode

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    with provider.get_tracer("test").start_as_current_span("failed.operation") as span:
        record_error("upstream_transport_failed", span)

    ended = exporter.get_finished_spans()
    assert len(ended) == 1
    assert ended[0].status.status_code is StatusCode.ERROR
    assert ended[0].status.description == "Upstream backend transport failed"
    exception = next(event for event in ended[0].events if event.name == "exception")
    assert exception.attributes["exception.type"] == "app.obs._RecordedError"
    assert exception.attributes["exception.message"] == "Upstream backend transport failed"
    assert exception.attributes["error.type"] == "upstream_transport_failed"
    assert exception.attributes["error.stage"] == "upstream"


def test_record_error_starts_span_for_background_failure(monkeypatch):
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr("app.obs._tracer", provider.get_tracer("test"))

    record_error("trajectory_event_persist_failed")

    ended = exporter.get_finished_spans()
    assert len(ended) == 1
    assert ended[0].name == "error.recorded"
    assert any(event.name == "exception" for event in ended[0].events)


def test_metrics_text_exposes_leg04_names():
    text = metrics_text()
    assert isinstance(text, bytes)
    for name in (
        b"llm_queue_depth",
        b"llm_retries_total",
        b"llm_fallbacks_total",
        b"llm_circuit_state",
        b"llm_truncation_avoided_total",
        b"ward_skill_use_total",
        b"agent_proxy_health_endpoint_requests_total",
        b"agent_proxy_readiness_checks_total",
        b"agent_proxy_route_ready",
    ):
        assert name in text


def test_init_sentry_no_dsn_is_noop(monkeypatch):
    # With no DSN configured, init_sentry must not raise (best-effort, no-op).
    from app import config

    monkeypatch.setattr(config.Settings, "resolved_sentry_dsn", lambda self: "")
    config.get_settings.cache_clear()
    try:
        init_sentry()
    finally:
        config.get_settings.cache_clear()


def test_sentry_budget_caps_events_per_process_minute(monkeypatch):
    from app import obs

    monkeypatch.setattr(obs, "_sentry_window", [])
    allowed = [obs._sentry_within_budget(100.0) for _ in range(obs.SENTRY_EVENTS_PER_MINUTE + 1)]
    assert allowed.count(True) == obs.SENTRY_EVENTS_PER_MINUTE
    assert allowed[-1] is False
    # A minute later the window has drained and events flow again.
    assert obs._sentry_within_budget(161.0) is True


def test_sentry_sdk_is_installed_and_initialises(monkeypatch):
    # The package was once absent from the image and the import failure was
    # swallowed, so a configured DSN reported nothing.
    import sentry_sdk

    from app import obs

    warnings = []
    monkeypatch.setattr(
        obs.structlog,
        "get_logger",
        lambda *_a: type("L", (), {"warning": lambda _s, *a, **k: warnings.append((a, k))})(),
    )
    try:
        obs._configure_sentry("https://public@example.invalid/1", "agent-proxy-test")
        assert sentry_sdk.get_client().is_active()
        assert warnings == []
    finally:
        sentry_sdk.init()


def test_sentry_init_failure_logs_the_class_and_never_the_message(monkeypatch):
    import sentry_sdk

    from app import obs

    def refuse(**_kwargs):
        raise ValueError("https://secret-key@o0.ingest.example/1")

    warnings = []
    monkeypatch.setattr(sentry_sdk, "init", refuse)
    monkeypatch.setattr(
        obs.structlog,
        "get_logger",
        lambda *_a: type("L", (), {"warning": lambda _s, *a, **k: warnings.append((a, k))})(),
    )
    obs._configure_sentry("https://secret-key@o0.ingest.example/1", "agent-proxy-test")
    assert warnings == [(("sentry.init_failed",), {"error_class": "ValueError"})]
    assert "secret-key" not in repr(warnings)


class _SentryCapture:
    def __init__(self):
        from sentry_sdk.transport import Transport

        class _T(Transport):
            def __init__(inner):
                super().__init__()

            def capture_envelope(inner, envelope):
                event = envelope.get_event()
                if event is not None:
                    self.events.append(event)

        self.events: list[dict] = []
        self.transport = _T()


@pytest.fixture
def sentry_capture(monkeypatch):
    import sentry_sdk

    from app import obs

    capture = _SentryCapture()
    real_init = sentry_sdk.init
    monkeypatch.setattr(
        sentry_sdk, "init", lambda **kwargs: real_init(transport=capture.transport, **kwargs)
    )
    monkeypatch.setattr(obs, "_sentry_window", [])
    obs._configure_sentry("https://public@example.invalid/1", "agent-proxy-test")
    yield capture
    real_init()


def _crash_app():
    from fastapi import FastAPI, HTTPException

    app = FastAPI()

    @app.post("/crash")
    async def crash(body: dict):
        prompt_text = body["prompt"]  # noqa: F841
        raise RuntimeError("route crashed")

    @app.get("/handled")
    async def handled():
        logging.getLogger("httpx").error("upstream 502, falling back")
        return {"ok": True}

    @app.get("/refused")
    async def refused():
        raise HTTPException(status_code=503, detail="deliberate")

    return app


def test_sentry_sends_a_crash_without_frame_locals(sentry_capture):
    import sentry_sdk
    from fastapi.testclient import TestClient

    client = TestClient(_crash_app(), raise_server_exceptions=False)
    secret = "-".join(["PROMPT", "SECRET"])
    assert client.post("/crash", json={"prompt": secret}).status_code == 500
    sentry_sdk.flush()
    assert [e["exception"]["values"][-1]["value"] for e in sentry_capture.events] == [
        "route crashed"
    ]
    assert secret not in json.dumps(sentry_capture.events)


def test_sentry_leaves_handled_errors_in_signoz(sentry_capture):
    import sentry_sdk
    from fastapi.testclient import TestClient

    client = TestClient(_crash_app(), raise_server_exceptions=False)
    assert client.get("/handled").status_code == 200
    assert client.get("/refused").status_code == 503
    sentry_sdk.flush()
    assert sentry_capture.events == []


def test_sentry_disables_the_mcp_tool_error_integration(sentry_capture):
    import sentry_sdk

    assert sentry_sdk.get_client().get_integration("mcp") is None
