# Caller backend preference

Part of [proxy.md](proxy.md).

## The ask

Issue #111, in Kai's words:

> I run ornith most of the time, and its great, but it cant fit in vram while
> I'm playing games. which means everything sirens echo does becomes blocked. I
> would like instead for sirens echo to be able to inform aproxy that future
> requests should "stick" to the fallback model if the primary model is
> persistently down.

The proxy detecting this for itself is
[saturation-failover.md](saturation-failover.md), and how long that detection
sticks is [saturation-stickiness.md](saturation-failover.md). This page is the
other half: the caller saying so outright, because Echo can know the tower is
behind a game before any threshold has had a chance to trip.

## The header

```
X-Prefer-Backend: hosted
```

The named backend is moved to the **front** of the route's chain for that
request. Everything else stays behind it in its configured order.

Reordering rather than filtering is the whole safety property. A hint from a
caller can never empty the chain, never disarm the fallback, and never turn a
recoverable turn into a hard failure. If the preferred backend is down, the
request falls back exactly as it would have.

An unrecognised name leaves the order untouched rather than erroring, because a
routing hint should not be able to fail a request that would otherwise have
worked. The value is trimmed and bounded to 64 characters before it reaches a
routing decision.

## Stickiness is the caller's

A header is per-request, so "future requests should stick" means Echo keeps
sending it. That is deliberate and it is the cheap half of #111:

- no new surface on a service that has had no authenticated writes
- no shared state, so two callers cannot disagree with each other
- `AGENTS.md` keeps holding, since Ward owns authorization and this asks the
  proxy for no authority it did not have

The alternative shape - an admin endpoint that pins a route for everyone until
an expiry - is still open on #111. It is the better fit for *you* flipping a
switch before starting a game, and the worse fit for the architecture boundary.
This header does not foreclose it.

## Visibility

Every request carrying the header logs `request.backend_preference` with the
requested name and whether it applied, so a preference that silently does
nothing - a typo, or a backend not in that route's chain - is visible rather
than mysterious. Which backend actually served is already on the spans as
`agentproxy.backend`, per [backend-regime.md](backend-catalog.md).

## See also

- [`saturation-failover.md`](saturation-failover.md) - the proxy noticing by itself.
- [`saturation-stickiness.md`](saturation-failover.md) - how long that lasts.
