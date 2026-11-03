# Upstream error classification

Part of [proxy.md](proxy.md).

## The distinction

A backend that does not answer and a backend that answers "no" are different
facts, and issue #114 measured what collapsing them costs. One 400 was bucketed
as `dispatch.transport_error`, retried three times with a byte-identical body,
and returned to the caller as:

```
502 {"error":{"message":"sirens-echo/deepseek: all backends failed (...)",
     "type":"upstream_error"}}
```

Nothing about the transport had failed. LiteLLM answered promptly on all three
attempts and served a different request successfully 7.5 seconds later.

## What the client raises

`app/upstream.py` splits the two:

- **`UpstreamStatusError`** - the backend answered with a non-2xx. Carries
  `status_code` and a bounded `body`.
- **`UpstreamError`** - anything else: refused connection, timeout, read error.

`is_retryable_status` decides which statuses a later identical attempt could
still resolve: every 5xx, plus `408`, `425`, and `429`. Every other 4xx is a
settled statement about a body that will not change.

## What the dispatcher does

A settled 4xx is not retried, does not walk the fallback chain, and does not
count against the backend's circuit breaker. Asking a second backend the same
invalid question cannot help, and the backend that answered promptly is not the
broken thing. It raises `UpstreamRequestRejected`, logged as
`dispatch.request_rejected`.

A retryable status keeps the existing behaviour: retry with backoff, then fall
back, then exhaust.

## What the caller receives

`UpstreamRequestRejected` returns the **upstream's own status and body**. When
the body parses as the OpenAI error shape it is passed through with
`upstream_status` added, so the caller reads the provider's own account of what
was wrong rather than a synthesized one.

`BackendUnavailable` still returns 502. `AllBackendsFailed` is a subclass of it
raised only when the chain actually offered more than one backend, so the phrase
"all backends failed" stops appearing for a single backend that rejected a
single payload.

## Span and log record

An upstream non-2xx sets `http.response.status_code` and
`agentproxy.upstream.status_code` on the upstream span and records an error, so
a failure of this shape is no longer invisible to the service error rate
(issue #106). The recorded codes are `upstream_request_rejected` and
`upstream_5xx`, both in the [exception taxonomy](exception-taxonomy.md).

## Resilience policies

`app/resilience.py` wraps upstream dispatch with four policies:

* **response validation** - non-empty, tool-call JSON parses, no degenerate
  repetition or truncation garbage.
* **retry with backoff** - a bad generation becomes a reroll
  (`llm_retries_total`).
* **fallback chain** - a down or busy backend advances to the next backend in the
  logical model's chain (`llm_fallbacks_total`).
* **per-backend circuit breaker** - stops hammering a dead backend
  (`llm_circuit_state`).

Transport and HTTP errors count against a backend's breaker. A merely bad
generation does not (the backend is alive, the token draw was unlucky) but still
triggers a reroll.
## See also

- [`exception-taxonomy.md`](exception-taxonomy.md) - the closed set of codes.
- [`proxy-request-path.md`](proxy-request-path.md) - where dispatch sits.
