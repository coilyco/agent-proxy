# Per-model context budget

Part of [proxy.md](proxy.md).

## `num_ctx` binds local routes only (issue #115)

`num_ctx` is an Ollama parameter: the window you allocate when loading a model
into VRAM. It has no meaning for a hosted OpenAI-compatible provider, which has
no VRAM to run out of. The proxy nonetheless derived every route's prompt budget
from it, so `sirens-echo/deepseek` - a route with no `direct` target - inherited
48128 and trimmed at 47104, roughly 4.7% of the window DeepSeek's v4 line
advertises. Eleven trims over seven days, all at that same invariant number, and
every one of them manufactured the tool-pairing 400 in issue #113.

The discriminator already lives in the registry. A route with a `direct` target
is served out of the tower's VRAM and the ceiling is correct for it; a route
without one is served by a hosted provider and the ceiling is meaningless.
`models.derive_context_budget` returns the bound and which of four things
produced it:

* `model_window` - the route's declared `context_window`, or Ollama's own
  `/api/tags` figure for a local target below the ceiling.
* `vram_ceiling` - `PROXY_NUM_CTX_CEILING`, local routes only.
* `cost_ceiling` - `PROXY_CONTEXT_COST_CEILING`, a deliberate budget below the
  model's window for spend or latency reasons. Named for what it is, so a trim
  log never presents a cost decision as a hardware limit.
* `unbounded` - a hosted route with no declared window and no cost ceiling. The
  proxy trims nothing and injects no `num_ctx`, and the provider enforces its own
  limit and reports it in an error the caller can read.

The bound rides `request.prompt_trimmed` as `budget_bound_by`.

Why the `num_ctx` settings in [`app/config.py`](../app/config.py) hold the
values they do. All read from the environment with the `PROXY_` prefix.

## `num_ctx_ceiling` - the VRAM-safe upper bound (issue #32)

The proxy reads a model's real `context_length` from `/api/tags` and injects
`min(context_length, num_ctx_ceiling) - num_ctx_headroom`. Without the ceiling a
model advertising a huge window (`qwen3:4b` reports 262144) would allocate more
KV cache than the tower can carry. The default is the leg-01 proven-safe value.

## `num_ctx_headroom` - reserved completion budget

Held back from the injected window so the response and the chat template's
framing tokens have somewhere to go.

## `ollama_num_parallel` - the NUM_PARALLEL coupling (issue #33)

ollama loads `num_ctx` as the model's **total** context and divides it across
`OLLAMA_NUM_PARALLEL` slots, so the usable per-request window is
`num_ctx / NUM_PARALLEL`. A backend serving more than one slot would silently
halve, or worse, the window the proxy asked for.

The proxy compensates by injecting `derived_num_ctx * ollama_num_parallel`, so
each slot still delivers the intended per-request window. Set this to match the
backend's real `OLLAMA_NUM_PARALLEL`. The deploy should pin the backend to 1
through the ansible ollama role; this setting is the proxy-side defense in
depth. A per-backend override rides in `PROXY_BACKENDS_JSON` as `num_parallel`.

## `context_truncation_tolerance` and `fail_on_context_truncation` (issue #33)

After a call the proxy compares the backend's `prompt_eval_count` against the
window it asked for. When the backend processed materially fewer prompt tokens
than **both** what was sent and the per-request target, it truncated below the
ask - the NUM_PARALLEL division, or a misconfigured `num_parallel`.

The tolerance is the slack that absorbs tokenizer drift between the proxy's
tiktoken estimate and ollama's real count. The NUM_PARALLEL division produces a
roughly 50% shortfall, far past any drift.

With `fail_on_context_truncation` set the request returns a loud 502 instead of
the short read. The default marks it - metric, `finish_reason=length`, and a
structured log - but still returns content.

## Other settings

- **MCP transport** - production deployments set the public hostname explicitly.
  A request with no `Origin` header is valid server-to-server traffic, while any
  supplied `Origin` must be allowlisted.
- **`backends_json` / `backends_file`** (issue #32) - a JSON array of backend
  specs carrying no tag, since the tag comes from the request. Lets a deploy
  supply siblings, a CPU target, or an OpenAI-dialect fallback beyond the single
  built-in tower backend.
- **`route_registry_*`** - Deploy mounts a service-local logical route registry.
  Compatibility mode preserves the legacy physical-model catalog only for
  development and an explicit rollback, and production disables it once a
  registry is mounted.
- **`trajectory_db_path`** - defaults to a file-backed SQLite WAL under the
  service working directory. Deployments mount this on durable storage rather
  than treating the container layer as retention.
- **`trajectory_request_emission_enabled`** - opt-in until the deployment mounts
  the database path durably. Emission is bounded and never awaits storage.
- **`ward_skill_use_input`** - a single `skill-usage.json` artifact or a
  directory of reaped run archives.
- **Tower resolution** - `PROXY_TOWER_BASE_URL` wins outright. Otherwise the
  FQDN resolves from SSM at boot and the base URL is built from it.

## See also

- [`proxy.md`](proxy.md) - the request path these settings govern.
- [`route-registry.md`](route-registry.md) - the logical route contract.
