# Request path and endpoints

Part of [proxy](proxy.md).

## Request path


A governed client sends an OpenAI-shaped request carrying a Deploy-owned
`<namespace>/<alias>` key as `model`. The proxy:

1. **resolves** the key against Deploy's mounted registry (`app/models.py`).
   LiteLLM mode sends the configured alias. Direct rollback sends a supported
   physical target and fails closed for an unsupported runtime. Backend context
   metadata derives `num_ctx = min(context_length, ceiling) - headroom`.
2. **guards the context budget** (`app/analysis.py`): counts prompt tokens and,
   if the prompt exceeds `num_ctx - headroom`, trims the oldest non-system turns,
   always keeping the system framing and the live turn. Increments
   `llm_truncation_avoided_total` when it actually drops a turn. It drops whole
   `assistant(tool_calls)` + `tool` groups, never a partial one, and rechecks
   the pairing before dispatch: a split group is a 400 upstream (#113), and a
   prompt that arrived unpaired gets a local 400 naming the message.
3. **sheds** the request with 429 when it arrived above
   `PROXY_RATE_LIMIT_PER_SECOND` for that route (`app/ratelimit.py`), after
   resolution so an unknown model still 404s without spending a token and before
   admission so a shed request never occupies the queue. See
   [admission rate limits](rate-limits.md).
4. **enqueues** the job on a bounded `asyncio.Queue` and awaits its future
   (`app/queue.py`). A full queue returns HTTP 429 (`llm_queue_depth`,
   `llm_queue_rejected_total`). The `queue.wait` span closes the moment a worker
   claims the job, so it measures admission delay and nothing else. It used to
   stay open for the whole request, which made a saturated proxy and a slow model
   look identical (#105). It carries `agentproxy.queue.admitted`. Cancelling the
   downstream request removes a waiting job or cancels its active dispatch task,
   releasing worker capacity without starting another retry or fallback.
5. a **worker** dispatches under the resilience policies (`app/resilience.py`):
   walk the fallback chain, retry each live backend with backoff, and validate
   every response. Transport errors trip a per-backend circuit breaker; a merely
   bad generation is rerolled but does not. A settled upstream 4xx is neither
   retried nor failed over and reaches the caller with its own status - see
   [upstream error classification](upstream-error-classification.md).
6. the **upstream client** (`app/upstream.py`) forwards to the backend's native
   API. Ollama backends use `/api/chat` with `options.num_ctx` injected. OpenAI
   backends like the llama-server gpt-oss target use `/v1/chat/completions`
   without injection, then normalize their response back to the proxy's
   canonical shape. Downstream disconnects cancel the in-flight httpx request
   and close an active response stream while recording a bounded `cancelled`
   outcome.
7. the result is shaped back to the OpenAI schema (`app/main.py`). Reasoning-model
   thought is surfaced as `reasoning_content`.

A streaming request also carries SSE comment lines reporting attempt and backend
state, which a spec-compliant client ignores and a curious one parses. See
[SSE heartbeats](sse-heartbeats.md).

Streaming requests take the same fallback chain and circuit breaker but skip the
reroll (a token stream cannot be validated after the fact), so a harness that
wants the full resilience guarantee uses the non-streaming path.

## Endpoints


* `POST /v1/chat/completions` - streaming and non-streaming.
* `POST /v1/completions` - modeled as a single user turn so it rides the same
  resilience path.
* `GET /v1/models` - lists enabled logical route keys and hides physical models.
* `GET /healthz` - liveness for Caddy / k8s probes.
* `GET /readyz/{namespace}/{alias}` - non-generating structural readiness for one
  governed logical route. See [readiness.md](readiness.md).
* `GET /metrics` - prometheus exposition.

Part of [proxy.md](proxy.md). Accepted request headers, their OpenAI
`metadata` fallbacks, and the span attribute each becomes.

* `x-request-id` or `metadata.request_id` - `agentproxy.request_id`
* `x-ward-run-id` or `metadata.ward.run_id` - `ward.run_id`
* `x-ward-container-name` or `metadata.ward.container_name` - `ward.container_name`
* `x-ward-role` or `metadata.ward.role` - `ward.role`
* `x-ward-harness` or `metadata.ward.harness` - `ward.harness`
* `x-ward-target-repo` or `metadata.ward.target_repo` - `ward.target_repo`
* `x-ward-issue-ref` or `metadata.ward.issue_ref` - `ward.issue_ref`
* `x-ward-workflow` or `metadata.ward.workflow` - `ward.workflow`
* `x-ward-context-level` or `metadata.ward.context_level` - `ward.context_level`
* `x-ward-version` or `metadata.ward.version` - `ward.version`
* `x-agent-session-id` or `metadata.agent.session_id` - `agent.session_id`

Prometheus labels stay unchanged. The new correlation fields live only in logs
and traces.
