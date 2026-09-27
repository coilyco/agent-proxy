"""Append-only SQLite retention and bounded cold-path trajectory ingestion."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import ValidationError

from app.obs import get_logger, record_error
from app.trajectory.schema import TrajectoryEvent, canonical_event_bytes, validate_event

log = get_logger("agent-proxy.trajectory.store")

IngestOutcome = Literal["accepted", "duplicate", "quarantined"]


@dataclass(frozen=True)
class IngestResult:
    outcome: IngestOutcome
    receipt_id: int
    event_id: str | None
    canonical_event_id: str | None
    errors: tuple[dict[str, Any], ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "receipt_id": self.receipt_id,
            "event_id": self.event_id,
            "canonical_event_id": self.canonical_event_id,
            "errors": list(self.errors),
        }


@dataclass(frozen=True)
class ReplayResult:
    attempted: int
    accepted: int
    duplicates: int
    quarantined: int


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _raw_bytes(payload: Any) -> bytes:
    if isinstance(payload, bytes):
        return payload
    if isinstance(payload, str):
        return payload.encode("utf-8")
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def _parse_payload(raw: bytes) -> Any:
    return json.loads(raw.decode("utf-8"))


def _validation_errors(exc: Exception) -> tuple[dict[str, Any], ...]:
    if isinstance(exc, ValidationError):
        return tuple(
            {
                "location": [str(part) for part in error["loc"]],
                "message": error["msg"],
                "type": error["type"],
            }
            for error in exc.errors(include_url=False, include_input=False)
        )
    return ({"location": [], "message": str(exc), "type": "json_invalid"},)


class TrajectoryStore:
    """File-backed immutable event store with receipt and quarantine ledgers."""

    def __init__(
        self,
        path: str | Path,
        *,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self.path = Path(path)
        self._clock = clock
        self._initialized = False

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def initialize(self) -> None:
        if self._initialized:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = FULL")
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS receipts (
                    receipt_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    received_at TEXT NOT NULL,
                    outcome TEXT NOT NULL CHECK (
                        outcome IN ('accepted', 'duplicate', 'quarantined', 'replayed')
                    ),
                    event_id TEXT,
                    canonical_event_id TEXT,
                    source_name TEXT,
                    idempotency_key TEXT,
                    envelope_sha256 TEXT NOT NULL,
                    raw_envelope BLOB NOT NULL,
                    errors_json TEXT NOT NULL DEFAULT '[]'
                );

                CREATE TABLE IF NOT EXISTS events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    source_name TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    schema_version TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    first_receipt_id INTEGER NOT NULL UNIQUE,
                    envelope_sha256 TEXT NOT NULL,
                    envelope BLOB NOT NULL,
                    retention_class TEXT NOT NULL,
                    access_tier TEXT NOT NULL,
                    UNIQUE (source_name, idempotency_key),
                    FOREIGN KEY (first_receipt_id) REFERENCES receipts(receipt_id)
                );

                CREATE TABLE IF NOT EXISTS event_aliases (
                    alias_event_id TEXT PRIMARY KEY,
                    canonical_event_id TEXT NOT NULL,
                    source_name TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    receipt_id INTEGER NOT NULL,
                    FOREIGN KEY (canonical_event_id) REFERENCES events(event_id),
                    FOREIGN KEY (receipt_id) REFERENCES receipts(receipt_id)
                );

                CREATE TABLE IF NOT EXISTS quarantine (
                    receipt_id INTEGER PRIMARY KEY,
                    errors_json TEXT NOT NULL,
                    FOREIGN KEY (receipt_id) REFERENCES receipts(receipt_id)
                );

                CREATE INDEX IF NOT EXISTS events_occurred_at_idx
                    ON events(occurred_at, sequence);
                CREATE INDEX IF NOT EXISTS receipts_canonical_event_idx
                    ON receipts(canonical_event_id, receipt_id);

                CREATE TRIGGER IF NOT EXISTS events_no_update
                    BEFORE UPDATE ON events
                    BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS events_no_delete
                    BEFORE DELETE ON events
                    BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS receipts_no_update
                    BEFORE UPDATE ON receipts
                    BEGIN SELECT RAISE(ABORT, 'receipts are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS receipts_no_delete
                    BEFORE DELETE ON receipts
                    BEGIN SELECT RAISE(ABORT, 'receipts are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS aliases_no_update
                    BEFORE UPDATE ON event_aliases
                    BEGIN SELECT RAISE(ABORT, 'event aliases are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS aliases_no_delete
                    BEFORE DELETE ON event_aliases
                    BEGIN SELECT RAISE(ABORT, 'event aliases are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS quarantine_no_update
                    BEFORE UPDATE ON quarantine
                    BEGIN SELECT RAISE(ABORT, 'quarantine is append-only'); END;
                CREATE TRIGGER IF NOT EXISTS quarantine_no_delete
                    BEFORE DELETE ON quarantine
                    BEGIN SELECT RAISE(ABORT, 'quarantine is append-only'); END;
                """)
        self._initialized = True

    def _insert_receipt(
        self,
        connection: sqlite3.Connection,
        *,
        outcome: str,
        raw: bytes,
        digest: str,
        event_id: str | None,
        canonical_event_id: str | None,
        source_name: str | None,
        idempotency_key: str | None,
        errors: tuple[dict[str, Any], ...] = (),
    ) -> int:
        cursor = connection.execute(
            """
            INSERT INTO receipts (
                received_at, outcome, event_id, canonical_event_id, source_name,
                idempotency_key, envelope_sha256, raw_envelope, errors_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _timestamp(self._clock()),
                outcome,
                event_id,
                canonical_event_id,
                source_name,
                idempotency_key,
                digest,
                raw,
                json.dumps(errors, sort_keys=True, separators=(",", ":")),
            ),
        )
        if cursor.lastrowid is None:
            raise RuntimeError("SQLite did not return a trajectory receipt id")
        return cursor.lastrowid

    def ingest(self, payload: Any) -> IngestResult:
        """Validate and durably retain one at-least-once delivery."""

        self.initialize()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            return self._ingest_on(connection, payload)

    def ingest_many(self, payloads: Iterable[Any]) -> tuple[IngestResult | Exception, ...]:
        """Retain a batch in one transaction, so one commit pays for every event.

        Each event runs under its own savepoint, so one that fails is rolled
        back alone and comes back as its exception in the matching position.
        """

        self.initialize()
        results: list[IngestResult | Exception] = []
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for payload in payloads:
                connection.execute("SAVEPOINT trajectory_event")
                try:
                    results.append(self._ingest_on(connection, payload))
                except Exception as exc:
                    connection.execute("ROLLBACK TO trajectory_event")
                    results.append(exc)
                connection.execute("RELEASE trajectory_event")
        return tuple(results)

    def _ingest_on(self, connection: sqlite3.Connection, payload: Any) -> IngestResult:
        raw = _raw_bytes(payload)
        raw_digest = hashlib.sha256(raw).hexdigest()
        try:
            parsed = _parse_payload(raw)
            event = validate_event(parsed)
        except (UnicodeDecodeError, json.JSONDecodeError, ValidationError, ValueError) as exc:
            errors = _validation_errors(exc)
            event_id = parsed.get("event_id") if isinstance(parsed, dict) else None
            source = parsed.get("source") if isinstance(parsed, dict) else None
            source_name = source.get("name") if isinstance(source, dict) else None
            idempotency_key = parsed.get("idempotency_key") if isinstance(parsed, dict) else None
            receipt_id = self._insert_receipt(
                connection,
                outcome="quarantined",
                raw=raw,
                digest=raw_digest,
                event_id=event_id if isinstance(event_id, str) else None,
                canonical_event_id=None,
                source_name=source_name if isinstance(source_name, str) else None,
                idempotency_key=(idempotency_key if isinstance(idempotency_key, str) else None),
                errors=errors,
            )
            connection.execute(
                "INSERT INTO quarantine (receipt_id, errors_json) VALUES (?, ?)",
                (
                    receipt_id,
                    json.dumps(errors, sort_keys=True, separators=(",", ":")),
                ),
            )
            return IngestResult(
                outcome="quarantined",
                receipt_id=receipt_id,
                event_id=event_id if isinstance(event_id, str) else None,
                canonical_event_id=None,
                errors=errors,
            )

        canonical = canonical_event_bytes(event)
        canonical_digest = hashlib.sha256(canonical).hexdigest()
        event_id = str(event.event_id)
        duplicate = connection.execute(
            """
            SELECT event_id
            FROM events
            WHERE event_id = ? OR (source_name = ? AND idempotency_key = ?)
            ORDER BY CASE WHEN event_id = ? THEN 0 ELSE 1 END
            LIMIT 1
            """,
            (event_id, event.source.name, event.idempotency_key, event_id),
        ).fetchone()
        if duplicate is not None:
            canonical_event_id = str(duplicate["event_id"])
            receipt_id = self._insert_receipt(
                connection,
                outcome="duplicate",
                raw=raw,
                digest=raw_digest,
                event_id=event_id,
                canonical_event_id=canonical_event_id,
                source_name=event.source.name,
                idempotency_key=event.idempotency_key,
            )
            if event_id != canonical_event_id:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO event_aliases (
                        alias_event_id, canonical_event_id, source_name,
                        idempotency_key, receipt_id
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        event_id,
                        canonical_event_id,
                        event.source.name,
                        event.idempotency_key,
                        receipt_id,
                    ),
                )
            return IngestResult(
                outcome="duplicate",
                receipt_id=receipt_id,
                event_id=event_id,
                canonical_event_id=canonical_event_id,
            )

        receipt_id = self._insert_receipt(
            connection,
            outcome="accepted",
            raw=raw,
            digest=raw_digest,
            event_id=event_id,
            canonical_event_id=event_id,
            source_name=event.source.name,
            idempotency_key=event.idempotency_key,
        )
        payload_fields = event.payload.model_dump(mode="json", exclude_none=True)
        retention_class = str(payload_fields.get("retention_class") or "standard")
        access_tier = str(
            payload_fields.get("access_tier")
            or ("restricted" if event.content.capture == "restricted_body" else "internal")
        )
        connection.execute(
            """
            INSERT INTO events (
                event_id, source_name, idempotency_key, schema_version,
                event_type, occurred_at, observed_at, first_receipt_id,
                envelope_sha256, envelope, retention_class, access_tier
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                event.source.name,
                event.idempotency_key,
                event.schema_version,
                event.event_type,
                _timestamp(event.occurred_at),
                _timestamp(event.observed_at),
                receipt_id,
                canonical_digest,
                canonical,
                retention_class,
                access_tier,
            ),
        )
        return IngestResult(
            outcome="accepted",
            receipt_id=receipt_id,
            event_id=event_id,
            canonical_event_id=event_id,
        )

    async def ingest_async(self, payload: Any) -> IngestResult:
        return await asyncio.to_thread(self.ingest, payload)

    async def ingest_many_async(
        self, payloads: Iterable[Any]
    ) -> tuple[IngestResult | Exception, ...]:
        return await asyncio.to_thread(self.ingest_many, tuple(payloads))

    def load_event(self, event_id: str) -> TrajectoryEvent | None:
        self.initialize()
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT envelope
                FROM events
                WHERE event_id = COALESCE(
                    (SELECT canonical_event_id FROM event_aliases WHERE alias_event_id = ?),
                    ?
                )
                """,
                (event_id, event_id),
            ).fetchone()
        if row is None:
            return None
        return validate_event(_parse_payload(bytes(row["envelope"])))

    def iter_events(
        self,
        *,
        occurred_from: datetime | None = None,
        occurred_to: datetime | None = None,
    ) -> Iterable[TrajectoryEvent]:
        self.initialize()
        clauses: list[str] = []
        params: list[str] = []
        if occurred_from is not None:
            clauses.append("occurred_at >= ?")
            params.append(_timestamp(occurred_from))
        if occurred_to is not None:
            clauses.append("occurred_at < ?")
            params.append(_timestamp(occurred_to))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT envelope FROM events {where} ORDER BY sequence",  # noqa: S608
                params,
            ).fetchall()
        return tuple(validate_event(_parse_payload(bytes(row["envelope"]))) for row in rows)

    def backup_to(self, path: str | Path) -> Path:
        """Write one transactionally consistent SQLite backup."""

        self.initialize()
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as source, sqlite3.connect(target) as destination:
            source.backup(destination)
        return target

    def replay_into(
        self,
        consumer: "TrajectoryStore",
        *,
        occurred_from: datetime | None = None,
        occurred_to: datetime | None = None,
        record_receipts: bool = True,
    ) -> ReplayResult:
        events = tuple(self.iter_events(occurred_from=occurred_from, occurred_to=occurred_to))
        accepted = 0
        duplicates = 0
        quarantined = 0
        for event in events:
            raw = canonical_event_bytes(event)
            result = consumer.ingest(raw)
            if result.outcome == "accepted":
                accepted += 1
            elif result.outcome == "duplicate":
                duplicates += 1
            else:
                quarantined += 1
            if record_receipts:
                with self._connect() as connection:
                    self._insert_receipt(
                        connection,
                        outcome="replayed",
                        raw=raw,
                        digest=hashlib.sha256(raw).hexdigest(),
                        event_id=str(event.event_id),
                        canonical_event_id=str(event.event_id),
                        source_name=event.source.name,
                        idempotency_key=event.idempotency_key,
                        errors=(
                            {
                                "location": [],
                                "message": f"consumer outcome: {result.outcome}",
                                "type": "replay_outcome",
                            },
                        ),
                    )
        return ReplayResult(
            attempted=len(events),
            accepted=accepted,
            duplicates=duplicates,
            quarantined=quarantined,
        )

    def receipt_outcomes(self) -> tuple[str, ...]:
        self.initialize()
        with self._connect() as connection:
            rows = connection.execute("SELECT outcome FROM receipts ORDER BY receipt_id").fetchall()
        return tuple(str(row["outcome"]) for row in rows)


class AsyncTrajectoryEmitter:
    """Bounded emitter for hot-path callers that must never await storage."""

    def __init__(self, store: TrajectoryStore, *, maxsize: int = 256, max_batch: int = 256) -> None:
        if maxsize < 1:
            raise ValueError("maxsize must be positive")
        if max_batch < 1:
            raise ValueError("max_batch must be positive")
        self.store = store
        self.max_batch = max_batch
        self._queue: asyncio.Queue[Any | None] = asyncio.Queue(maxsize=maxsize)
        self._worker: asyncio.Task[None] | None = None
        self.dropped = 0
        self.failed = 0
        self.last_error_class = ""

    async def start(self) -> None:
        if self._worker is None:
            self._worker = asyncio.create_task(self._run(), name="trajectory-ingest")

    def emit_nowait(self, payload: Any) -> bool:
        try:
            self._queue.put_nowait(payload)
            return True
        except asyncio.QueueFull:
            self.dropped += 1
            return False

    async def flush(self) -> None:
        await self._queue.join()

    async def stop(self) -> None:
        if self._worker is None:
            return
        await self.flush()
        await self._queue.put(None)
        await self._worker
        self._worker = None

    async def _run(self) -> None:
        # One transaction per drained batch, never per event
        # (docs/trajectory-retention.md, teable:coilyco/agent-proxy#8378).
        while True:
            batch = [await self._queue.get()]
            while len(batch) < self.max_batch and batch[-1] is not None:
                try:
                    batch.append(self._queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
            stopping = batch[-1] is None
            payloads = batch[:-1] if stopping else batch
            try:
                if payloads:
                    await self._persist(payloads)
            finally:
                for _ in batch:
                    self._queue.task_done()
            if stopping:
                return

    async def _persist(self, payloads: list[Any]) -> None:
        try:
            results: tuple[Any, ...] = await self.store.ingest_many_async(payloads)
        except Exception as exc:
            results = (exc,) * len(payloads)
        for result in results:
            if isinstance(result, Exception):
                self.failed += 1
                self.last_error_class = type(result).__name__
                record_error("trajectory_event_persist_failed")
                log.warning(
                    "trajectory.event.persist_failed",
                    error_class=self.last_error_class,
                    failed=self.failed,
                )
