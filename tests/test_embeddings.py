"""/v1/embeddings: OpenAI shape in, a local Ollama embedding model behind it (#8650).
Upstream is stubbed so these run without the tower."""

import base64
import struct

import httpx
import pytest

from app import embeddings, models
from app.embeddings import EmbeddingRequest, EmbeddingRequestError, EmbeddingResult
from app.models import Backend
from app.upstream import UpstreamError, UpstreamStatusError

ROUTE = "nomic-embed-text"
CATALOG: dict[str, int | None] = {ROUTE: 8192}


@pytest.fixture
def client(monkeypatch, app_client):
    async def fake_catalog(_base_url):
        return dict(CATALOG), True

    monkeypatch.setattr(models, "_catalog", fake_catalog)
    models.reset_catalog()
    return app_client


def _stub_embed(monkeypatch, vectors=((0.5, -1.0, 2.0),), tokens=7, calls=None):
    async def fake(backend, request):
        if calls is not None:
            calls.append((backend.name, request))
        return EmbeddingResult(
            [list(v) for v in vectors][: len(request.inputs)] * len(request.inputs), tokens
        )

    monkeypatch.setattr("app.main.embed_ollama", fake)


def test_parse_accepts_a_string_or_a_list_and_defaults_to_float():
    assert embeddings.parse_request({"input": "x"}) == EmbeddingRequest(["x"], "float", None)
    parsed = embeddings.parse_request(
        {"input": ["a", "b"], "encoding_format": "base64", "dimensions": 256}
    )
    assert (parsed.inputs, parsed.encoding_format, parsed.dimensions) == (["a", "b"], "base64", 256)


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"input": []},
        {"input": [""]},
        {"input": [[1, 2, 3]]},
        {"input": 5},
        {"input": ["x"] * (embeddings.MAX_INPUTS + 1)},
        {"input": "x", "encoding_format": "int8"},
        {"input": "x", "dimensions": 0},
        {"input": "x", "dimensions": True},
    ],
)
def test_parse_refuses_what_it_cannot_serve(body):
    with pytest.raises(EmbeddingRequestError):
        embeddings.parse_request(body)


def test_base64_is_little_endian_float32_as_the_openai_sdk_decodes_it():
    request = EmbeddingRequest(["x"], "base64", None)
    rendered = embeddings.render_response(
        "rt/embed", request, EmbeddingResult([[0.5, -1.0, 2.0]], 3)
    )
    raw = base64.b64decode(rendered["data"][0]["embedding"])
    assert struct.unpack("<3f", raw) == (0.5, -1.0, 2.0)
    assert rendered["model"] == "rt/embed"
    assert rendered["usage"] == {"prompt_tokens": 3, "total_tokens": 3}


def test_endpoint_returns_the_openai_list_shape_under_the_logical_name(client, monkeypatch):
    calls: list = []
    _stub_embed(monkeypatch, calls=calls)
    resp = client.post("/v1/embeddings", json={"model": ROUTE, "input": ["a", "b"]})
    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "list" and body["model"] == ROUTE
    assert [d["index"] for d in body["data"]] == [0, 1]
    assert body["data"][0]["embedding"] == [0.5, -1.0, 2.0]
    assert body["usage"]["prompt_tokens"] == 7
    assert len(calls) == 1 and calls[0][1].inputs == ["a", "b"]


def test_endpoint_refuses_bad_requests_before_touching_a_backend(client, monkeypatch):
    calls: list = []
    _stub_embed(monkeypatch, calls=calls)
    assert client.post("/v1/embeddings", content=b"not json").status_code == 400
    assert client.post("/v1/embeddings", json=[1]).status_code == 400
    assert (
        client.post("/v1/embeddings", json={"model": ROUTE, "input": [[1, 2]]}).status_code == 400
    )
    assert client.post("/v1/embeddings", json={"model": "nope", "input": "x"}).status_code == 404
    assert calls == []


