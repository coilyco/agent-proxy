---
ward:
  workflow: merge-remote-main
---
# Agent instructions - agent-proxy

## Scope

Agent Proxy is the **observation, trajectory collection, and data-processing plane** for the agentic operations stack. LiteLLM is the commodity inference gateway beneath that plane. The current reliability proxy is a valuable first collection tap and remains in service until LiteLLM parity is proven.

Read before changing anything here:

- [`README.md`](README.md) explains the current product charter and what is implemented today.
- [`docs/architecture-v2.md`](docs/architecture-v2.md) defines ownership boundaries and the migration inventory.
- [`docs/trajectory-contract-v1.md`](docs/trajectory-contract-v1.md) is the producer and consumer contract for trajectory events.
- [`docs/ROADMAP.md`](docs/ROADMAP.md) and [`docs/work-graph.md`](docs/work-graph.md) define the dependency-ordered implementation work.

The aosh build documents remain evidence for the delivered reliability behavior. They are not a prohibition on the v2 work in this repository.

## Project shape

- **`app/`** - the service. Request path, resilience policy, upstream clients, body capture, and the `app/trajectory/` package for storage, materialization, evaluation, and export.
- **`docs/`** - the design and contract documents listed above.
- **`tests/`** - pytest suites mirroring the `app/` modules.
- **`scripts/`** - tracked programs invoked by workflows and Ward verbs, never inlined into a workflow step.

## Repo boundaries

- LiteLLM owns provider integration, routing, retries, fallbacks, keys, budgets, and inference cost accounting after parity is accepted.
- Agent Proxy owns identity, policy, correlation, context safety, cheap structural detection, event emission, ingestion, durable raw retention, replay, trajectory assembly, evaluation joins, and dataset materialization.
- Ward owns authorization, execution, lifecycle, recovery, and governance. Agent Proxy must not become an execution authority.
- SigNoz and OTLP are operational evidence surfaces. They are not the sole durable trajectory store.
- Keep expensive processing, materialization, evaluation, and export work off the latency-sensitive model request path.

## Commands

Route development commands through the [`justfile`](justfile). Use `just <verb>` rather than bare `uv`, `pytest`, or `docker`.

There is no Makefile. Each recipe runs its command directly through uv.

## Validation

- `just format-check`, `just lint`, `just typecheck`, and `just test` are the offline gates.
- `just pre-commit` runs the catalog suite over all tracked files. Never pass `--no-verify`.
- A fresh clone has no hooks in `.git/hooks`. Run `just pre-commit-install` once per clone before relying on the commit-time gate.
- `just test-container` needs a Docker daemon. `just boot-probe` is the daemonless equivalent.

## Safety

- Opaque ids, tokens, and tailnet FQDNs go in AWS SSM, never in tracked files. Resolve at runtime. The tower FQDN is `/coilysiren/kai-tower-3026/tailnet-fqdn`.
- Body capture is opt-in and defaults off. When it is on, structured logs and span attributes carry complete model I/O, so the configured sink is governed as restricted model content.
- Do not delete or weaken the current reliability behavior until issue #41 demonstrates LiteLLM parity.

## Cross-repo contracts

- **coilyco-bridge/deploy** deploys this service to ser8 and owns the mounted route registry. It references this repository without this repository referencing it.
- **coilyco-bridge/agentic-os-hardware#36** is the canonical companion architecture decision. Keep this repository aligned with it.
- [`docs/trajectory-contract-v1.md`](docs/trajectory-contract-v1.md) is the producer and consumer contract other services build against.

## Release

This file's frontmatter declares `workflow: merge-remote-main`. Canonical history lives on Forgejo.

Do not claim an architecture-v2 component is landed until code and verification land, and keep [`docs/FEATURES.md`](docs/FEATURES.md) current when a shipped capability changes.

## Agent rules

<!-- BEGIN managed by agentic-os/scripts/apply-git-workflow.py -->
### Git workflow

**This repo runs the `merge-remote-main` lane**, declared as `ward.workflow` in this file's frontmatter. The agent commits, pushes straight to `main`, and closes the issue. Pushing `main` here is the expected path, not an escalation.

The fleet runs two lanes, and both authorize the same core actions:

* `merge-remote-main` - the agent commits, pushes to `main`, and closes the issue. No branch and no pull request.
* `pull-request-and-merge` - the agent commits to a task branch, pushes it, opens a pull request, and merges that pull request itself once it is green.

**Every lane slug names what the AGENT does, never what someone else does.** `pull-request-and-merge` carries the merge because the agent that authored the code merges its own pull request. `pull-request` drops `-and-merge` because the author stops at the pull request and the director merge lane takes over. Reading `pull-request-and-merge` as "someone else merges it later" inverts the two lanes and leaves finished work sitting unmerged.

**These actions are pre-authorized on every lane, and the agent MUST take them without asking first.** Committing, creating a branch, pushing a branch, pushing the lane's own destination, and opening a pull request are ordinary reversible work, not the destructive wall that earns a question. Stopping to ask is how a turn ends with the work stranded in a dirty worktree.

* **ALWAYS commit** in-scope work and **ALWAYS push** it to the canonical remote before pausing, reporting a checkpoint, handing off, or ending a turn. A local-only commit is not a checkpoint.
* **ALWAYS open the pull request** in the same turn as the branch's first push, on every lane except `remote-branch-only`. A pushed branch with no pull request is litter nobody reviews.
* **NEVER `--no-verify`** and **NEVER force-push**. Those two are the real walls, and they stay closed.
* **ALWAYS merge your own pull request on `pull-request-and-merge`**, in the same turn, as soon as it is green. Reporting it as open and awaiting someone is the failure this lane exists to prevent.
* **NEVER merge on `pull-request` or `remote-branch-only`.** Those two stop where they stop, and the director merge lane carries a `pull-request` from there.
<!-- END managed by agentic-os/scripts/apply-git-workflow.py -->

- Python, FastAPI or Starlette async, httpx, and uvicorn or Hypercorn remain appropriate for the I/O-bound Agent Proxy surface.
- Preserve the OpenAI-compatible surface while LiteLLM parity is evaluated.
- She/her for Kai in every artifact. No em dashes or semicolons in prose. Use bold anchors and flat bullets over prose tables.
- Name the actor in action sentences.

## Checkout residency

This repo is not in Agent Compose's `repository-plan.yaml`, so it has no
resident checkout under `~/projects/<owner>/`. That is intentional. Work it
from a task-scoped temporary clone, and remove that clone once the work lands.

A temporary root can be purged at any time, so commit and push before pausing,
switching tasks, or ending a session. The remote is the only durable artifact.

## See also

- [README.md](README.md) - human-facing intro and current charter.
- [docs/FEATURES.md](docs/FEATURES.md) - inventory of what ships today.
- [.ward/ward.yaml](.ward/ward.yaml) - allowlisted commands and catalog metadata.
