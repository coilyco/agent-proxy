# MCP prompt surface

Agent Proxy exposes a stateless Model Context Protocol endpoint at `/mcp`.
The endpoint uses Streamable HTTP with JSON responses so a remote MCP client
can discover local models and submit a prompt without learning the
OpenAI-compatible API shape.

## Tools

- **`list_models`** - returns the model names currently visible through Agent
  Proxy's live model catalog.
- **`send_prompt`** - accepts `prompt`, `model`, and optional `system_prompt`,
  `max_tokens`, and `temperature` values. It returns the normalized content,
  reasoning content, tool calls, finish reason, and token usage.

## Roster stability

The tool roster is static. Agent Proxy declares `tools.listChanged: false` at
initialization and never sends `notifications/tools/list_changed`, so a client
may discover the roster once and cache it for the life of its process.

That declaration is a contract rather than an accident of the current tool
count. The transport is stateless Streamable HTTP with JSON responses, which
gives the server no channel on which to deliver an unsolicited notification, so
a client that cached the roster and waited for an invalidation signal would wait
forever. `tests/test_api.py` fails if the capability flips.

`list_models` returns the live model catalog, but that is a tool *result*
computed when the tool is called, not a tool *definition*. The catalog moving
never changes the roster, so rediscovering it buys a caller nothing.

Issue #102
recorded one `mcp.tools.list` per turn against a surface that almost never
changes. The client-side cache that fixes it belongs to the MCP client, which
for the Discord path is Sirens Echo
(sirens-echo#163).
This section is the server-side half: the guarantee that makes such a cache
correct against Agent Proxy.

`send_prompt` enters the same non-streaming chat path as
`/v1/chat/completions`. Model resolution, context budgeting, queue admission,
retry, fallback, circuit breaking, request telemetry, and bounded trajectory
emission therefore remain single-owner behavior. The MCP layer does not
authorize or execute agent work.

## Transport security

The MCP transport validates every `Host` header and any supplied `Origin`
header. Local development accepts loopback hosts and the in-process test host.
A deployment sets comma-separated public values through:

- `PROXY_MCP_ALLOWED_HOSTS`, for example `mcp.example.com`
- `PROXY_MCP_ALLOWED_ORIGINS`, only when a browser client supplies an Origin

Server-to-server MCP clients commonly omit `Origin`, which remains valid.
Unexpected supplied origins are rejected.

Agent Proxy does not issue or validate OAuth tokens at this boundary. Do not
publish `/mcp` directly. An internet-facing deployment must place an
OAuth-capable or equivalent authenticated ingress in front of the route and
must preserve the MCP request and response headers.

## Claude remote connectors

Claude custom connectors call remote MCP servers from Anthropic's cloud, not
from the phone or desktop client. The deployed endpoint must therefore be
reachable over HTTPS from that service. After authenticated ingress and the
public hostname allowlist are configured, register the full
`https://mcp.example.com/mcp` URL as a custom connector. Claude can then call
`list_models` and `send_prompt` from web, desktop, or mobile conversations.

Public reachability, OAuth configuration, and live model dispatch are
deployment checks. Repository tests prove MCP initialization, tool discovery,
prompt dispatch through the existing chat path, error conversion, and Origin
rejection without contacting a live tower.
