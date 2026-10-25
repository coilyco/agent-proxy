"""Tests for observability wiring helpers."""

import json
import logging

import pytest

from app import obs
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
                for item in envelope.items:
                    if item.type == "transaction":
                        self.transactions.append(item.payload.json)
                    elif item.type == "log":
                        self.logs.extend(item.payload.json["items"])

        self.events: list[dict] = []
        self.transactions: list[dict] = []
        self.logs: list[dict] = []
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
    obs._configure_sentry("https://public@example.invalid/1", "agent-proxy-test", 1.0)
    yield capture
    real_init()


def _crash_app():
    from fastapi import FastAPI, HTTPException

    app = FastAPI()

    @app.post("/crash")
    async def crash(request: dict):
        route_name = request["model"]  # noqa: F841
        messages = request["messages"]  # noqa: F841
        logging.getLogger("app.test").warning("routing request")
        raise RuntimeError("route crashed")

    @app.post("/ok")
    async def ok(request: dict):
        return {"ok": True}

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    @app.get("/handled")
    async def handled():
        logging.getLogger("httpx").error("upstream 502, falling back")
        return {"ok": True}

    @app.get("/refused")
    async def refused():
        raise HTTPException(status_code=503, detail="deliberate")

    return app


def test_sentry_keeps_locals_and_scrubs_the_keys_holding_user_data(sentry_capture):
    import sentry_sdk
    from fastapi.testclient import TestClient

    client = TestClient(_crash_app(), raise_server_exceptions=False)
    secret = "-".join(["PROMPT", "SECRET"])
    body = {"model": "chat/default", "messages": [{"role": "user", "content": secret}]}
    assert client.post("/crash", json=body).status_code == 500
    sentry_sdk.flush()
    (event,) = sentry_capture.events
    assert event["exception"]["values"][-1]["value"] == "route crashed"
    frame_vars = event["exception"]["values"][-1]["stacktrace"]["frames"][-1]["vars"]
    # Locals are what make the trace useful, so a harmless one stays readable.
    assert "chat/default" in frame_vars["route_name"]
    assert secret not in json.dumps(sentry_capture.events)


def test_sentry_leaves_handled_errors_in_signoz(sentry_capture):
    import sentry_sdk
    from fastapi.testclient import TestClient

    client = TestClient(_crash_app(), raise_server_exceptions=False)
    assert client.get("/handled").status_code == 200
    assert client.get("/refused").status_code == 503
    sentry_sdk.flush()
    assert sentry_capture.events == []


def test_a_crash_carries_its_breadcrumbs_and_request_context(sentry_capture):
    import sentry_sdk
    from fastapi.testclient import TestClient

    client = TestClient(_crash_app(), raise_server_exceptions=False)
    body = {"model": "chat/default", "messages": []}
    assert client.post("/crash", json=body).status_code == 500
    sentry_sdk.flush()
    (event,) = sentry_capture.events
    crumbs = [crumb.get("message") for crumb in event["breadcrumbs"]["values"]]
    assert "routing request" in crumbs
    assert event["request"]["method"] == "POST"
    assert event["request"]["url"].endswith("/crash")


def test_sentry_reports_mcp_tool_errors_without_their_arguments(sentry_capture):
    import sentry_sdk

    client = sentry_sdk.get_client()
    assert client.get_integration("mcp") is not None
    # The MCP integration records tool arguments and results only with PII on.
    assert client.options["send_default_pii"] is False


def test_a_sentry_event_carries_the_active_otel_trace_id():
    from opentelemetry.sdk.trace import TracerProvider

    tracer = TracerProvider().get_tracer("t")
    with tracer.start_as_current_span("server") as span:
        event = _sentry_before_send({"request": {"url": "http://proxy/v1/x"}}, {})
        want = f"{span.get_span_context().trace_id:032x}"
    assert event["tags"]["trace_id"] == want


def test_a_sentry_event_outside_any_span_has_no_trace_id_tag():
    event = _sentry_before_send({"request": {"url": "http://proxy/v1/x"}}, {})
    assert "trace_id" not in event.get("tags", {})


def test_a_real_crash_under_the_fastapi_instrumentor_carries_the_server_span_trace_id(
    sentry_capture,
):
    import sentry_sdk
    from fastapi.testclient import TestClient
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    from app.main import _instrument_fastapi

    app = _crash_app()
    _instrument_fastapi(app, tracer_provider=provider)
    client = TestClient(app, raise_server_exceptions=False)
    assert client.post("/crash", json={"model": "m", "messages": []}).status_code == 500
    sentry_sdk.flush()
    (event,) = sentry_capture.events
    server_traces = {f"{span.context.trace_id:032x}" for span in exporter.get_finished_spans()}
    assert event["tags"]["trace_id"] in server_traces


def test_two_crashes_each_carry_their_own_trace_id(sentry_capture):
    import sentry_sdk
    from fastapi.testclient import TestClient
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from app.main import _instrument_fastapi

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    app = _crash_app()
    _instrument_fastapi(app, tracer_provider=provider)
    client = TestClient(app, raise_server_exceptions=False)
    for _ in range(2):
        assert client.post("/crash", json={"model": "m", "messages": []}).status_code == 500
    sentry_sdk.flush()
    seen = [event["tags"]["trace_id"] for event in sentry_capture.events]
    server_traces = {f"{span.context.trace_id:032x}" for span in exporter.get_finished_spans()}
    assert len(seen) == 2 and len(set(seen)) == 2
    assert set(seen) <= server_traces


