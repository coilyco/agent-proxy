# Burst probe

Part of [proxy.md](proxy.md).

## The question

Issue #105 measured admission delay between 0.71ms and 4.52ms across 29 traces
and could not separate three explanations, because at 0.0047 req/s an empty
queue is the expected result either way:

1. queueing active and never contended
2. queueing configured but not wired to the request path
3. no queue at all, span emitted unconditionally

Span data cannot tell them apart at that load. One burst can.

## Running it

`just burst-probe --base-url http://ser8:8080 --model <route> --yes`

It **issues real model calls and costs real tokens**, so it refuses to start
without `--yes` and names the call count in the refusal. Defaults are the method
from issue #107: concurrency 1, 5, 10, 30, three repeats each, shortest possible
identical prompt, `max_tokens: 1`, because this measures admission rather than
generation.

## Two things to set first, or the run is worthless

**Disable the rate limiter.** `PROXY_RATE_LIMIT_PER_SECOND=0` for the duration.
Issue #110 ships shedding on at 1/s with a burst of 1, so a 30-way burst gets 29
`429`s in milliseconds and the decision table reads "the proxy sheds, never
queues" when what it measured is the limiter. This is the single most likely way
to get a confidently wrong answer, so the `SHED` verdict says so out loud.

**Declare an idle backend.** `PROXY_BACKEND_REGIME=idle`, and only after
confirming nothing else is using the GPU. Every span in the run then carries
`agentproxy.backend.regime`, so a run that turns out to have been contended can
be discarded on evidence rather than argued about. Issue #109 is why: a
contended GPU confounds this completely, and "was the box quiet" should not be a
memory.

Do not run it during an incident.

## Reading the result

The probe reports what a client can see: admitted versus shed counts, the
rejection statuses, whether `Retry-After` was present, and admitted-request
latency per level. It prints one summary line per level as it goes, then a full
JSON record and a verdict.

The verdict is deliberately weak, because the client side cannot see admission
delay. `NEITHER OBSERVED` cannot separate "no admission control" from "the
backend absorbed the burst", and the probe says so rather than picking.

The trace side is where the answer actually lives. After the run, read
`queue.wait` per trace over the window. Since issue #105 that span closes when a
worker claims the job, so its **duration is the admission delay** and needs no
subtraction. The #107 method describes `resilience.attempt start − queue.wait
start`, which still works and now agrees with the simpler number.

## What the answer decides

- delay rises above the millisecond floor with concurrency → queueing is live,
  and #104's heartbeats should carry queue position
- rejections with no delay rise → the proxy sheds and never queues, so
  `queue.wait` can never show a wait by construction and should be deleted
  rather than kept
- neither → admission control lives only in the caller, and both of the above
  are moot

## See also

- [`backend-regime.md`](backend-catalog.md) - tagging the run as idle.
- Issue #110 - the admission limiter that must be off for this to mean anything.
