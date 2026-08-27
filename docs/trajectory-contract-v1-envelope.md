# Envelope, identity, and correlation

Part of [trajectory-contract-v1](trajectory-contract-v1.md).

## Purpose and status


This is the interoperable contract for events that describe an agentic trajectory. It is concrete enough for independent producers and consumers to implement. It is a design contract, not a claim that durable ingestion or materialization is already implemented.

The canonical envelope name is `agentproxy.trajectory.event`. A producer emits schema version `1.0` until a compatible revision is published. Unknown optional fields are preserved where possible. Unknown required fields or incompatible major versions are rejected to a quarantined delivery path.

## Normative envelope


Every event is a UTF-8 JSON object with these fields. Names ending in `_ref` identify external or separately retained content rather than embedding large state.

```json
{
  "event_id": "018f6d1d-5e54-7c20-bf7e-5bd1ca1e8198",
  "schema_name": "agentproxy.trajectory.event",
  "schema_version": "1.0",
  "event_type": "execution.completed",
  "occurred_at": "2026-07-23T05:30:34.123Z",
  "observed_at": "2026-07-23T05:30:34.401Z",
  "source": {
    "name": "agent-proxy",
    "version": "0.1.0",
    "instance_id": "proxy-pod-opaque-id"
  },
  "idempotency_key": "source-event-or-deterministic-key",
  "correlation": {
    "trace_id": "otel-trace-id",
    "span_id": "otel-span-id",
    "ward_run_id": "ward-run-id",
    "episode_id": "episode-id",
    "agent_session_id": "agent-session-id",
    "request_id": "request-id",
    "repository": "owner/repository",
    "issue_ref": "owner/repository#40",
    "workflow": "merge-remote-main"
  },
  "actor": {
    "type": "agent",
    "id": "opaque-agent-id",
    "role": "engineer"
  },
  "attributes": {
    "service.name": "agent-proxy",
    "gen_ai.operation.name": "chat",
    "gen_ai.request.model": "model-tag",
    "agentproxy.policy.decision": "allow"
  },
  "payload": {},
  "content": {
    "capture": "metadata_only",
    "body_ref": "",
    "body_sha256": "",
    "redaction": {
      "status": "not_captured",
      "policy_version": "v1"
    }
  },
  "provenance": {
    "producer_event_ids": [],
    "input_refs": [],
    "transform": "",
    "transform_version": "",
    "content_sha256": ""
  }
}
```

Identity, time, and correlation rules are in
[trajectory-contract-v1.md](trajectory-contract-v1.md).
