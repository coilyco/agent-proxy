# Jev decision shim

`POST /v1/systemone` fronts TypeSafe's System One model (Jev). Jev takes program state plus typed questions and returns typed answers with probabilities. It has no chat shape, so it does not go through LiteLLM or the chat route. The decision and what it forecloses: `teable:coilyco-flight-deck/agent-proxy#7903`.

Implementation: [`app/systemone.py`](../app/systemone.py) for the upstream call, [`app/main.py`](../app/main.py) for the route, spans and events.

## What the route does

- **The caller surface is TypeSafe's own.** The request body goes upstream unchanged and the answer comes back unchanged, so the official SDKs work with `TYPESAFE_BASE_URL` pointed at the proxy. This adds no contract of its own.
- **The caller holds no key.** The proxy sends `Authorization: Bearer` from a mounted file and drops whatever the caller sent.
- **One upstream attempt.** The SDKs already retry 408, 429 and 5xx and read `Retry-After`, so a retry here would hide failures from the telemetry below. Status, body, `Retry-After` and `retry-after-ms` pass through.
- **Failures the proxy makes itself.** 400 for an unreadable body, 404 for a model not on the allowlist, 503 when no key is mounted, 502 when TypeSafe is unreachable or answers a 2xx that is not a JSON object, 504 on a timeout.
- **A caller deadline only shortens the timeout.** `x-request-deadline-ms` is read the way the chat route reads it, and a timeout it caused is a `deadline_exceeded` outcome instead of `upstream_failed`.

The backend is not an entry in `PROXY_BACKENDS_JSON`. That list is the fallback chain of every chat route, so a Jev entry would become a chat fallback. The shim builds its one backend itself, named `typesafe`, dialect `systemone`, regime `hosted` ([backend-catalog.md](backend-catalog.md)).

## `POST /v1/systemone/choice`

A flat-bodied sibling of the plain route, for a caller that cannot author TypeSafe's `choice` criteria map directly: `{"model", "state"?, "instructions", "options": [{"name", "description"}, ...]}`. `app.systemone.build_choice_body` reshapes it into `questions.answer` (the one fixed question key every `/v1/systemone/<type>` route uses, a tool call being one question) with `criteria` built from `options`, then calls the same internal path the plain route does, so the span, cost, trajectory events and error handling are identical, keyed by the same `agentproxy.decision.questions` count (1).

This exists for `teable:coilyco-flight-deck/agent-proxy#7987`, whose MCP surface fronts Jev through beaver, and beaver's guardfile speaks umbra's inline grammar (typed inputs are scalars and arrays of scalars, per `umbra/docs/opcore-inline.md`). `criteria` is a map keyed by caller-chosen option names, which that grammar cannot express. `noul` has no nested field to reshape, and `score`'s `criteria` is TypeSafe's own ordered array rather than a map, so both pass through the plain route with `array ... raw=true` and need no sibling route of their own.

A bad shape (empty or non-string `model`/`instructions`, an empty or non-array `options`, a non-object option, a missing or duplicate `name`) is a 400 before anything reaches TypeSafe.

## Upstream contract

Read from TypeSafe's docs on 2026-09-19 (`docs.typesafe.ai/api`, `/models.md`, `/primitives/choice.md`).

- **Request** - `POST https://api.typesafe.ai/v1/systemone` with `state`, `model` and `questions`. Question types are `noul`, `choice` and `score`.
- **Reply** - `model` (the resolved version), `answers` and `usage` with `input_tokens` and `output_tokens`. There is no cost, request id or latency field.
- **Errors** - 401, 422, 429 and 529, with no documented body shape.
- **Limits** - 255 options per choice question, 64k tokens per request, 1,200 requests per minute, all adjusting without notice.
- **Price** - 0.042 USD per million input tokens, and output tokens are free. That is a default and not a fact the API reports, so a price change needs `PROXY_SYSTEMONE_INPUT_USD_PER_MTOK`.

## Configuration

- `PROXY_SYSTEMONE_API_KEY_FILE` - path of the mounted key. Unset leaves the route answering 503.
- `PROXY_SYSTEMONE_BASE_URL` - default `https://api.typesafe.ai`.
- `PROXY_SYSTEMONE_TIMEOUT` - seconds, default 10, the SDK's own default.
- `PROXY_SYSTEMONE_INPUT_USD_PER_MTOK` - default 0.042.
- `PROXY_SYSTEMONE_MODELS` - comma list, default `jev-latest,jev-1.13.0,jev-preview`. The body picks the model and the model becomes a metric label, so an unlisted name is a 404.

## Telemetry

The span is `request.systemone`. It carries the chat span's attributes plus these.

- `agentproxy.backend`, `agentproxy.backend_dialect`, `agentproxy.backend.regime` - `typesafe`, `systemone`, `hosted`.
- `gen_ai.request.model` and `gen_ai.response.model` - the alias asked for and the version TypeSafe resolved it to.
- `gen_ai.usage.input_tokens` and `gen_ai.usage.output_tokens`.
- `agentproxy.cost.usd` and `agentproxy.decision.questions`.
- `agentproxy.upstream.status_code`.

Metrics reuse the chat names.

- `llm_upstream_latency_seconds{logical_model, backend="typesafe"}` counts served calls only, as it does for chat.
- `llm_requests_total{logical_model, outcome}` uses the chat outcomes.
- `llm_cost_usd_total{logical_model, backend}` is new, and is the first cost signal in the proxy.

Trajectory events are the chat ones: `action.proposed`, then `execution.completed` or `execution.failed`, with `agentproxy.request_kind` set to `systemone`.

## Reading the bake-off numbers

An item here is one request. A request with several questions is one item, and `agentproxy.decision.questions` gives the per-question figure.

- **p50 and p99 latency** - `histogram_quantile(0.99, sum by (le) (rate(llm_upstream_latency_seconds_bucket{backend="typesafe"}[1h])))`
- **Cost per 1k items** - `1000 * sum(increase(llm_cost_usd_total{backend="typesafe"}[1h])) / sum(increase(llm_requests_total{logical_model="jev-latest", outcome="ok"}[1h]))`

## Not built

- **Body capture.** Events and spans are metadata only.
- **Queue, rate limit, breaker and failover.** There is one hosted backend and the SDKs pace themselves.
- **A readiness route or a `/v1/models` entry.**
- **The Deploy side.** Mounting the key and setting the variables above belongs to the sysadmin seat.

## See also

- [backend-catalog.md](backend-catalog.md) - the regime values and the span attributes.
- [proxy-request-path.md](proxy-request-path.md) - the chat path this sits beside.
