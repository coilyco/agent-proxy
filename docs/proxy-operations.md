# Running, proving, and metrics

Part of [proxy](proxy.md).

## Running and proving locally


```
just sync                                             # uv sync (installs app + dev)
PROXY_TOWER_BASE_URL=http://<tower>:11434 just serve  # proxy on 127.0.0.1:8080
just test                                             # offline suite, tower not required

# compatibility-mode proof with the proxy and tower reachable:
TOWER=<tower> MODEL=qwen3-coder:30b just proof        # 32767 (direct) vs num_ctx-injected (proxy)
TOWER=<tower> MODEL=qwen3-coder:30b just reliability --target both --turns 6 --json reliability.json
```

The invocation is `just <verb>`, defined in the `justfile`. Script arguments
ride directly after the verb, because `set positional-arguments` forwards them
to the recipe verbatim.

`scripts/truncation_proof.py` reproduces the leg-01 truncation test through the
proxy. `scripts/reliability_loop.py` is the leg-05 reliability harness: it scores
a context-growing, tool-using loop with the proxy's own `validate_response` and
emits a reliability percentage and failure histogram. `--target both` runs the
`direct` baseline and the `proxy` after in one pass and prints the comparison;
`--json PATH` writes a durable, machine-readable artifact (stable schema, no FQDN
inside) so a future before/after check re-runs the same command and diffs the
JSON. The measured result and its reproduction command live in
`docs/reliability_baseline.md`. Both scripts resolve the tower via `TOWER` /
`PROXY_TOWER_BASE_URL` / SSM and never write the FQDN into a file.

## Metrics


`llm_requests_total`, `llm_queue_depth`, `llm_queue_rejected_total`,
`llm_retries_total`, `llm_fallbacks_total`, `llm_circuit_state`,
`llm_truncation_avoided_total`, `llm_validation_failures_total`,
`llm_context_truncated_total`, `llm_prompt_tokens`, `llm_upstream_latency_seconds`,
`ward_skill_use_total`, `llm_prompt_cache_hit_tokens_total`,
`llm_prompt_cache_miss_tokens_total`, `llm_prompt_cache_write_tokens_total`.

The three prompt-cache counters publish only for a provider that accounts for
caching. See [proxy-prompt-cache.md](proxy-prompt-cache.md).

## Out of scope here


Deploy to kai-server (leg 09), the Caddy front selector and 2-replica manifests
(leg 09), and the capability phases (tool injection, MCP credential passthrough,
RAG, upskilling). This leg stays tightly the reliability proxy.

## Trace correlation metadata

Each request can carry ward run metadata in headers, with OpenAI `metadata` as a
fallback when a client cannot set custom headers. The proxy copies the values
into `RequestTraceContext.extra`, structured logs, and span attributes so
SigNoz can join proxy traces with ward-run logs.

Every structured log emitted while an OpenTelemetry span is active also carries
the current lowercase hexadecimal `trace_id` and `span_id`. The ser8 SigNoz
`json-body` ingest pipeline promotes those fields from the retained JSON body,
which enables the traces-to-logs jump without duplicating log export in the
request path. Logs outside a valid span omit both fields.

Every handled request, stream, validation, transport, queue-worker, and
trajectory-persistence failure records an OpenTelemetry `exception` event and
marks its span as an error. The exception uses a static service-local type, a
closed-set human summary as its message, and `error.type` plus `error.stage`
attributes drawn from the bounded taxonomy in
[exception-taxonomy.md](exception-taxonomy.md). Dynamic diagnostics remain outside the
exception grouping event, giving the SigNoz Exceptions page complete baseline
coverage without adding new body capture.

`app.obs.record_error` is the single writer of that telemetry, so the event and
the `StatusCode.ERROR` status can never drift apart. Failure is recorded at
**attempt** granularity: a retried attempt and a dead backend each keep their own
error span, while the attempt that recovers or serves the request is left clean.
A transient blip therefore stays visible in Error Management without turning a
request that ultimately succeeded into a false alert. `tests/test_error_spans.py`
holds that contract for the non-streaming, streaming, retry-recovery,
backend-fallback, and redaction paths.

FastAPI instrumentation is installed while the application is assembled,
before Starlette freezes its middleware stack. The HTTP server span extracts
the caller's W3C `traceparent`, and `request.chat` or `request.completions`
continues beneath it. HTTPX then carries that same context into LiteLLM. Body
capture remains off unless `PROXY_TRACE_BODIES` is explicitly enabled. When it
is enabled, the request span owns the strict complete request and response
capture contract below.
