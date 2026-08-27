# Trajectory retention and replay

How raw contract-v1 envelopes reach durable storage, what the append-only ledger
guarantees, and how replay and recovery work.

Agent Proxy retains contract-v1 trajectory deliveries in a file-backed SQLite
database using WAL mode and full synchronous commits. SQLite is the first
storage boundary because the current service is single-writer, the complete raw
ledger remains portable, and replay does not depend on a separate live service.
The schema and replay API keep a later storage migration possible.

## Intake

`POST /v1/trajectory/events` is the internal cold-path intake:

* `202` means the canonical envelope was accepted and committed.
* `200` means the event was an idempotent duplicate. The new receipt remains in
  the ledger, but no second logical event is created.
* `422` means validation failed. The raw delivery, safe validation errors, and
  receipt metadata remain in quarantine.

The endpoint grants no execution authority. Deployment policy controls who can
reach it, and Ward remains the authorization and lifecycle authority.

Hot-path producers use `AsyncTrajectoryEmitter.emit_nowait()`. The emitter has a
fixed queue bound, returns `False` instead of waiting when full, and moves the
SQLite commit onto a worker thread. A model response never waits for
materialization, evaluation, or a cold-path storage write. This describes the
current metadata-only emitter, not the opt-in full-I/O capture contract.

## Restricted model I/O

Model body capture is opt-in and defaults off. When enabled, Agent Proxy captures
every field in the complete normalized request and response bodies as separate
restricted content artifacts. The trajectory ledger carries their content
references and hashes, not copied bodies. Callers retain correlation and
operational metadata only.

With capture disabled, stdout, OTLP, and SigNoz remain metadata-only. With
capture enabled, the configured body-bearing sink requires restricted handling.
The repository implementation acknowledges complete request and response bodies
through paired structured-log events and canonical request-span attributes
without silently degrading to selected fields or request-only evidence. The
restricted ser8 deployment opt-in and live sink verification completed under
[issue #77](https://forgejo.coilysiren.me/coilyco-flight-deck/agent-proxy/issues/77).
Durable restricted artifact retention and trajectory-ledger content references
remain separate cold-path work and are not implied by the operational capture
events.

## Immutable ledger

The database contains four append-only surfaces:

* `events` stores one canonical validated envelope per logical event.
* `receipts` stores every accepted, duplicate, quarantined, and replay delivery
  with receipt time, raw bytes, and content digest.
* `event_aliases` maps a producer-rekeyed duplicate to its canonical event.
* `quarantine` records validation failures without rewriting the raw delivery.

Database triggers reject update and delete statements on all four surfaces.
Duplicate detection uses either `event_id` or
`(source.name, idempotency_key)`. Events retain domain and observed timestamps,
while receipts retain ingestion time.

## Retention and access

Artifact events carry required `retention_class` and `access_tier` values.
Other events default to `standard` retention and `internal` access. Restricted
body capture always defaults to the `restricted` tier. Derived consumers may
narrow access but cannot upgrade it.

This slice records retention and access policy. It does not implement expiry,
legal hold, or destructive compaction. Those operations require a separately
authorized lifecycle surface because the raw ledger is deliberately immutable.

## Deployment

`PROXY_TRAJECTORY_DB_PATH` selects the database and defaults to
`./data/trajectory.sqlite3`. A deployment must mount that path on durable
storage. A container layer is not durable retention. Backups copy the database
through SQLite's online backup mechanism or from a quiesced volume, never by
editing ledger rows.

## Replay and recovery

`TrajectoryStore.replay_into()` reads canonical envelopes in receipt order or a
declared occurrence-time range. It preserves event identity, schema version,
source, domain time, and provenance. The source ledger appends a distinct
`replayed` receipt for each delivery.

Recovery creates a fresh store or consumer, replays the retained ledger, and
compares accepted plus duplicate counts with the attempted count. Quarantined
replay results stop promotion until the consumer contract is corrected. The
repository test suite demonstrates reconstruction into an empty database.
