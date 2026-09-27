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
