# Roadmap

Dependency-ordered sequencing for the architecture-v2 work. It explains order
without treating future capabilities as prohibited work.

Agent Proxy is becoming the observation, trajectory collection, and data-processing plane for the agentic operations stack. The delivered reliability proxy remains useful as the first collection tap. LiteLLM becomes the commodity inference gateway only after a parity decision demonstrates that it preserves required behavior.

This roadmap is an active work graph, not a ban on later capability work. The ownership boundary is defined in [`architecture-v2.md`](architecture-v2.md), and event interoperability is defined in [`trajectory-contract-v1.md`](trajectory-contract-v1.md).

## Current baseline

- The current service is an OpenAI-compatible reliability gateway with logical
  route validation, context protection, queueing, validation, retries,
  fallbacks, circuit breaking, and operational telemetry.
- The request path remains stateless except for its in-memory queue and caches.
- Ward correlation metadata reaches logs and traces.
- Skill-use artifact ingestion durably retains contract-v1 observations and
  keeps logs and Prometheus as operational projections.
- Append-only SQLite retention, replay, materialization, evaluation joins,
  dataset exports, and governed views have landed in the cold path.
- SigNoz and OTLP support operations without becoming the trajectory system of
  record.

## Implementation sequence

1. [#41 LiteLLM standalone versus SDK parity spike](https://forgejo.coilysiren.me/coilyco-flight-deck/agent-proxy/issues/41)
   - Landed: selects the standalone LiteLLM Proxy and adds an executable surface parity runner.
   - Current reliability behavior stays until the documented live tower, key, budget, spend, context-safety, and trace gates pass.
2. [#42 Trajectory schema package and validation fixtures](https://forgejo.coilysiren.me/coilyco-flight-deck/agent-proxy/issues/42)
   - Landed: establishes the shared executable contract independently of the retention system.
3. [#43 Append-only ingestion and replayable raw retention](https://forgejo.coilysiren.me/coilyco-flight-deck/agent-proxy/issues/43)
   - Depends on #42.
   - Landed: SQLite WAL retention, immutable receipt and quarantine ledgers, replay, and a bounded asynchronous emitter provide durable evidence outside the hot path.
4. [#44 Episode and trajectory materialization](https://forgejo.coilysiren.me/coilyco-flight-deck/agent-proxy/issues/44)
   - Depends on #42 and #43.
   - Landed: deterministically joins retained events, exposes partial and late state, and appends provenance-preserving derived revisions.
5. [#45 Evaluation and annotation records](https://forgejo.coilysiren.me/coilyco-flight-deck/agent-proxy/issues/45)
   - Depends on #42 and #44.
   - Landed: evaluator, verifier, annotation, and human-intervention records append immutable evidence without changing Ward authority.
6. [#46 Versioned training and held-out evaluation exports](https://forgejo.coilysiren.me/coilyco-flight-deck/agent-proxy/issues/46)
   - Depends on #44 and #45.
   - Landed: reproducible SFT, preference, verifier, reward, and held-out evaluation artifacts use write-once provenance manifests and trajectory-level splits.
7. [#47 Operational queries, Ward dossier inputs, and harness-fit views](https://forgejo.coilysiren.me/coilyco-flight-deck/agent-proxy/issues/47)
   - Depends on #43, #44, and #45.
   - Landed: exposes governed operational evidence, OTLP joins, harness-fit comparisons, and evidence-only dossier inputs without moving authority out of Ward.

## Producer follow-through

- [#51 Agent-compose evidence](https://forgejo.coilysiren.me/coilyco-flight-deck/agent-proxy/issues/51) - landed - maps immutable manifests and public-safe selection traces without copying context bodies.
- [#52 Guard evidence](https://forgejo.coilysiren.me/coilyco-flight-deck/agent-proxy/issues/52) - landed - maps cli-guard audit rows and specgen policy snapshots without retaining sensitive arguments, diagnostics, paths, or hosts.
- [#54 Ward skill-use evidence](https://forgejo.coilysiren.me/coilyco-flight-deck/agent-proxy/issues/54) - landed - persists normalized reap artifacts while preserving log and metric projections.
- [#55 Request lifecycle evidence](https://forgejo.coilysiren.me/coilyco-flight-deck/agent-proxy/issues/55) - landed - adds opt-in bounded action and terminal execution emission without storage waits or body capture.
- [#77 Opt-in full-I/O capture](https://forgejo.coilysiren.me/coilyco-flight-deck/agent-proxy/issues/77) - landed - captures every field in complete normalized request and response bodies without allowing selected-field or request-only degradation, with the restricted ser8 deployment and live SigNoz proof complete.

## Delivery rules

- Keep expensive ingestion, materialization, evaluation, and export processing off the model request path.
- When full-I/O capture is enabled, permit only the bounded acknowledgement required to prevent partial or request-only capture on the model request path.
- Use durable raw event retention as the replay source. Do not treat SigNoz or OTLP as the training-data system of record.
- Make raw and derived data privacy-aware, redacted where required, and access-tiered.
- Preserve source event ids, schema versions, provenance, content hashes, and transform versions through every derived dataset.
- Keep Ward as the authorization, execution, lifecycle, recovery, and governance authority.
- Record actual shipped capabilities in `docs/FEATURES.md`. Planning documents must not imply that unimplemented v2 components are landed.

## Historical context

The aosh architecture correction in `coilyco-bridge/agentic-os-hardware#36` is the companion source for this reset. Earlier reliability-proxy work remains useful evidence. It is not a substitute for the new work graph and must not be reopened or repurposed.
