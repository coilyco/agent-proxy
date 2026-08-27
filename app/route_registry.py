"""Strict loader for the Deploy-owned logical route registry."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import get_settings

REGISTRY_FORMAT = "agent-proxy-route-registry/v1"
MAX_REGISTRY_BYTES = 1024 * 1024
KNOWN_DIRECT_RUNTIMES = {"llama.cpp", "ollama"}
ROUTE_KEY_PATTERN = re.compile(r"[a-z0-9][a-z0-9._-]*/[a-z0-9][a-z0-9._-]*")


class RouteRegistryError(ValueError):
    """The configured route registry cannot be trusted."""


@dataclass(frozen=True)
class DirectTarget:
    model: str
    runtime: str


@dataclass(frozen=True)
class Route:
    key: str
    upstream_alias: str
    direct: DirectTarget | None
    readiness_targets: tuple[DirectTarget, ...] = ()
    enabled: bool = True
    # The upstream model's real context window, when Deploy knows it (#115).
    # Contract: docs/route-registry.md.
    context_window: int | None = None


@dataclass(frozen=True)
class RouteRegistry:
    routes: dict[str, Route]
    source: dict[str, str | int]

    def listed_keys(self) -> list[str]:
        return sorted(key for key, route in self.routes.items() if route.enabled)


def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise RouteRegistryError(f"duplicate JSON field: {key}")
        out[key] = value
    return out


def _keys(value: dict[str, Any], allowed: set[str], label: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise RouteRegistryError(f"{label} has unsupported fields: {', '.join(unknown)}")


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 256:
        raise RouteRegistryError(f"{label} must be non-empty text")
    if any(ord(char) < 32 for char in value):
        raise RouteRegistryError(f"{label} contains control characters")
    return value


def _route_key(value: Any) -> str:
    key = _text(value, "route key")
    if ROUTE_KEY_PATTERN.fullmatch(key) is None:
        raise RouteRegistryError(f"invalid logical route key: {key!r}")
    return key


def _source(value: Any) -> dict[str, str | int]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise RouteRegistryError("source must be an object")
    _keys(
        value,
        {
            "evaluation_routes_sha256",
            "format",
            "revision",
            "service_routes_sha256",
            "sha256",
            "version",
        },
        "source",
    )
    parsed: dict[str, str | int] = {}
    for key, item in value.items():
        if key == "version":
            if not isinstance(item, int) or isinstance(item, bool) or item < 1:
                raise RouteRegistryError("source version must be a positive integer")
            parsed[key] = item
        else:
            parsed[key] = _text(item, f"source {key}")
    return parsed


def _direct(value: Any, key: str) -> DirectTarget | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise RouteRegistryError(f"{key}: direct must be an object")
    _keys(value, {"model", "runtime"}, f"{key}: direct")
    model = _text(value.get("model"), f"{key}: direct model")
    runtime = _text(value.get("runtime"), f"{key}: direct runtime")
    if runtime not in KNOWN_DIRECT_RUNTIMES:
        raise RouteRegistryError(f"{key}: unknown direct runtime: {runtime}")
    return DirectTarget(model=model, runtime=runtime)


def _context_window(value: Any, key: str) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise RouteRegistryError(f"{key}: context_window must be a positive integer")
    return value


def _readiness_targets(value: Any, key: str) -> tuple[DirectTarget, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not value:
        raise RouteRegistryError(f"{key}: readiness_targets must be a non-empty list")
    targets: list[DirectTarget] = []
    seen: set[DirectTarget] = set()
    for index, item in enumerate(value):
        target = _direct(item, f"{key}: readiness_targets[{index}]")
        if target is None:
            raise RouteRegistryError(f"{key}: readiness_targets[{index}] must be an object")
        if target in seen:
            raise RouteRegistryError(f"{key}: duplicate readiness target")
        seen.add(target)
        targets.append(target)
    return tuple(targets)


def load_route_registry(path_value: str | Path) -> RouteRegistry:
    """Read and validate one regular, bounded JSON registry file."""

    path = Path(path_value)
    if path.is_symlink() or not path.is_file():
        raise RouteRegistryError("route registry must be a regular file")
    try:
        if path.stat().st_size > MAX_REGISTRY_BYTES:
            raise RouteRegistryError("route registry exceeds the size limit")
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise RouteRegistryError("route registry is unreadable") from exc
    try:
        payload = json.loads(raw, object_pairs_hook=_object_without_duplicates)
    except (json.JSONDecodeError, RouteRegistryError) as exc:
        raise RouteRegistryError("route registry is invalid JSON") from exc
    if not isinstance(payload, dict):
        raise RouteRegistryError("route registry root must be an object")
    _keys(payload, {"format", "routes", "source"}, "route registry")
    if payload.get("format") != REGISTRY_FORMAT:
        raise RouteRegistryError(f"unsupported route registry format: {payload.get('format')!r}")
    rows = payload.get("routes")
    if not isinstance(rows, list) or not rows:
        raise RouteRegistryError("route registry routes must be a non-empty list")

    routes: dict[str, Route] = {}
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise RouteRegistryError(f"route {index} must be an object")
        _keys(
            row,
            {
                "key",
                "upstream_alias",
                "direct",
                "readiness_targets",
                "enabled",
                "context_window",
            },
            f"route {index}",
        )
        key = _route_key(row.get("key"))
        if key in routes:
            raise RouteRegistryError(f"duplicate logical route key: {key}")
        alias = _text(row.get("upstream_alias"), f"{key}: upstream_alias")
        enabled = row.get("enabled", True)
        if not isinstance(enabled, bool):
            raise RouteRegistryError(f"{key}: enabled must be boolean")
        routes[key] = Route(
            key=key,
            upstream_alias=alias,
            direct=_direct(row.get("direct"), key),
            readiness_targets=_readiness_targets(row.get("readiness_targets"), key),
            enabled=enabled,
            context_window=_context_window(row.get("context_window"), key),
        )
    return RouteRegistry(routes=routes, source=_source(payload.get("source")))


_registry: RouteRegistry | None = None
_initialized = False


def initialize_route_registry() -> RouteRegistry | None:
    """Load the configured registry, or enter the explicit compatibility mode."""

    global _initialized, _registry
    settings = get_settings()
    if settings.route_registry_file:
        _registry = load_route_registry(settings.route_registry_file)
    elif settings.route_registry_compatibility_mode:
        _registry = None
    else:
        raise RouteRegistryError("route registry is required when compatibility mode is disabled")
    _initialized = True
    return _registry


def get_route_registry() -> RouteRegistry | None:
    if not _initialized:
        return initialize_route_registry()
    return _registry


def reset_route_registry() -> None:
    """Clear process state for tests and controlled configuration reloads."""

    global _initialized, _registry
    _initialized = False
    _registry = None