def test_a_route_with_no_local_backend_fails_closed(client, monkeypatch):
    calls: list = []
    _stub_embed(monkeypatch, calls=calls)
    monkeypatch.setattr(
        models,
        "_backend_specs",
        lambda: [{"name": "hosted", "url": "https://api.example", "dialect": "openai"}],
    )
    resp = client.post("/v1/embeddings", json={"model": ROUTE, "input": "x"})
    assert resp.status_code == 503
    assert "never go to a hosted one" in resp.json()["error"]["message"]
    assert calls == []


def test_a_settled_refusal_is_a_generic_400_that_names_no_physical_model(client, monkeypatch):
    async def refuse(backend, request):
        raise UpstreamStatusError(
            "tower: 400", status_code=400, body='"all-minilm" does not support embeddings'
        )

    monkeypatch.setattr("app.main.embed_ollama", refuse)
    resp = client.post("/v1/embeddings", json={"model": ROUTE, "input": "x"})
    assert resp.status_code == 400
    assert "all-minilm" not in resp.text and "tower" not in resp.text


def test_a_backend_that_fails_hands_over_to_the_next_local_one(client, monkeypatch):
    monkeypatch.setattr(
        models,
        "_backend_specs",
        lambda: [{"name": "first", "url": "http://a"}, {"name": "second", "url": "http://b"}],
    )
    seen: list[str] = []

    async def flaky(backend, request):
        seen.append(backend.name)
        if backend.name == "first":
            raise UpstreamError("first: connection refused")
        return EmbeddingResult([[1.0]], 1)

    monkeypatch.setattr("app.main.embed_ollama", flaky)
    resp = client.post("/v1/embeddings", json={"model": ROUTE, "input": "x"})
    assert resp.status_code == 200 and seen == ["first", "second"]


def test_every_backend_failing_is_a_502(client, monkeypatch):
    async def down(backend, request):
        raise UpstreamStatusError("tower: 503", status_code=503)

    monkeypatch.setattr("app.main.embed_ollama", down)
    assert client.post("/v1/embeddings", json={"model": ROUTE, "input": "x"}).status_code == 502


def _backend() -> Backend:
    return Backend(name="tower", url="http://tower:11434", ollama_tag="nomic-embed-text")


def _mock_ollama(monkeypatch, handler):
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(embeddings, "get_client", lambda: httpx.AsyncClient(transport=transport))


@pytest.mark.asyncio
async def test_embed_ollama_posts_the_native_embed_shape(monkeypatch):
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = request.read()
        return httpx.Response(
            200, json={"embeddings": [[1.0, 2.0], [3.0, 4.0]], "prompt_eval_count": 9}
        )

    _mock_ollama(monkeypatch, handler)
    result = await embeddings.embed_ollama(_backend(), EmbeddingRequest(["a", "b"], "float", 2))
    assert seen["path"] == "/api/embed"
    assert b'"model":"nomic-embed-text"' in seen["body"].replace(b" ", b"")
    assert b'"dimensions":2' in seen["body"].replace(b" ", b"")
    assert result == EmbeddingResult([[1.0, 2.0], [3.0, 4.0]], 9)


@pytest.mark.asyncio
async def test_embed_ollama_rejects_a_vector_count_that_does_not_match(monkeypatch):
    _mock_ollama(monkeypatch, lambda request: httpx.Response(200, json={"embeddings": [[1.0]]}))
    with pytest.raises(UpstreamError, match="1 vectors for 2 inputs"):
        await embeddings.embed_ollama(_backend(), EmbeddingRequest(["a", "b"], "float", None))


@pytest.mark.asyncio
async def test_embed_ollama_reports_a_status_as_a_status_error(monkeypatch):
    _mock_ollama(monkeypatch, lambda request: httpx.Response(400, text="no embeddings"))
    with pytest.raises(UpstreamStatusError) as caught:
        await embeddings.embed_ollama(_backend(), EmbeddingRequest(["a"], "float", None))
    assert caught.value.status_code == 400
