#!/usr/bin/env python3
"""Reliability harness (leg 05).

Runs the context-growing, tool-using loop against a target endpoint and scores
each response as usable vs failed using the proxy's own
``app.resilience.validate_response``.

Targets:
* ``direct`` - the tower's ``/v1`` with no ``num_ctx``.
* ``proxy`` - the local proxy's logical ``fast-think``.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import sys
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Any

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts._endpoints import proxy_base_url, tower_base_url
from app.resilience import validate_response
from app.upstream import UpstreamResult

_BLOB = "\n".join(
    f"    row[{i}] = compute(payload[{i}], config['key_{i}'], flags={i % 7})  # noqa"
    for i in range(1200)
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


@dataclasses.dataclass(frozen=True)
class TurnResult:
    turn: int
    expect_tool: bool
    ok: bool
    reason: str


@dataclasses.dataclass(frozen=True)
class RunSummary:
    target: str
    model: str
    turns: int
    usable: int
    reliability: float
    reasons: dict[str, int]
    turn_results: list[TurnResult]


def _endpoint_and_model(target: str) -> tuple[str, str]:
    if target == "proxy":
        return f"{proxy_base_url()}/v1/chat/completions", "fast-think"
    return f"{tower_base_url()}/v1/chat/completions", "qwen3-coder:30b"


def _to_result(openai_resp: dict[str, Any]) -> UpstreamResult:
    msg = openai_resp["choices"][0]["message"]
    return UpstreamResult(
        model=openai_resp.get("model", ""),
        content=msg.get("content") or "",
        tool_calls=msg.get("tool_calls") or [],
    )


def _score_turn(url: str, model: str, messages: list[dict[str, Any]], expect_tool: bool) -> tuple[bool, str]:
    body: dict[str, Any] = {"model": model, "messages": messages, "max_tokens": 512, "stream": False}
    if expect_tool:
        body["tools"] = _TOOLS
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data, {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=600) as resp:
            payload = json.load(resp)
    except urllib.error.HTTPError as exc:
        return False, "upstream_5xx" if exc.code >= 500 else f"http_{exc.code}"
    except (urllib.error.URLError, TimeoutError):
        return False, "timeout"

    result = _to_result(payload)
    ok, reason = validate_response(result)
    if not ok:
        return False, reason
    if expect_tool and not result.tool_calls:
        return False, "missed_toolcall"
    return True, "ok"


def run(target: str, turns: int) -> RunSummary:
    url, model = _endpoint_and_model(target)
    messages: list[dict[str, Any]] = [{"role": "system", "content": _SYSTEM}]
    reasons: Counter[str] = Counter()
    turn_results: list[TurnResult] = []
    usable = 0

    for turn in range(turns):
        messages.append({"role": "user", "content": f"Here is file part {turn}:\n{_BLOB}"})
        messages.append({"role": "assistant", "content": f"Noted file part {turn}."})

        expect_tool = turn % 2 == 1
        if expect_tool:
            messages.append({"role": "user", "content": "How many lines are in the accumulated file context?"})
        else:
            messages.append({"role": "user", "content": "Summarize what you have seen so far in one sentence."})

        ok, reason = _score_turn(url, model, messages, expect_tool)
        reasons[reason] += 1
        usable += int(ok)
        turn_results.append(TurnResult(turn=turn, expect_tool=expect_tool, ok=ok, reason=reason))
        messages.append({"role": "assistant", "content": "acknowledged."})
        print(f"turn {turn:02d} expect_tool={expect_tool!s:5} -> {'OK' if ok else 'FAIL:' + reason}")

    reliability = 100.0 * usable / turns if turns else 0.0
    return RunSummary(
        target=target,
        model=model,
        turns=turns,
        usable=usable,
        reliability=reliability,
        reasons=dict(reasons),
        turn_results=turn_results,
    )


def render_markdown(summary: RunSummary) -> str:
    lines = [
        "# M2 Reliability Measurement",
        "",
        f"- Target: `{summary.target}`",
        f"- Model: `{summary.model}`",
        f"- Run shape: context-growing, tool-using loop",
        f"- Turns: `{summary.turns}`",
        f"- Reliability: `{summary.reliability:.0f}%`",
        f"- Failure histogram: `{json.dumps(summary.reasons, sort_keys=True)}`",
        "",
        "## Turns",
    ]
    for turn in summary.turn_results:
        lines.append(
            f"- turn {turn.turn:02d} expect_tool={str(turn.expect_tool).lower()} "
            f"status={'ok' if turn.ok else 'fail'} reason={turn.reason}"
        )
    return "\n".join(lines) + "\n"


def render_json(summary: RunSummary) -> str:
    payload = {
        "target": summary.target,
        "model": summary.model,
        "run_shape": "context-growing, tool-using loop",
        "turns": summary.turns,
        "usable": summary.usable,
        "reliability": summary.reliability,
        "failure_histogram": summary.reasons,
        "turn_results": [dataclasses.asdict(turn) for turn in summary.turn_results],
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def write_report(summary: RunSummary, *, markdown_path: Path | None, json_path: Path | None) -> None:
    if markdown_path is not None:
        markdown_path.write_text(render_markdown(summary), encoding="utf-8")
    if json_path is not None:
        json_path.write_text(render_json(summary), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", choices=["direct", "proxy"], default="proxy")
    ap.add_argument("--turns", type=int, default=6)
    ap.add_argument("--markdown-report", type=Path)
    ap.add_argument("--json-report", type=Path)
    args = ap.parse_args(argv)

    summary = run(args.target, args.turns)
    print(f"\ntarget={summary.target} model={summary.model} runs={summary.turns} reliability={summary.reliability:.0f}%")
    print("failure histogram:", json.dumps(summary.reasons, sort_keys=True))
    write_report(summary, markdown_path=args.markdown_report, json_path=args.json_report)
    if args.markdown_report or args.json_report:
        print("report written")
    return 0


if __name__ == "__main__":
    sys.exit(main())
