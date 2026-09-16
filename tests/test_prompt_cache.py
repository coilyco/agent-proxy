"""Provider prompt-cache accounting through the proxy surface (issue #101).

Issue #101 reported a 53 KB system prefix resent byte-identical on all 46 turns
of a window and concluded it was uncached. The proxy could neither confirm nor
refute that: it read ``prompt_tokens`` and ``completion_tokens`` out of the
upstream usage block and dropped every cache field beside them. These tests hold
the accounting the conclusion needed.
"""

import json

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app import models, obs, resilience, upstream
from app.main import _request_shape_attrs, _usage_block
from app.models import Backend
from app.upstream import UpstreamResult, parse_cache_usage

CATALOG: dict[str, int | None] = {"qwen3:4b": 262144}


class _StreamingClient:
    """Replays a canned SSE body so the stream normalizer runs without a backend."""

    def __init__(self, lines):
        self.lines = lines

    def stream(self, method, url, json=None, timeout=None, headers=None):
        lines = self.lines

        class _Response:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            def raise_for_status(self):
                return None

            async def aiter_lines(self):
                for line in lines:
                    yield line

        return _Response()


def _hit_tokens(model: str) -> float:
    return obs.llm_prompt_cache_hit_tokens_total.labels(logical_model=model)._value.get()


def _miss_tokens(model: str) -> float:
    return obs.llm_prompt_cache_miss_tokens_total.labels(logical_model=model)._value.get()


def _write_tokens(model: str) -> float:
    return obs.llm_prompt_cache_write_tokens_total.labels(logical_model=model)._value.get()


@pytest.fixture
def cache_client(monkeypatch, app_client):
    """A backend that reports DeepSeek-shaped cache accounting on every turn."""

    async def fake_chat(
        backend, num_ctx, messages, *, tools=None, tool_policy=None, options=None, span_attrs=None
    ):
        return UpstreamResult(
            model=backend.ollama_tag,
            content="Paris",
            prompt_eval_count=15243,
            eval_count=3,
            cache_usage_reported=True,
            cache_read_tokens=14976,
        )

    async def fake_catalog(_base_url):
        return dict(CATALOG), True

    monkeypatch.setattr(upstream, "chat", fake_chat)
    monkeypatch.setattr(models, "_catalog", fake_catalog)
    models.reset_catalog()
    yield app_client


@pytest.fixture
def silent_client(monkeypatch, app_client):
    """An Ollama-shaped backend that never reports cache accounting."""

    async def fake_chat(
        backend, num_ctx, messages, *, tools=None, tool_policy=None, options=None, span_attrs=None
    ):
        return UpstreamResult(
            model=backend.ollama_tag,
            content="Paris",
            prompt_eval_count=42,
            eval_count=3,
        )

    async def fake_catalog(_base_url):
        return dict(CATALOG), True

    monkeypatch.setattr(upstream, "chat", fake_chat)
    monkeypatch.setattr(models, "_catalog", fake_catalog)
    models.reset_catalog()
    yield app_client


# Parsing the three provider shapes


def test_parse_cache_usage_reads_deepseek_native_fields():
    reported, read, write = parse_cache_usage(
        {"prompt_tokens": 15243, "prompt_cache_hit_tokens": 14976, "prompt_cache_miss_tokens": 267}
    )
    assert (reported, read, write) == (True, 14976, 0)


def test_parse_cache_usage_reads_openai_prompt_tokens_details():
    reported, read, write = parse_cache_usage(
        {"prompt_tokens": 15243, "prompt_tokens_details": {"cached_tokens": 14976}}
    )
    assert (reported, read, write) == (True, 14976, 0)


def test_parse_cache_usage_reads_anthropic_style_read_and_write():
    reported, read, write = parse_cache_usage(
        {
            "prompt_tokens": 15243,
            "cache_read_input_tokens": 14976,
            "cache_creation_input_tokens": 267,
        }
    )
    assert (reported, read, write) == (True, 14976, 267)


