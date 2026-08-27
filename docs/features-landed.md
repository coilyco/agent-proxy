# Landed capabilities

Part of [FEATURES](FEATURES.md).

## Landed reliability collection tap


- **OpenAI-compatible request surface** - landed - `/v1/chat/completions`, `/v1/completions`, and `/v1/models`, including streaming and normalized reasoning content.
- **Remote MCP prompt surface** - landed - stateless Streamable HTTP at `/mcp`
  exposes model discovery and non-streaming prompt tools through the existing
  Agent Proxy policy, reliability, telemetry, and trajectory path. See
  [mcp.md](mcp.md).
- **Logical route registry** - landed - strict Deploy-mounted service and
  evaluation aliases hide physical backends from governed clients, route
  aliases through LiteLLM, and fail closed when direct rollback cannot serve a runtime. See
  [route-registry.md](route-registry.md).
- **Backend-derived context safety** - landed - safe `num_ctx` derivation and injection, `OLLAMA_NUM_PARALLEL` compensation, context-budget trimming, and loud delivered-context truncation detection.
- **Current gateway resilience** - landed - bounded in-memory queue and workers, queue backpressure, structural response validation, retry with backoff, fallback chains, and per-backend circuit breakers. Validation rejects only structurally broken output (empty, unparsable tool arguments, truncation garbage, degenerate repetition); it does not judge the meaning of assistant text.
- **Operational evidence** - landed - trace-correlated structured JSON logs, Prometheus metrics, OpenTelemetry traces, closed-set SigNoz exception events for every handled runtime failure under a bounded 13-code taxonomy with stage tags, Sentry initialization, request spans, and Ollama final-response token plus phase-duration measurements for streaming and non-streaming requests.
- **Stream accounting instead of chunk spans** - landed - a streamed completion
  emits no per-SSE-chunk `http send` span. Frame count, bytes, total duration,
  and first-token latency ride on the request span instead, so a trace holding a
  streamed turn stays under the backend's per-trace span cap and still renders
  the work an operator opened it for. See
  [stream-accounting.md](stream-accounting.md).
- **Opt-in complete model I/O capture** - landed - complete normalized request
  and response bodies for non-streaming chat, reconstructed streaming chat,
  text completions, and MCP prompt calls are written to paired structured events
  and request-span attributes. Capture defaults off, fails hard on field loss,
  and is enabled on ser8 against restricted SigNoz storage. See
  [issue #77](https://forgejo.coilysiren.me/coilyco-flight-deck/agent-proxy/issues/77).
- **Ward correlation** - landed - request, Ward run, workflow, repository, issue, and agent-session metadata joins in logs and spans.
- **Skill-use artifact observation** - landed - Ward reap `skill-usage.json`
  parsing durably retains metadata-only skill observations with run and
  engineering correlations while preserving structured logs and the
  `ward_skill_use_total` Prometheus counter.


- **LiteLLM parity decision and runner** - landed - a machine-readable comparison selects standalone LiteLLM, while an executable endpoint probe gates model discovery, chat shape, streaming, finish reasons, usage, and error mapping.
- **Authenticated LiteLLM inner-gateway client** - landed - mounted-file bearer authentication, service-key model filtering, tower-backed context metadata, safe top-level `num_ctx`, OpenAI option translation, and body-safe Ward correlation support a standalone LiteLLM hop without weakening Agent Proxy policy or trajectory ownership.
- **Provider prompt-cache accounting** - landed - the proxy normalizes DeepSeek,
  OpenAI-compatible, and Anthropic-style cache usage into the response usage
  block, spans, Prometheus counters, and the trajectory ledger, and keeps a
  provider that reports nothing distinguishable from a measured cache miss. It
  does not inject cache breakpoints or own a caching policy. See
  [proxy-prompt-cache.md](proxy-prompt-cache.md).
- **Agent-compose trajectory ingestion** - landed - a cold-path adapter maps the
  immutable manifest and public-safe decision trace into actor, artifact, and
  observation events without copying the opaque context tree or granting
  execution authority. See [agent-compose-ingestion.md](agent-compose-ingestion.md).
- **Guard trajectory ingestion** - landed - cold-path adapters map cli-guard
  audit rows into action, policy, and execution events, and hash specgen
  guardfiles and locks into linked policy evidence without retaining sensitive
  argv, diagnostics, paths, or hosts. See [guard-ingestion.md](guard-ingestion.md).
- **Runtime and delivery checks** - landed - SSM-backed configuration, local
  `/healthz`, metrics-only non-generating route readiness, `/metrics`,
  daemonless boot probing, container probing, and a reliability harness. Route
  readiness verifies authenticated LiteLLM control surfaces for hosted routes
  and adds Ollama catalog checks only when the registry declares local physical
  targets. It does not claim that GPU execution or completion validity was
  proven. See [readiness.md](readiness.md).

- **Versioned trajectory schema package** - landed - Pydantic producer and consumer validation, stable canonical serialization, compatibility fixtures, and a committed JSON Schema implement contract v1. Durable intake is the separate landed retention capability below.
- **Append-only trajectory retention** - landed - an internal cold-path API validates and idempotently commits raw contract-v1 envelopes to SQLite, retains duplicate and quarantine receipts, blocks mutation with database triggers, and replays into fresh consumers. A bounded emitter keeps storage waits off the model hot path.
- **Request lifecycle trajectory emission** - landed - an opt-in hot-path tap
  offers metadata-only model actions and terminal execution outcomes to the
  bounded emitter without waiting for storage or retaining request and response
  bodies. Deployment enablement remains planned with durable storage.
- **Episode and trajectory materialization** - landed - deterministic connected-component assembly preserves every correlation dimension, orders events, exposes partial and late state, records retries, fallbacks, and human interventions, and appends content-hashed derived revisions.
- **Evaluation and annotation records** - landed - automatic evaluations, verifiers, human annotations, and interventions join to stable trajectories with immutable evidence, supersession, disagreement, late-arrival, replay, redaction, and access-tier semantics.
- **Versioned dataset exports** - landed - SFT, preference, verifier, reward, and held-out evaluation schemas produce write-once manifests with source provenance, content hashes, deterministic trajectory-level splits, reproducibility, and opt-in restricted body references.
- **Operational and governance views** - landed - internal reliability, cost and latency, policy, evaluation, harness-fit, and skill-fit query contracts join durable trajectories to OTLP context, enforce access tiers, publish freshness and reconstruction limits, and provide evidence-only Ward dossier inputs. The read-only `agent-proxy-query` helper filters and joins those views for repository-owned investigation, harness-fit, and skill-use skills. Skill-fit preserves skill identity through materialization and keeps selection claims and observed use as separate facts.
