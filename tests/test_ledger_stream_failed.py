"""Request-ledger retry and fallback counts on the paths COI-2222 left at zero.

COI-2222 filled them for a served, non-streamed request. These cover a streamed
request served by a fallback and a request that spent retries and then failed.
"""

from __future__ import annotations

import pytest

from app import main, models, resilience, upstream
from app.config import get_settings
from app.models import Backend, LogicalModel
from app.upstream import UpstreamError, UpstreamStatusError

MODEL = "qwen3:4b"


def _model(*names: str) -> LogicalModel:
    return LogicalModel(
        name=MODEL,
        num_ctx=4096,
        backends=[Backend(name=name, url=f"http://{name}", ollama_tag="t") for name in names],
    )


@pytest.fixture
def ledger(monkeypatch, app_client):
    """The terminal execution events the request path emits, with the chain installed."""
    events: list = []

    async def fake_catalog(_base_url):
        return {MODEL: 262144}, True

    async def resolve_model(name):
        return chain["model"] if name == MODEL else None

    chain: dict[str, LogicalModel] = {"model": _model("primary", "secondary")}
    monkeypatch.setattr(models, "_catalog", fake_catalog)
    models.reset_catalog()
    monkeypatch.setattr(main, "resolve", resolve_model)
    monkeypatch.setattr(main, "_emit_trajectory_event", lambda event: events.append(event) or True)
    monkeypatch.setattr(resilience, "breakers", resilience.CircuitBreakerRegistry())
    monkeypatch.setattr(get_settings(), "retry_base_delay", 0.0)

    def terminal():
        done = [e for e in events if e.event_type.startswith("execution.")]
        assert len(done) == 1
        return done[0]

    app_client.chain = chain
    app_client.terminal = terminal
    return app_client


def _post(client, *, stream: bool):
    return client.post(
        "/v1/chat/completions",
        json={"model": MODEL, "stream": stream, "messages": [{"role": "user", "content": "hi"}]},
    )


def test_streamed_request_served_by_a_fallback_writes_the_fallback(ledger, monkeypatch):
    async def chat_stream(backend, num_ctx, messages, **_kwargs):
        if backend.name == "primary":
            raise UpstreamError("primary down")
            yield  # pragma: no cover - generator marker
        yield {"message": {"content": "ok"}, "done": True, "done_reason": "stop"}

    monkeypatch.setattr(upstream, "chat_stream", chat_stream)

    _post(ledger, stream=True)

    done = ledger.terminal()
    row = done.payload.model_execution
    assert done.payload.outcome == "succeeded"
    assert row.fallback_count == 1
    assert row.fallback_from == ["primary"]
    # A stream is never retried, so the count stays an honest zero.
    assert row.retry_count == 0


def test_streamed_fallback_survives_a_trailing_usage_chunk(ledger, monkeypatch):
    """An OpenAI-dialect stream folds usage onto the result after the done chunk."""

    async def chat_stream(backend, num_ctx, messages, **_kwargs):
        if backend.name == "primary":
            raise UpstreamError("primary down")
            yield  # pragma: no cover - generator marker
        yield {"message": {"content": "ok"}, "done": True, "done_reason": "stop"}
        yield {"usage": {"prompt_tokens": 5, "completion_tokens": 2}}

    monkeypatch.setattr(upstream, "chat_stream", chat_stream)

    _post(ledger, stream=True)

    row = ledger.terminal().payload.model_execution
    assert row.request_tokens == 5
    assert row.fallback_from == ["primary"]


def test_streamed_request_with_no_fallback_still_writes_zero(ledger, monkeypatch):
    async def chat_stream(backend, num_ctx, messages, **_kwargs):
        yield {"message": {"content": "ok"}, "done": True, "done_reason": "stop"}

    monkeypatch.setattr(upstream, "chat_stream", chat_stream)

    _post(ledger, stream=True)

    row = ledger.terminal().payload.model_execution
    assert row.fallback_count == 0
    assert row.fallback_from == []


def test_streamed_request_that_failed_everywhere_writes_what_it_passed_over(ledger, monkeypatch):
    async def chat_stream(backend, num_ctx, messages, **_kwargs):
        raise UpstreamError(f"{backend.name} down")
        yield  # pragma: no cover - generator marker

    monkeypatch.setattr(upstream, "chat_stream", chat_stream)

    _post(ledger, stream=True)

    done = ledger.terminal()
    assert done.payload.outcome == "stream_failed"
    assert done.payload.model_execution.fallback_from == ["primary", "secondary"]


def test_request_that_retried_then_failed_writes_its_retry_count(ledger, monkeypatch):
    ledger.chain["model"] = _model("only")
    calls = {"n": 0}

    async def chat(backend, num_ctx, messages, **_kwargs):
        calls["n"] += 1
        raise UpstreamError("down")

    monkeypatch.setattr(upstream, "chat", chat)

    response = _post(ledger, stream=False)

    retries = get_settings().max_retries
    assert response.status_code == 502
    assert calls["n"] == retries + 1
    done = ledger.terminal()
    assert done.payload.outcome == "upstream_failed"
    assert done.payload.model_execution.retry_count == retries


def test_request_that_retried_and_fell_back_then_failed_writes_both(ledger, monkeypatch):
    async def chat(backend, num_ctx, messages, **_kwargs):
        raise UpstreamError("down")

    monkeypatch.setattr(upstream, "chat", chat)

    _post(ledger, stream=False)

    row = ledger.terminal().payload.model_execution
    # Every backend burns its own retries, and the whole spend rides on the failure.
    assert row.retry_count == 2 * get_settings().max_retries
    assert row.fallback_from == ["primary", "secondary"]
    assert row.fallback_count == 2


def test_request_the_second_backend_rejected_keeps_what_the_first_cost(ledger, monkeypatch):
    async def chat(backend, num_ctx, messages, **_kwargs):
        if backend.name == "primary":
            raise UpstreamError("down")
        raise UpstreamStatusError("bad body", status_code=400, body="nope")

    monkeypatch.setattr(upstream, "chat", chat)

    response = _post(ledger, stream=False)

    assert response.status_code == 400
    done = ledger.terminal()
    assert done.payload.outcome == "upstream_rejected"
    assert done.payload.model_execution.retry_count == get_settings().max_retries
    assert done.payload.model_execution.fallback_from == ["primary"]


async def test_deadline_exceeded_after_a_retry_carries_the_retry(monkeypatch):
    model = _model("only")
    clock = {"now": 0.0}
    monkeypatch.setattr(resilience, "_now", lambda: clock["now"])
    monkeypatch.setattr(resilience, "breakers", resilience.CircuitBreakerRegistry())
    monkeypatch.setattr(get_settings(), "retry_base_delay", 0.0)

    async def chat(backend, num_ctx, messages, **_kwargs):
        clock["now"] = 100.0  # the caller's budget is gone once this attempt fails
        raise UpstreamError("down")

    monkeypatch.setattr(upstream, "chat", chat)

    with pytest.raises(resilience.RequestDeadlineExceeded) as raised:
        await resilience.dispatch(model, [{"role": "user", "content": "hi"}], deadline=10.0)

    assert raised.value.retry_count == 1
    assert raised.value.fallback_from == []
