"""Acceptance for the name-based resilient dispatch entry (leg 04 step 10, #14).

Covers the issue's stated acceptance:

* a backend that fails twice then succeeds -> result returned and
  ``llm_retries_total`` incremented per retry,
* a backend that always fails -> a clean ``AllBackendsFailed`` (never a hang),
* a dead primary in a multi-backend chain -> fall back to the next backend and
  ``llm_fallbacks_total`` incremented.

The engine (``dispatch``) is exercised through ``dispatch_resilient`` so the
tag -> catalog resolution -> chain path is proven end to end, not just the engine.
"""

import pytest

from app import resilience, upstream
from app.models import Backend, LogicalModel
from app.obs import llm_fallbacks_total, llm_retries_total
from app.resilience import AllBackendsFailed, BackendUnavailable, UnknownModel
from app.upstream import UpstreamError, UpstreamResult, UpstreamStatusError


def _good(content: str = "ok") -> UpstreamResult:
    return UpstreamResult(model="m", content=content, prompt_eval_count=1, eval_count=1)


def _install_resolve(monkeypatch, model: LogicalModel) -> None:
    """Point ``dispatch_resilient``'s tag resolution at a controlled one-model
    catalog: the seeded tag resolves, everything else is unknown."""

    async def fake_resolve(name):
        return model if name == model.name else None

    monkeypatch.setattr(resilience, "resolve", fake_resolve)


def _retries(model: str, backend: str) -> float:
    return llm_retries_total.labels(logical_model=model, backend=backend)._value.get()


def _fallbacks(model: str, backend: str) -> float:
    return llm_fallbacks_total.labels(logical_model=model, backend=backend)._value.get()


@pytest.fixture(autouse=True)
def _fast_and_isolated(monkeypatch):
    # Zero the backoff sleep so the retry tests run instantly, and give each test
    # a fresh breaker registry so an open circuit can't leak across cases.
    async def _no_sleep(_seconds):
        return None

    monkeypatch.setattr(resilience.asyncio, "sleep", _no_sleep)
    monkeypatch.setattr(resilience, "breakers", resilience.CircuitBreakerRegistry())


async def test_fails_twice_then_succeeds_counts_retries(monkeypatch):
    backend = Backend(name="b-retry", url="http://x", ollama_tag="t")
    model = LogicalModel("qwen3:4b", 4096, [backend])
    _install_resolve(monkeypatch, model)

    calls = {"n": 0}

    async def flaky(
        be, num_ctx, messages, *, tools=None, tool_policy=None, options=None, span_attrs=None
    ):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise UpstreamError("transient")
        return _good("recovered")

    monkeypatch.setattr(upstream, "chat", flaky)

    before = _retries(model.name, backend.name)
    result = await resilience.dispatch_resilient(model.name, [{"role": "user", "content": "hi"}])

    assert result.content == "recovered"
    assert calls["n"] == 3  # two failures rerolled, third attempt won
    assert _retries(model.name, backend.name) - before == 2


async def test_always_fails_surfaces_clean_error(monkeypatch):
    backend = Backend(name="b-dead", url="http://x", ollama_tag="t")
    model = LogicalModel("qwen3:8b", 4096, [backend])
    _install_resolve(monkeypatch, model)

    async def dead(*args, **kwargs):
        raise UpstreamError("backend down")

    monkeypatch.setattr(upstream, "chat", dead)

    # One backend was attempted, so the error says so rather than claiming a
    # chain-wide outage (issue #114).
    with pytest.raises(BackendUnavailable) as caught:
        await resilience.dispatch_resilient(model.name, [{"role": "user", "content": "hi"}])
    assert not isinstance(caught.value, AllBackendsFailed)
    assert "b-dead" in str(caught.value)


async def test_all_backends_failed_names_a_real_chain(monkeypatch):
    primary = Backend(name="b-primary", url="http://x", ollama_tag="t")
    secondary = Backend(name="b-secondary", url="http://y", ollama_tag="t")
    model = LogicalModel("qwen3:32b", 4096, [primary, secondary])
    _install_resolve(monkeypatch, model)

    async def dead(*args, **kwargs):
        raise UpstreamError("backend down")

    monkeypatch.setattr(upstream, "chat", dead)

    with pytest.raises(AllBackendsFailed, match="all 2 backends failed"):
        await resilience.dispatch_resilient(model.name, [{"role": "user", "content": "hi"}])


