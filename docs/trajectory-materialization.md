# Episode and trajectory materialization

Materialization is a deterministic cold-path projection over the immutable raw
ledger. `TrajectoryMaterializer` performs reconstruction in memory and
`MaterializationStore` appends changed revisions to SQLite. Neither component is
imported by the model request path.

## Correlation

Events form a connected component when they share a strong execution identity:

* episode id
* Ward run id
* agent session id
* request id
* trace id

Repository, issue, workflow, span, causation, parent, and general correlation ids
remain sorted dimensions on the assembled record. Repository or issue alone
does not collapse unrelated runs into one trajectory.

The trajectory id is a stable digest of the component's strong identities. An
event without any strong identity receives an event-scoped trajectory id and an
explicit `missing_primary_correlation` partial reason.

## Ordering and watermarks

Events sort by domain occurrence time, then observed time, then event id. The
materializer sets its watermark to the maximum observed time minus the declared
allowed lateness, which defaults to five minutes. An event whose observation
delay exceeds that bound is listed in `late_event_ids`.

A trajectory stays `partial` when it lacks a terminal event or a primary
correlation. Missing facts are never replaced with fabricated completion.
Retries, fallbacks, human intervention, event-type counts, access tier, and
every correlation dimension remain explicit on the record.

## Re-materialization and provenance

Every record uses schema `agentproxy.trajectory.materialized` version `1.0`,
lists its ordered `source_event_ids`, and carries a SHA-256 digest of its
canonical representation.

The derived store is append-only. The same semantic reconstruction reuses its
existing revision. A late event or other raw evidence change appends the next
revision with a new content hash. Earlier revisions remain queryable and
database triggers reject update or delete operations.

`materialize_retained_events()` rebuilds the complete projection from the raw
ledger. The repository fixtures cover retries, fallbacks, human intervention,
late arrivals, missing terminal facts, and missing correlations.

`app.trajectory` is the executable producer and consumer contract for
[`trajectory-contract-v1.md`](trajectory-contract-v1.md). Python callers use
`validate_event()` and non-Python callers use
`schemas/trajectory-event-v1.schema.json`.

## Compatibility rules

* **Major versions** - consumers reject an event whose schema major is not `1`.
  Rejected deliveries belong in quarantine with their receipt metadata.
* **Compatible revisions** - producers may add optional fields within version
  `1.x`. Consumers preserve unknown fields when validating, retaining, replaying,
  or re-emitting the envelope.
* **Required fields** - removing a required field, changing its meaning, or
  narrowing an accepted value requires a new major version.
* **Event types** - a new event type requires a contract revision and updated
  producer and consumer fixtures. Consumers do not silently reinterpret an
  unknown event type.
* **Stable identity** - retransmission preserves `event_id`, `source`, and
  `idempotency_key`. Replay preserves the original identity in provenance and
  adds a distinct replay receipt.
* **Content capture** - metadata-only records cannot contain body references.
  Redacted and restricted bodies require an external reference, a SHA-256
  digest, and the matching redaction status.

## Producer use

Producers create a UTF-8 JSON object, validate it before delivery, and retain the
resulting stable identity for retries:

```python
from app.trajectory import canonical_event_bytes, validate_event

event = validate_event(payload)
wire_bytes = canonical_event_bytes(event)
```

`canonical_event_bytes()` sorts keys and preserves compatible extensions. It is
the hashing and append-only retention representation.

## Consumer use

Consumers validate before materialization and treat validation failures as
quarantined deliveries. They deduplicate on `event_id` or
`(source.name, idempotency_key)`, distinguish domain time from observed time,
and retain unknown optional fields.

The fixtures under `tests/fixtures/trajectory/` cover valid, invalid, duplicate,
late, partial, replay, redacted-body, and restricted-body behavior. Every
producer or consumer can replay the same fixtures as its compatibility gate.

## Schema export

`just schema` regenerates the committed JSON Schema from the Pydantic
models. A clean regeneration is required whenever the package changes.
