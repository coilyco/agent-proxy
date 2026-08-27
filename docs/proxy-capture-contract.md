# Model I/O capture contract

Part of [proxy](proxy.md).

## Model I/O capture contract


Body capture is opt-in and defaults off. When enabled, Agent Proxy captures
every field in both the complete normalized request body and the complete
normalized response body. This includes messages or prompts, tool definitions
and calls, model-visible options, generated content, reasoning content, usage,
and finish state when present. There is no selected-field or request-only
capture mode. Transport credentials and hop-by-hop headers are not model I/O
and remain excluded.

Agent Proxy is the single capture owner. Upstream callers, including
orchestration services such as Sirens Echo, keep correlation and operational
metadata but do not duplicate model payloads in their logs. With capture off,
stdout, OTLP spans, and SigNoz stay metadata-only. With capture on, body-bearing
structured logs and trace attributes contain the complete request and response
bodies. The configured OTLP or SigNoz sink must be governed as restricted model
content accordingly.

The repository enforces this condition for non-streaming chat, reconstructed
streaming chat, text completions, and MCP prompt calls. The restricted ser8
deployment opt-in and live SigNoz verification completed under
[issue #77](https://forgejo.coilysiren.me/coilyco-flight-deck/agent-proxy/issues/77).

The stable log events, fields, and viewing flow are in
[proxy-signoz-viewing.md](proxy-prompt-cache.md).

Part of [proxy.md](proxy.md). Why the captured events carry a projection of
selected fields beside the complete body, and how it behaves when a value is
absent.

A projection is a convenience beside the complete body, never a selected-field
capture mode, and never a substitute for the body it sits next to. Every value
is already inside `request.body` or `response.body`. They are lifted here
because Agent Proxy is the one place that serves every capture consumer, and
because the user message is otherwise unreachable: it is the last matching
element of a variable-length messages list, and no log pipeline field path can
address a last element.

Projection is total. An absent, blank, or unreadable value omits its field
rather than failing a capture that would otherwise have succeeded, so a request
with no user turn and an incomplete response both still capture. A truncated
completion is legible from the projection alone, as `agentproxy.finish_reason`
of `length` with `agentproxy.completion_tokens` at the request's cap and no
`agentproxy.assistant_message`. Adding these fields is backward compatible and
does not advance the capture schema version.
The `trace_id` and request-span `span_id` pair the two events even when one
Sirens Echo turn makes multiple model calls. The request span also carries
canonical JSON strings under `agentproxy.request.body` and
`agentproxy.response.body` for direct span inspection.

`agentproxy.capture.status` is `complete` when the whole normalized body is
present. A failed, cancelled, or interrupted response emits
`model.response.captured` with `agentproxy.capture.status=incomplete`, every
response field available at the boundary, and a closed-set
`agentproxy.capture.reason`. The reason is one of `cancelled`,
`context_truncated`, `interrupted`, `queue_rejected`, `response_failed`,
`stream_failed`, or `upstream_failed`.
Streaming capture reconstructs the complete normalized response returned to the
caller. When capture is disabled, none of these body events or attributes are
emitted.

Chat capture preserves every accepted request field and records the
post-context-guard message list. Text completion capture preserves every field
and records a list prompt in its normalized joined-string form. MCP prompt
capture records the MCP tool arguments and the exact structured tool result,
not a duplicate synthetic OpenAI boundary. Capture serializes canonical JSON
before dispatch and before response delivery. Serialization, span attachment,
or structured-log delivery failure raises a hard capture error rather than
reporting a successful model response with incomplete evidence.

SigNoz Logs is the primary content viewer because it retains the structured JSON
objects. From the Agent Proxy `request.chat` or `request.completions` span, use
the trace-to-logs action, restrict the results to the same `trace_id`, `span_id`,
and `service.name=agent-proxy`, then open the two captured events and expand
`request.body` or `response.body`. The span attribute panel provides the same
canonical content for direct inspection.

The accepted headers, their `metadata` fallbacks, and the span attribute each
becomes are listed in
[proxy-correlation-fields.md](proxy-request-path.md).
