# Saturation failover

Part of [proxy.md](proxy.md).

## A saturated backend is not a failed backend

Issue #108, on the 2026-08-12 Echo outage, in Kai's words: "echo failed its
turns b/c I had a game running on the 3026 and aproxy doesn't know how to
communicate that limitation / state".

A game on the local GPU host starved local inference. Echo failed **100% of
turns for ~2.5 hours** at ~180s each while Deep answered `ping` in 2.18s through
the same proxy. A resource conflict the proxy could not see, route around, or
describe.

Two independent gaps, either one sufficient. **Health is not capacity**: `GET
/api/tags` returns 200 in 6.7ms while the GPU is fully occupied, which says the
model is installed, not that inference can proceed. And **a hang is not an
error**: the machinery fires on validation failures, 500s, and refused
connections, and a saturated backend produces none of those - it accepts the
request and goes quiet, and the proxy waited until the caller's deadline killed
it. Slowness was not a failure condition, and for a self-hosted GPU tier sharing
hardware with a human it is the *primary* one.

## What changed

`PROXY_BACKEND_SLOW_AFTER` is the seconds an attempt may run before its backend
counts as saturated. `0` disables it, which is the default. Past it the attempt is abandoned, the chain **advances to the next tier** rather
than retrying the same backend, and the backend's circuit breaker records a
failure so subsequent requests skip it for the cooldown. A backend nobody can
get a token out of is unavailable, whatever it says about itself. This is what
makes the cloud failover in `coilyco-gaming/sirens-echo#81` reachable: it could
not help before, because from the proxy's point of view nothing had failed yet.

## Only the wait is bounded

On the streaming path the threshold bounds **time to the first chunk**. A stream
that has started is making progress, and cutting it for being long would be the
opposite of what the issue asks for. On the non-streaming path there is no
first-token signal to observe, so it bounds the whole attempt: set it above the
slowest legitimate completion on that route, or a long answer will be mistaken
for a stalled one.


## Deadline versus saturation

Different facts, and the caller is told which. When the request's own budget
(`PROXY_REQUEST_DEADLINE`, [request-deadline.md](request-deadline.md)) ran out,
the answer is `504 request_deadline_exceeded` and no failover happens, because
there is no time left to fail over into. When the budget survives and only this
backend was slow, the chain advances silently unless every tier is saturated.

## Saying so

A streaming caller gets an SSE comment as it happens, which is the literal
content of "aproxy doesn't know how to communicate that limitation / state" -
see [sse-heartbeats.md](sse-heartbeats.md).

```
: {"state":"backend_saturated","backend":"tower-3026","failing_over":true}
```

The attempt span carries `agentproxy.outcome=saturated` and
`agentproxy.backend.regime=saturated`, beside
`llm_backend_saturated_total{logical_model,backend}` and
`dispatch.backend_saturated`.

## What this does not do

It reacts rather than prevents. Option 3 in #108, capacity derived from GPU
utilisation, would stop the request being sent at all; its operator-supplied
half is the `regime` field in [backend-regime.md](backend-catalog.md) and the
automatic half is open. Nothing here probes capacity either: `/api/tags` still
reports presence, and replacing it with a tiny real completion is option 1.

See also [`backend-regime.md`](backend-catalog.md) and
[`request-deadline.md`](request-deadline.md).

Issue #111 asked for an automatic fallback when a model is *persistently*
unavailable, and for that choice to *stick*. It also said both words needed
defining. This is the threshold half of that answer. The explicit signal - a
caller telling the proxy to prefer the fallback outright - is still open on
that issue and needs a product decision.

## The two definitions

Saturation counts and cools on its own terms, because a busy backend is not a
broken one. `PROXY_SATURATION_THRESHOLD` (default 2) is the consecutive count
that opens the breaker, lower than the failure threshold because each saturation
costs a full slow-path wait. `PROXY_SATURATION_COOLDOWN` (default 900) is how
long it sticks, because the condition behind it is a play session rather than a
blip and a 30-second cooldown would re-probe a busy GPU roughly 120 times an
hour. The count is consecutive, so one good turn clears it, and half-open
probing still recovers without a human.

## What is still open

The caller can now say so outright with `X-Prefer-Backend` - see
[prefer-backend.md](prefer-backend.md). What is still open on #111 is the
*shared* version: an admin endpoint pinning a route for every caller until an
expiry, which fits an operator flipping a switch before a game and sits less
comfortably with Ward owning authorization.

## See also
