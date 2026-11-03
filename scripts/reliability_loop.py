#!/usr/bin/env python3
"""Reliability harness (leg 05): scores a sustained tool-using loop usable or failed.

Runs against ``--target direct``, ``proxy`` or ``both`` and scores each response
with the proxy's own ``resilience.validate_response``. Emits a reliability
percentage and a failure histogram, and ``--json PATH`` writes the durable
artifact. Run it as ``TOWER=<host> just reliability --target both --turns 6``.
Target meanings and the artifact contract: docs/reliability_baseline.md.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
import urllib.error
import urllib.request
from collections import Counter
from typing import Any

from _endpoints import proxy_base_url, tower_base_url

from app.resilience import validate_response
from app.upstream import UpstreamResult

# A file-shaped blob so each accumulated tool output pushes context upward. About
# ~8k tokens per blob; a couple of these already overflow the 32k default.
_BLOB_LINES = 1200
_BLOB = "\n".join(
    f"    row[{i}] = compute(payload[{i}], config['key_{i}'], flags={i % 7})  # noqa"
    for i in range(_BLOB_LINES)
)

_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_line_count",
            "description": "Return how many lines are in the accumulated file context.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    }
]

_SYSTEM = (
    "You are a coding agent. CRITICAL RULE: whenever the user asks how many lines, "
    "you MUST call the get_line_count tool and never answer from memory. Keep every "
    "other answer to one short sentence."
)


# The real ollama tag both targets run against (issue #32: pass-through, no alias).
_MODEL = os.environ.get("MODEL", "qwen3-coder:30b")


def _endpoint_and_model(target: str) -> tuple[str, str]:
    if target == "proxy":
        return f"{proxy_base_url()}/v1/chat/completions", _MODEL
    return f"{tower_base_url()}/v1/chat/completions", _MODEL


def _to_result(openai_resp: dict) -> UpstreamResult:
    msg = openai_resp["choices"][0]["message"]
    return UpstreamResult(
        model=openai_resp.get("model", ""),
        content=msg.get("content") or "",
        tool_calls=msg.get("tool_calls") or [],
    )


def score_payload(payload: dict, expect_tool: bool) -> tuple[bool, str]:
    """Score one already-fetched OpenAI response ``(ok, reason)``.

    Pure and network-free so the offline test suite can exercise every scoring
    branch. Reuses the proxy's own ``validate_response`` for the usable/garbage
    decision, then layers the harness-only ``missed_toolcall`` rule on top: a
    response that validates as text but ignores the tool contract is the leg-01
    "weak context management" failure mode and counts as a miss.
    """
    result = _to_result(payload)
    ok, reason = validate_response(result)
    if not ok:
        return False, reason
    if expect_tool and not result.tool_calls:
        return False, "missed_toolcall"
    return True, "ok"


def _fetch(url: str, body: dict) -> dict:
    """POST ``body`` to ``url`` and return the decoded JSON. Raises on transport
    or HTTP error - the caller maps those to failure reasons."""
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data, {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=600) as resp:
        return json.load(resp)


def _score_turn(url: str, model: str, messages: list, expect_tool: bool) -> tuple[bool, str]:
    body: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "max_tokens": 512,
        "stream": False,
    }
    if expect_tool:
        body["tools"] = _TOOLS
    try:
        payload = _fetch(url, body)
    except urllib.error.HTTPError as exc:
        return False, "upstream_5xx" if exc.code >= 500 else f"http_{exc.code}"
    except (urllib.error.URLError, TimeoutError):
        return False, "timeout"
    return score_payload(payload, expect_tool)


def run(target: str, turns: int) -> dict:
    """Run the loop against one target and return a structured result.

    The returned dict is the per-target artifact shape: ``reliability_pct`` and a
    sorted ``failure_histogram`` are the durable numbers; ``turns_detail`` keeps
    the per-turn record so a regression can be traced to the turn that flipped.
    """
    url, model = _endpoint_and_model(target)
    messages: list[dict[str, Any]] = [{"role": "system", "content": _SYSTEM}]
    reasons: Counter[str] = Counter()
    detail: list[dict[str, Any]] = []
    usable = 0

    for turn in range(turns):
        # Grow context: accumulate a file-shaped blob as a prior tool result.
        messages.append({"role": "user", "content": f"Here is file part {turn}:\n{_BLOB}"})
        messages.append({"role": "assistant", "content": f"Noted file part {turn}."})

        expect_tool = turn % 2 == 1
        if expect_tool:
            messages.append(
                {"role": "user", "content": "How many lines are in the accumulated file context?"}
            )
        else:
            messages.append(
                {"role": "user", "content": "Summarize what you have seen so far in one sentence."}
            )

        ok, reason = _score_turn(url, model, messages, expect_tool)
        reasons[reason] += 1
        usable += int(ok)
        detail.append({"turn": turn, "expect_tool": expect_tool, "ok": ok, "reason": reason})
        # Feed a plausible assistant turn back so the conversation keeps growing.
        messages.append({"role": "assistant", "content": "acknowledged."})
        print(
            f"  turn {turn:2d} expect_tool={expect_tool!s:5} -> {'OK' if ok else 'FAIL:' + reason}"
        )

    pct = 100.0 * usable / turns if turns else 0.0
    # Sort the histogram so a committed artifact diffs cleanly across runs.
    histogram = dict(sorted(reasons.items()))
    print(f"\ntarget={target} model={model} runs={turns} reliability={pct:.0f}%")
    print("failure reasons:", histogram)
    return {
        "target": target,
        "model": model,
        "turns": turns,
        "usable": usable,
        "reliability_pct": round(pct, 1),
        "failure_histogram": histogram,
        "turns_detail": detail,
    }


def build_artifact(results: list[dict], turns: int) -> dict:
    """Assemble the durable, machine-readable report from one or more per-target
    runs. Deterministic apart from ``generated_at``; no endpoint FQDN inside."""
    return {
        "harness": "reliability_loop",
        "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "run_shape": {
            "turns": turns,
            "blob_lines": _BLOB_LINES,
            "tool_rule": "odd turns must call get_line_count; even turns are a one-line summary",
            "scored_by": "app.resilience.validate_response + harness missed_toolcall rule",
        },
        "results": {r["target"]: r for r in results},
    }


def _print_comparison(results: list[dict]) -> None:
    """Print the baseline-to-after table for a multi-target run."""
    print("\n=== reliability comparison ===")
    print(f"{'target':8} {'model':16} {'turns':>5} {'reliability':>11}  histogram")
    for r in results:
        print(
            f"{r['target']:8} {r['model']:16} {r['turns']:>5} "
            f"{r['reliability_pct']:>10.1f}%  {r['failure_histogram']}"
        )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--target", choices=["direct", "proxy", "both"], default="proxy")
    ap.add_argument("--turns", type=int, default=6)
    ap.add_argument(
        "--json",
        dest="json_path",
        metavar="PATH",
        help="write the durable machine-readable artifact to PATH (schema is stable across runs)",
    )
    args = ap.parse_args()

    targets = ["direct", "proxy"] if args.target == "both" else [args.target]
    results = []
    for target in targets:
        print(f"\n--- target: {target} ---")
        results.append(run(target, args.turns))

    if len(results) > 1:
        _print_comparison(results)

    if args.json_path:
        artifact = build_artifact(results, args.turns)
        with open(args.json_path, "w") as fh:
            json.dump(artifact, fh, indent=2, sort_keys=False)
            fh.write("\n")
        print(f"\nArtifact written to: {args.json_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
