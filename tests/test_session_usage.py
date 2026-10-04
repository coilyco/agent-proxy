"""Per-session context usage for the aterm meter (docs/operational-views.md)."""

import pytest

from app import models, session_usage, upstream
from app.route_registry import Route, RouteRegistry
from app.session_usage import MAX_IDS_PER_READ, SessionUsageLedger, get_session_usage
from app.upstream import UpstreamResult


def test_context_is_the_latest_request_and_totals_accumulate():
    ledger = SessionUsageLedger()
    ledger.record("seat-a", "m", prompt_tokens=1000, completion_tokens=50)
    ledger.record("seat-a", "m", prompt_tokens=1200, completion_tokens=80)
    view = ledger.read(["seat-a"])["seat-a"]
    assert view["context_tokens"] == 1280
    assert view["input_tokens"] == 2200
    assert view["output_tokens"] == 130
    assert view["requests"] == 2


def test_a_request_with_no_usage_does_not_blank_the_reading():
    ledger = SessionUsageLedger()
    ledger.record("seat-a", "m", prompt_tokens=900, completion_tokens=10)
    ledger.record("seat-a", "m", prompt_tokens=0, completion_tokens=0)
    view = ledger.read(["seat-a"])["seat-a"]
    assert view["context_tokens"] == 910
    assert view["requests"] == 2


def test_an_unseen_id_is_absent_rather_than_zero():
    ledger = SessionUsageLedger()
    ledger.record("seat-a", "m", prompt_tokens=5, completion_tokens=1)
    assert set(ledger.read(["seat-a", "seat-b"])) == {"seat-a"}


@pytest.mark.parametrize("bad", ["", "has space", 'quo"te', "a" * 129, "line\nbreak"])
def test_an_id_outside_the_alphabet_is_never_recorded(bad):
    ledger = SessionUsageLedger()
    ledger.record(bad, "m", prompt_tokens=5, completion_tokens=1)
    assert ledger.read([bad]) == {}


def test_the_ledger_drops_the_least_recently_touched_session():
    ledger = SessionUsageLedger(max_sessions=2)
    ledger.record("one", "m", prompt_tokens=1, completion_tokens=1)
    ledger.record("two", "m", prompt_tokens=1, completion_tokens=1)
    ledger.record("one", "m", prompt_tokens=1, completion_tokens=1)
    ledger.record("three", "m", prompt_tokens=1, completion_tokens=1)
    assert set(ledger.read(["one", "two", "three"])) == {"one", "three"}


def test_the_window_comes_from_the_route_registry(monkeypatch):
    route = Route(key="evaluation/deepseek-v4-pro", upstream_alias="x", direct=None)
    sized = Route(key="evaluation/sized", upstream_alias="x", direct=None, context_window=1_000_000)
    registry = RouteRegistry(routes={route.key: route, sized.key: sized}, source={})
    monkeypatch.setattr(session_usage, "get_route_registry", lambda: registry)
    ledger = SessionUsageLedger()
    ledger.record("a", "evaluation/sized", prompt_tokens=10, completion_tokens=1)
    ledger.record("b", "evaluation/deepseek-v4-pro", prompt_tokens=10, completion_tokens=1)
    ledger.record("c", "qwen3:4b", prompt_tokens=10, completion_tokens=1)
    seen = ledger.read(["a", "b", "c"])
    assert seen["a"]["context_window"] == 1_000_000
    assert seen["b"]["context_window"] is None
    assert seen["c"]["context_window"] is None


@pytest.fixture
def served(monkeypatch, app_client):
    async def fake_chat(
        backend, num_ctx, messages, *, tools=None, tool_policy=None, options=None, span_attrs=None
    ):
        return UpstreamResult(
            model=backend.ollama_tag, content="ok", prompt_eval_count=4200, eval_count=60
        )

    async def fake_catalog(_base_url):
        return {"qwen3:4b": 262144}, True

    monkeypatch.setattr(upstream, "chat", fake_chat)
    monkeypatch.setattr(models, "_catalog", fake_catalog)
    models.reset_catalog()
    get_session_usage().reset()
    yield app_client
    get_session_usage().reset()


def _chat(client, session: str | None):
    headers = {"x-agent-session-id": session} if session else {}
    return client.post(
        "/v1/chat/completions",
        json={"model": "qwen3:4b", "messages": [{"role": "user", "content": "hi"}]},
        headers=headers,
    )


def test_a_served_chat_turn_reaches_the_read_route(served):
    assert _chat(served, "eng-platform-ep48").status_code == 200
    body = served.get("/v1/sessions/usage", params={"id": "eng-platform-ep48"}).json()
    assert body["format"] == "agent-proxy.session-usage.v1"
    seat = body["sessions"]["eng-platform-ep48"]
    assert (seat["context_tokens"], seat["requests"], seat["model"]) == (4260, 1, "qwen3:4b")


def test_a_caller_with_no_session_header_is_not_tracked(served):
    assert _chat(served, None).status_code == 200
    assert served.get("/v1/sessions/usage", params={"id": "anything"}).json()["sessions"] == {}


def test_one_read_answers_several_ids(served):
    _chat(served, "seat-a")
    _chat(served, "seat-b")
    response = served.get("/v1/sessions/usage?id=seat-a&id=seat-b&id=seat-c")
    assert set(response.json()["sessions"]) == {"seat-a", "seat-b"}


def test_a_read_past_the_id_cap_is_refused(served):
    ids = [("id", f"s{n}") for n in range(MAX_IDS_PER_READ + 1)]
    assert served.get("/v1/sessions/usage", params=ids).status_code == 400
