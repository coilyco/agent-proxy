# Agent Proxy

The observation, trajectory collection, and data-processing plane for the
agentic operations stack. It sits on the model request path, protects it with
Agent Proxy-specific policy and safety behavior, and turns operational work into
trustworthy trajectory evidence. LiteLLM is the intended commodity inference
gateway underneath it.

**This repository is in transition.** Treat its interfaces as unstable. The
OpenAI-compatible reliability proxy remains the first collection tap, and it now
supports a standalone LiteLLM Proxy as an authenticated inner gateway while
retaining direct tower routing as the rollback. The
[parity decision](docs/litellm-parity.md) keeps the current queueing, retry,
fallback, and circuit behavior until joined live evidence proves which
responsibilities can move.

## The split that matters

Heavy data processing stays off the latency-sensitive request path.

- **Hot path** owns identity, policy, correlation, context safety, cheap
  structural detectors, and asynchronous event emission. It never blocks on
  storage.
- **Cold path** owns ingestion, normalization, durable raw retention, replay,
  trajectory assembly, evaluation joins, and dataset materialization.

Around it, **LiteLLM** will own provider protocols, routing, retry, fallback,
keys, budgets, and cost accounting. **Ward** owns authorization, execution,
lifecycle, and governance, so Agent Proxy supplies evidence and never becomes an
execution authority. **SigNoz and OTLP** carry operational traces, logs, and
metrics, and are deliberately not the durable training-data store.

Target architecture and migration dispositions in
[docs/architecture-v2.md](docs/architecture-v2.md). Independent producers and
consumers implement against
[docs/trajectory-contract-v1.md](docs/trajectory-contract-v1.md).

## Model I/O capture is opt-in and all-or-nothing

Body capture defaults off. When enabled, Agent Proxy captures the complete
normalized request and response body for every routed model call. Complete means
every model I/O field present at the boundary. There is no selected-field or
request-only mode, and enabled capture fails hard rather than returning success
after field loss.

Agent Proxy owns that capture so callers and wrappers do not duplicate prompt,
response, or tool payloads in their own logs. Transport credentials and
hop-by-hop headers are not model I/O and are never included.

**Turning it on changes what your sinks hold.** With capture off, application
logs, OTLP spans, and SigNoz stay metadata-only. With it on, complete bodies
reach structured logs and trace attributes, so any receiving sink needs the
restricted controls appropriate for model content. Log events, fields, pairing
keys, and the SigNoz viewing flow are in
[docs/proxy.md](docs/proxy.md#signoz-content-viewing-contract).

## The request path

1. A client sends a Deploy-owned logical `<namespace>/<alias>` key plus Ward
   correlation metadata.
2. Agent Proxy validates the route, applies context safety and cheap structural
   checks, and emits operational evidence.
3. The reliability gateway queues and dispatches through the configured inner
   gateway, either direct tower access or authenticated standalone LiteLLM.
4. Agent Proxy returns the normalized response and emits bounded request
   evidence.
5. Cold-path workers durably ingest and materialize that evidence, never
   blocking the response.

## What exists today

An OpenAI-compatible `/v1/chat/completions`, `/v1/completions`, `/v1/embeddings`
(local Ollama only), and `/v1/models` surface, plus a stateless Streamable HTTP MCP surface at `/mcp` sharing the same
policy and evidence path. Underneath it: a bounded in-memory worker queue,
response validation, retry, fallback, per-backend circuit breaking, context-budget
protection, and non-generating readiness that checks control surfaces without
loading model weights.

On the evidence side: an executable trajectory contract with producer and
consumer validation, append-only SQLite ingestion with idempotent receipts and
replay, deterministic episode reconstruction with provenance and content hashes,
immutable evaluation and annotation records with supersession semantics, and
reproducible dataset exports with write-once manifests and trajectory-level
leakage prevention.

[docs/FEATURES.md](docs/FEATURES.md) is the complete current inventory, and
planned v2 components are marked planned there rather than implied here.

## Development

```sh
just sync
just test          # unit suite, tower not required
just lint typecheck format-check
just pre-commit    # full validation over every tracked file
just serve         # 127.0.0.1:8080
```

A fresh clone has no hooks wired, so run `just pre-commit-install` once per
clone to get the pre-commit and pre-push gates. `PROXY_HOST`, `PROXY_PORT`, and
`LOG_LEVEL` override the defaults.

`just test-container` needs a Docker daemon and probes `/healthz`, `/v1/models`,
and `/metrics` against the built image. `just boot-probe` is the daemonless
equivalent. `just trajectory-query --help` reads governed evidence without
mutating anything.

The MCP endpoint and remote connector setup are in [docs/mcp.md](docs/mcp.md). A
deployment must allowlist its public hostname with `PROXY_MCP_ALLOWED_HOSTS` and
put authenticated ingress in front of `/mcp` before exposing it.

## License

MIT. Kai Ase Siren holds the copyright. See [LICENSE](LICENSE).

## See also

- [AGENTS.md](AGENTS.md) - agent instructions, ownership boundaries, conventions.
- [docs/FEATURES.md](docs/FEATURES.md) - inventory of what ships today.
- [docs/architecture-v2.md](docs/architecture-v2.md) - target stack, data boundaries, privacy model.
- [docs/trajectory-contract-v1.md](docs/trajectory-contract-v1.md) - the versioned event envelope.
- [docs/litellm-parity.md](docs/litellm-parity.md) - the standalone decision and cutover blockers.
- [docs/ROADMAP.md](docs/ROADMAP.md) - sequencing, without treating future work as prohibited.
- [justfile](justfile) - every dev verb, and `just` alone lists them.
