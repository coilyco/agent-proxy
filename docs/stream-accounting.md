# Stream accounting

Part of [proxy.md](proxy.md).

## One span per chunk made streamed traces unreadable

Issue #140: the ASGI instrumentation wrapped every send with a child span, and
on the streaming path one send is one SSE frame. A streamed completion therefore
cost one `POST /v1/chat/completions http send` span per chunk. Over a 24h window
ending 2026-08-19 the proxy emitted 361,384 of them, the clear majority of all
trace volume it produced.

Ingest cost was the smaller half. The trace API caps a single trace at 1000
spans, and a sampled 183s `sirens-dowel` turn spent 965 of that budget on chunk
spans from one completion. The turn span and its tool calls fell outside the cap
and never rendered. Signal to noise in that trace was about 1.5 percent, so the
investigation fell back to aggregate queries. Any trace containing a streamed
completion was effectively opaque, which is the exact trace an operator opens.

## What is emitted instead

`exclude_spans=["send"]` on `FastAPIInstrumentor.instrument_app` retires the
per-chunk spans outright. The knob is per-app rather than per-route, so the
single `http send` span on non-streaming responses goes with them. That span
carried a status code the server span already reports, so nothing was lost.

`"receive"` joined it on 2026-10-02. A sampled trace of one Sirens Deep turn held 899
Agent Proxy spans, 868 of them `http receive`, and over 7 days at least 69
percent of this service's spans were that kind. The server span already carries
the request body size, so nothing was lost there either.

The four numbers worth keeping ride on the `request.chat` span, where a single
row now describes the whole stream:

- `agentproxy.stream.frames` - SSE frames written to the caller. Comment frames
  count, so this matches what the retired send spans counted rather than only
  the content deltas.
- `agentproxy.stream.bytes` - total UTF-8 bytes written.
- `agentproxy.stream.duration_ms` - request receipt to last frame.
- `agentproxy.stream.first_token_ms` - request receipt to the first frame
  carrying generated content or reasoning.

## Why the clock starts at request receipt

Both durations are measured from the same `time.perf_counter()` stamp the
request lifecycle already uses, not from the first upstream byte. Admission,
retries, and backend failover are the things that make a turn slow, and a
first-token figure that excluded them would report a healthy number for exactly
the turn under investigation. `agentproxy.stream.first_token_ms` is therefore
comparable against the request span's own duration.

## An absent first token is not a zero

`agentproxy.stream.first_token_ms` is omitted when a stream produced no content
at all. A stream that died before generating and one that answered instantly are
different events, and a zero would collapse them. Query for absence rather than
for `= 0`.

## A failed stream still reports

The totals are stamped on every exit path, cancellation and mid-flight backend
failure included. A partial stream keeps whatever it managed to send, so the
frames, bytes, and first-token figures survive alongside the `error.type` the
failure recorded. Those are the traces most worth reading.

## The dependency floor is load-bearing

`exclude_spans` arrived in `opentelemetry-instrumentation-fastapi` 0.48b0, and
`pyproject.toml` pins that floor. `_instrument_fastapi` swallows exceptions so
observability can never block startup, which means an older wheel would reject
the keyword and silently drop **all** inbound tracing rather than just the send
spans. `tests/test_stream_spans.py` asserts the server span survives for that
reason, not only that the chunk spans are gone.
