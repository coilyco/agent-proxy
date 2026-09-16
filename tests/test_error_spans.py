"""Error-span contract for the SigNoz Error Management producer surface (#71).

`app.obs.record_error` is the single writer of exception telemetry, and
`tests/test_obs.py` proves the helper itself sets both the exception event and
`StatusCode.ERROR`. What that unit coverage cannot show is whether the real
request paths route their failures through it, and - the load-bearing half -
whether a failure that is later retried successfully leaks an error status onto
the surviving request span. These tests exercise the dispatch paths end to end
against an in-memory exporter and assert both directions.
"""

import asyncio

import httpx
import pytest

from app import obs, resilience, upstream
from app.analysis import count_message_tokens
from app.models import Backend, LogicalModel
from app.upstream import UpstreamError, UpstreamResult


def _exporter_provider():
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return exporter, provider


@pytest.fixture
def traced(monkeypatch):
    """Wire app.obs onto an in-memory exporter and hand back the exporter."""
    exporter, provider = _exporter_provider()
    tracer = provider.get_tracer("test")
    monkeypatch.setattr(obs, "_tracer", tracer)
    return exporter


def _backend(name: str = "b1") -> Backend:
    return Backend(name=name, url="http://backend.invalid", ollama_tag="m")


def _model(*backends: Backend) -> LogicalModel:
    return LogicalModel(name="logical", num_ctx=4096, backends=list(backends or (_backend(),)))


def _messages():
    return [{"role": "user", "content": "hello"}]


def _ok(content: str = "ok") -> UpstreamResult:
    """A successful result whose reported prompt usage matches what was sent.

    ``prompt_eval_count`` has to agree with the proxy's own token count or the
    delivered-context detector (issue #33) reads the gap as a truncated window
    and records ``context_truncated`` on the attempt - which would mark an
    otherwise-clean span as an error and make these assertions lie.
    """
    sent = count_message_tokens(_messages())
    return UpstreamResult(model="m", content=content, prompt_eval_count=sent, eval_count=1)


def _error_spans(exporter):
    from opentelemetry.trace import StatusCode

    return [s for s in exporter.get_finished_spans() if s.status.status_code is StatusCode.ERROR]


def _has_exception(span) -> bool:
    return any(event.name == "exception" for event in span.events)


# Non-streaming


def test_nonstreaming_transport_failure_marks_attempt_span_error(traced, monkeypatch):
    """Every attempt fails: each attempt span carries the exception AND the status."""

    async def always_fails(*args, **kwargs):
        raise UpstreamError("connection refused")

    monkeypatch.setattr(upstream, "chat", always_fails)
    monkeypatch.setattr(resilience.get_settings(), "max_retries", 1, raising=False)

    with pytest.raises(resilience.BackendUnavailable):
        asyncio.run(resilience.dispatch(_model(), _messages()))

    errored = _error_spans(traced)
    assert errored, "a failed dispatch must produce at least one error span"
    for span in errored:
        assert _has_exception(span), f"{span.name} has error status but no exception event"
        assert span.attributes["error.type"] == "upstream_transport_failed"
        assert span.attributes["error.stage"] == "upstream"


def test_validation_failure_marks_attempt_span_error(traced, monkeypatch):
    """A structurally invalid response is a recorded error, not a silent reroll."""

    async def returns_empty(*args, **kwargs):
        return UpstreamResult(model="m", content="", prompt_eval_count=1, eval_count=1)

    monkeypatch.setattr(upstream, "chat", returns_empty)
    monkeypatch.setattr(resilience.get_settings(), "max_retries", 0, raising=False)

    with pytest.raises(resilience.BackendUnavailable):
        asyncio.run(resilience.dispatch(_model(), _messages()))

    errored = _error_spans(traced)
    assert errored
    assert any(s.attributes["error.type"] == "response_validation_failed" for s in errored)
    for span in errored:
        assert _has_exception(span)


# The retry-does-not-poison-success contract


