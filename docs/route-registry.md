# Logical route registry

The Deploy-mounted logical route contract that keeps physical backend names
behind the proxy boundary, and how a deployment configures and rolls it back.

Agent Proxy accepts stable `<namespace>/<alias>` keys from governed clients.
Deploy mounts a service-local JSON registry that resolves each key without
exposing a physical backend through `/v1/models`, MCP discovery, responses, or
public telemetry. Namespaces identify owning services or evaluation surfaces,
not Agent Compose roles.

## Ownership

* Deploy owns service and evaluation aliases plus their selected backends.
* Agent Proxy validates keys, applies context policy, and dispatches an alias.
* LiteLLM translates the alias into provider routing, retry, and fallback.

Agent Proxy never imports or fetches a route source. Endpoints, secret paths,
and resources stay in Deploy-owned backend configuration, outside this
registry.

## Version 1

```json
{
  "format": "agent-proxy-route-registry/v1",
  "source": {
    "evaluation_routes_sha256": "evaluation-input-digest",
    "format": "deploy.agent-proxy-routes/v1",
    "service_routes_sha256": "service-input-digest",
    "version": 1
  },
  "routes": [
    {
      "key": "sirens-echo/default",
      "upstream_alias": "sirens-echo/default",
      "direct": {
        "model": "ornith:35b",
        "runtime": "ollama"
      },
      "readiness_targets": [
        {
          "model": "ornith:35b",
          "runtime": "ollama"
        },
        {
          "model": "ornith:9b",
          "runtime": "ollama"
        }
      ]
    }
  ]
}
```

The source object records the exact Deploy-owned service and evaluation inputs
used to generate the mounted registry. Agent Proxy retains both SHA-256 digests
as provenance while continuing to reject unrecognized source fields.

`upstream_alias` is sent to LiteLLM. `direct` is optional deployment data for
the established rollback path. Direct mode currently supports Ollama targets.
A known llama.cpp target fails closed instead of silently selecting another
model.

`readiness_targets` is an optional ordered list of physical primary and
fallback targets rendered by Deploy from the same route inputs that render
LiteLLM routing. Agent Proxy uses the list only for non-generating installed-model
checks. If it is absent, readiness checks the existing `direct` target for
backward compatibility. When both fields are absent, LiteLLM mode treats the
route as hosted-provider-only and verifies only the authenticated LiteLLM
control surfaces. Direct mode fails closed. Physical target names never enter
readiness responses, logs, traces, or metric labels.

`context_window` is an optional positive integer: the upstream model's real
context window in tokens. Ollama reports its own through `/api/tags`, so a local
target rarely needs it, while a hosted provider reports nothing. Set it when a
model-accurate number is known. Full derivation and the four bounds:
[context-budget-per-model.md](context-budget-per-model.md).

The loader rejects unknown formats or fields, duplicate keys, missing aliases,
malformed targets, unsafe file types, oversized files, and invalid JSON.
Configured invalid files stop process startup before traffic is served.

## Configuration

* `PROXY_ROUTE_REGISTRY_FILE` points to the mounted JSON file.
* `PROXY_ROUTE_UPSTREAM_MODE=litellm` sends `upstream_alias` to the configured
  OpenAI-shaped inner gateway.
* `PROXY_ROUTE_UPSTREAM_MODE=direct` sends a supported physical direct target.
* `PROXY_ROUTE_REGISTRY_COMPATIBILITY_MODE=true` permits legacy physical-model
  discovery only when no registry path is set. Production sets it to `false`.

Logical key and upstream mode are trace, log, and metric dimensions. Physical
identity remains restricted backend telemetry. Logical routes are not appended
to messages or prompts. Body-safe LiteLLM metadata remains outside
model-visible context.
