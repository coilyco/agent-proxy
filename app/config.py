"""
Configuration for the resilience proxy.

Settings come from environment variables (prefix ``PROXY_``) with a best-effort
AWS SSM fallback for the tower FQDN and secrets. Nothing is hardcoded that a
deploy cannot override, and no secret is ever committed - the Sentry DSN and any
API-fallback keys resolve from SSM (or env) at boot. See leg 04 "Secrets and
config" and leg 02 for the design this implements.
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from typing import Any, Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# SSM parameter names (never the resolved values) live in the source. The tower
# FQDN is deliberately not written into any tracked file - it resolves here.
SSM_TOWER_FQDN = "/coilysiren/kai-tower-3026/tailnet-fqdn"
SSM_SENTRY_DSN = "/coilysiren/agent-proxy/sentry-dsn"
SSM_API_FALLBACK_KEY = "/coilysiren/agent-proxy/api-fallback-key"


def _ssm_get(name: str) -> str | None:
    """Best-effort SSM SecureString read. Returns None if unavailable.

    boto3 and credentials are optional at dev time; the proxy runs fully against
    a local/stub backend without them. In the cluster the pod's role provides
    both and this resolves the real values at boot.
    """
    try:
        import boto3  # imported lazily so dev without AWS still works
    except ImportError:
        return None
    try:
        client = boto3.client("ssm")
        resp = client.get_parameter(Name=name, WithDecryption=True)
        return resp["Parameter"]["Value"]
    except Exception:
        # No creds, no network, missing param - all degrade to None, never fatal.
        return None


class Settings(BaseSettings):
    """Proxy settings. Env wins over SSM wins over the built-in default."""

    model_config = SettingsConfigDict(env_prefix="PROXY_", extra="ignore")

    proxy_host: str = Field(default="127.0.0.1")
    proxy_port: int = Field(default=8080)
    log_level: str = Field(default="INFO")

    # Streamable HTTP MCP transport. Allowlist rules: docs/context-budget-per-model.md.
    mcp_allowed_hosts: str = Field(default="127.0.0.1:*,localhost:*,testserver")
    mcp_allowed_origins: str = Field(default="http://127.0.0.1:*,http://localhost:*,http://[::1]:*")

    # Queue / worker sizing (leg 04 step 3).
    queue_maxsize: int = Field(default=100)
    worker_count: int = Field(default=4)

    # Admission rate per logical route, sustained (#110). 0 disables shedding.
    # Burst semantics: docs/rate-limits.md.
    rate_limit_per_second: float = Field(default=1.0, ge=0.0)
    rate_limit_burst: int = Field(default=1, ge=1)

    # Seconds an attempt may run before its backend counts as saturated and the
    # chain advances (#108). 0 disables. Rationale: docs/saturation-failover.md.
    backend_slow_after: float = Field(default=0.0, ge=0.0)

    # Seconds between SSE keepalive comments while a state persists (#104).
    # 0 disables them. Wire shape: docs/sse-heartbeats.md.
    heartbeat_interval: float = Field(default=10.0, ge=0.0)

    # Resilience knobs (leg 04 step 4).
    max_retries: int = Field(default=2, description="Retries per backend before falling back")
    retry_base_delay: float = Field(default=0.5, description="Backoff base seconds")
    circuit_fail_threshold: int = Field(
        default=5, description="Consecutive fails before opening a breaker"
    )
    circuit_cooldown: float = Field(default=30.0, description="Seconds a breaker stays open")
    # Saturation is a busy backend, not a broken one, so it sticks on its own
    # terms (#111). Definitions: docs/saturation-failover.md.
    saturation_threshold: int = Field(default=2, ge=1)
    saturation_cooldown: float = Field(default=900.0, ge=0.0)
    request_timeout: float = Field(
        default=600.0, description="Per-backend upstream timeout seconds"
    )
    # Wall clock for one caller request across every attempt and fallback (#112).
    # 0 disables it. Rationale: docs/request-deadline.md.
    request_deadline: float = Field(default=0.0, ge=0.0)
    readiness_timeout: float = Field(
        default=3.0,
        gt=0.0,
        description="Per-dependency timeout for non-generating route readiness checks",
    )

    # Context-budget headroom reserved for the completion (leg 04 step 5).
    num_ctx_headroom: int = Field(default=1024)

    # VRAM-safe upper bound on the injected num_ctx, local routes only (#32, #115).
    # Derivation: docs/context-budget-per-model.md.
    num_ctx_ceiling: int = Field(default=49152)

    # A prompt ceiling below the model's window, for cost rather than VRAM (#115).
    # 0 leaves the model's own window as the only bound.
    context_cost_ceiling: int = Field(default=0, ge=0)

    # The operating regime every backend reports unless its spec overrides it
    # (#109). Values and who sets them: docs/backend-catalog.md.
    backend_regime: str = Field(default="unknown")

    # Must match the backend's real OLLAMA_NUM_PARALLEL (issue #33).
    # Why the injected value is scaled: docs/context-budget-per-model.md.
    ollama_num_parallel: int = Field(default=1)

    # Fail-loud verification of the delivered context (issue #33).
    # Detection rule and both outcomes: docs/context-budget-per-model.md.
    context_truncation_tolerance: float = Field(default=0.15)
    fail_on_context_truncation: bool = Field(default=False)

    # Observability wiring (leg 04 step 1).
    sentry_dsn: str = Field(default="")
    otel_exporter_otlp_endpoint: str = Field(default="")
    service_name: str = Field(default="agent-proxy")
    trace_bodies: bool = Field(default=False)

    # Ward reap telemetry ingest. The path may point at a single
    # ``skill-usage.json`` artifact or a directory of reaped run archives.
    ward_skill_use_input: str = Field(default="")

    # Cold-path trajectory retention. Deployments mount this path on
    # durable storage. See docs/context-budget-per-model.md.
    trajectory_db_path: str = Field(default="./data/trajectory.sqlite3")
    trajectory_ingest_queue_size: int = Field(default=256, ge=1)
    # Request-path emission is opt-in until the deployment mounts the database
    # path durably. Emission is bounded and never awaits storage.
    trajectory_request_emission_enabled: bool = Field(default=False)

    # Tower resolution. If PROXY_TOWER_BASE_URL is set it wins outright; else the
    # FQDN resolves from SSM at boot and the base URL is built from it.
    tower_base_url: str = Field(default="")
    tower_port: int = Field(default=11434)

    # Backend chain override (issue #32). Spec shape and intent:
    # docs/context-budget-per-model.md.
    backends_json: str = Field(default="")
    backends_file: str = Field(default="")

    # Deploy mounts a service-local logical route registry.
    # Compatibility-mode rules: docs/context-budget-per-model.md.
    route_registry_file: str = Field(default="")
    route_registry_compatibility_mode: bool = Field(default=True)
    route_upstream_mode: Literal["litellm", "direct"] = Field(default="direct")

    def resolved_tower_base_url(self) -> str:
        """The primary ollama base URL, from env, SSM, or a safe local default."""
        if self.tower_base_url:
            return self.tower_base_url.rstrip("/")
        fqdn = _ssm_get(SSM_TOWER_FQDN)
        if fqdn:
            return f"http://{fqdn}:{self.tower_port}"
        # Dev default: a local ollama (or stub) on the standard port.
        return f"http://127.0.0.1:{self.tower_port}"

    def resolved_sentry_dsn(self) -> str:
        return self.sentry_dsn or (_ssm_get(SSM_SENTRY_DSN) or "")

    def resolved_api_fallback_key(self) -> str:
        return _ssm_get(SSM_API_FALLBACK_KEY) or os.environ.get("PROXY_API_FALLBACK_KEY", "")

    def resolved_mcp_allowed_hosts(self) -> list[str]:
        return [value.strip() for value in self.mcp_allowed_hosts.split(",") if value.strip()]

    def resolved_mcp_allowed_origins(self) -> list[str]:
        return [value.strip() for value in self.mcp_allowed_origins.split(",") if value.strip()]

    def backend_overrides(self) -> list[dict[str, Any]] | None:
        """Parsed backend-chain override, or None to use the built-in tower."""
        raw = self.backends_json
        if not raw and self.backends_file:
            try:
                with open(self.backends_file, "r", encoding="utf-8") as fh:
                    raw = fh.read()
            except OSError:
                return None
        if not raw:
            return None
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, list) and parsed else None


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton (SSM reads happen once at first access)."""
    return Settings()


# Convenience module-level handle.
settings = get_settings()
