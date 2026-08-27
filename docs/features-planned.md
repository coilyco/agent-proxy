# Planned work and references

Part of [FEATURES](FEATURES.md).

## Planned architecture v2


- **LiteLLM commodity behavior retirement** - planned - joined live evidence must still prove context delivery, trace continuity, retry ownership, and rollback before Agent Proxy provider routing, retries, fallbacks, queueing, or circuit behavior can retire.

## References


- [`architecture-v2.md`](architecture-v2.md) states ownership boundaries and the migration inventory.
- [`trajectory-contract-v1.md`](trajectory-contract-v1.md) specifies the event contract.
- [`trajectory-schema-package.md`](trajectory-materialization.md) documents package compatibility and producer or consumer use.
- [`trajectory-retention.md`](trajectory-retention.md) documents intake, raw retention, replay, and recovery.
- [`trajectory-materialization.md`](trajectory-materialization.md) documents deterministic assembly and re-materialization.
- [`evaluation-records.md`](evaluation-records.md) documents evaluation, annotation, and intervention evidence.
- [`dataset-exports.md`](evaluation-records.md) documents schemas, manifests, split safety, and reproducibility.
- [`operational-views.md`](operational-views.md) documents queries, dossiers, access, freshness, and backfill.
- [`litellm-parity.md`](litellm-parity.md) documents the standalone decision and cutover gates.
- [`route-registry.md`](route-registry.md) documents logical routing and rollback.
- [`ROADMAP.md`](ROADMAP.md) and [`work-graph.md`](work-graph.md) define execution order.
- [`proxy.md`](proxy.md) remains the detailed guide to the currently landed reliability behavior.
