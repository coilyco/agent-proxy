# Operational views and Ward dossier inputs

Agent Proxy exposes internal cold-path views under
`/v1/trajectory/views/<name>` and evidence-only dossier inputs under
`/v1/trajectory/dossiers/<trajectory-id>`.

## Read-only query helper

Use the repository-owned helper for deterministic filtering and cross-view joins:

```text
just trajectory-query --help
just trajectory-query investigate --issue owner/repository#42
just trajectory-query harness-fit --harness codex --model logical/model
just trajectory-query skill-use --skill coding-python --role engineer
```

`investigate` accepts exact repository, issue, workflow, and trajectory filters,
then joins reliability, cost and latency, policy, evaluation, and dossier
evidence by trajectory id. `harness-fit` filters the existing observational
aggregate by harness or model. It does not add a time window or repository
dimension that the underlying view does not contain. `skill-use` filters the
skill aggregate by skill, role, harness, or model.

The helper defaults to `PROXY_BASE_URL` or `http://127.0.0.1:8080`. It emits
JSON on stdout, copies no response bodies into errors, and never mutates Agent
Proxy.

## Query contracts

Versioned contracts cover:

* **reliability** - completion, partial reasons, retries, fallbacks, human
  intervention, and late-event counts
* **cost_latency** - models, providers, token use, latency, and cost by currency
* **policy** - observed allow, deny, review, and defer decisions
* **evaluation** - active labels, disagreement, supersession, and late evidence
* **harness_fit** - comparative completion, retry, fallback, latency, and cost by
  harness and model
* **skill_fit** - observed skill selection and use against completion, retry,
  fallback, intervention, and evaluation evidence, by skill, role, harness, and
  model. Selection and observed use are separate facts and are never merged. A
  selected skill with no matching Ward observation keeps a row flagged
  `selected_without_observed_use`, since absent evidence and evidence of absence
  are different claims.

Every trajectory row carries repository, issue, workflow, trace, and span joins.
Trace ids join the durable evidence to OTLP and SigNoz. Those operational
systems do not become the trajectory store.

## Ward boundary

`agentproxy.ward.dossier-input` version `1.0` contains reliability, evaluation,
governance correlation, and trace evidence. Its `may_authorize` field is always
false. Ward alone decides authorization, execution, lifecycle, recovery, and
governance.

## Access and redaction

Access filtering happens before row construction. The unauthenticated internal
HTTP surface returns only `internal` rows. Restricted consumers must construct
an explicitly authorized `AccessPolicy` inside a controlled deployment
boundary. Views never contain prompt, response, tool, annotation, or
environment bodies.

## Freshness and recovery

Every view publishes:

* generation time
* latest source materialization time
* complete-through watermark
* age in seconds
* the immutable raw ledger as its backfill source
* reconstruction limits

The view pipeline replays retained raw evidence, appends changed materialization
revisions, and reassembles evaluation records. It cannot reconstruct events or
bodies that were never captured or are outside the selected access tier.

## Session usage

The aterm client shows a context-token count per seat. A seat whose model
endpoint is Agent Proxy writes no transcript that reports usage, so the proxy
holds its count. `GET /v1/sessions/usage?id=<session>&id=<session>` answers
`agent-proxy.session-usage.v1`, with `sessions` keyed by id.

A succeeded request that carries `x-agent-session-id` is folded into a rollup in
[`app/session_usage.py`](../app/session_usage.py) by `_emit_request_terminal`. A
failed, rejected, or cancelled request is not counted, and neither is a caller
with no header. Per session it keeps:

* `context_tokens` - prompt plus completion of the latest request, which is what
  the next prompt starts from. A request whose backend reported no usage counts
  as a request and leaves it alone, so a silent backend cannot blank a reading.
* `input_tokens`, `output_tokens`, `requests`, `model`, `first_seen`, `last_seen`.
* `context_window` - the route's `context_window` from the
  [route registry](route-registry.md), `null` when Deploy declares none. A
  client shows a percent only when it is present.

An id the proxy has not seen is absent, never zero. More than 64 `id` parameters
is a 400, and an id outside `[A-Za-z0-9._:/@-]{1,128}` is never recorded.

Caveats:

* **No restart survival.** The rollup lives in the process, capped at 2048
  sessions, least recently touched dropped first. After a restart a seat reads as
  absent until its next turn. The trajectory store has no session index, so a
  per-read scan would cost every poll the whole table.
* **Backend-reported size.** An Ollama backend that reuses its KV cache counts
  only the new prompt tokens, so a local route can read low.
* **One process.** Hypercorn serves one process here. A second replica needs a
  shared store, and this is the seam to change then.
