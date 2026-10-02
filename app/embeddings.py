"""The /v1/embeddings surface: OpenAI shape in, a local Ollama embedding model behind it."""

from __future__ import annotations

import base64
import struct
from dataclasses import dataclass
from typing import Any

import httpx

from .models import Backend
from .obs import record_error
from .upstream import (
    UpstreamError,
    UpstreamStatusError,
    _status_error,
    _timeout,
    get_client,
    request_auth_kwargs,
)

MAX_INPUTS = 256


class EmbeddingRequestError(ValueError):
    """The request body cannot be served as an embedding request."""


@dataclass(frozen=True)
class EmbeddingRequest:
    inputs: list[str]
    encoding_format: str
    dimensions: int | None


@dataclass(frozen=True)
class EmbeddingResult:
    vectors: list[list[float]]
    prompt_tokens: int


def parse_request(body: dict[str, Any]) -> EmbeddingRequest:
    """Validate the OpenAI embedding body. Token arrays are refused, not guessed at."""
    raw = body.get("input")
    inputs = [raw] if isinstance(raw, str) else raw
    if not isinstance(inputs, list) or not inputs:
        raise EmbeddingRequestError("input must be a string or a non-empty list of strings")
    if len(inputs) > MAX_INPUTS:
        raise EmbeddingRequestError(f"input takes at most {MAX_INPUTS} strings")
    if not all(isinstance(item, str) and item for item in inputs):
        raise EmbeddingRequestError(
            "every input must be a non-empty string, token arrays are not supported"
        )
    encoding = body.get("encoding_format") or "float"
    if encoding not in ("float", "base64"):
        raise EmbeddingRequestError("encoding_format must be float or base64")
    dimensions = body.get("dimensions")
    if dimensions is not None and (
        not isinstance(dimensions, int) or isinstance(dimensions, bool) or dimensions < 1
    ):
        raise EmbeddingRequestError("dimensions must be a positive integer")
    return EmbeddingRequest(list(inputs), encoding, dimensions)


async def embed_ollama(backend: Backend, request: EmbeddingRequest) -> EmbeddingResult:
    """One call to Ollama's native /api/embed, which batches a list of inputs."""
    payload: dict[str, Any] = {"model": backend.ollama_tag, "input": request.inputs}
    if request.dimensions is not None:
        payload["dimensions"] = request.dimensions
    try:
        resp = await get_client().post(
            f"{backend.url}/api/embed",
            json=payload,
            timeout=_timeout(backend),
            **request_auth_kwargs(backend),
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise _status_error(backend, exc) from exc
    except httpx.HTTPError as exc:
        record_error("upstream_transport_failed")
        raise UpstreamError(f"{backend.name}: {exc}") from exc
    data = resp.json()
    vectors = data.get("embeddings")
    if not isinstance(vectors, list) or len(vectors) != len(request.inputs):
        raise UpstreamError(
            f"{backend.name}: answered {len(vectors or [])} vectors for {len(request.inputs)} inputs"
        )
    return EmbeddingResult(vectors, int(data.get("prompt_eval_count", 0) or 0))


def render_response(
    logical_model: str, request: EmbeddingRequest, result: EmbeddingResult
) -> dict[str, Any]:
    """OpenAI's list-of-embeddings shape, naming the logical route and never the physical model.

    base64 is little-endian float32, which is what the OpenAI SDK asks for and decodes
    when the caller does not pick a format.
    """

    def encode(vector: list[float]) -> Any:
        if request.encoding_format == "base64":
            return base64.b64encode(struct.pack(f"<{len(vector)}f", *vector)).decode("ascii")
        return vector

    return {
        "object": "list",
        "model": logical_model,
        "data": [
            {"object": "embedding", "index": index, "embedding": encode(vector)}
            for index, vector in enumerate(result.vectors)
        ],
        "usage": {"prompt_tokens": result.prompt_tokens, "total_tokens": result.prompt_tokens},
    }


def settled_refusal(exc: UpstreamStatusError) -> bool:
    """A 4xx that no retry or other backend will change: the model does not embed, or the body is bad."""
    return 400 <= exc.status_code < 500 and exc.status_code not in (408, 425, 429)
