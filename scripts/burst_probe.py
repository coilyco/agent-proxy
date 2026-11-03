#!/usr/bin/env python3
"""Concurrency burst probe: does Agent Proxy queue, shed, or neither (issue #107).

The client half of the burst run that separates the three explanations #105
could not. It issues real model calls and costs real tokens, so it refuses to
start without ``--yes``. Read docs/burst-probe.md before running it, and do not
run it while the rate limiter is enabled.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

# The prompt should not add variance or cost. This measures admission, not
# generation. Method: docs/burst-probe.md.
PROMPT = "hi"
MAX_TOKENS = 1


@dataclass
class Attempt:
    level: int
    round_index: int
    status: int | None
    seconds: float
    retry_after: str | None = None
    error: str = ""

    @property
    def admitted(self) -> bool:
        return self.status is not None and 200 <= self.status < 300

    @property
    def shed(self) -> bool:
        return self.status in (429, 503)


@dataclass
class LevelResult:
    level: int
    attempts: list[Attempt] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        latencies = sorted(a.seconds for a in self.attempts if a.admitted)
        shed = [a for a in self.attempts if a.shed]
        return {
            "concurrency": self.level,
            "issued": len(self.attempts),
            "admitted": sum(1 for a in self.attempts if a.admitted),
            "shed": len(shed),
            "shed_statuses": sorted({a.status for a in shed if a.status is not None}),
            "retry_after_present": any(a.retry_after for a in shed),
            "errors": sum(1 for a in self.attempts if a.error),
            "admitted_min_s": round(latencies[0], 4) if latencies else None,
            "admitted_median_s": round(statistics.median(latencies), 4) if latencies else None,
            "admitted_max_s": round(latencies[-1], 4) if latencies else None,
        }


async def _one(client: httpx.AsyncClient, url: str, model: str, level: int, index: int) -> Attempt:
    body = {
        "model": model,
        "messages": [{"role": "user", "content": PROMPT}],
        "max_tokens": MAX_TOKENS,
        "stream": False,
    }
    started = time.monotonic()
    try:
        response = await client.post(url, json=body)
    except Exception as exc:  # a refused or reset connection is a real result
        return Attempt(level, index, None, time.monotonic() - started, error=type(exc).__name__)
    return Attempt(
        level,
        index,
        response.status_code,
        time.monotonic() - started,
        retry_after=response.headers.get("retry-after"),
    )


async def _burst(client: httpx.AsyncClient, url: str, model: str, level: int) -> list[Attempt]:
    return list(await asyncio.gather(*(_one(client, url, model, level, i) for i in range(level))))


async def run(args: argparse.Namespace) -> dict[str, Any]:
    url = f"{args.base_url.rstrip('/')}/v1/chat/completions"
    results: list[LevelResult] = []
    timeout = httpx.Timeout(args.timeout, connect=5.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        for level in args.levels:
            result = LevelResult(level=level)
            for round_index in range(args.repeats):
                result.attempts.extend(await _burst(client, url, args.model, level))
                if round_index + 1 < args.repeats:
                    await asyncio.sleep(args.settle)
            results.append(result)
            print(json.dumps(result.summary()), flush=True)
            await asyncio.sleep(args.settle)
    return {
        "base_url": args.base_url,
        "model": args.model,
        "repeats": args.repeats,
        "levels": [r.summary() for r in results],
    }


def _verdict(levels: list[dict[str, Any]]) -> str:
    """Read the decision table in issue #107 off the observations."""
    any_shed = any(level["shed"] for level in levels)
    medians = [level["admitted_median_s"] for level in levels if level["admitted_median_s"]]
    rose = bool(medians) and medians[-1] > medians[0] * 2

    if any_shed:
        return (
            "SHED: requests were rejected. Confirm the rate limiter was disabled before "
            "reading this as admission control - see docs/burst-probe.md."
        )
    if rose:
        return (
            "QUEUED or SERIALISED: no rejections and latency rose with concurrency. "
            "Read queue.wait per trace to tell admission delay from upstream contention."
        )
    return (
        "NEITHER OBSERVED: no rejections and no latency rise. Consistent with no admission "
        "control at this layer, and also with a backend that absorbed the burst."
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True, help="e.g. http://ser8:8080")
    parser.add_argument("--model", required=True, help="logical route key")
    parser.add_argument("--levels", type=int, nargs="+", default=[1, 5, 10, 30])
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--settle", type=float, default=5.0, help="seconds between rounds")
    parser.add_argument("--timeout", type=float, default=240.0)
    parser.add_argument("--out", default="", help="write the full JSON record here")
    parser.add_argument(
        "--yes",
        action="store_true",
        help="required: this issues real model calls and costs real tokens",
    )
    args = parser.parse_args(argv)

    if not args.yes:
        total = sum(args.levels) * args.repeats
        parser.error(
            f"this would issue {total} real model calls against {args.base_url}. "
            "Re-run with --yes once the GPU is idle and the rate limiter is off."
        )

    record = asyncio.run(run(args))
    record["verdict"] = _verdict(record["levels"])
    print(json.dumps(record, indent=2))
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            json.dump(record, handle, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