def test_sentry_has_no_per_minute_event_cap(sentry_capture):
    import sentry_sdk
    from fastapi.testclient import TestClient

    client = TestClient(_crash_app(), raise_server_exceptions=False)
    for _ in range(30):
        assert client.post("/crash", json={"model": "m", "messages": []}).status_code == 500
    sentry_sdk.flush()
    assert len(sentry_capture.events) == 30


def test_a_request_is_a_sentry_trace_that_carries_no_model_text(sentry_capture):
    import sentry_sdk
    from fastapi.testclient import TestClient

    client = TestClient(_crash_app(), raise_server_exceptions=False)
    secret = "-".join(["PROMPT", "SECRET"])
    body = {"model": "chat/default", "messages": [{"role": "user", "content": secret}]}
    assert client.post("/ok", json=body).status_code == 200
    sentry_sdk.flush()
    (transaction,) = sentry_capture.transactions
    assert transaction["transaction"] == "/ok"
    assert "data" not in transaction["request"]
    assert secret not in json.dumps(transaction)


def test_a_plain_text_body_is_dropped_from_the_trace():
    event = {"request": {"url": "http://proxy/v1/x", "data": "PROMPT-SECRET"}}
    assert obs._sentry_before_send_transaction(event, {}) == {
        "request": {"url": "http://proxy/v1/x"}
    }


def test_health_probes_are_not_traced(sentry_capture):
    import sentry_sdk
    from fastapi.testclient import TestClient

    client = TestClient(_crash_app(), raise_server_exceptions=False)
    assert client.get("/healthz").status_code == 200
    sentry_sdk.flush()
    assert sentry_capture.transactions == []


def test_the_trace_sampler_takes_the_rate_a_caller_decision_and_skips_health():
    sampler = obs._make_traces_sampler(0.25)
    assert sampler({"asgi_scope": {"path": "/v1/chat/completions"}}) == 0.25
    assert sampler({"asgi_scope": {"path": "/readyz/chat/default"}}) == 0.0
    assert sampler({"asgi_scope": {"path": "/metrics"}, "parent_sampled": True}) == 0.0
    assert sampler({"asgi_scope": {"path": "/v1/x"}, "parent_sampled": True}) == 1.0
    assert sampler({"asgi_scope": {"path": "/v1/x"}, "parent_sampled": False}) == 0.0


def test_an_error_log_reaches_sentry_logs_with_allowlisted_fields_only(sentry_capture):
    import sentry_sdk

    secret = "-".join(["PROMPT", "SECRET"])
    obs.log.error(
        "stream.failed",
        logical_model="chat/default",
        request_id="req-1",
        outcome="failed",
        error=f"upstream echoed {secret}",
        reason=secret,
        origin=secret,
        messages=[{"content": secret}],
    )
    obs.log.warning("stream.request_rejected", logical_model="chat/default")
    sentry_sdk.flush()
    (record,) = sentry_capture.logs
    assert record["body"] == "stream.failed"
    assert record["level"] == "error"
    attributes = {k: v["value"] for k, v in record["attributes"].items()}
    assert attributes["logical_model"] == "chat/default"
    assert attributes["request_id"] == "req-1"
    assert attributes["outcome"] == "failed"
    assert not {"error", "reason", "origin", "messages"} & attributes.keys()
    assert secret not in json.dumps(sentry_capture.logs)


def test_stdlib_error_records_stay_out_of_sentry_logs(sentry_capture):
    import sentry_sdk

    logging.getLogger("httpx").error("upstream said PROMPT-SECRET")
    sentry_sdk.flush()
    assert sentry_capture.logs == []


def test_a_log_whose_body_is_not_an_event_name_is_refused():
    record = {"body": "free text with a prompt", "attributes": {}}
    assert obs._sentry_before_send_log(record, {}) is None


def test_a_failed_agent_proxy_request_is_a_sentry_trace_and_an_error_log(
    sentry_capture, monkeypatch, app_client
):
    """COI-2904 acceptance: the trace and the log line, and no model text in either."""
    import sentry_sdk

    from app import models, resilience, upstream
    from app.main import app
    from app.upstream import UpstreamStatusError

    async def fake_catalog(_base_url):
        return {"qwen3:4b": 262144}, True

    async def unavailable(*_args, **_kwargs):
        raise UpstreamStatusError("litellm: Server error '500'", status_code=500)

    monkeypatch.setattr(models, "_catalog", fake_catalog)
    monkeypatch.setattr(upstream, "chat", unavailable)
    monkeypatch.setattr(resilience.get_settings(), "retry_base_delay", 0.0, raising=False)
    # Sentry wraps the stack when it is built, and the shared app's was built earlier.
    monkeypatch.setattr(app, "middleware_stack", None)
    models.reset_catalog()

    secret = "-".join(["PROMPT", "SECRET"])
    response = app_client.post(
        "/v1/chat/completions",
        json={"model": "qwen3:4b", "messages": [{"role": "user", "content": secret}]},
    )
    sentry_sdk.flush()

    assert response.status_code == 502
    assert [t["transaction"] for t in sentry_capture.transactions] == ["/v1/chat/completions"]
    (record,) = [r for r in sentry_capture.logs if r["body"] == "request.completed"]
    assert record["level"] == "error"
    attributes = {k: v["value"] for k, v in record["attributes"].items()}
    assert attributes["outcome"] == "failed"
    assert attributes["agentproxy.logical_model"] == "qwen3:4b"
    assert not {"error", "agent.origin", "gen_ai.request.model"} & attributes.keys()
    assert secret not in json.dumps([sentry_capture.transactions, sentry_capture.logs])