async def test_falls_back_to_next_backend(monkeypatch):
    primary = Backend(name="b-primary", url="http://x", ollama_tag="t")
    secondary = Backend(name="b-secondary", url="http://y", ollama_tag="t")
    model = LogicalModel("qwen3:32b", 4096, [primary, secondary])
    _install_resolve(monkeypatch, model)

    async def chat(
        be, num_ctx, messages, *, tools=None, tool_policy=None, options=None, span_attrs=None
    ):
        if be.name == primary.name:
            raise UpstreamError("dead primary")
        return _good("from-secondary")

    monkeypatch.setattr(upstream, "chat", chat)

    before = _fallbacks(model.name, primary.name)
    result = await resilience.dispatch_resilient(model.name, [{"role": "user", "content": "hi"}])

    assert result.content == "from-secondary"
    assert _fallbacks(model.name, primary.name) - before == 1


async def test_unknown_tag_raises(monkeypatch):
    model = LogicalModel("known", 4096, [Backend(name="b", url="http://x", ollama_tag="t")])
    _install_resolve(monkeypatch, model)

    with pytest.raises(UnknownModel):
        await resilience.dispatch_resilient("does-not-exist", [])


# Upstream status classification (issue #114). The trace: one 400, three
# identical attempts, and a 502 naming a backend that was serving other traffic.


def _status_error(status: int, body: str = "") -> UpstreamStatusError:
    return UpstreamStatusError(f"litellm: Client error '{status}'", status_code=status, body=body)


async def test_settled_4xx_is_not_retried(monkeypatch):
    backend = Backend(name="litellm", url="http://x", ollama_tag="t")
    model = LogicalModel("sirens-echo/deepseek", 4096, [backend])
    _install_resolve(monkeypatch, model)
    calls = {"n": 0}

    async def rejects(*args, **kwargs):
        calls["n"] += 1
        raise _status_error(400, '{"error":{"message":"bad tool pairing"}}')

    monkeypatch.setattr(upstream, "chat", rejects)
    monkeypatch.setattr(resilience.get_settings(), "max_retries", 2, raising=False)

    with pytest.raises(resilience.UpstreamRequestRejected) as caught:
        await resilience.dispatch_resilient(model.name, [{"role": "user", "content": "hi"}])

    # The body is byte-identical between attempts, so attempts 1 and 2 were
    # guaranteed to fail before they were sent.
    assert calls["n"] == 1
    assert caught.value.status_code == 400
    assert "bad tool pairing" in caught.value.body


async def test_settled_4xx_does_not_walk_the_fallback_chain(monkeypatch):
    primary = Backend(name="litellm", url="http://x", ollama_tag="t")
    secondary = Backend(name="tower", url="http://y", ollama_tag="t")
    model = LogicalModel("sirens-echo/deepseek", 4096, [primary, secondary])
    _install_resolve(monkeypatch, model)
    seen: list[str] = []

    async def rejects(be, *args, **kwargs):
        seen.append(be.name)
        raise _status_error(400)

    monkeypatch.setattr(upstream, "chat", rejects)

    with pytest.raises(resilience.UpstreamRequestRejected):
        await resilience.dispatch_resilient(model.name, [{"role": "user", "content": "hi"}])

    # Asking a second backend the same invalid question cannot help.
    assert seen == ["litellm"]


async def test_settled_4xx_leaves_the_breaker_closed(monkeypatch):
    backend = Backend(name="litellm-breaker", url="http://x", ollama_tag="t")
    model = LogicalModel("sirens-echo/deepseek", 4096, [backend])
    _install_resolve(monkeypatch, model)

    async def rejects(*args, **kwargs):
        raise _status_error(400)

    monkeypatch.setattr(upstream, "chat", rejects)
    monkeypatch.setattr(resilience.get_settings(), "circuit_fail_threshold", 2, raising=False)

    for _ in range(4):
        with pytest.raises(resilience.UpstreamRequestRejected):
            await resilience.dispatch_resilient(model.name, [{"role": "user", "content": "hi"}])

    # The backend answered promptly every time. It is not the broken thing.
    assert resilience.breakers.allow(backend)


@pytest.mark.parametrize("status", [408, 425, 429, 500, 502, 503])
async def test_retryable_statuses_still_retry(monkeypatch, status):
    backend = Backend(name=f"retry-{status}", url="http://x", ollama_tag="t")
    model = LogicalModel(f"retryable-{status}", 4096, [backend])
    _install_resolve(monkeypatch, model)
    calls = {"n": 0}

    async def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _status_error(status)
        return _good("recovered")

    monkeypatch.setattr(upstream, "chat", flaky)
    monkeypatch.setattr(resilience.get_settings(), "max_retries", 2, raising=False)
    monkeypatch.setattr(resilience.get_settings(), "retry_base_delay", 0.0, raising=False)

    result = await resilience.dispatch_resilient(model.name, [{"role": "user", "content": "hi"}])

    assert result.content == "recovered"
    assert calls["n"] == 2
