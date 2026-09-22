"""Jev (TypeSafe System One) decision shim: the upstream half.

The route, spans and trajectory events live in ``app.main`` beside the chat
handler. This module owns what is specific to ``POST /v1/systemone``: the
outbound call, the key read, and usage read back out of the reply.
Contract and telemetry: docs/systemone-shim.md.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import upstream
from .config import get_settings

BACKEND_NAME = "typesafe"
BACKEND_DIALECT = "systemone"
# TypeSafe's capacity is not the fleet's to observe. See docs/backend-catalog.md.
BACKEND_REGIME = "hosted"
# What the official SDKs read back to pace a retry.
_PACING_HEADERS = ("retry-after", "retry-after-ms")


class SystemOneUnavailable(Exception):
    """No key is configured, or the mounted key cannot be read."""


@dataclass
class SystemOneReply:
    """One upstream answer, still in the caller's terms: status, body, pacing."""

    status_code: int
    body: bytes
    content_type: str
    headers: dict[str, str] = field(default_factory=dict)
    elapsed_seconds: float = 0.0
    # Read from a 2xx body. ``model`` is the resolved version, not the alias.
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    # A 2xx whose body is not the JSON object the SDKs parse.
    malformed: bool = False

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300 and not self.malformed


def known_model(body: dict[str, Any]) -> str:
    """The requested model when it is on the allowlist, else ``""``.

    The body picks the model, and the model becomes a metric label, so an
    unlisted name is refused here rather than minting a series per caller string.
    """
    model = body.get("model")
    if isinstance(model, str) and model in get_settings().resolved_systemone_models():
        return model
    return ""


def question_count(body: dict[str, Any]) -> int:
    questions = body.get("questions")
    return len(questions) if isinstance(questions, dict) else 0


# The one fixed question key every /v1/systemone/<type> route uses. A tool
# call is one question, so there is nothing for a caller to name.
CHOICE_QUESTION_KEY = "answer"


def build_choice_body(flat: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
    """Reshape a flat choice request into TypeSafe's nested ``criteria`` map.

    TypeSafe's ``choice`` question takes ``criteria`` as a map keyed by
    caller-chosen option names. umbra's inline guardfile grammar has no
    primitive for turning an array element's own field into an object key
    (typed inputs are scalars and arrays of scalars; objects fail closed,
    per umbra/docs/opcore-inline.md), so a beaver guardfile cannot author
    this shape directly. This is the one reshape a flat tool call still
    needs: ``noul`` has no nested field at all, and ``score``'s ``criteria``
    is TypeSafe's own ordered array, not a map, so both pass straight
    through the existing grammar's ``array ... raw=true`` escape hatch.

    Returns ``(body, "")`` on a well-formed flat request, or
    ``(None, message)`` naming the first thing wrong with it.
    """
    model = flat.get("model")
    if not isinstance(model, str) or not model:
        return None, "model must be a non-empty string"
    instructions = flat.get("instructions")
    if not isinstance(instructions, str) or not instructions:
        return None, "instructions must be a non-empty string"
    options = flat.get("options")
    if not isinstance(options, list) or not options:
        return None, "options must be a non-empty array"
    criteria: dict[str, Any] = {}
    for option in options:
        if not isinstance(option, dict):
            return None, "each option must be an object"
        name = option.get("name")
        if not isinstance(name, str) or not name:
            return None, "each option needs a non-empty string name"
        if name in criteria:
            return None, f"duplicate option name '{name}'"
        criteria[name] = option.get("description")
    body: dict[str, Any] = {
        "model": model,
        "questions": {
            CHOICE_QUESTION_KEY: {
                "type": "choice",
                "instructions": instructions,
                "criteria": criteria,
            }
        },
    }
    if "state" in flat:
        body["state"] = flat["state"]
    return body, ""


def input_cost_usd(input_tokens: int) -> float:
    """Output tokens are free at the source, so input is the whole charge."""
    return input_tokens * get_settings().systemone_input_usd_per_mtok / 1_000_000


def _api_key() -> str:
    path = get_settings().systemone_api_key_file
    if not path:
        raise SystemOneUnavailable("systemone backend is not configured")
    try:
        key = Path(path).read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise SystemOneUnavailable("systemone authentication key unavailable") from exc
    if not key:
        raise SystemOneUnavailable("systemone authentication key unavailable")
    return key


def _count(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return max(int(value), 0)


def _read_usage(reply: SystemOneReply, payload: Any) -> None:
    if not isinstance(payload, dict):
        reply.malformed = True
        return
    model = payload.get("model")
    reply.model = model if isinstance(model, str) else ""
    usage = payload.get("usage")
    if isinstance(usage, dict):
        reply.input_tokens = _count(usage.get("input_tokens"))
        reply.output_tokens = _count(usage.get("output_tokens"))


async def forward(body: dict[str, Any], *, timeout: float) -> SystemOneReply:
    """Send ``body`` upstream once and return the answer as it came back.

    One attempt, on purpose: the official SDKs already retry 408, 429 and 5xx
    and read ``Retry-After``, so a retry here would hide failures from the
    telemetry the bake-off reads. Transport errors propagate as ``httpx``
    exceptions for the route to classify.
    """
    key = _api_key()
    url = f"{get_settings().systemone_base_url.rstrip('/')}/v1/systemone"
    started = time.perf_counter()
    response = await upstream.get_client().post(
        url, json=body, headers={"Authorization": f"Bearer {key}"}, timeout=timeout
    )
    reply = SystemOneReply(
        status_code=response.status_code,
        body=response.content,
        content_type=response.headers.get("content-type", "application/json"),
        headers={n: response.headers[n] for n in _PACING_HEADERS if n in response.headers},
        elapsed_seconds=time.perf_counter() - started,
    )
    if 200 <= reply.status_code < 300:
        try:
            _read_usage(reply, response.json())
        except ValueError:
            reply.malformed = True
    return reply
