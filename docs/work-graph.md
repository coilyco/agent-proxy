# Architecture v2 work graph

The issues below are fresh implementation work for Agent Proxy architecture v2. They are intentionally separate from closed reliability-proxy issues, which remain evidence only.

1. #41 LiteLLM standalone versus SDK parity spike
   - No dependency.
   - Landed: selects standalone LiteLLM and supplies executable gates that must pass before current behavior is removed.
2. #42 Trajectory schema package and validation fixtures
   - No dependency.
   - Implements the shared contract in [`trajectory-contract-v1.md`](trajectory-contract-v1.md).
3. #43 Append-only ingestion and replayable raw retention
   - Depends on #42.
   - Landed: establishes durable, replayable SQLite evidence without using SigNoz as the sole store.
4. #44 Episode and trajectory materialization
   - Depends on #42 and #43.
   - Landed: builds deterministic, re-materializable correlated trajectories.
5. #45 Evaluation and annotation records
   - Depends on #42 and #44.
   - Landed: adds evaluator, verifier, annotation, and human-intervention evidence.
6. #46 Versioned training and held-out evaluation exports
   - Depends on #44 and #45.
   - Landed: produces reproducible SFT, preference, verifier, reward, and held-out evaluation manifests.
7. #47 Operational queries, Ward dossier inputs, and harness-fit views
   - Depends on #43, #44, and #45.
   - Landed: produces governed evidence views while keeping Ward as the authority.

Every dependency-ordered issue above has landed.

## Producer and request-path follow-through

- #51 maps agent-compose bundle evidence.
- #52 maps cli-guard audit and specgen policy evidence.
- #54 durably retains Ward skill-use observations.
- #55 offers request lifecycle evidence to the bounded emitter.
- #77 - landed - enforces opt-in capture of every field in complete model request and response bodies at the Agent Proxy boundary, with the restricted ser8 deployment and live SigNoz proof complete.

These slices are independently verified and keep Ward as the execution
authority. Live deployment, access, and cutover gates remain coordinated
follow-through rather than speculative changes to the current gateway.
