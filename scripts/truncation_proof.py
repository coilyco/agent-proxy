#!/usr/bin/env python3
"""Reproduce the leg-01 truncation test through the proxy (leg 04 local validation).

Sends a ~55k-token prompt direct to the tower and through the proxy and compares
``prompt_eval_count``. Run it as ``TOWER=<host> MODEL=qwen3-coder:30b uv run
python scripts/truncation_proof.py`` with the proxy already running locally.
Expected numbers and why: docs/proxy-num-ctx.md.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request

from _endpoints import proxy_base_url, tower_base_url

# The real ollama tag to prove against, passed straight through the proxy.
MODEL = os.environ.get("MODEL", "qwen3-coder:30b")


def _big_prompt(nlines: int = 6000) -> str:
    lines = [
        f"Line {i}: the quick brown fox jumps over the lazy dog {i} alpha beta gamma delta."
        for i in range(nlines)
    ]
    return "\n".join(lines) + "\n\nAfter reading all the lines above, reply with exactly: OK"


def _post(url: str, body: dict) -> dict:
    req = urllib.request.Request(
        url, json.dumps(body).encode(), {"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=600) as resp:
        return json.load(resp)


def main() -> int:
    prompt = _big_prompt()
    tower = tower_base_url()
    proxy = proxy_base_url()

    direct = _post(
        f"{tower}/v1/chat/completions",
        {
            "model": MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 16,
        },
    )
    direct_kept = direct["usage"]["prompt_tokens"]

    through = _post(
        f"{proxy}/v1/chat/completions",
        {
            "model": MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 1024,
        },
    )
    proxy_kept = through["usage"]["prompt_tokens"]

    print(f"direct tower /v1 (no num_ctx): prompt_eval_count = {direct_kept}  <- the 32k cliff")
    print(f"through proxy {MODEL}: prompt_eval_count = {proxy_kept}  <- num_ctx injected")

    ok = direct_kept <= 32768 and proxy_kept > 32768
    print("PASS - truncation removed" if ok else "FAIL - unexpected counts")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
