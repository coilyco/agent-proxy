# Trajectory contract v1

The versioned producer and consumer contract for trajectory events. Independent
services build against this contract, so the filename and version are stable
even though the detail is split across the pages below.

The executable package and committed JSON Schema are the normative artifacts;
these pages state the rules they encode.

## Identity and time

- `event_id` is a stable UUIDv7 generated once by the producing system. Retransmission preserves it.
- `schema_name` is exactly `agentproxy.trajectory.event` for this contract.
- `schema_version` is a semantic version string. A major-version change is incompatible.
- `occurred_at` is the RFC 3339 UTC timestamp when the represented fact happened.
- `observed_at` is the RFC 3339 UTC timestamp when the producer or ingestion service observed the fact. It can be later than `occurred_at`.
- `source.name`, `source.version`, and `source.instance_id` identify the producer without exposing secrets or unstable hostnames.
- `idempotency_key` is stable for a producer's logical event. Consumers deduplicate by `(source.name, idempotency_key)` and retain event-id aliases when a producer rekeys an event during recovery.

## Correlation

`correlation` is an object whose string fields are optional when unknown and must be present as empty or omitted only when the producer cannot know them.

- `trace_id` and `span_id` use the active OpenTelemetry context when available.
- `ward_run_id` joins the Ward execution lifecycle.
- `episode_id` joins a multi-event unit of agent work.
- `agent_session_id` joins a harness or agent conversation.
- `request_id` joins one model or tool request.
- `repository`, `issue_ref`, and `workflow` join the engineering and governance context.
- Producers may add `parent_event_id`, `causation_event_id`, and `correlation_id` when their source has them.

No consumer may infer authorization from correlation. These fields are joins, not authority grants.

## Model execution facts

Model-request, model-response, and execution events include `payload.model_execution` when applicable:

```json
{
  "model": "qwen3:4b",
  "provider": "ollama",
  "provider_model": "qwen3:4b",
  "request_tokens": 1200,
  "response_tokens": 340,
  "total_tokens": 1540,
  "latency_ms": 812,
  "cost": {
    "amount": "0.000000",
    "currency": "USD",
    "calculation_version": "gateway-cost-v1"
  },
  "retry_count": 1,
  "fallback_count": 0,
  "fallback_from": [],
  "finish_reason": "stop"
}
```

- Use OpenTelemetry and OpenTelemetry GenAI semantic convention names in `attributes` where they fit, including `service.name`, `gen_ai.operation.name`, `gen_ai.request.model`, `gen_ai.response.model`, and applicable `gen_ai.usage.*` fields.
- Record domain joins and policy facts under `agentproxy.*`, including `agentproxy.policy.decision`, `agentproxy.ward.run_id`, `agentproxy.episode.id`, `agentproxy.context.safe_limit`, and `agentproxy.context.truncated`.
- `retry_count`, `fallback_count`, and `finish_reason` describe the final observed attempt. Individual attempts can be emitted as separate execution or observation events with their own ids.
- Token counts, latency, and cost may be `null` when unavailable. A consumer must distinguish unavailable from zero.

## Continued

- [trajectory-contract-v1-envelope](trajectory-contract-v1-envelope.md)
- [trajectory-contract-v1-taxonomy](trajectory-contract-v1-taxonomy.md)
- [trajectory-contract-v1-governance](trajectory-contract-v1-taxonomy.md)
