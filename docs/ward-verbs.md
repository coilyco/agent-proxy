# Verb notes

Rationale for the recipes in the [`justfile`](../justfile). Each recipe carries
a one-line description, and this page holds the reasoning that does not fit
there.

## Validation verbs

`format-check`, `lint`, `typecheck`, and `test` are the offline gates. They need
no tower and no Docker daemon.

`pre-commit` runs the catalog hook suite over every tracked file.
`pre-commit-install` wires the pre-commit and pre-push hooks into a clone. A
fresh clone has neither, so the commit-time gate does not run until someone
installs it (agent-proxy#78).

## Container acceptance (agent-proxy#24)

`test-container` builds the image and asserts it boots and serves `/healthz`,
`/v1/models`, and `/metrics`. It needs a Docker daemon.

`boot-probe` validates the same boot path against the frozen runtime deps with
no daemon. It is the check that runs inside a daemonless container.

## Daemonless smoke (agent-proxy#31)

`smoke` asserts the runtime deps are a proper PEP 621 array, every runtime dep
is locked, and the app imports clean under runtime-only deps. No daemon and no
tower, so the CI gate runs it too.

## Tower-dependent verbs

`proof` proves the 32k truncation cliff is gone through the proxy (leg 04 local
validation). It needs the tower reachable through `TOWER=<host>` or
`PROXY_TOWER_BASE_URL`, and `just serve` already running.

`reliability` is the leg-05 harness. It scores a context-growing, tool-using
loop and reports a reliability percentage plus a failure histogram. Pass args
after `--`, for example:

```bash
just reliability --target both --turns 6 --json out.json
```

It needs the tower reachable, and `just serve` running for the `proxy` target.

## Security policy

`security:` is empty because agent-proxy declares no protected-binary routing of
its own. The container acceptance path uses `docker` directly by design.

`ward doctor` (ward#450) treats an empty `security:` as undeclared and also runs
a Makefile drift check, so it does not fully pass here. This repo has no
Makefile and runs its tooling through uv, matching
[`.forgejo/workflows/ci.yml`](../.forgejo/workflows/ci.yml), which invoked the
same commands through raw `uv run` because the CI runner had no ward. Forgejo
runners retired on 2026-11-01 and this repo has no GitHub workflow yet.

`just <verb>` needs neither, so it is the supported entry point.

## See also

- [`justfile`](../justfile) - the recipes themselves.
- [AGENTS.md](../AGENTS.md) - which verbs agents are expected to run.
