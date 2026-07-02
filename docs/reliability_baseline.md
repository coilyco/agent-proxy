# Reliability Test Results - M2 Measurement

## Status

The leg-05 harness now produces a durable markdown or JSON report, and the
offline scoring path is covered by tests. The live tower measurement for the
M2 before/after comparison still needs a run with tower reachability and SSM
credentials.

## Harness shape

- Target `direct` - tower `/v1` with no injected `num_ctx`
- Target `proxy` - local proxy `fast-think`
- Run shape - context-growing, tool-using loop
- Default turns - `6`
- Scoring - `app.resilience.validate_response`

## Report output

`scripts/reliability_loop.py` accepts:

- `--markdown-report <path>`
- `--json-report <path>`

The JSON report includes turn-by-turn scores, reliability percentage, and the
failure histogram for reproducible before/after comparison.

## Live result

Pending a tower-connected run.
