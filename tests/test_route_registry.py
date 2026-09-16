"""Deploy-owned logical route registry validation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app import route_registry

DEPLOY_V1_FIXTURE = Path(__file__).parent / "fixtures" / "route_registry" / "deploy-v1.json"


def _payload() -> dict[str, object]:
    return {
        "format": "agent-proxy-route-registry/v1",
        "source": {
            "evaluation_routes_sha256": "1" * 64,
            "format": "deploy.agent-proxy-routes/v1",
            "service_routes_sha256": "2" * 64,
            "version": 1,
        },
        "routes": [
            {
                "key": "sirens-echo/default",
                "upstream_alias": "sirens-echo/default",
                "direct": {"model": "ornith:35b", "runtime": "ollama"},
                "readiness_targets": [
                    {"model": "ornith:35b", "runtime": "ollama"},
                    {"model": "ornith:9b", "runtime": "ollama"},
                ],
            },
            {
                "key": "engineer/autonomous-coding",
                "upstream_alias": "engineer/autonomous-coding",
                "direct": {"model": "gpt-oss:120b", "runtime": "llama.cpp"},
                "enabled": False,
            },
        ],
    }


def _write(tmp_path, payload: dict[str, object]):
    path = tmp_path / "routes.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def _reset_registry():
    route_registry.reset_route_registry()
    yield
    route_registry.reset_route_registry()


def test_valid_registry_loads_logical_routes(tmp_path):
    registry = route_registry.load_route_registry(_write(tmp_path, _payload()))

    assert registry.listed_keys() == ["sirens-echo/default"]
    route = registry.routes["sirens-echo/default"]
    assert route.upstream_alias == route.key
    assert route.direct == route_registry.DirectTarget("ornith:35b", "ollama")
    assert route.readiness_targets == (
        route_registry.DirectTarget("ornith:35b", "ollama"),
        route_registry.DirectTarget("ornith:9b", "ollama"),
    )
    assert registry.source == {
        "evaluation_routes_sha256": "1" * 64,
        "format": "deploy.agent-proxy-routes/v1",
        "service_routes_sha256": "2" * 64,
        "version": 1,
    }


def test_deploy_9b35fee_registry_fixture_loads():
    registry = route_registry.load_route_registry(DEPLOY_V1_FIXTURE)

    assert registry.listed_keys() == [
        "community/conversation-management",
        "evaluation/deepseek-v4-flash",
        "evaluation/ornith-35b",
        "sirens-echo/default",
    ]
    assert registry.source == {
        "evaluation_routes_sha256": (
            "d096681f1ebc77181e6911e81aa3a1491c02fc6d44ea77172dd323a9397b4b00"
        ),
        "format": "deploy.agent-proxy-routes/v1",
        "service_routes_sha256": (
            "11653aec99727c0d6514fd6f460a9645f71273b06407b17757d43dfdb2fd0309"
        ),
        "version": 1,
    }


def test_unknown_source_field_is_rejected(tmp_path):
    payload = _payload()
    payload["source"]["unexpected"] = "value"

    with pytest.raises(route_registry.RouteRegistryError, match="unsupported fields: unexpected"):
        route_registry.load_route_registry(_write(tmp_path, payload))


def test_unknown_format_is_rejected(tmp_path):
    payload = _payload()
    payload["format"] = "agent-proxy-route-registry/v2"

    with pytest.raises(route_registry.RouteRegistryError, match="unsupported"):
        route_registry.load_route_registry(_write(tmp_path, payload))


def test_duplicate_route_key_is_rejected(tmp_path):
    payload = _payload()
    payload["routes"].append(dict(payload["routes"][0]))

    with pytest.raises(route_registry.RouteRegistryError, match="duplicate logical"):
        route_registry.load_route_registry(_write(tmp_path, payload))


def test_missing_upstream_alias_is_rejected(tmp_path):
    payload = _payload()
    del payload["routes"][0]["upstream_alias"]

    with pytest.raises(route_registry.RouteRegistryError, match="upstream_alias"):
        route_registry.load_route_registry(_write(tmp_path, payload))


def test_oversized_registry_is_rejected_before_parsing(tmp_path):
    path = tmp_path / "routes.json"
    path.write_text("x" * (route_registry.MAX_REGISTRY_BYTES + 1), encoding="utf-8")

    with pytest.raises(route_registry.RouteRegistryError, match="size limit"):
        route_registry.load_route_registry(path)


def test_malformed_direct_target_is_rejected(tmp_path):
    payload = _payload()
    del payload["routes"][0]["direct"]["runtime"]

    with pytest.raises(route_registry.RouteRegistryError, match="direct runtime"):
        route_registry.load_route_registry(_write(tmp_path, payload))


def test_duplicate_readiness_target_is_rejected(tmp_path):
    payload = _payload()
    payload["routes"][0]["readiness_targets"].append(
        dict(payload["routes"][0]["readiness_targets"][0])
    )

    with pytest.raises(route_registry.RouteRegistryError, match="duplicate readiness target"):
        route_registry.load_route_registry(_write(tmp_path, payload))


def test_configured_invalid_registry_fails_initialization(monkeypatch, tmp_path):
    path = tmp_path / "routes.json"
    path.write_text("not-json", encoding="utf-8")
    settings = route_registry.get_settings()
    monkeypatch.setattr(settings, "route_registry_file", str(path))
    monkeypatch.setattr(settings, "route_registry_compatibility_mode", False)

    with pytest.raises(route_registry.RouteRegistryError, match="invalid JSON"):
        route_registry.initialize_route_registry()


def test_missing_registry_requires_explicit_compatibility(monkeypatch):
    settings = route_registry.get_settings()
    monkeypatch.setattr(settings, "route_registry_file", "")
    monkeypatch.setattr(settings, "route_registry_compatibility_mode", False)

    with pytest.raises(route_registry.RouteRegistryError, match="required"):
        route_registry.initialize_route_registry()


def test_registry_accepts_a_declared_context_window(tmp_path):
    path = _write(
        tmp_path,
        {
            "format": route_registry.REGISTRY_FORMAT,
            "routes": [
                {
                    "key": "sirens-echo/deepseek",
                    "upstream_alias": "sirens-echo/deepseek",
                    "direct": None,
                    "context_window": 1000000,
                }
            ],
        },
    )
    registry = route_registry.load_route_registry(path)
    assert registry.routes["sirens-echo/deepseek"].context_window == 1000000


def test_registry_defaults_context_window_to_unknown(tmp_path):
    path = _write(
        tmp_path,
        {
            "format": route_registry.REGISTRY_FORMAT,
            "routes": [{"key": "sirens-echo/deepseek", "upstream_alias": "sirens-echo/deepseek"}],
        },
    )
    registry = route_registry.load_route_registry(path)
    assert registry.routes["sirens-echo/deepseek"].context_window is None


def test_registry_rejects_a_nonsense_context_window(tmp_path):
    path = _write(
        tmp_path,
        {
            "format": route_registry.REGISTRY_FORMAT,
            "routes": [
                {
                    "key": "sirens-echo/deepseek",
                    "upstream_alias": "sirens-echo/deepseek",
                    "context_window": 0,
                }
            ],
        },
    )
    with pytest.raises(route_registry.RouteRegistryError, match="context_window"):
        route_registry.load_route_registry(path)


DEPLOY_CONTENT_LANE_FIXTURE = (
    Path(__file__).parent / "fixtures" / "route_registry" / "deploy-content-lane.json"
)


def test_the_registry_deploy_actually_mounts_loads() -> None:
    """Verbatim copy of services/agent-proxy/chart/files/route-registry.json.

    This exact file crash-looped the ser8 pod: Deploy added a content lane, its
    renderer emitted a third `content_routes_sha256` provenance digest, and the
    source allowlist rejected the unknown field at startup. The loader fails
    closed by design, so a metadata addition on Deploy's side became an outage
    on the next image roll rather than at the config change that caused it.

    Deliberately asserts loadability and the digests rather than the route list,
    which churns every time Deploy adds an alias.
    """
    registry = route_registry.load_route_registry(DEPLOY_CONTENT_LANE_FIXTURE)

    assert registry.routes
    assert registry.source["content_routes_sha256"]
    assert registry.source["service_routes_sha256"]
    assert registry.source["evaluation_routes_sha256"]


def test_an_unheard_of_lane_digest_is_accepted(tmp_path: Path) -> None:
    """The next lane must not need an agent-proxy release to avoid an outage."""
    payload = _payload()
    payload["source"]["telemetry_routes_sha256"] = "9" * 64  # type: ignore[index]

    registry = route_registry.load_route_registry(_write(tmp_path, payload))

    assert registry.source["telemetry_routes_sha256"] == "9" * 64


def test_an_unknown_non_digest_source_field_is_still_rejected(tmp_path: Path) -> None:
    """Only provenance digests are shape-matched. Everything else stays strict."""
    payload = _payload()
    payload["source"]["upstream_mode"] = "litellm"  # type: ignore[index]

    with pytest.raises(route_registry.RouteRegistryError, match="unsupported fields"):
        route_registry.load_route_registry(_write(tmp_path, payload))
