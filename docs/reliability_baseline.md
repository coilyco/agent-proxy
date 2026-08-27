# Reliability measurement - M2 baseline-to-after

How the M2 reliability measurement is taken, what the harness emits, and how to
read the result without overclaiming.

The durable record for the M2 milestone number: the reliability harness
(`scripts/reliability_loop.py`, leg 05) run against both targets, scored by the
proxy's own `validate_response`, producing a reliability percentage and a
failure histogram for each. This file is the human record; the harness's
`--json` artifact is the machine-readable companion.

## Measurement status

**PENDING - not yet measured against the tower.** The harness, its durable JSON
artifact, and the offline scoring tests all landed under agent-proxy#19, but the
engineer container that did that work had **no tailnet reachability to the
tower** (no `tailscaled`, no `WARD_TOWER_OLLAMA` bridge, no SSM credentials), and
both targets require a live tower LLM. So the numbers below are deliberately left
unfilled rather than fabricated - recording an invented percentage is exactly the
failure this milestone exists to fix. Taking the measurement is a single
reproducible command (below) from any tower-reachable host.

Do **not** copy a number into the results table from anywhere but a real harness
run's output or its `--json` artifact.

## How to take the measurement

From a host with the tower reachable, with the proxy running for the `proxy`
target:

```bash
# 1. start the proxy (a second shell); resolves the tower FQDN from SSM if unset
PROXY_TOWER_BASE_URL=http://<tower>:11434 just serve

# 2. run both targets in one pass and write the durable artifact
TOWER=<tower> just reliability --target both --turns 6 --json docs/reliability_m2.json
```

`just reliability` runs `scripts/reliability_loop.py`; bare
`just reliability` also works via ward's unknown-verb fallback. Args ride after
`--`. The tower FQDN is resolved at runtime and never written into the artifact
or this file.

## Run shape

- **Turns**: 6 (default; `--turns N` to change). Odd turns must call the
  `get_line_count` tool; even turns ask for a one-line summary.
- **Context growth**: each turn appends a ~1200-line file-shaped blob (~8k
  tokens), so the accumulated prompt crosses the 32k default `num_ctx` cliff
  within a couple of turns - the condition that produces silent truncation on
  the direct target.
- **Scoring**: `app.resilience.validate_response` decides usable vs garbage
  (empty, malformed tool call, truncation garbage, degenerate repetition), plus
  a harness-only `missed_toolcall` rule for a turn that ignored the tool
  contract. The harness rule is sound where the proxy-side one was not: the
  harness knows out-of-band that the turn required a tool call.
- **Targets**:
  - `direct` - tower `/v1` (`qwen3-coder:30b`), no `num_ctx` (the opencode/crush shape).
  - `proxy` - local proxy, same real tag (`qwen3-coder:30b`), derived `num_ctx`
    injected (issue #32: the `fast-think` alias was retired).

## Results

Fill from the harness output / `--json` artifact of a real run. One row per
target; the histogram sums to the turn count.

| Target | Model           | Turns | Usable | Reliability | Failure histogram |
|--------|-----------------|-------|--------|-------------|-------------------|
| direct | qwen3-coder:30b | _pending_ | _pending_ | _pending_ | _pending_ |
| proxy  | qwen3-coder:30b | _pending_ | _pending_ | _pending_ | _pending_ |

Baseline-to-after delta (proxy reliability minus direct reliability): _pending_.

### Failure histogram reasons

The reason labels the histogram can contain, all emitted by the scoring path:

- `ok` - usable response.
- `empty` - no content, no tool call, no thinking.
- `missed_toolcall` - text validated but the required tool call was absent.
- `malformed_toolcall` - a tool call whose arguments do not parse.
- `truncation_garbage` - a 1-3 char non-word reply (the leg-01 truncation tell).
- `repetition` - degenerate decoder loop.
- `upstream_5xx` / `http_<code>` - backend HTTP error.
- `timeout` - transport error or timeout reaching the endpoint.

## Artifact schema

`--json PATH` writes:

```json
{
  "harness": "reliability_loop",
  "generated_at": "<iso8601, seconds>",
  "run_shape": {"turns": 6, "blob_lines": 1200, "tool_rule": "...", "scored_by": "..."},
  "results": {
    "direct": {"target": "...", "model": "...", "turns": 6, "usable": N,
               "reliability_pct": P, "failure_histogram": {...}, "turns_detail": [...]},
    "proxy":  { ... }
  }
}
```

The schema is stable across runs and the histogram is sorted, so two artifacts
diff cleanly for a before/after check. Commit the artifact (e.g.
`docs/reliability_m2.json`) alongside the filled table above when the
measurement is taken.

## Interpreting it

M2 ("resilience core measured") is complete when this table carries real numbers
and the delta shows the proxy lifting reliability over the direct baseline. Until
then M2 is **harness-ready, measurement-pending**: the code, artifact, and tests
exist and are proven offline, but the milestone number is not yet recorded.
