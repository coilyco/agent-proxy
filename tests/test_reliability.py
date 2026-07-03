"""Tests for the leg-05 reliability harness scoring path.

The harness itself needs the tower to run, but its scoring, error-mapping, and
aggregation logic are pure and must stay covered offline. These tests import the
script the same way the runtime does (``scripts/`` on ``sys.path`` so its bare
``from _endpoints import ...`` resolves) and drive every branch with synthetic
payloads and a mocked fetch - no network.
"""

from __future__ import annotations

import io
import json
import os
import sys
import urllib.error
from unittest.mock import patch

_SCRIPTS = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts"))
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import reliability_loop as rl  # noqa: E402


def _payload(content: str = "", tool_calls: list | None = None) -> dict:
    """A minimal OpenAI-shaped chat completion the scorer accepts."""
    message: dict = {"content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    return {"model": "test-model", "choices": [{"message": message}]}


_GOOD_TOOL_CALL = [{"function": {"name": "get_line_count", "arguments": "{}"}}]


# --------------------------------------------------------------------------- #
# score_payload - the pure scoring branch
# --------------------------------------------------------------------------- #


def test_score_plain_text_ok():
    ok, reason = rl.score_payload(_payload("A one line summary."), expect_tool=False)
    assert ok and reason == "ok"


def test_score_tool_call_ok_when_expected():
    ok, reason = rl.score_payload(_payload("", _GOOD_TOOL_CALL), expect_tool=True)
    assert ok and reason == "ok"


def test_score_missed_toolcall():
    # Text validates, but the tool contract was ignored -> harness-only miss.
    ok, reason = rl.score_payload(_payload("It has 1200 lines."), expect_tool=True)
    assert not ok and reason == "missed_toolcall"


def test_score_empty_response():
    ok, reason = rl.score_payload(_payload(""), expect_tool=False)
    assert not ok and reason == "empty"


def test_score_malformed_toolcall():
    bad = [{"function": {"name": "get_line_count", "arguments": "{not json"}}]
    ok, reason = rl.score_payload(_payload("", bad), expect_tool=True)
    assert not ok and reason == "malformed_toolcall"


# --------------------------------------------------------------------------- #
# _endpoint_and_model - target -> (url, model) resolution
# --------------------------------------------------------------------------- #


def test_proxy_target_uses_fast_think():
    url, model = rl._endpoint_and_model("proxy")
    assert model == "fast-think" and url.endswith("/v1/chat/completions")


def test_direct_target_model_is_overridable():
    # The tower tag the direct target asks for is env-overridable so the harness
    # runs where the present tags differ from the default table. Patch the tower
    # resolver so the test needs no TOWER/SSM to resolve an endpoint.
    with patch.object(rl, "tower_base_url", return_value="http://tower:11434"):
        _, model = rl._endpoint_and_model("direct")
    assert model == rl._DIRECT_MODEL


# --------------------------------------------------------------------------- #
# _score_turn - the HTTP error -> failure-reason mapping
# --------------------------------------------------------------------------- #


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("http://x", code, "err", {}, io.BytesIO(b""))  # type: ignore[arg-type]


def test_score_turn_maps_5xx():
    with patch.object(rl, "_fetch", side_effect=_http_error(503)):
        ok, reason = rl._score_turn("http://x", "m", [], expect_tool=False)
    assert not ok and reason == "upstream_5xx"


def test_score_turn_maps_4xx():
    with patch.object(rl, "_fetch", side_effect=_http_error(404)):
        ok, reason = rl._score_turn("http://x", "m", [], expect_tool=False)
    assert not ok and reason == "http_404"


def test_score_turn_maps_timeout():
    with patch.object(rl, "_fetch", side_effect=urllib.error.URLError("unreachable")):
        ok, reason = rl._score_turn("http://x", "m", [], expect_tool=False)
    assert not ok and reason == "timeout"


def test_score_turn_scores_good_payload():
    with patch.object(rl, "_fetch", return_value=_payload("hi")):
        ok, reason = rl._score_turn("http://x", "m", [], expect_tool=False)
    assert ok and reason == "ok"


# --------------------------------------------------------------------------- #
# run - aggregation into reliability % + histogram
# --------------------------------------------------------------------------- #


def test_run_aggregates_reliability_and_histogram():
    # Scripted per-turn outcomes: 4 turns, 3 usable -> 75%. Reasons must be a
    # deterministic, sorted histogram summing to the turn count.
    outcomes = [(True, "ok"), (False, "missed_toolcall"), (True, "ok"), (True, "ok")]
    with (
        patch.object(rl, "_score_turn", side_effect=outcomes),
        patch.object(rl, "_endpoint_and_model", return_value=("http://x", "test-model")),
    ):
        result = rl.run("direct", turns=4)

    assert result["usable"] == 3
    assert result["reliability_pct"] == 75.0
    assert result["failure_histogram"] == {"missed_toolcall": 1, "ok": 3}
    assert sum(result["failure_histogram"].values()) == 4
    assert len(result["turns_detail"]) == 4
    assert list(result["failure_histogram"]) == sorted(result["failure_histogram"])


def test_run_zero_turns_is_zero_pct_not_crash():
    with patch.object(rl, "_endpoint_and_model", return_value=("http://x", "m")):
        result = rl.run("proxy", turns=0)
    assert result["reliability_pct"] == 0.0 and result["turns"] == 0


# --------------------------------------------------------------------------- #
# build_artifact - the durable, reproducible report shape
# --------------------------------------------------------------------------- #


def test_build_artifact_shape():
    results = [
        {
            "target": "direct",
            "model": "qwen3-coder:30b",
            "turns": 6,
            "usable": 4,
            "reliability_pct": 66.7,
            "failure_histogram": {"missed_toolcall": 1, "ok": 4, "timeout": 1},
            "turns_detail": [],
        },
        {
            "target": "proxy",
            "model": "fast-think",
            "turns": 6,
            "usable": 6,
            "reliability_pct": 100.0,
            "failure_histogram": {"ok": 6},
            "turns_detail": [],
        },
    ]
    artifact = rl.build_artifact(results, turns=6)

    assert artifact["harness"] == "reliability_loop"
    assert set(artifact["results"]) == {"direct", "proxy"}
    assert artifact["run_shape"]["turns"] == 6
    assert artifact["run_shape"]["blob_lines"] == rl._BLOB_LINES
    # No endpoint URL is ever carried into the durable artifact.
    assert "http://" not in json.dumps(artifact)


if __name__ == "__main__":
    import subprocess

    raise SystemExit(subprocess.call([sys.executable, "-m", "pytest", __file__, "-v"]))
