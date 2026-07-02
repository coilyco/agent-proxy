# The reliability proxy (phase 1)

This is the walkthrough for the phase-1 reliability proxy built per aosh leg
`04-headless-proxy-build.md` against the locked leg-02 architecture. It covers
the request path, the `app/` modules, configuration, how to run it, and how to
prove the core `num_ctx` fix. The design is locked upstream and not re-argued
here - see the source-of-truth pointers in the README.

## Request path

A harness sends an OpenAI-shaped request to a **logical** model name. The proxy:

1. **resolves** the logical name (`fast-think`, `fast`, `ctx-think`, `ctx`,
   `tune`) to a per-model safe `num_ctx` and an ordered fallback chain
   (`app/models.py`).
2. **guards the context budget** (`app/analysis.py`): counts prompt tokens and,
   if the prompt exceeds `num_ctx - headroom`, trims the oldest non-system turns,
   always keeping the system framing and the live turn. Increments
   `llm_truncation_avoided_total` when it actually drops a turn.
3. **enqueues** the job on a bounded `asyncio.Queue` and awaits its future
   (`app/queue.py`). A full queue returns HTTP 429 (`llm_queue_depth`,
   `llm_queue_rejected_total`).
4. a **worker** dispatches under the resilience policies (`app/resilience.py`):
   walk the fallback chain, retry each live backend with backoff, and validate
   every response. Transport errors trip a per-backend circuit breaker; a merely
   bad generation is rerolled but does not.
5. the **upstream client** (`app/upstream.py`) forwards to the backend's native
   ollama `/api/chat` with `options.num_ctx` injected - the caller can never
   override it.
6. the result is shaped back to the OpenAI schema (`app/main.py`). Reasoning-model
   thought is surfaced as `reasoning_content`.

Streaming requests take the same fallback chain and circuit breaker but skip the
reroll (a token stream cannot be validated after the fact), so a harness that
wants the full resilience guarantee uses the non-streaming path.

## Endpoints

* `POST /v1/chat/completions` - streaming and non-streaming.
* `POST /v1/completions` - modeled as a single user turn so it rides the same
  resilience path.
* `GET /v1/models` - lists the logical names.
* `GET /healthz` - liveness for Caddy / k8s probes.
* `GET /metrics` - prometheus exposition.

## Validation

A response is *usable* when it is non-empty, any emitted tool call has parseable
arguments, and it is not degenerate repetition. Two deliberate refinements,
both surfaced by live testing against the tower:

* a legitimately short word answer (`OK`, `42`, `no`) is **not** truncation
  garbage - only a 1-3 char *non-word* reply (a stray symbol) is.
* a reasoning model that emitted `thinking` but ran out of token budget before
  final content did real work - it is surfaced as a length-limited response, not
  rerolled into a 502.

## Configuration

All settings read from the environment with prefix `PROXY_` and fall back to AWS
SSM for the tower FQDN and secrets (`app/config.py`). Nothing is hardcoded and no
secret is committed. Key knobs:

* `PROXY_TOWER_BASE_URL` - the primary ollama base URL. If unset, the tower FQDN
  resolves from SSM `/coilysiren/kai-tower-3026/tailnet-fqdn` at boot.
* `PROXY_MODELS_JSON` / `PROXY_MODELS_FILE` - override the logical-model table (a
  ConfigMap later) without touching code.
* `PROXY_WORKER_COUNT`, `PROXY_QUEUE_MAXSIZE` - queue / worker sizing.
* `PROXY_MAX_RETRIES`, `PROXY_CIRCUIT_FAIL_THRESHOLD`, `PROXY_CIRCUIT_COOLDOWN` -
  resilience knobs.
* `PROXY_SENTRY_DSN`, `PROXY_OTEL_EXPORTER_OTLP_ENDPOINT` - observability. Both
  degrade to no-ops when unset.

### Per-model num_ctx

`fast-think` (Qwen3-A3B) defaults to **49152** (proven safe, leg 01); every other
logical model defaults to a conservative **32768**. These are placeholders until
the leg-03 benchmark locks per-model values, and a deploy overrides them via the
model-table config above.

## Running and proving locally

```
ward sync                                             # uv sync (installs app + dev)
PROXY_TOWER_BASE_URL=http://<tower>:11434 ward serve  # proxy on 127.0.0.1:8080
ward test                                             # 26 tests, tower not required

# with the proxy running and a tower reachable:
TOWER=<tower> ward exec proof                          # 32767 (direct) vs 49151 (proxy)
TOWER=<tower> ward exec reliability -- --target proxy --turns 6
```

`scripts/truncation_proof.py` reproduces the leg-01 truncation test through the
proxy. `scripts/reliability_loop.py` is the leg-05 harness scaffold: it scores a
context-growing, tool-using loop with the proxy's own `validate_response` and
emits a reliability percentage and failure histogram. It can also write a
durable markdown or JSON report with `--markdown-report` and `--json-report`.
Both resolve the tower via `TOWER` / `PROXY_TOWER_BASE_URL` / SSM and never
write the FQDN into a file.

## Metrics

`llm_requests_total`, `llm_queue_depth`, `llm_queue_rejected_total`,
`llm_retries_total`, `llm_fallbacks_total`, `llm_circuit_state`,
`llm_truncation_avoided_total`, `llm_validation_failures_total`,
`llm_prompt_tokens`, `llm_upstream_latency_seconds`.

## M2 measurement status

The harness and its offline tests are in place, but the durable M2 baseline-to-
after measurement still needs a live tower run with SSM-backed resolution.

## Out of scope here

Deploy to kai-server (leg 09), the Caddy front selector and 2-replica manifests
(leg 09), and the capability phases (tool injection, MCP credential passthrough,
RAG, upskilling). This leg stays tightly the reliability proxy.
