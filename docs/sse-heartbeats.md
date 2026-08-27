# SSE heartbeats

Part of [proxy.md](proxy.md).

## What a caller could not tell apart

Issue #104: a caller waiting on a streaming completion cannot distinguish
admitted-and-queued, retrying after a failure, generating, and hung. It gets
silence, then either a response or a deadline. `sirens-echo` resolved that
ambiguity by giving up at ~179.5s and reporting a timeout, including in cases
where the proxy was still doing legitimate work.

## The wire shape

Lines beginning with `:` are SSE **comments**, which every spec-compliant client
ignores. A consumer that does not parse them sees the same `data:` frames it saw
before. That property is why comments beat empty-delta chunks
(`{"choices":[{"delta":{}}]}`), which some OpenAI-compatible clients mishandle.

```
: {"state":"attempt","n":1,"of":2,"backend":"tower-3026","regime":"idle"}
: {"state":"upstream_started","backend":"tower-3026"}
: {"state":"attempt","n":2,"of":2,"backend":"litellm","regime":"hosted"}
data: {"id":"chatcmpl-...","choices":[{"delta":{"content":"Paris"}}]}
```

## States

- `attempt` - a backend is about to be tried, with `n`, `of`, `backend`, and the
  backend's `regime`. Emitted before each chain entry.
- `upstream_started` - the first chunk of a real response arrived.
- A keepalive repeats the current state every
  `PROXY_HEARTBEAT_INTERVAL` seconds (default 10, `0` disables) so a persisting
  state stays visible without a transition.

## Retry visibility is the part that earns this

`coilyco-gaming/sirens-echo#137` documents three attempts burning ~9s before a
turn 502s. From outside that is indistinguishable from one slow attempt.
`attempt n of N` turns a silent fallback into something a caller can log,
display, and alert on.

## Queue position is not carried

Issue #105 measured admission delay at 0.7ms to 4.5ms across 29 traces while
`queue.wait` itself varied 25-fold, so queue position has nothing to report at
current traffic. It is also moot on this path: the streaming surface calls
`dispatch_stream` directly and does not go through the queue at all. Whether
position becomes worth carrying under concurrency is #107's question.

## This does nothing on its own

A **total** request deadline ignores heartbeats by construction. `sirens-echo`
wraps its call in a context deadline that fires on schedule no matter how many
bytes arrive, so the consuming side must move to an idle or read timeout for any
of this to change behaviour. That half lives in `sirens-echo`. This is the
emitting side, and it is inert until the consuming side lands.

## Non-streaming requests

Heartbeats have nowhere to go without breaking the response contract, so a
caller that wants progress opts into `stream: true`.

## Visibility

Each emission increments `llm_stream_heartbeats_total{logical_model,state}` and
adds a `stream.heartbeat` event to the request span, so a silent heartbeat path
cannot regress unnoticed.

## Implementation note

`_with_keepalives` shields the in-flight read while it waits, so a keepalive
tick never cancels the chunk it was waiting for.

## See also

- [`backend-regime.md`](backend-catalog.md) - the `regime` field on `attempt`.
- [`request-deadline.md`](request-deadline.md) - the proxy-side deadline.
