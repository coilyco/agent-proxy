"""Jev decision shim (`POST /v1/systemone`). TypeSafe is faked at the HTTP layer.

Contract and telemetry: docs/systemone-shim.md. The upstream shape below is the
one TypeSafe documents at docs.typesafe.ai/api, read 2026-09-19.
"""

import json

import httpx
import pytest
from prometheus_client import REGISTRY

from app import main, obs, systemone, upstream
from app.config import get_settings

REQUEST = {
    "model": "jev-latest",
    "state": {"ticket": "the export button is greyed out"},
    "questions": {
        "urgent": {"type": "noul", "instructions": "Is this urgent?"},
        "team": {
            "type": "choice",
            "instructions": "Which team?",
            "criteria": {"billing": "Payments", "technical": "Bugs"},
        },
    },
}
REPLY = {
    "model": "jev-1.13.0",
    "answers": {
        "urgent": {"type": "noul", "noul": 0.12},
        "team": {
            "type": "choice",
            "choice": "technical",
            "probabilities": {"billing": 0.1, "technical": 0.9},
            "confidence": 0.88,
        },
    },
    "usage": {"input_tokens": 500_000, "output_tokens": 0},
}
LABELS = {"logical_model": "jev-latest", "backend": "typesafe"}


def _sample(name: str, **labels: str) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


