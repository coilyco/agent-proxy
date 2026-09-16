"""Admission rate limiting and 429 shedding (issue #110)."""

import pytest

from app import models
from app.config import get_settings
from app.ratelimit import RateLimiter, TokenBucket, get_rate_limiter
from app.upstream import UpstreamResult

CATALOG: dict[str, int | None] = {"qwen3:4b": 262144}


@pytest.fixture
def limited(monkeypatch, app_client):
    """A client whose limiter is live at 1/s with no burst, the shipped default."""

    async def fake_chat(
        backend, num_ctx, messages, *, tools=None, tool_policy=None, options=None, span_attrs=None
    ):
        return UpstreamResult(model=backend.ollama_tag, content="ok")

    async def fake_catalog(_base_url):
        return dict(CATALOG), True

    from app import upstream

    monkeypatch.setattr(upstream, "chat", fake_chat)
    monkeypatch.setattr(models, "_catalog", fake_catalog)
    models.reset_catalog()
    monkeypatch.setattr(get_settings(), "rate_limit_per_second", 1.0)
    monkeypatch.setattr(get_settings(), "rate_limit_burst", 1)
    get_rate_limiter().reset()
    yield app_client


def _chat(client, model: str = "qwen3:4b"):
    return client.post(
        "/v1/chat/completions",
        json={"model": model, "messages": [{"role": "user", "content": "hi"}]},
    )


def test_first_request_passes_and_the_next_sheds(limited):
    assert _chat(limited).status_code == 200
    response = _chat(limited)
    assert response.status_code == 429
    assert response.json()["error"]["type"] == "rate_limit_error"


def test_shed_response_tells_the_caller_when_to_come_back(limited):
    _chat(limited)
    response = _chat(limited)
    assert response.status_code == 429
    assert int(response.headers["Retry-After"]) >= 1


def test_an_unknown_model_still_404s_rather_than_spending_a_token(limited):
    assert _chat(limited, "no-such:model").status_code == 404
    # The token was never spent, so a real request still passes.
    assert _chat(limited).status_code == 200


def test_the_completions_surface_sheds_too(limited):
    assert (
        limited.post("/v1/completions", json={"model": "qwen3:4b", "prompt": "hi"}).status_code
        == 200
    )
    assert (
        limited.post("/v1/completions", json={"model": "qwen3:4b", "prompt": "hi"}).status_code
        == 429
    )


def test_health_routes_are_never_shed(limited):
    _chat(limited)
    _chat(limited)
    assert limited.get("/healthz").status_code == 200
    assert limited.get("/metrics").status_code == 200


def test_rate_zero_disables_shedding(monkeypatch, limited):
    monkeypatch.setattr(get_settings(), "rate_limit_per_second", 0.0)
    get_rate_limiter().reset()
    for _ in range(5):
        assert _chat(limited).status_code == 200


# --- the bucket itself, on a controlled clock ------------------------------- #


def test_bucket_refills_at_the_configured_rate():
    bucket = TokenBucket(rate=1.0, capacity=1.0)
    assert bucket.take(now=100.0)
    assert not bucket.take(now=100.5)
    # One second later, one more token.
    assert bucket.take(now=101.0)


def test_bucket_never_banks_more_than_its_capacity():
    bucket = TokenBucket(rate=1.0, capacity=2.0)
    assert bucket.take(now=100.0)
    # Idle for a minute, then a burst: capacity caps what was banked.
    assert bucket.take(now=160.0)
    assert bucket.take(now=160.0)
    assert not bucket.take(now=160.0)


def test_bucket_reports_when_a_token_returns():
    bucket = TokenBucket(rate=2.0, capacity=1.0)
    bucket.take(now=100.0)
    assert bucket.retry_after(now=100.0) == pytest.approx(0.5)
    assert bucket.retry_after(now=100.5) == 0.0


def test_a_zero_rate_bucket_admits_everything():
    bucket = TokenBucket(rate=0.0, capacity=1.0)
    assert all(bucket.take(now=100.0) for _ in range(10))


def test_routes_get_independent_buckets(monkeypatch):
    monkeypatch.setattr(get_settings(), "rate_limit_per_second", 1.0)
    monkeypatch.setattr(get_settings(), "rate_limit_burst", 1)
    limiter = RateLimiter()

    assert limiter.allow("sirens-echo/default")[0]
    assert not limiter.allow("sirens-echo/default")[0]
    # A busy route must not starve a quiet one.
    assert limiter.allow("sirens-echo/deepseek")[0]


def test_a_settings_change_rebuilds_the_bucket(monkeypatch):
    monkeypatch.setattr(get_settings(), "rate_limit_per_second", 1.0)
    monkeypatch.setattr(get_settings(), "rate_limit_burst", 1)
    limiter = RateLimiter()
    assert limiter.allow("route")[0]
    assert not limiter.allow("route")[0]

    monkeypatch.setattr(get_settings(), "rate_limit_burst", 10)
    assert limiter.allow("route")[0]