def test_parse_cache_usage_reports_a_populating_turn_that_read_nothing():
    # The turn that filled the cache read zero back. It is still a measured
    # route, so it must not be mistaken for a provider that stays silent.
    reported, read, write = parse_cache_usage(
        {"prompt_tokens": 15243, "prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 15243}
    )
    assert (reported, read, write) == (True, 0, 0)


@pytest.mark.parametrize(
    "usage",
    [None, {}, {"prompt_tokens": 42, "completion_tokens": 3}, {"prompt_tokens_details": {}}],
    ids=["absent", "empty", "ollama-shaped", "empty-details"],
)
def test_parse_cache_usage_stays_silent_without_provider_accounting(usage):
    # An Ollama backend reuses its KV cache without reporting it. Publishing a
    # 100% miss here would invent a regression the backend never had.
    assert parse_cache_usage(usage) == (False, 0, 0)


def test_openai_chat_response_carries_cache_accounting():
    result = upstream._parse_openai_chat_response(
        {
            "model": "deepseek-v4-flash",
            "choices": [{"message": {"content": "Paris"}, "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": 15243,
                "completion_tokens": 88,
                "prompt_cache_hit_tokens": 14976,
                "prompt_cache_miss_tokens": 267,
            },
        }
    )
    assert result.cache_usage_reported is True
    assert result.cache_read_tokens == 14976
    assert result.cache_miss_tokens == 267


def test_cache_miss_derives_from_the_billed_prompt():
    result = UpstreamResult(
        model="deepseek-v4-flash",
        content="",
        prompt_eval_count=15243,
        cache_usage_reported=True,
        cache_read_tokens=14976,
    )
    assert result.cache_miss_tokens == 267


# The usage block returned to callers


def test_usage_block_publishes_cached_tokens_under_the_openai_field():
    usage = _usage_block(
        UpstreamResult(
            model="deepseek-v4-flash",
            content="Paris",
            prompt_eval_count=15243,
            eval_count=88,
            cache_usage_reported=True,
            cache_read_tokens=14976,
            cache_write_tokens=267,
        )
    )
    assert usage == {
        "prompt_tokens": 15243,
        "completion_tokens": 88,
        "total_tokens": 15331,
        "prompt_tokens_details": {"cached_tokens": 14976},
        "cache_creation_input_tokens": 267,
    }


def test_usage_block_omits_cache_fields_when_the_provider_is_silent():
    usage = _usage_block(
        UpstreamResult(model="qwen3:4b", content="Paris", prompt_eval_count=42, eval_count=3)
    )
    assert usage == {"prompt_tokens": 42, "completion_tokens": 3, "total_tokens": 45}
    assert "prompt_tokens_details" not in usage


def test_chat_completion_surfaces_cached_tokens_to_the_caller(cache_client):
    response = cache_client.post(
        "/v1/chat/completions",
        json={"model": "qwen3:4b", "messages": [{"role": "user", "content": "ping"}]},
    )
    assert response.status_code == 200
    assert response.json()["usage"]["prompt_tokens_details"] == {"cached_tokens": 14976}


# Metrics


def test_served_response_records_hit_and_miss_tokens_once(cache_client):
    before_hits = _hit_tokens("qwen3:4b")
    before_misses = _miss_tokens("qwen3:4b")

    response = cache_client.post(
        "/v1/chat/completions",
        json={"model": "qwen3:4b", "messages": [{"role": "user", "content": "ping"}]},
    )
    assert response.status_code == 200

    assert _hit_tokens("qwen3:4b") == before_hits + 14976
    assert _miss_tokens("qwen3:4b") == before_misses + 267


def test_unreported_cache_usage_publishes_no_metric(silent_client):
    before_hits = _hit_tokens("qwen3:4b")
    before_misses = _miss_tokens("qwen3:4b")
    before_writes = _write_tokens("qwen3:4b")

    response = silent_client.post(
        "/v1/chat/completions",
        json={"model": "qwen3:4b", "messages": [{"role": "user", "content": "ping"}]},
    )
    assert response.status_code == 200

    assert _hit_tokens("qwen3:4b") == before_hits
    assert _miss_tokens("qwen3:4b") == before_misses
    assert _write_tokens("qwen3:4b") == before_writes


def test_streaming_turn_records_cache_tokens_from_the_terminal_chunk(monkeypatch, app_client):
    # The streaming surface rebuilds its terminal result from the normalized
    # chunk, so the chunk is the only carrier that reaches the metric.
    async def fake_dispatch_stream(
        model,
        messages,
        *,
        tools=None,
        tool_policy=None,
        options=None,
        trace_ctx=None,
        deadline=None,
    ):
        yield {
            "message": {"content": "Paris"},
            "done": True,
            "done_reason": "stop",
            "prompt_eval_count": 15243,
            "eval_count": 3,
            "cache_usage_reported": True,
            "cache_read_tokens": 14976,
        }

    async def fake_catalog(_base_url):
        return dict(CATALOG), True

    monkeypatch.setattr(resilience, "dispatch_stream", fake_dispatch_stream)
    monkeypatch.setattr(models, "_catalog", fake_catalog)
    models.reset_catalog()

    before_hits = _hit_tokens("qwen3:4b")
    before_misses = _miss_tokens("qwen3:4b")

    response = app_client.post(
        "/v1/chat/completions",
        json={
            "model": "qwen3:4b",
            "stream": True,
            "messages": [{"role": "user", "content": "ping"}],
        },
    )
    assert response.status_code == 200

    assert _hit_tokens("qwen3:4b") == before_hits + 14976
    assert _miss_tokens("qwen3:4b") == before_misses + 267


@pytest.mark.asyncio
async def test_openai_stream_chunk_carries_cache_accounting_forward(monkeypatch):
    backend = Backend(name="gateway", url="http://gateway", ollama_tag="alias", dialect="openai")
    final = json.dumps(
        {
            "model": "deepseek-v4-flash",
            "choices": [{"delta": {}, "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": 15243,
                "completion_tokens": 3,
                "prompt_cache_hit_tokens": 14976,
                "prompt_cache_miss_tokens": 267,
            },
        }
    )
    lines = ['data: {"choices": [{"delta": {"content": "Paris"}}]}', f"data: {final}"]
    monkeypatch.setattr(upstream, "get_client", lambda: _StreamingClient(lines))

    chunks = [chunk async for chunk in upstream.chat_stream(backend, 1024, [], span_attrs=None)]

    terminal = upstream.parse_stream_result(chunks[-1], backend.ollama_tag)
    assert terminal.cache_usage_reported is True
    assert terminal.cache_read_tokens == 14976
    assert terminal.cache_miss_tokens == 267


def test_metrics_endpoint_exposes_the_prompt_cache_names(cache_client):
    text = cache_client.get("/metrics").text
    assert "llm_prompt_cache_hit_tokens_total" in text
    assert "llm_prompt_cache_miss_tokens_total" in text
    assert "llm_prompt_cache_write_tokens_total" in text


# Span attributes


def _isolated_tracer(exporter: InMemorySpanExporter):
    """A private provider, so these spans never reach the process-wide pipeline."""

    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider.get_tracer("test-prompt-cache")


def test_result_span_attributes_include_cache_reads():
    exporter = InMemorySpanExporter()
    tracer = _isolated_tracer(exporter)

    result = UpstreamResult(
        model="deepseek-v4-flash",
        content="Paris",
        prompt_eval_count=15243,
        eval_count=88,
        cache_usage_reported=True,
        cache_read_tokens=14976,
        cache_write_tokens=267,
    )
    with tracer.start_as_current_span("upstream.chat") as span:
        upstream.set_result_span_attributes(span, result)

    attributes = exporter.get_finished_spans()[-1].attributes
    assert attributes["gen_ai.usage.cache_read_input_tokens"] == 14976
    assert attributes["gen_ai.usage.cache_creation_input_tokens"] == 267


def test_result_span_omits_cache_attributes_when_unreported():
    exporter = InMemorySpanExporter()
    tracer = _isolated_tracer(exporter)

    result = UpstreamResult(model="qwen3:4b", content="Paris", prompt_eval_count=42, eval_count=3)
    with tracer.start_as_current_span("upstream.chat") as span:
        upstream.set_result_span_attributes(span, result)

    attributes = exporter.get_finished_spans()[-1].attributes
    assert "gen_ai.usage.cache_read_input_tokens" not in attributes


# Request shape, the second half of issue #101


def test_request_shape_sizes_the_system_prefix_and_tool_roster():
    messages = [
        {"role": "system", "content": "x" * 200},
        {"role": "user", "content": "ping"},
    ]
    tools = [{"type": "function", "function": {"name": "a"}} for _ in range(17)]

    attrs = _request_shape_attrs(messages, tools)

    assert attrs["gen_ai.request.tool_count"] == 17
    assert attrs["gen_ai.request.tool_bytes"] > 0
    # The system block is measured alone, so its share of the request is legible
    # without subtracting the live turn from a whole-body byte count.
    assert attrs["gen_ai.request.system_bytes"] > 200
    assert "ping" not in str(attrs)


def test_request_shape_reports_zero_for_a_toolless_turn():
    attrs = _request_shape_attrs([{"role": "user", "content": "ping"}], None)
    assert attrs == {
        "gen_ai.request.system_bytes": 0,
        "gen_ai.request.tool_count": 0,
        "gen_ai.request.tool_bytes": 0,
    }


# The served model, which agentproxy.backend cannot answer for a proxied route.
# coilyco-flight-deck/agent-proxy#136.


def test_result_span_records_the_model_that_answered():
    exporter = InMemorySpanExporter()
    tracer = _isolated_tracer(exporter)

    result = UpstreamResult(
        model="deepseek-ai/DeepSeek-V4-Flash-0731",
        content="Paris",
        prompt_eval_count=42,
        eval_count=3,
        served_by="litellm",
        served_regime="hosted",
    )
    with tracer.start_as_current_span("upstream.chat") as span:
        upstream.set_result_span_attributes(span, result)

    attributes = exporter.get_finished_spans()[-1].attributes
    # The proxy's own chain entry and the model that answered are different
    # facts, and a LiteLLM fallback only moves the second one.
    assert attributes["agentproxy.backend"] == "litellm"
    assert attributes["gen_ai.response.model"] == "deepseek-ai/DeepSeek-V4-Flash-0731"


def test_result_span_omits_the_model_when_upstream_named_none():
    exporter = InMemorySpanExporter()
    tracer = _isolated_tracer(exporter)

    result = UpstreamResult(model="", content="Paris", prompt_eval_count=1, eval_count=1)
    with tracer.start_as_current_span("upstream.chat") as span:
        upstream.set_result_span_attributes(span, result)

    assert "gen_ai.response.model" not in exporter.get_finished_spans()[-1].attributes


# stream_options.include_usage


def _body(backend, *, stream):
    return upstream._chat_body(
        backend,
        1024,
        [],
        stream=stream,
        tools=None,
        options=None,
        span_attrs=None,
    )


def test_openai_streaming_asks_the_provider_for_usage():
    """Without this the terminal chunk carries no usage, so no cache field exists
    to parse and the route reports silence that reads as a 100% miss."""

    backend = Backend(name="gateway", url="http://gateway", ollama_tag="alias", dialect="openai")

    assert _body(backend, stream=True)["stream_options"] == {"include_usage": True}


def test_openai_non_streaming_does_not_send_stream_options():
    backend = Backend(name="gateway", url="http://gateway", ollama_tag="alias", dialect="openai")

    assert "stream_options" not in _body(backend, stream=False)


def test_ollama_streaming_does_not_send_stream_options():
    """Ollama accounts for its own stream in the done payload and rejects unknown
    top-level keys, so the OpenAI-only option must not reach it."""

    backend = Backend(name="local", url="http://local", ollama_tag="qwen3:4b", dialect="ollama")

    assert "stream_options" not in _body(backend, stream=True)


# The usage-only trailing chunk (issue #138)


def _sse(payload: dict) -> str:
    return "data: " + json.dumps(payload)


@pytest.mark.asyncio
async def test_streaming_reads_usage_from_a_trailing_chunk(monkeypatch):
    """LiteLLM's real shape: finish_reason first, usage in a later chunk of its own.

    Captured from ser8 on 2026-08-19. Keyed on finish_reason alone the accounting
    is dropped, which is what left the route unmeasured.
    """

    backend = Backend(name="gateway", url="http://gateway", ollama_tag="alias", dialect="openai")
    lines = [
        _sse({"choices": [{"index": 0, "delta": {"content": "ok"}}]}),
        _sse({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}),
        _sse(
            {
                "model": "sirens-echo/deepseek",
                "choices": [{"index": 0, "delta": {}}],
                "usage": {
                    "prompt_tokens": 85,
                    "completion_tokens": 20,
                    "prompt_tokens_details": {"cached_tokens": 64},
                },
            }
        ),
    ]
    monkeypatch.setattr(upstream, "get_client", lambda: _StreamingClient(lines))
    exporter = InMemorySpanExporter()
    monkeypatch.setattr(upstream, "get_tracer", lambda: _isolated_tracer(exporter))

    [chunk async for chunk in upstream.chat_stream(backend, 1024, [], span_attrs=None)]

    span = next(s for s in exporter.get_finished_spans() if s.name == "upstream.chat_stream")
    assert span.attributes["gen_ai.usage.input_tokens"] == 85
    assert span.attributes["gen_ai.usage.cache_read_input_tokens"] == 64


@pytest.mark.asyncio
async def test_streaming_without_a_usage_chunk_still_reports_nothing(monkeypatch):
    """A provider that accounts for nothing stays absent rather than reading zero."""

    backend = Backend(name="gateway", url="http://gateway", ollama_tag="alias", dialect="openai")
    lines = [
        _sse({"choices": [{"index": 0, "delta": {"content": "ok"}}]}),
        _sse({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}),
    ]
    monkeypatch.setattr(upstream, "get_client", lambda: _StreamingClient(lines))
    exporter = InMemorySpanExporter()
    monkeypatch.setattr(upstream, "get_tracer", lambda: _isolated_tracer(exporter))

    [chunk async for chunk in upstream.chat_stream(backend, 1024, [], span_attrs=None)]

    span = next(s for s in exporter.get_finished_spans() if s.name == "upstream.chat_stream")
    assert "gen_ai.usage.cache_read_input_tokens" not in span.attributes