@pytest.fixture
def jev(monkeypatch, tmp_path, app_client):
    """The app with a key mounted and a fake TypeSafe. Calls land in ``seen``."""
    key = tmp_path / "typesafe-key"
    key.write_text("test-key\n")
    settings = get_settings()
    monkeypatch.setattr(settings, "systemone_api_key_file", str(key))
    monkeypatch.setattr(settings, "systemone_base_url", "https://api.typesafe.test")
    seen: list[httpx.Request] = []
    behavior = {"handler": lambda request: httpx.Response(200, json=REPLY)}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return behavior["handler"](request)

    monkeypatch.setattr(
        upstream, "_client", httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    app_client.seen = seen
    app_client.respond = lambda fn: behavior.update(handler=fn)
    return app_client


@pytest.fixture
def spans(monkeypatch):
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(obs, "_tracer", provider.get_tracer("test"))
    return lambda: [s for s in exporter.get_finished_spans() if s.name == "request.systemone"]


@pytest.fixture
def events(monkeypatch):
    class _Emitter:
        dropped = 0

        def __init__(self):
            self.emitted = []

        def emit_nowait(self, event):
            self.emitted.append(event)
            return True

    emitter = _Emitter()
    monkeypatch.setattr(main, "_trajectory_emitter", emitter)
    return emitter.emitted


def test_forwards_the_body_with_the_mounted_key_and_returns_the_answer_verbatim(jev):
    response = jev.post(
        "/v1/systemone", json=REQUEST, headers={"Authorization": "Bearer the-callers-own"}
    )
    assert response.status_code == 200
    assert response.json() == REPLY
    (call,) = jev.seen
    assert str(call.url) == "https://api.typesafe.test/v1/systemone"
    assert json.loads(call.content) == REQUEST
    # The caller never holds the key, and whatever it sent never reaches TypeSafe.
    assert call.headers["authorization"] == "Bearer test-key"


def test_a_served_call_leaves_a_span_a_latency_histogram_and_a_cost(jev, spans):
    before = {
        "latency": _sample("llm_upstream_latency_seconds_count", **LABELS),
        "cost": _sample("llm_cost_usd_total", **LABELS),
        "ok": _sample("llm_requests_total", logical_model="jev-latest", outcome="ok"),
    }
    assert jev.post("/v1/systemone", json=REQUEST).status_code == 200
    assert _sample("llm_upstream_latency_seconds_count", **LABELS) == before["latency"] + 1
    # 500k input tokens at 0.042 USD per million. Output tokens are free.
    assert _sample("llm_cost_usd_total", **LABELS) == pytest.approx(before["cost"] + 0.021)
    assert (
        _sample("llm_requests_total", logical_model="jev-latest", outcome="ok") == before["ok"] + 1
    )
    (span,) = spans()
    attrs = dict(span.attributes)
    assert attrs["agentproxy.backend"] == "typesafe"
    assert attrs["agentproxy.backend_dialect"] == "systemone"
    assert attrs["agentproxy.backend.regime"] == "hosted"
    assert attrs["agentproxy.decision.questions"] == 2
    assert attrs["gen_ai.request.model"] == "jev-latest"
    # The alias moves, so the resolved version is what gets recorded.
    assert attrs["gen_ai.response.model"] == "jev-1.13.0"
    assert attrs["gen_ai.usage.input_tokens"] == 500_000
    assert attrs["gen_ai.usage.output_tokens"] == 0
    assert attrs["agentproxy.cost.usd"] == pytest.approx(0.021)
    assert attrs["agentproxy.upstream.status_code"] == 200


def test_a_served_call_emits_the_same_trajectory_events_a_chat_call_does(jev, events):
    assert jev.post("/v1/systemone", json=REQUEST).status_code == 200
    assert [e["event_type"] for e in events] == ["action.proposed", "execution.completed"]
    terminal = events[1]
    assert terminal["attributes"]["agentproxy.request_kind"] == "systemone"
    execution = terminal["payload"]["model_execution"]
    assert execution["provider_model"] == "jev-1.13.0"
    assert execution["request_tokens"] == 500_000
    assert execution["response_tokens"] == 0


def test_a_refusal_passes_through_and_costs_nothing(jev, spans, events):
    body = {"detail": "questions.team.criteria: too many options"}
    jev.respond(lambda request: httpx.Response(422, json=body))
    before = {
        "latency": _sample("llm_upstream_latency_seconds_count", **LABELS),
        "cost": _sample("llm_cost_usd_total", **LABELS),
        "rejected": _sample(
            "llm_requests_total", logical_model="jev-latest", outcome="request_rejected"
        ),
    }
    response = jev.post("/v1/systemone", json=REQUEST)
    assert (response.status_code, response.json()) == (422, body)
    assert _sample("llm_upstream_latency_seconds_count", **LABELS) == before["latency"]
    assert _sample("llm_cost_usd_total", **LABELS) == before["cost"]
    assert (
        _sample("llm_requests_total", logical_model="jev-latest", outcome="request_rejected")
        == before["rejected"] + 1
    )
    assert events[-1]["payload"]["outcome"] == "upstream_rejected"
    (span,) = spans()
    assert span.attributes["agentproxy.upstream.status_code"] == 422


def test_an_overload_passes_its_pacing_headers_through_for_the_sdk_to_read(jev, events):
    jev.respond(
        lambda request: httpx.Response(
            429, json={"error": "slow down"}, headers={"Retry-After": "2", "retry-after-ms": "1500"}
        )
    )
    response = jev.post("/v1/systemone", json=REQUEST)
    assert response.status_code == 429
    assert response.headers["retry-after"] == "2"
    assert response.headers["retry-after-ms"] == "1500"
    # One attempt only. Retrying here would hide the failure from the telemetry.
    assert len(jev.seen) == 1
    assert events[-1]["payload"]["outcome"] == "upstream_failed"


def test_a_timeout_is_a_504_and_a_callers_own_shorter_deadline_says_so(jev, events):
    def slow(request):
        raise httpx.ReadTimeout("slow", request=request)

    jev.respond(slow)
    response = jev.post("/v1/systemone", json=REQUEST)
    assert response.status_code == 504
    assert response.json()["error"]["type"] == "upstream_error"
    assert events[-1]["payload"]["outcome"] == "upstream_failed"
    bounded = jev.post("/v1/systemone", json=REQUEST, headers={"x-request-deadline-ms": "500"})
    assert bounded.status_code == 504
    assert bounded.json()["error"]["type"] == "request_deadline_exceeded"
    assert events[-1]["payload"]["outcome"] == "deadline_exceeded"


def test_a_callers_deadline_only_ever_shortens_the_upstream_timeout(jev):
    jev.post("/v1/systemone", json=REQUEST, headers={"x-request-deadline-ms": "500"})
    jev.post("/v1/systemone", json=REQUEST, headers={"x-request-deadline-ms": "600000"})
    shorter, longer = (call.extensions["timeout"]["read"] for call in jev.seen)
    assert shorter == pytest.approx(0.5)
    assert longer == pytest.approx(get_settings().systemone_timeout)


def test_an_unreachable_upstream_is_a_502(jev):
    def down(request):
        raise httpx.ConnectError("no route", request=request)

    jev.respond(down)
    response = jev.post("/v1/systemone", json=REQUEST)
    assert response.status_code == 502
    assert response.json()["error"]["type"] == "upstream_transport_failed"


def test_a_2xx_that_is_not_a_json_object_is_a_502_not_a_served_call(jev):
    jev.respond(lambda request: httpx.Response(200, content=b"<html>gateway</html>"))
    before = _sample("llm_cost_usd_total", **LABELS)
    response = jev.post("/v1/systemone", json=REQUEST)
    assert response.status_code == 502
    assert response.json()["error"]["type"] == "response_validation_failed"
    assert _sample("llm_cost_usd_total", **LABELS) == before


def test_an_unlisted_model_is_refused_before_it_can_become_a_metric_label(jev):
    response = jev.post("/v1/systemone", json={**REQUEST, "model": "jev-from-a-caller"})
    assert response.status_code == 404
    assert response.json()["error"]["type"] == "model_not_found"
    assert jev.seen == []


@pytest.mark.parametrize("payload", ["[]", "not json"])
def test_a_body_that_is_not_a_json_object_is_a_400(jev, payload):
    response = jev.post(
        "/v1/systemone", content=payload, headers={"content-type": "application/json"}
    )
    assert response.status_code == 400
    assert jev.seen == []


def test_no_key_or_an_unreadable_one_is_a_503_that_never_reaches_upstream(jev, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "systemone_api_key_file", "")
    unset = jev.post("/v1/systemone", json=REQUEST)
    monkeypatch.setattr(settings, "systemone_api_key_file", "/nonexistent/typesafe-key")
    unreadable = jev.post("/v1/systemone", json=REQUEST)
    assert (unset.status_code, unreadable.status_code) == (503, 503)
    assert unset.json()["error"]["type"] == "model_unavailable"
    assert jev.seen == []


def test_input_cost_is_the_configured_price_per_million_input_tokens():
    assert systemone.input_cost_usd(1_000_000) == pytest.approx(0.042)
    assert systemone.input_cost_usd(0) == 0.0


CHOICE_FLAT = {
    "model": "jev-latest",
    "state": {"ticket": "the export button is greyed out"},
    "instructions": "Which team?",
    "options": [
        {"name": "billing", "description": "Payments"},
        {"name": "technical", "description": "Bugs"},
    ],
}
CHOICE_NESTED = {
    "model": "jev-latest",
    "state": {"ticket": "the export button is greyed out"},
    "questions": {
        "answer": {
            "type": "choice",
            "instructions": "Which team?",
            "criteria": {"billing": "Payments", "technical": "Bugs"},
        }
    },
}


def test_build_choice_body_reshapes_options_into_the_criteria_map():
    body, message = systemone.build_choice_body(CHOICE_FLAT)
    assert message == ""
    assert body == CHOICE_NESTED


def test_build_choice_body_drops_state_when_the_caller_sent_none():
    flat = {k: v for k, v in CHOICE_FLAT.items() if k != "state"}
    body, message = systemone.build_choice_body(flat)
    assert message == ""
    assert body is not None
    assert "state" not in body


@pytest.mark.parametrize(
    "bad,message",
    [
        ({**CHOICE_FLAT, "model": ""}, "model must be a non-empty string"),
        (
            {k: v for k, v in CHOICE_FLAT.items() if k != "model"},
            "model must be a non-empty string",
        ),
        ({**CHOICE_FLAT, "instructions": ""}, "instructions must be a non-empty string"),
        ({**CHOICE_FLAT, "options": []}, "options must be a non-empty array"),
        ({**CHOICE_FLAT, "options": "billing"}, "options must be a non-empty array"),
        ({**CHOICE_FLAT, "options": ["billing"]}, "each option must be an object"),
        (
            {**CHOICE_FLAT, "options": [{"description": "Payments"}]},
            "each option needs a non-empty string name",
        ),
        (
            {**CHOICE_FLAT, "options": [{"name": "billing"}, {"name": "billing"}]},
            "duplicate option name 'billing'",
        ),
    ],
)
def test_build_choice_body_rejects_each_bad_shape(bad, message):
    body, got = systemone.build_choice_body(bad)
    assert body is None
    assert got == message


def test_the_choice_route_forwards_the_reshaped_body_and_returns_the_answer_verbatim(jev):
    reply = {
        "model": "jev-1.13.0",
        "answers": {
            "answer": {
                "type": "choice",
                "choice": "technical",
                "probabilities": {"billing": 0.1, "technical": 0.9},
                "confidence": 0.88,
            }
        },
        "usage": {"input_tokens": 400_000, "output_tokens": 0},
    }
    jev.respond(lambda request: httpx.Response(200, json=reply))
    response = jev.post("/v1/systemone/choice", json=CHOICE_FLAT)
    assert response.status_code == 200
    assert response.json() == reply
    (call,) = jev.seen
    assert str(call.url) == "https://api.typesafe.test/v1/systemone"
    assert json.loads(call.content) == CHOICE_NESTED


def test_the_choice_route_leaves_the_same_span_shape_as_the_plain_route(jev, spans):
    jev.respond(
        lambda request: httpx.Response(
            200,
            json={
                "model": "jev-1.13.0",
                "answers": {"answer": {"type": "choice", "choice": "technical"}},
                "usage": {"input_tokens": 400_000, "output_tokens": 0},
            },
        )
    )
    assert jev.post("/v1/systemone/choice", json=CHOICE_FLAT).status_code == 200
    (span,) = spans()
    attrs = dict(span.attributes)
    assert attrs["agentproxy.decision.questions"] == 1
    assert attrs["gen_ai.response.model"] == "jev-1.13.0"


def test_the_choice_route_rejects_a_bad_shape_before_it_can_reach_upstream(jev):
    response = jev.post("/v1/systemone/choice", json={**CHOICE_FLAT, "options": []})
    assert response.status_code == 400
    assert response.json()["error"]["type"] == "invalid_request_error"
    assert jev.seen == []


@pytest.mark.parametrize("payload", ["[]", "not json"])
def test_the_choice_route_rejects_a_body_that_is_not_a_json_object(jev, payload):
    response = jev.post(
        "/v1/systemone/choice", content=payload, headers={"content-type": "application/json"}
    )
    assert response.status_code == 400
    assert jev.seen == []
