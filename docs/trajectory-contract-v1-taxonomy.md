# Event taxonomy and model execution facts

Part of [trajectory-contract-v1](trajectory-contract-v1.md).

## Event taxonomy and payload requirements


`event_type` is one of the following values. The event-specific facts live in `payload` and retain the common envelope.

- `actor.observed`
  - `payload.actor_ref`, identity source, role, and capability claims as references or metadata.
- `action.proposed`
  - `payload.action_kind`, `payload.action_ref`, target references, intent, and `before_state_ref` when known.
- `policy.decided`
  - `payload.decision` with `allow`, `deny`, `require_review`, or `defer`, policy name and version, reason code, and `action_ref`.
- `execution.started`, `execution.completed`, `execution.failed`
  - `payload.execution_id`, executor reference, action reference, outcome, error class where applicable, and `after_state_ref` when known.
- `observation.recorded`
  - `payload.observation_kind`, `payload.observation_ref`, subject reference, and measured facts.
- `state.changed`
  - `payload.before_state_ref`, `payload.after_state_ref`, change kind, and the action or execution reference that caused it.
- `evaluation.recorded`
  - `payload.evaluation_id`, evaluator or rubric version, input references, output label or score, confidence, and supersedes reference when applicable.
- `human.intervened`
  - `payload.intervention_kind`, human role or opaque actor reference, rationale reference, and the affected action or trajectory reference.
- `artifact.created`, `artifact.observed`
  - `payload.artifact_ref`, artifact kind, media type, content hash, size, and retention class.

Large state, prompts, responses, file bodies, and tool outputs belong in `*_ref` fields or `content.body_ref`. Producers must not duplicate them into every envelope.

### Agent Proxy model I/O profile

Agent Proxy model body capture is opt-in and defaults off. Enabling it captures
every field in the complete normalized request and response bodies. The two
directions are separate restricted content artifacts with their own references
and hashes. They include messages or prompts, tool definitions and calls,
model-visible options, generated content, reasoning content, usage, and finish
state when present. They exclude transport credentials and hop-by-hop headers.

Retries, fallbacks, tool continuations, repair turns, streaming assembly, and
terminal failures preserve enough separate content references and causation to
reconstruct what the model received and produced at each attempt. When capture
is enabled, a successful response requires acknowledgement of both its complete
request and response content. Capture must not silently degrade to selected
fields or request-only evidence.

Other producers and Agent Proxy calls with capture disabled remain metadata-only
unless their own contract opts into body capture. Agent Proxy callers must not
duplicate model payloads into their logs. Runtime enforcement and the
restricted ser8 deployment opt-in landed under
[issue #77](https://forgejo.coilysiren.me/coilyco-flight-deck/agent-proxy/issues/77).

Model execution fact requirements are in
[trajectory-contract-v1.md](trajectory-contract-v1.md).

## Content, redaction, and access tiers

- `content.capture` is one of `metadata_only`, `redacted_body`, or `restricted_body`.
- `content.body_ref` points to separately retained content only when body capture is explicitly enabled. Enabling Agent Proxy body capture requires complete restricted request and response capture for that call.
- `content.body_sha256` hashes the retained canonical byte sequence. It is empty when no body is retained.
- `content.redaction.status` is one of `not_captured`, `redacted`, `restricted`, or `withheld`.
- `content.redaction.policy_version` identifies the redaction rules applied at capture or ingestion.
- `payload.retention_class` and `payload.access_tier` are required for artifacts and recommended for every event that has retained content.
- Consumers must honor the source access tier and cannot upgrade access through a derived dataset.

## Delivery, ordering, and replay

- Delivery is at least once. Consumers must be idempotent.
- A duplicate has the same `event_id` or the same `(source.name, idempotency_key)`. The raw store retains receipt metadata and the canonical accepted envelope without silently inventing a second logical event.
- Events can arrive out of order. `occurred_at` orders domain time and `observed_at` orders receipt time. Materializers use a documented watermark and can re-materialize when a late event arrives.
- A trajectory is partial when required correlations or terminal facts are missing. Materializers expose an explicit partial status instead of fabricating completion.
- Replay reads immutable raw envelopes in receipt order or a declared bounded time range. Replayers preserve the original event id, schema version, source, occurred time, and provenance while adding a separate replay receipt record.
- Invalid events are quarantined with validation errors and source receipt metadata. They are not transformed into valid events silently.

## Provenance and derived datasets

- `provenance.producer_event_ids` identifies raw events used to derive this event or record.
- `provenance.input_refs` identifies other source artifacts or state references.
- `provenance.transform` and `provenance.transform_version` identify the materializer, evaluator, or export transform.
- `provenance.content_sha256` is the hash of the canonical derived representation when one exists.
- Dataset manifests additionally record the schema version, selection policy, split policy, redaction policy, source event ids or immutable query boundary, and content hashes.

## Producer and consumer rules

- Producers generate stable ids before delivery and do not put secrets in the envelope.
- Producers emit the smallest sufficient metadata-only record when body capture is not opted in. Agent Proxy capture is an explicit deployment opt-in, not an automatic consequence of routing through the proxy.
- Consumers validate schema version and required fields before materialization.
- Consumers preserve unknown optional fields for forward compatibility.
- Consumers never use trace, Ward, repository, or issue correlation as an authorization mechanism.
- Contract fixtures must cover valid events, invalid events, duplicate delivery, late delivery, partial trajectories, replay, redacted bodies, and restricted bodies.

## Executable package

The contract is implemented by `app.trajectory`, exported for non-Python
consumers as `schemas/trajectory-event-v1.schema.json`, and exercised by the
shared fixtures under `tests/fixtures/trajectory/`. Compatibility and producer
or consumer use are documented in
[`trajectory-schema-package.md`](trajectory-materialization.md).
