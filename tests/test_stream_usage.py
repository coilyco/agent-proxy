"""A streamed chat completion carries its token usage (agent-proxy#8373).

OpenCode compacts on the input tokens a stream reports. With none, its overflow
check never fires, so every long session ran uncompacted.
"""

import json

import pytest

from app import models, resilience

CATALOG: dict[str, int | None] = {"qwen3:4b": 262144}


def _events(text: str) -> list[dict]:
    frames = [line[len("data: ") :] for line in text.splitlines() if line.startswith("data: ")]
    assert frames[-1] == "[DONE]"
    return [json.loads(frame) for frame in frames[:-1]]


@pytest.fixture
def streamed(monkeypatch, app_client):
    async def fake_dispatch_stream(model, messages, **_):
        yield {"message": {"content": "Par"}, "done": False}
        yield {
            "message": {"content": "is"},
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

    def post(**extra):
        body = {"model": "qwen3:4b", "stream": True, "messages": [{"role": "user", "content": "?"}]}
        response = app_client.post("/v1/chat/completions", json={**body, **extra})
        assert response.status_code == 200
        return _events(response.text)

    return post


EXPECTED = {
    "prompt_tokens": 15243,
    "completion_tokens": 3,
    "total_tokens": 15246,
    "prompt_tokens_details": {"cached_tokens": 14976},
}


def test_a_client_that_asks_gets_the_openai_usage_chunk_before_done(streamed):
    events = streamed(stream_options={"include_usage": True})
    tail = events[-1]
    assert tail["choices"] == [] and tail["usage"] == EXPECTED
    assert events[-2]["choices"][0]["finish_reason"] == "stop"
    assert "usage" not in events[-2]


def test_a_client_that_does_not_ask_finds_usage_on_the_finish_chunk(streamed):
    events = streamed()
    final = events[-1]
    assert final["choices"][0]["finish_reason"] == "stop"
    assert final["usage"] == EXPECTED
    # No choice-less chunk surprises a client that never asked for one.
    assert all(event["choices"] for event in events)


def _sse(payload: dict) -> str:
    return "data: " + json.dumps(payload)


def _lines(trailing_choices: list) -> list[str]:
    """finish_reason first, then usage in a chunk of its own (agent-proxy#8376)."""
    return [
        _sse({"choices": [{"index": 0, "delta": {"content": "ok"}}]}),
        _sse({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}),
        _sse(
            {
                "model": "evaluation/deepseek-v4-flash",
                "choices": trailing_choices,
                "usage": {
                    "prompt_tokens": 85,
                    "completion_tokens": 20,
                    "prompt_tokens_details": {"cached_tokens": 64},
                },
            }
        ),
    ]


# LiteLLM's captured shape keeps an empty-delta choice; DeepSeek's sends none.
@pytest.fixture(params=[[{"index": 0, "delta": {}}], []], ids=["litellm", "deepseek"])
def through_the_translator(request, monkeypatch, app_client):
    """Run the real OpenAI-dialect stream translator over LiteLLM's lines."""
    from app import upstream
    from app.models import Backend
    from tests.test_prompt_cache import _StreamingClient

    backend = Backend(name="gateway", url="http://gateway", ollama_tag="alias", dialect="openai")
    lines = _lines(request.param)
    monkeypatch.setattr(upstream, "get_client", lambda: _StreamingClient(lines))

    async def via_translator(model, messages, **_):
        async for chunk in upstream.chat_stream(backend, 1024, messages):
            yield chunk

    async def fake_catalog(_base_url):
        return dict(CATALOG), True

    monkeypatch.setattr(resilience, "dispatch_stream", via_translator)
    monkeypatch.setattr(models, "_catalog", fake_catalog)
    models.reset_catalog()

    def post(**extra):
        body = {"model": "qwen3:4b", "stream": True, "messages": [{"role": "user", "content": "?"}]}
        response = app_client.post("/v1/chat/completions", json={**body, **extra})
        assert response.status_code == 200
        return _events(response.text)

    return post


def test_a_trailing_usage_chunk_reaches_the_client_with_its_counts(through_the_translator):
    events = through_the_translator(stream_options={"include_usage": True})
    assert events[-1]["usage"] == {
        "prompt_tokens": 85,
        "completion_tokens": 20,
        "total_tokens": 105,
        "prompt_tokens_details": {"cached_tokens": 64},
    }


def test_a_trailing_usage_chunk_reaches_the_finish_chunk_unasked(through_the_translator):
    events = through_the_translator()
    assert events[-1]["choices"][0]["finish_reason"] == "stop"
    assert events[-1]["usage"]["prompt_tokens"] == 85