def test_recovered_retry_leaves_the_successful_attempt_clean(traced, monkeypatch):
    """#71's load-bearing criterion.

    A transport failure followed by a successful retry must stay individually
    visible as a failed attempt, while the attempt that actually succeeded must
    NOT be marked as an error. Marking the recovery would turn every transient
    blip into a false alert in SigNoz Error Management.
    """
    from opentelemetry.trace import StatusCode

    calls = {"n": 0}

    async def fails_then_succeeds(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise UpstreamError("transient reset")
        return _ok("recovered")

    monkeypatch.setattr(upstream, "chat", fails_then_succeeds)
    monkeypatch.setattr(resilience.get_settings(), "max_retries", 2, raising=False)
    monkeypatch.setattr(resilience.get_settings(), "retry_base_delay", 0.0, raising=False)

    result = asyncio.run(resilience.dispatch(_model(), _messages()))
    assert result.content == "recovered"
    assert calls["n"] == 2

    attempts = [s for s in traced.get_finished_spans() if s.name == "resilience.attempt"]
    assert len(attempts) == 2, "both the failed and the recovering attempt must be visible"

    failed, recovered = attempts[0], attempts[1]
    assert failed.status.status_code is StatusCode.ERROR
    assert _has_exception(failed)

    assert recovered.status.status_code is not StatusCode.ERROR, (
        "the attempt that succeeded must not be marked as an error - "
        "a recovered retry is not a failed request"
    )
    assert not _has_exception(recovered)


def test_fallback_to_second_backend_does_not_mark_the_serving_backend(traced, monkeypatch):
    """A dead primary is an error; the backend that served the request is not."""
    from opentelemetry.trace import StatusCode

    async def fail_first_backend(backend, *args, **kwargs):
        if backend.name == "dead":
            raise UpstreamError("no route to host")
        return _ok("served")

    monkeypatch.setattr(upstream, "chat", fail_first_backend)
    monkeypatch.setattr(resilience.get_settings(), "max_retries", 0, raising=False)
    resilience.breakers._breakers.clear()

    model = _model(_backend("dead"), _backend("live"))
    result = asyncio.run(resilience.dispatch(model, _messages()))
    assert result.content == "served"

    attempts = [s for s in traced.get_finished_spans() if s.name == "resilience.attempt"]
    by_backend = {s.attributes.get("agentproxy.backend"): s for s in attempts}
    assert by_backend["dead"].status.status_code is StatusCode.ERROR
    assert by_backend["live"].status.status_code is not StatusCode.ERROR


# Streaming


def test_streaming_connect_failure_records_an_error_span(traced, monkeypatch):
    """A stream that dies before the first chunk records the error like the
    non-streaming path.

    Driven through the real ``upstream.chat_stream`` with a failing transport
    rather than by stubbing ``chat_stream`` itself, because the ``record_error``
    call lives inside that function. Stubbing it out would remove the very code
    under test and leave the assertion passing vacuously.
    """
    from opentelemetry.trace import StatusCode

    class _FailingStream:
        async def __aenter__(self):
            raise httpx.ConnectError("stream connect failed")

        async def __aexit__(self, *exc):
            return False

    class _FailingClient:
        def stream(self, *args, **kwargs):
            return _FailingStream()

    monkeypatch.setattr(upstream, "get_client", lambda: _FailingClient())

    async def drain():
        async for _ in upstream.chat_stream(
            _backend(), 4096, _messages(), span_attrs={"agentproxy.logical_model": "logical"}
        ):
            pass

    with pytest.raises(UpstreamError):
        asyncio.run(drain())

    errored = _error_spans(traced)
    assert errored, "a streaming connect failure must produce an error span"
    for span in errored:
        assert _has_exception(span)
        assert span.attributes["error.type"] == "upstream_transport_failed"
    assert errored[0].status.status_code is StatusCode.ERROR


# Redaction - no request content may reach exception fields


def test_exception_fields_carry_no_request_content(traced, monkeypatch):
    """The closed-set contract: a secret in the prompt or the upstream error
    message must never reach an exception message, status description, or
    error.type attribute."""
    secret = "sk-live-DO-NOT-LEAK-9f3a"

    async def fails_with_secret(*args, **kwargs):
        raise UpstreamError(f"401 unauthorized for token {secret}")

    monkeypatch.setattr(upstream, "chat", fails_with_secret)
    monkeypatch.setattr(resilience.get_settings(), "max_retries", 0, raising=False)

    messages = [{"role": "user", "content": f"my key is {secret}"}]
    with pytest.raises(resilience.BackendUnavailable):
        asyncio.run(resilience.dispatch(_model(), messages))

    for span in traced.get_finished_spans():
        for event in span.events:
            for value in (event.attributes or {}).values():
                assert secret not in str(value), f"{event.name} leaked request content"
        if span.status.description:
            assert secret not in span.status.description
        for value in (span.attributes or {}).values():
            assert secret not in str(value)


def test_request_span_carries_the_status_the_caller_received(traced, monkeypatch, app_client):
    """Issue #106: an empty response_status_code makes the error rate unreadable."""
    from app import models
    from app.upstream import UpstreamStatusError

    async def fake_catalog(_base_url):
        return {"qwen3:4b": 262144}, True

    async def unavailable(*args, **kwargs):
        raise UpstreamStatusError("litellm: Server error '500'", status_code=500)

    monkeypatch.setattr(models, "_catalog", fake_catalog)
    monkeypatch.setattr(upstream, "chat", unavailable)
    monkeypatch.setattr(resilience.get_settings(), "retry_base_delay", 0.0, raising=False)
    models.reset_catalog()

    response = app_client.post(
        "/v1/chat/completions",
        json={"model": "qwen3:4b", "messages": [{"role": "user", "content": "hi"}]},
    )

    assert response.status_code == 502
    statuses = [
        span.attributes.get("http.response.status_code")
        for span in traced.get_finished_spans()
        if span.name == "request.chat"
    ]
    assert 502 in statuses, "the request span must record the status the caller received"


def test_spans_carry_backend_identity_and_regime(traced, monkeypatch, app_client):
    """Issue #109: p99 by backend by regime needs both to be groupable."""
    from app import models
    from app.upstream import UpstreamResult

    async def fake_catalog(_base_url):
        return {"qwen3:4b": 262144}, True

    async def served(
        backend, num_ctx, messages, *, tools=None, tool_policy=None, options=None, span_attrs=None
    ):
        return UpstreamResult(model=backend.ollama_tag, content="ok")

    monkeypatch.setattr(models, "_catalog", fake_catalog)
    monkeypatch.setattr(upstream, "chat", served)
    monkeypatch.setattr(models.get_settings(), "backend_regime", "idle")
    models.reset_catalog()

    response = app_client.post(
        "/v1/chat/completions",
        json={"model": "qwen3:4b", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert response.status_code == 200

    by_name = {span.name: span.attributes for span in traced.get_finished_spans()}
    # The request span is the one a latency query groups on, and it used to
    # carry no backend at all.
    assert by_name["request.chat"]["agentproxy.backend"] == "tower-3026"
    assert by_name["request.chat"]["agentproxy.backend.regime"] == "idle"
    assert by_name["resilience.attempt"]["agentproxy.backend.regime"] == "idle"


def test_a_backend_spec_can_declare_its_own_regime(monkeypatch):
    import json as _json

    from app import models

    monkeypatch.setattr(
        models.get_settings(),
        "backends_json",
        _json.dumps(
            [
                {"name": "tower", "url": "http://tower", "regime": "contended"},
                {"name": "hosted", "url": "http://hosted", "dialect": "openai"},
            ]
        ),
    )
    monkeypatch.setattr(models.get_settings(), "backend_regime", "idle")

    backends = models._backends_for_model("t")

    # A per-backend declaration wins; the rest inherit the fleet default.
    assert backends[0].regime == "contended"
    assert backends[1].regime == "idle"


def test_regime_defaults_to_unknown_rather_than_a_guess(monkeypatch):
    from app import models

    monkeypatch.setattr(models.get_settings(), "backends_json", "")
    assert models.get_settings().backend_regime == "unknown"
    assert models._backends_for_model("t")[0].regime == "unknown"
