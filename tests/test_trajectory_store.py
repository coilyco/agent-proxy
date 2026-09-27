"""Durable append-only trajectory ingestion and replay."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from app.trajectory import AsyncTrajectoryEmitter, TrajectoryStore
from app.trajectory.evidence import verify_trajectory_recovery

FIXTURES = Path("tests/fixtures/trajectory")


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_accepted_event_survives_store_reopen(tmp_path):
    path = tmp_path / "trajectory.sqlite3"
    result = TrajectoryStore(path).ingest(_fixture("valid.json"))

    reopened = TrajectoryStore(path)
    event = reopened.load_event(result.event_id or "")
    assert result.outcome == "accepted"
    assert event is not None
    assert str(event.event_id) == result.event_id
    assert reopened.receipt_outcomes() == ("accepted",)


def test_duplicate_delivery_retains_receipt_without_second_event(tmp_path):
    store = TrajectoryStore(tmp_path / "trajectory.sqlite3")

    original = store.ingest(_fixture("duplicate-original.json"))
    duplicate = store.ingest(_fixture("duplicate-redelivery.json"))

    assert original.outcome == "accepted"
    assert duplicate.outcome == "duplicate"
    assert duplicate.canonical_event_id == original.event_id
    assert len(tuple(store.iter_events())) == 1
    assert store.receipt_outcomes() == ("accepted", "duplicate")


def test_rekeyed_duplicate_creates_a_resolvable_alias(tmp_path):
    store = TrajectoryStore(tmp_path / "trajectory.sqlite3")
    original_payload = _fixture("duplicate-original.json")
    original = store.ingest(original_payload)
    rekeyed = dict(original_payload)
    rekeyed["event_id"] = "018f6d1d-5e54-7c20-bf7e-5bd1ca1e81a1"

    duplicate = store.ingest(rekeyed)

    assert duplicate.outcome == "duplicate"
    alias = store.load_event(rekeyed["event_id"])
    assert alias is not None
    assert str(alias.event_id) == original.event_id


def test_invalid_delivery_is_quarantined_with_raw_receipt(tmp_path):
    store = TrajectoryStore(tmp_path / "trajectory.sqlite3")

    result = store.ingest(_fixture("invalid-missing-event-id.json"))

    assert result.outcome == "quarantined"
    assert result.errors
    assert tuple(store.iter_events()) == ()
    assert store.receipt_outcomes() == ("quarantined",)


def test_store_tables_reject_update_and_delete(tmp_path):
    store = TrajectoryStore(tmp_path / "trajectory.sqlite3")
    result = store.ingest(_fixture("valid.json"))

    with sqlite3.connect(store.path) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                "UPDATE events SET event_type = 'execution.failed' WHERE event_id = ?",
                (result.event_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("DELETE FROM receipts")


def test_replay_rebuilds_a_fresh_consumer_and_records_receipt(tmp_path):
    source = TrajectoryStore(tmp_path / "source.sqlite3")
    target = TrajectoryStore(tmp_path / "target.sqlite3")
    source.ingest(_fixture("valid.json"))
    source.ingest(_fixture("late-event.json"))

    result = source.replay_into(target)

    assert result.attempted == 2
    assert result.accepted == 2
    assert len(tuple(target.iter_events())) == 2
    assert source.receipt_outcomes() == (
        "accepted",
        "accepted",
        "replayed",
        "replayed",
    )


def test_online_backup_and_replay_evidence_is_counts_only(tmp_path):
    source = TrajectoryStore(tmp_path / "source.sqlite3")
    source.ingest(_fixture("valid.json"))
    source.ingest(_fixture("late-event.json"))

    evidence = verify_trajectory_recovery(
        source.path,
        tmp_path / "backup.sqlite3",
        tmp_path / "replay.sqlite3",
    )

    assert evidence.passed is True
    assert evidence.source_events == 2
    assert evidence.backup_events == 2
    assert evidence.replay_events == 2
    assert evidence.replay.accepted == 2
    assert evidence.source_quarantined_receipts == 0
    rendered = evidence.model_dump(mode="json")
    assert "raw_envelope" not in str(rendered)
    assert "parity fixture" not in str(rendered)


def test_recovery_evidence_refuses_to_overwrite_artifacts(tmp_path):
    source = TrajectoryStore(tmp_path / "source.sqlite3")
    source.ingest(_fixture("valid.json"))
    backup = tmp_path / "backup.sqlite3"
    backup.touch()

    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        verify_trajectory_recovery(source.path, backup, tmp_path / "replay.sqlite3")


async def test_async_emitter_is_bounded_and_flushes_to_durable_store(tmp_path):
    store = TrajectoryStore(tmp_path / "trajectory.sqlite3")
    emitter = AsyncTrajectoryEmitter(store, maxsize=1)

    assert emitter.emit_nowait(_fixture("valid.json")) is True
    assert emitter.emit_nowait(_fixture("late-event.json")) is False
    assert emitter.dropped == 1

    await emitter.start()
    await emitter.stop()

    assert len(tuple(store.iter_events())) == 1


async def test_async_emitter_survives_one_persistence_failure():
    class FlakyStore:
        def __init__(self):
            self.calls = 0

        async def ingest_many_async(self, payloads):
            self.calls += 1
            if self.calls == 1:
                raise OSError("fixture persistence failure")
            return tuple(None for _ in payloads)

    store = FlakyStore()
    emitter = AsyncTrajectoryEmitter(store, maxsize=2, max_batch=1)
    await emitter.start()

    assert emitter.emit_nowait({"event": 1})
    assert emitter.emit_nowait({"event": 2})
    await emitter.stop()

    assert store.calls == 2
    assert emitter.failed == 1
    assert emitter.last_error_class == "OSError"


async def test_async_emitter_drains_a_queued_burst_in_one_batch(tmp_path, monkeypatch):
    store = TrajectoryStore(tmp_path / "trajectory.sqlite3")
    batches: list[int] = []
    original = store.ingest_many

    def counted(payloads):
        batches.append(len(payloads))
        return original(payloads)

    monkeypatch.setattr(store, "ingest_many", counted)
    emitter = AsyncTrajectoryEmitter(store, maxsize=8)
    for name in ("valid.json", "late-event.json", "replay.json"):
        assert emitter.emit_nowait(_fixture(name))

    await emitter.start()
    await emitter.stop()

    assert batches == [3]
    assert len(tuple(store.iter_events())) == 3
    assert emitter.failed == 0


def test_ingest_many_commits_once_and_isolates_a_failing_event(tmp_path, monkeypatch):
    store = TrajectoryStore(tmp_path / "trajectory.sqlite3")
    store.initialize()
    connects = [0]
    original_connect = store._connect

    def counted_connect():
        connects[0] += 1
        return original_connect()

    original_ingest_on = store._ingest_on

    def failing_on_late(connection, payload):
        result = original_ingest_on(connection, payload)
        if payload.get("idempotency_key") == "late-observation":
            raise OSError("fixture failure after the event's writes")
        return result

    monkeypatch.setattr(store, "_connect", counted_connect)
    monkeypatch.setattr(store, "_ingest_on", failing_on_late)
    results = store.ingest_many(
        [
            _fixture("duplicate-original.json"),
            _fixture("late-event.json"),
            _fixture("duplicate-redelivery.json"),
            _fixture("invalid-missing-event-id.json"),
        ]
    )

    assert connects[0] == 1
    assert results[0].outcome == "accepted"
    assert isinstance(results[1], OSError)
    assert results[2].outcome == "duplicate"
    assert results[3].outcome == "quarantined"
    assert len(tuple(store.iter_events())) == 1
    assert store.receipt_outcomes() == ("accepted", "duplicate", "quarantined")
