# Backend catalog and prompt measurement

How the proxy discovers what a backend serves and how it measures a prompt
against that. Implementation lives in [`app/models.py`](../app/models.py) and
[`app/analysis.py`](../app/analysis.py).

## The `/api/tags` catalog cache

`/api/tags` on the primary backend is the single source of truth for which tags
exist and each tag's real `context_length`.

It is read once per base URL and cached. A cached value of `None` means the tag
is present but not reporting a context length, so `derive_num_ctx` falls back to
the configured ceiling.

The lookup also returns whether the fetch succeeded. That second element is what
makes an unreachable backend fail **open**: a real request is still served with
a conservative window, rather than every tag turning into a 404.

Only successful reads are cached. A failed fetch is retried on the next request,
so a tower that comes up after the proxy started is still discovered.

## Token counting

Prompt sizing uses tiktoken's `cl100k_base`, which is an approximation of
ollama's tokenizer rather than a match. The proxy reserves headroom instead of
aiming for an exact count, so drift between the two never turns a prompt that
fits into one that overflows.

Each message also carries a small fixed overhead approximating the chat
template's role and delimiter tokens. The same overhead is applied by both the
budget guard and the caller-facing `count_tokens` surface, so the two never
disagree about how large a prompt is.

That deliberate slack is also why the delivered-context check
([context-safety-settings.md](context-budget-per-model.md)) needs a tolerance: it
compares the proxy's estimate against the backend's real `prompt_eval_count`.

## See also

- [`context-safety-settings.md`](context-budget-per-model.md) - the `num_ctx`
  settings this feeds.
- [`route-registry.md`](route-registry.md) - logical route resolution above it.

## Why percentiles were unreadable

`litellm_request` over 24 hours on 2026-08-12: p50 **3.42s**, p99 **233.71s**. A
68x spread, read at the time as a heavy tail and guessed at as "contention or a
stall". Issue #108 found it was neither. It is **bimodal**, and the two modes are
*GPU idle* and *GPU shared with a game*.

Percentiles describe one population, and there were two. A fully predictable,
human-caused state was presented as random variance, which is why three separate
pieces of analysis reached for contention, prefill cost, and queueing before
anyone asked what else was running on the box.

Slicing needs two dimensions that were missing (#109):

- **backend identity** - which backend actually served the request. It was
  inferable only from a client URL, and a URL does not group.
- **capacity state at dispatch** - which regime the backend was in.

## What the spans carry

`agentproxy.backend` and `agentproxy.backend.regime` ride on:

- `upstream.chat` and `upstream.chat_stream`
- `resilience.attempt`
- **`request.chat`** - the span a latency query actually groups on, which
  previously carried no backend at all. Dispatch stamps the serving backend onto
  the result, so the identity reflects which chain entry won rather than which
  one was tried first.

`p99 by backend, by regime` is then a readable number and the two modes stop
hiding each other.

## Who sets the regime

`PROXY_BACKEND_REGIME` is the fleet-wide value. A backend spec entry may
override it with its own `"regime"` field, so a shared tower and a hosted
provider in the same chain can report different states.

The default is **`unknown`**, deliberately. The proxy has no way to observe GPU
occupancy today, and a default of `idle` would assert something nobody measured.

## Values

- `idle` - the backend has its hardware to itself
- `contended` - something else is using the same hardware
- `saturated` - the backend cannot start work now
- `hosted` - a provider whose capacity is not the fleet's to know
- `unknown` - nobody has said

Nothing enforces this list. It is a grouping key, and an operator who needs a
sixth value should use one rather than mislabel a run.

## What this does not do yet

Nothing derives the regime automatically. Issue #108 covers detecting
saturation, and its option 3 - deriving capacity state from GPU utilisation via
the fleet's node-stats telemetry - is what would eventually set this without a
human. Until then the value is operator-supplied, which is exactly how #108
framed it: the state is known in advance by a human, because nobody starts a
game by accident.

That is enough for the thing #109 asked for last: a run of the #107 burst test
or an #81 model sweep can be shown, after the fact, to have executed against an
idle backend, instead of producing a result that looks like model weakness.

## Metrics are unchanged

`llm_upstream_latency_seconds` keeps its existing `logical_model` and `backend`
labels. Regime is a span dimension only, because adding a label to a live
histogram changes its cardinality and breaks every existing query against it.

## See also

- [`proxy-request-path.md`](proxy-request-path.md) - where dispatch picks a backend.
- [`backend-catalog.md`](backend-catalog.md) - how the chain is configured.
