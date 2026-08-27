# Prompt cache accounting

Part of [proxy](proxy.md).

Agent Proxy records what a provider charged for a repeated prompt prefix. It
does not create the cache, mark breakpoints, or choose a caching policy. Those
belong to the provider and to Deploy's backend selection.

## Why this exists

[Issue #101](https://forgejo.coilysiren.me/coilyco-flight-deck/agent-proxy/issues/101)
measured a 53 KB system block sent byte-identical on all 46 turns of a window,
against a four-byte user turn, and concluded the prefix was uncached. The proxy
could not settle that. It read `prompt_tokens` and `completion_tokens` out of
the upstream usage block and discarded every cache field beside them, so a fully
cached route and a fully uncached one produced identical evidence.

## The three provider shapes

`app.upstream.parse_cache_usage` normalizes all of them:

- **DeepSeek** reports `prompt_cache_hit_tokens` and `prompt_cache_miss_tokens`.
  Its caching is automatic, applies to any prefix past its block minimum, and
  has no request-side opt-in, so `sirens-echo/deepseek` needs no configuration
  to be cached.
- **OpenAI-compatible providers** report `prompt_tokens_details.cached_tokens`,
  which is also the field LiteLLM normalizes into.
- **Anthropic-style providers** report `cache_read_input_tokens` beside a
  separate `cache_creation_input_tokens` write charge.

## Reported zero is not silence

A provider that reports nothing is an unmeasured route, not a cache miss. An
Ollama backend reuses its KV cache without ever saying so, and publishing that
silence as a 100% miss would invent a regression it never had. So
`UpstreamResult` carries `cache_usage_reported` beside the counts, and every
surface below stays absent until a provider actually accounts for caching. A
reported zero, which is what the turn that populates a cache returns, is a real
measurement and is published.

## Where the numbers surface

- **Response usage** - `prompt_tokens_details.cached_tokens`, the
  OpenAI-canonical spelling, so a caller reading the compatible surface finds
  the cache read in the same place whichever provider served the turn. A write
  charge adds `cache_creation_input_tokens`.
- **Spans** - `gen_ai.usage.cache_read_input_tokens` and
  `gen_ai.usage.cache_creation_input_tokens` on `request.chat`,
  `request.completions`, and `upstream.chat`.
- **Metrics** - `llm_prompt_cache_hit_tokens_total`,
  `llm_prompt_cache_miss_tokens_total`, and
  `llm_prompt_cache_write_tokens_total`, all by `logical_model`.
- **Trajectory** - the same two span attributes on the terminal execution event.

Metrics are recorded once per **served** response rather than once per upstream
attempt, so a turn that retried counts the tokens the caller was actually
billed for. Misses derive from `prompt_tokens - cached_tokens` rather than from
a provider's own miss field, which keeps the two counters summing to the billed
prompt on every provider.

## Request shape

The same request span carries `gen_ai.request.system_bytes`,
`gen_ai.request.tool_count`, and `gen_ai.request.tool_bytes`. Issue #101's
second half asks whether a narrower default tool roster is worth its lost tool
calls. That trade needs the roster's share of the request measured at the one
point every governed route passes through, and these are byte counts, so they
carry no model-visible content and are emitted with body capture off.

## What the proxy does not do

Context trimming drops the oldest non-system turns and always keeps the system
framing, so it never invalidates a system-block prefix. It does invalidate
everything after it, which is the correct trade: fitting the window beats
holding a cache. Nothing here injects `cache_control` breakpoints. No configured
backend needs them today, and adding an unexercised injector would be dead
policy on the live routes.

### SigNoz content viewing contract

When capture is enabled, Agent Proxy emits exactly one structured request-body
event and one structured response-body event for each boundary model call:

* `model.request.captured` stores the complete normalized JSON object under
  `request.body`.
* `model.response.captured` stores the complete normalized JSON object under
  `response.body`.

Both events carry `agentproxy.capture.schema_version=1`,
`agentproxy.capture.status`, `agentproxy.request_id`, `trace_id`, and `span_id`.
Each event also carries a projection of the fields worth reading without
parsing the whole body. `model.request.captured` carries
`agentproxy.user_message`, the verbatim text of the final user turn.
`model.response.captured` carries `agentproxy.finish_reason`,
`agentproxy.assistant_message`, `agentproxy.completion_tokens`, and
`agentproxy.prompt_tokens`, taken from the first choice and the usage block.

Why those events also carry a field projection, and how it behaves when a
value is absent, is in
[proxy-capture-projections.md](proxy-capture-contract.md).
