"""Fail-loud verification of the delivered context (issue #33).

When a backend serves ``OLLAMA_NUM_PARALLEL > 1`` it loads the injected ``num_ctx``
as the model's *total* window and divides it across the slots, so a single
request's usable window is silently halved (or worse). These cover the proxy's
detection: the marked-and-counted default, the opt-in hard fail, and the cases
that must *not* trip (a full-window delivery, an openai-dialect backend).
"""

import pytest

from app import resilience, upstream
from app.config import Settings
from app.models import Backend, LogicalModel
from app.obs import RequestTraceContext, llm_context_truncated_total
from app.resilience import ContextTruncated
from app.upstream import UpstreamResult


def _truncated_result() -> UpstreamResult:
    # num_ctx=49152 asked, NUM_PARALLEL=2 -> the backend caps the prompt at 24578.
    return UpstreamResult(model="m", content="ok", prompt_eval_count=24578, eval_count=8)


def _counter(model: str, backend: str) -> float:
    return llm_context_truncated_total.labels(logical_model=model, backend=backend)._value.get()


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    monkeypatch.setattr(resilience, "breakers", resilience.CircuitBreakerRegistry())
    # The prompt the proxy "sent" dwarfs the delivered 24578 - a real clip, not a
    # small prompt that fit. Fixed so the test needn't encode 30k real tokens.
    monkeypatch.setattr(resilience, "count_message_tokens", lambda _messages: 48000)


def _use_settings(monkeypatch, **overrides) -> None:
    settings = Settings(**overrides)
    monkeypatch.setattr(resilience, "get_settings", lambda: settings)


async def test_truncation_is_marked_and_counted(monkeypatch):
    _use_settings(monkeypatch)  # defaults: mark, do not hard-fail
    backend = Backend(name="b-parallel", url="http://x", ollama_tag="t", num_parallel=2)
    model = LogicalModel("qwen3:4b", 49152, [backend])
    events = []
    attributes = {}

    class Span:
        def is_recording(self):
            return True

        def add_event(self, name, attrs):
            events.append((name, attrs))

        def set_attribute(self, key, value):
            attributes[key] = value

    monkeypatch.setattr("app.obs._current_span", lambda: Span())
    trace_ctx = RequestTraceContext(
        logical_model=model.name, request_model=model.name, request_kind="chat"
    )

    async def chat(
        be, num_ctx, messages, *, tools=None, tool_policy=None, options=None, span_attrs=None
    ):
        return _truncated_result()

    monkeypatch.setattr(upstream, "chat", chat)

    before = _counter(model.name, backend.name)
    result = await resilience.dispatch(
        model, [{"role": "user", "content": "hi"}], trace_ctx=trace_ctx
    )

    assert result.context_truncated is True  # surfaced, not silent
    assert result.content == "ok"  # content still returned in the default mode
    assert _counter(model.name, backend.name) - before == 1
    assert events == [
        (
            "dispatch.context_truncated",
            {
                "logical_model": model.name,
                "request_model": model.name,
                "request_kind": "chat",
                "backend": backend.name,
                "outcome": "context-truncated",
                "prompt_tokens_sent": 48000,
                "prompt_eval_count": 24578,
                "target_num_ctx": 49152,
                "num_parallel": 2,
            },
        )
    ]
    assert attributes["backend"] == backend.name
    assert attributes["target_num_ctx"] == 49152


async def test_hard_fail_raises_when_configured(monkeypatch):
    _use_settings(monkeypatch, fail_on_context_truncation=True)
    backend = Backend(name="b-strict", url="http://x", ollama_tag="t", num_parallel=2)
    model = LogicalModel("qwen3:4b", 49152, [backend])

    async def chat(
        be, num_ctx, messages, *, tools=None, tool_policy=None, options=None, span_attrs=None
    ):
        return _truncated_result()

    monkeypatch.setattr(upstream, "chat", chat)

    with pytest.raises(ContextTruncated):
        await resilience.dispatch(model, [{"role": "user", "content": "hi"}])


async def test_full_window_delivery_not_flagged(monkeypatch):
    _use_settings(monkeypatch)
    backend = Backend(name="b-single", url="http://x", ollama_tag="t", num_parallel=1)
    model = LogicalModel("qwen3:4b", 49152, [backend])

    async def chat(
        be, num_ctx, messages, *, tools=None, tool_policy=None, options=None, span_attrs=None
    ):
        # The full window was delivered (NUM_PARALLEL=1): prompt_eval_count ~= num_ctx.
        return UpstreamResult(model="m", content="ok", prompt_eval_count=49151, eval_count=8)

    monkeypatch.setattr(upstream, "chat", chat)

    result = await resilience.dispatch(model, [{"role": "user", "content": "hi"}])
    assert result.context_truncated is False


async def test_openai_backend_is_never_flagged(monkeypatch):
    # An openai-dialect backend carries its window at launch and is not subject to
    # the NUM_PARALLEL division, so a low prompt_eval_count must not be misread.
    _use_settings(monkeypatch, fail_on_context_truncation=True)
    backend = Backend(
        name="b-openai", url="http://x", ollama_tag="t", dialect="openai", injects_num_ctx=False
    )
    model = LogicalModel("gpt-oss:120b", 49152, [backend])

    async def chat(
        be, num_ctx, messages, *, tools=None, tool_policy=None, options=None, span_attrs=None
    ):
        return _truncated_result()

    monkeypatch.setattr(upstream, "chat", chat)

    result = await resilience.dispatch(model, [{"role": "user", "content": "hi"}])
    assert result.context_truncated is False
