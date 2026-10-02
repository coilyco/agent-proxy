"""Stream shape as span attributes instead of one span per SSE chunk (#140).

A streamed completion used to emit a `POST /v1/chat/completions http send` span
for every chunk - 965 of them in one sampled turn - which pushed the spans an
operator actually wanted past the backend's 1000-span per-trace cap. These tests
hold both halves: the chunk spans stay gone, and the four numbers worth keeping
land on the request span instead.
"""

import pytest
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient

from app import main, models, obs, resilience, upstream

CATALOG: dict[str, int | None] = {"qwen3:4b": 262144}


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
    """Point app.obs at an in-memory exporter and hand it back."""
    exporter, provider = _exporter_provider()
    monkeypatch.setattr(obs, "_tracer", provider.get_tracer("test"))
    return exporter


@pytest.fixture
def streaming(monkeypatch, app_client):
    async def fake_catalog(_base_url):
        return dict(CATALOG), True

    monkeypatch.setattr(models, "_catalog", fake_catalog)
    models.reset_catalog()
    yield app_client


def _one_backend_stream(monkeypatch, chunks):
    async def fake_chat_stream(backend, num_ctx, messages, **_kwargs):
        for chunk in chunks:
            yield chunk

    monkeypatch.setattr(upstream, "chat_stream", fake_chat_stream)
    monkeypatch.setattr(resilience, "breakers", resilience.CircuitBreakerRegistry())


def _stream(client, **body):
    return client.post(
        "/v1/chat/completions",
        json={
            "model": "qwen3:4b",
            "stream": True,
            "messages": [{"role": "user", "content": "hi"}],
            **body,
        },
    )


def _request_chat_span(exporter):
    spans = [s for s in exporter.get_finished_spans() if s.name == "request.chat"]
    assert spans, "the streaming path must produce a request.chat span"
    return spans[-1]


# The chunk spans themselves


def test_asgi_send_and_receive_spans_are_suppressed_but_the_server_span_survives():
    """The real proof the exclusion is wired: no `http send`, still a server span.

    Asserting only the absence would pass vacuously if `instrument_app` raised
    and `_instrument_fastapi` swallowed it, so the server span is asserted too.
    """
    exporter, provider = _exporter_provider()
    application = FastAPI()

    @application.get("/stream")
    def stream() -> StreamingResponse:
        def frames():
            for index in range(20):
                yield f"data: {index}\n\n"

        return StreamingResponse(frames(), media_type="text/event-stream")

    main._instrument_fastapi(application, tracer_provider=provider)
    with TestClient(application) as client:
        assert client.get("/stream").status_code == 200

    names = [span.name for span in exporter.get_finished_spans()]
    assert any(
        name.endswith("/stream") for name in names
    ), f"inbound instrumentation did not install at all: {names}"
    assert not [
        name for name in names if name.endswith("http send")
    ], f"per-chunk send spans came back: {names}"
    assert not [
        name for name in names if name.endswith("http receive")
    ], f"receive spans came back: {names}"


# The attributes that replace them


def test_stream_totals_land_on_the_request_span(streaming, monkeypatch, traced):
    _one_backend_stream(
        monkeypatch,
        [
            {"message": {"content": "Pa"}},
            {"message": {"content": "ris"}},
            {"message": {"content": ""}, "done": True, "done_reason": "stop"},
        ],
    )

    body = _stream(streaming).text
    attributes = _request_chat_span(traced).attributes

    # Every SSE frame is counted, comments included, so the total matches what
    # the retired send spans counted rather than only the content deltas.
    written = [line for line in body.splitlines() if line.startswith(("data:", ":"))]
    assert attributes["agentproxy.stream.frames"] == len(written)
    assert attributes["agentproxy.stream.bytes"] == len(body.encode("utf-8"))
    assert attributes["agentproxy.stream.duration_ms"] >= 0
    assert attributes["agentproxy.stream.first_token_ms"] >= 0
    assert (
        attributes["agentproxy.stream.first_token_ms"]
        <= attributes["agentproxy.stream.duration_ms"]
    )


def test_first_token_is_absent_when_the_stream_never_generated(streaming, monkeypatch, traced):
    """An unreported first token and an instantaneous one are different events."""
    _one_backend_stream(monkeypatch, [{"message": {"content": ""}, "done": True}])

    _stream(streaming)
    attributes = _request_chat_span(traced).attributes

    assert "agentproxy.stream.first_token_ms" not in attributes
    assert attributes["agentproxy.stream.frames"] > 0


def test_a_failed_stream_still_reports_what_it_managed_to_send(streaming, monkeypatch, traced):
    """Partial streams are exactly the traces an operator opens."""

    async def dies_midway(backend, num_ctx, messages, **_kwargs):
        yield {"message": {"content": "Pa"}}
        raise upstream.UpstreamError("backend went away")

    monkeypatch.setattr(upstream, "chat_stream", dies_midway)
    monkeypatch.setattr(resilience, "breakers", resilience.CircuitBreakerRegistry())
    monkeypatch.setattr(resilience.get_settings(), "max_retries", 0, raising=False)

    body = _stream(streaming).text
    attributes = _request_chat_span(traced).attributes

    # The turn really did fail: no [DONE], and the partial content still shipped.
    # The turn really did fail, and the partial content still reached the caller.
    assert attributes["error.type"] == "stream_failed"
    assert '"content": "Pa"' in body

    assert attributes["agentproxy.stream.frames"] > 0
    assert attributes["agentproxy.stream.bytes"] == len(body.encode("utf-8"))
    # Generation started before the backend went away, so the timing survives.
    assert attributes["agentproxy.stream.first_token_ms"] >= 0


def test_frame_count_scales_with_the_stream_not_with_the_span_count(streaming, monkeypatch, traced):
    """The 965-chunk turn from #140 is now four attributes, not 965 spans."""
    _one_backend_stream(
        monkeypatch,
        [{"message": {"content": str(index)}} for index in range(200)]
        + [{"message": {"content": ""}, "done": True, "done_reason": "stop"}],
    )

    _stream(streaming)
    finished = traced.get_finished_spans()
    attributes = _request_chat_span(traced).attributes

    assert attributes["agentproxy.stream.frames"] >= 200
    # 200 content deltas, and the trace stays small enough to read.
    assert len(finished) < 50, [span.name for span in finished]
