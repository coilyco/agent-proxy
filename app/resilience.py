"""
Resilience policies (leg 02 "resilience policies", leg 04 step 4).

Wraps upstream dispatch with:

* **response validation** - non-empty, tool-call JSON parses, no degenerate
  repetition / truncation garbage.
* **retry with backoff** - a capricious single bad generation becomes a reroll,
  not a user-visible failure (``llm_retries_total``).
* **fallback chain** - a down or busy backend advances to the next backend in the
  logical model's chain (``llm_fallbacks_total``).
* **per-backend circuit breaker** - stops hammering a dead backend and protects
  tail latency (``llm_circuit_state``).

Transport/HTTP errors count against a backend's breaker; a merely bad generation
does not (the backend is alive, the token draw was unlucky) but still triggers a
reroll.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import Counter
from dataclasses import dataclass
from enum import IntEnum
from typing import Any, AsyncIterator

from .config import get_settings
from .analysis import count_message_tokens, detect_context_truncation
from .models import Backend, LogicalModel, resolve
from .obs import (
    InstrumentedAction,
    RequestTraceContext,
    emit_instrumented_action,
    llm_backend_saturated_total,
    llm_circuit_state,
    llm_context_truncated_total,
    llm_fallbacks_total,
    llm_ollama_duration_seconds,
    llm_retries_total,
    llm_upstream_latency_seconds,
    llm_validation_failures_total,
    log,
    record_error,
    request_log_fields,
    get_tracer,
)
from . import upstream
from .upstream import UpstreamError, UpstreamResult, UpstreamStatusError, is_retryable_status


def _observe_ollama_measurements(
    logical_model: str, backend: Backend, result: UpstreamResult
) -> None:
    if backend.dialect != "ollama":
        return
    for name, milliseconds in result.ollama_measurements_ms().items():
        phase = name.removeprefix("ollama.").removesuffix("_duration_ms")
        llm_ollama_duration_seconds.labels(
            logical_model=logical_model,
            backend=backend.name,
            phase=phase,
        ).observe(milliseconds / 1000)


class BackendUnavailable(Exception):
    """Dispatch could not get a usable response out of the backend chain."""


class AllBackendsFailed(BackendUnavailable):
    """Every backend in the chain was exhausted or open.

    Raised only when the chain actually offered more than one backend. Issue
    #114 recorded the wording being applied to a single backend that rejected a
    single payload, which points an operator at capacity when the defect was in
    the request.
    """


class UpstreamRequestRejected(Exception):
    """The upstream rejected the request itself, not the backend's availability.

    Carries the upstream status and body through to the caller. A 400 is a
    settled fact about a byte-identical body, so it is neither retried nor
    reported as a backend failure (issue #114).
    """

    def __init__(self, message: str, status_code: int, body: str = "") -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class ContextTruncated(Exception):
    """The backend delivered a shorter context than the proxy asked for (issue #33).

    Raised only when ``PROXY_FAIL_ON_CONTEXT_TRUNCATION`` is set - the opt-in hard
    fail that turns a silently-halved window (the ``OLLAMA_NUM_PARALLEL`` division)
    into a loud 502 instead of returning the short read. The default path marks the
    result (metric + ``finish_reason=length`` + structured log) and returns it.
    """


class UnknownModel(Exception):
    """``dispatch_resilient`` was handed a tag the backend catalog does not know."""


class BackendSaturated(Exception):
    """A backend accepted the request and went quiet past the slow threshold.

    Not an error the backend reported - it never reported anything, which is the
    whole problem in issue #108. Raised internally so the chain advances to the
    next tier instead of waiting out the caller's deadline.
    """


class RequestDeadlineExceeded(BackendUnavailable):
    """The request's total wall-clock budget ran out (issue #112).

    Distinct from a per-attempt timeout: the budget spans queue wait, every
    retry, and every fallback, so a request can never cost more upstream time
    than the caller agreed to wait.
    """


# Key on a stream chunk that carries proxy progress rather than model output.
# The streaming surface turns it into an SSE comment (#104).
STREAM_STATE_KEY = "agentproxy.state"


def _state_chunk(state: str, **fields: Any) -> dict[str, Any]:
    return {STREAM_STATE_KEY: {"state": state, **fields}}


def _remaining_budget(deadline: float | None) -> float | None:
    """Seconds left before ``deadline``, or ``None`` when unbounded."""
    if deadline is None:
        return None
    return deadline - _now()


def _out_of_budget(deadline: float | None) -> bool:
    remaining = _remaining_budget(deadline)
    return remaining is not None and remaining <= 0


def request_deadline(header_ms: float | None = None) -> float | None:
    """The monotonic instant one request must finish by, or ``None``.

    The caller's own deadline may only shorten the configured one. A caller
    cannot buy itself more upstream time than the operator allowed, and the
    operator's ceiling is useless if a caller can talk past it.
    """
    configured = get_settings().request_deadline
    budgets = [value for value in (configured or None, header_ms) if value]
    if not budgets:
        return None
    return _now() + min(budgets)


async def _first_chunk_bounded(
    source: AsyncIterator[dict[str, Any]], limit: float | None
) -> AsyncIterator[dict[str, Any]]:
    """Yield from ``source``, raising :class:`TimeoutError` if nothing arrives in time.

    Only the wait for the first chunk is bounded. A stream that has started is
    making progress, and cutting it because generation is long would be the
    opposite of what issue #108 asks for.
    """
    iterator = source.__aiter__()
    if limit is None:
        async for item in iterator:
            yield item
        return
    try:
        first = await asyncio.wait_for(iterator.__anext__(), timeout=limit)
    except StopAsyncIteration:
        return
    yield first
    async for item in iterator:
        yield item


def _slow_after(deadline: float | None) -> float | None:
    """The attempt bound: the slow threshold, or the remaining budget if sooner."""
    slow = get_settings().backend_slow_after or None
    budget = _remaining_budget(deadline)
    candidates = [value for value in (slow, budget) if value is not None]
    return min(candidates) if candidates else None


def _saturated(
    model: LogicalModel,
    trace_ctx: RequestTraceContext | None,
    backend: Backend,
    attempt_span: Any,
) -> BackendSaturated:
    """Record a backend that went quiet, and open its breaker."""
    llm_backend_saturated_total.labels(logical_model=model.name, backend=backend.name).inc()
    log.warning(
        "dispatch.backend_saturated",
        **request_log_fields(
            trace_ctx,
            backend=backend.name,
            outcome="saturated",
            slow_after=get_settings().backend_slow_after,
        ),
    )
    if attempt_span is not None:
        attempt_span.set_attribute("agentproxy.outcome", "saturated")
        attempt_span.set_attribute("agentproxy.backend.regime", "saturated")
    record_error("backend_saturated", attempt_span)
    # A backend nobody can get a token out of is unavailable, whatever it says
    # about itself, so stop sending it work for the cooldown.
    breakers.record_saturation(backend)
    return BackendSaturated(f"{model.name}: backend {backend.name} did not respond in time")


def _deadline_exceeded(
    model: LogicalModel,
    trace_ctx: RequestTraceContext | None,
    backend: Backend,
    attempt_span: Any,
) -> RequestDeadlineExceeded:
    log.warning(
        "dispatch.deadline_exceeded",
        **request_log_fields(trace_ctx, backend=backend.name, outcome="deadline-exceeded"),
    )
    if attempt_span is not None:
        attempt_span.set_attribute("agentproxy.outcome", "deadline-exceeded")
    record_error("request_deadline_exceeded", attempt_span)
    return RequestDeadlineExceeded(f"{model.name}: request deadline exceeded before the response")


# Response validation

_SHORT_REPLY_CHARS = 3  # leg-01 truncation garbage is 1-3 chars of non-word junk.


def _tool_calls_parse(tool_calls: list[dict[str, Any]]) -> bool:
    """Every emitted tool call must carry parseable arguments (dict or JSON str)."""
    for call in tool_calls:
        fn = call.get("function", call)
        args = fn.get("arguments")
        if args is None:
            return False
        if isinstance(args, str):
            try:
                json.loads(args)
            except json.JSONDecodeError:
                return False
        elif not isinstance(args, dict):
            return False
    return True


def _is_degenerate_repetition(text: str) -> bool:
    """Heuristic loop detector: a long output dominated by one short token."""
    stripped = text.strip()
    if len(stripped) < 40:
        return False
    tokens = stripped.split()
    if len(tokens) >= 20 and len(set(tokens)) <= 2:
        return True
    # A single character/short substring repeated to fill the reply.
    if len(set(stripped)) <= 2 and len(stripped) >= 40:
        return True
    # A multi-word line echoed past any legitimate need is a stuck decoder.
    # Threshold rationale: docs/proxy.md#validation.
    lines = [ln.strip() for ln in stripped.splitlines() if ln.strip()]
    if len(lines) > 20:
        _, count = Counter(lines).most_common(1)[0]
        if count > 20:
            return True
    return False


def validate_response(result: UpstreamResult) -> tuple[bool, str]:
    """Return ``(ok, reason)``. ``reason`` is one of the leg-05 failure labels."""
    has_tools = bool(result.tool_calls)
    has_thinking = bool((result.thinking or "").strip())
    content = (result.content or "").strip()

    # Truly empty means nothing at all: a reasoning model that emitted only
    # `thinking` did real work. See docs/proxy.md#validation.
    if not content and not has_tools and not has_thinking:
        return False, "empty"
    if has_tools and not _tool_calls_parse(result.tool_calls):
        return False, "malformed_toolcall"
    # Truncation garbage is a 1-3 char *non-word* reply. A short real answer
    # ("OK", "42") has alphanumerics. See docs/proxy.md#validation.
    if (
        not has_tools
        and 0 < len(content) <= _SHORT_REPLY_CHARS
        and not any(c.isalnum() for c in content)
    ):
        return False, "truncation_garbage"
    if _is_degenerate_repetition(content):
        return False, "repetition"
    return True, "ok"


# Circuit breaker


class CircuitState(IntEnum):
    CLOSED = 0
    OPEN = 1
    HALF_OPEN = 2


@dataclass
class _Breaker:
    state: CircuitState = CircuitState.CLOSED
    consecutive_failures: int = 0
    consecutive_saturations: int = 0
    opened_at: float = 0.0
    # Whichever cooldown opened this breaker. 0 means the configured default.
    cooldown: float = 0.0


class CircuitBreakerRegistry:
    """Per-backend breakers. Open after N consecutive transport failures, cool
    down, then admit a single half-open probe before fully closing."""

    def __init__(self) -> None:
        self._breakers: dict[str, _Breaker] = {}

    def _get(self, backend: Backend) -> _Breaker:
        b = self._breakers.get(backend.name)
        if b is None:
            b = _Breaker()
            self._breakers[backend.name] = b
            llm_circuit_state.labels(backend=backend.name).set(CircuitState.CLOSED)
        return b

    def allow(self, backend: Backend) -> bool:
        """Whether a request may be sent to this backend right now."""
        b = self._get(backend)
        if b.state == CircuitState.OPEN:
            cooldown = b.cooldown or get_settings().circuit_cooldown
            if _now() - b.opened_at >= cooldown:
                b.state = CircuitState.HALF_OPEN
                llm_circuit_state.labels(backend=backend.name).set(CircuitState.HALF_OPEN)
                return True  # admit one probe
            return False
        return True

    def record_success(self, backend: Backend) -> None:
        b = self._get(backend)
        b.consecutive_failures = 0
        b.consecutive_saturations = 0
        b.cooldown = 0.0
        if b.state != CircuitState.CLOSED:
            log.info("circuit.close", backend=backend.name)
        b.state = CircuitState.CLOSED
        llm_circuit_state.labels(backend=backend.name).set(CircuitState.CLOSED)

    def record_saturation(self, backend: Backend) -> None:
        """A backend that went quiet, counted and cooled on its own terms.

        A busy backend is not a broken one (#111). Each saturation costs a full
        slow-path wait, so the count that trips it is lower than the failure
        threshold, and the condition behind it - a game on the shared GPU -
        outlasts a 30-second cooldown by hours, so the stick is longer.
        """
        b = self._get(backend)
        b.consecutive_saturations += 1
        settings = get_settings()
        if (
            b.state == CircuitState.HALF_OPEN
            or b.consecutive_saturations >= settings.saturation_threshold
        ):
            b.state = CircuitState.OPEN
            b.opened_at = _now()
            b.cooldown = settings.saturation_cooldown
            llm_circuit_state.labels(backend=backend.name).set(CircuitState.OPEN)
            log.warning(
                "circuit.open_saturated",
                backend=backend.name,
                saturations=b.consecutive_saturations,
                cooldown=settings.saturation_cooldown,
            )

    def record_failure(self, backend: Backend) -> None:
        b = self._get(backend)
        b.consecutive_failures += 1
        threshold = get_settings().circuit_fail_threshold
        if b.state == CircuitState.HALF_OPEN or b.consecutive_failures >= threshold:
            b.state = CircuitState.OPEN
            b.opened_at = _now()
            llm_circuit_state.labels(backend=backend.name).set(CircuitState.OPEN)
            log.warning("circuit.open", backend=backend.name, failures=b.consecutive_failures)


def _now() -> float:
    return time.monotonic()


# Module-wide breaker registry (per process, matching the per-pod queue).
breakers = CircuitBreakerRegistry()


# Delivered-context verification (issue #33)


def _verify_delivered_context(
    model: LogicalModel,
    backend: Backend,
    result: UpstreamResult,
    prompt_tokens_sent: int,
    trace_ctx: RequestTraceContext | None,
    attempt_span: Any,
) -> None:
    """Fail loud when the backend delivered less context than the proxy asked for.

    Every backend that accepts Agent Proxy's derived ``num_ctx`` must report
    prompt usage so the proxy can detect a shorter delivered window. Ollama
    receives ``options.num_ctx`` directly. LiteLLM receives the same policy as a
    top-level extension and returns OpenAI usage. On detection this marks the
    result (so ``finish_reason`` becomes ``length``, never a silent short read),
    increments ``llm_context_truncated_total``, and emits a structured warning.
    When ``fail_on_context_truncation`` is set it raises
    :class:`ContextTruncated` for a hard 502 instead.
    """
    if not backend.injects_num_ctx:
        return
    settings = get_settings()
    if not detect_context_truncation(
        prompt_tokens_sent,
        result.prompt_eval_count,
        model.num_ctx,
        settings.context_truncation_tolerance,
    ):
        return
    result.context_truncated = True
    emit_instrumented_action(
        InstrumentedAction(
            log_event="dispatch.context_truncated",
            metric=lambda: llm_context_truncated_total.labels(
                logical_model=model.name, backend=backend.name
            ).inc(),
            span_event="dispatch.context_truncated",
            fields=request_log_fields(
                trace_ctx,
                backend=backend.name,
                outcome="context-truncated",
                prompt_tokens_sent=prompt_tokens_sent,
                prompt_eval_count=result.prompt_eval_count,
                target_num_ctx=model.num_ctx,
                num_parallel=backend.num_parallel,
            ),
        ),
    )
    if attempt_span is not None:
        attempt_span.set_attribute("agentproxy.context_truncated", True)
        attempt_span.set_attribute("agentproxy.target_num_ctx", model.num_ctx)
    record_error("context_truncated", attempt_span)
    if settings.fail_on_context_truncation:
        raise ContextTruncated(
            f"{model.name}: backend {backend.name} delivered {result.prompt_eval_count} "
            f"prompt tokens against a {model.num_ctx}-token window - effective context "
            f"was cut below the ask. Verify the gateway's num_ctx forwarding and the "
            f"provider's effective context configuration."
        )


# Dispatch with resilience


async def dispatch(
    model: LogicalModel,
    messages: list[dict[str, Any]],
    *,
    tools: list[dict[str, Any]] | None = None,
    tool_policy: upstream.ToolPolicy | None = None,
    options: dict[str, Any] | None = None,
    trace_ctx: RequestTraceContext | None = None,
    deadline: float | None = None,
) -> UpstreamResult:
    """Dispatch a non-streaming chat with full resilience. Walks the fallback
    chain, retrying each live backend with backoff, validating every response.

    ``deadline`` is a ``time.monotonic`` instant bounding the whole call, retries
    and fallbacks included. Past it no further attempt starts and the in-flight
    attempt is cut, so abandoned work stops costing inference (issue #112).
    """
    settings = get_settings()
    last_error: str = "no backends"
    attempted_backends: list[str] = []
    tracer = get_tracer()
    trace_attrs = trace_ctx.attrs() if trace_ctx else None
    # The proxy's own prompt count, identical across attempts, held once as the
    # reference for the delivered-context check (issue #33).
    prompt_tokens_sent = count_message_tokens(messages)

    for idx, backend in enumerate(model.backends):
        if not breakers.allow(backend):
            last_error = f"{backend.name} circuit open"
            if idx + 1 < len(model.backends):
                llm_fallbacks_total.labels(logical_model=model.name, backend=backend.name).inc()
            log.warning(
                "dispatch.circuit_open",
                **request_log_fields(trace_ctx, backend=backend.name, outcome="fallback"),
            )
            continue

        attempted_backends.append(backend.name)
        for attempt in range(settings.max_retries + 1):
            # Driven manually, not `with`: the body can continue/break/return
            # mid-await, and a leaked span leaks its OTel context token.
            attempt_span_cm = tracer.start_as_current_span("resilience.attempt") if tracer else None
            attempt_span = attempt_span_cm.__enter__() if attempt_span_cm is not None else None
            try:
                if attempt_span is not None:
                    attempt_span.set_attribute("agentproxy.logical_model", model.name)
                    attempt_span.set_attribute("agentproxy.backend", backend.name)
                    attempt_span.set_attribute("agentproxy.backend_dialect", backend.dialect)
                    attempt_span.set_attribute("agentproxy.backend.regime", backend.regime)
                    attempt_span.set_attribute("agentproxy.resolved_backend", backend.url)
                    attempt_span.set_attribute("agentproxy.attempt", attempt)
                    if trace_attrs is not None:
                        for key, value in trace_attrs.items():
                            attempt_span.set_attribute(key, value)
                try:
                    budget = _remaining_budget(deadline)
                    if budget is not None and budget <= 0:
                        raise _deadline_exceeded(model, trace_ctx, backend, attempt_span)
                    start = _now()
                    # wait_for cancels the attempt at the deadline, which closes
                    # the httpx request and with it the upstream connection.
                    result = await asyncio.wait_for(
                        upstream.chat(
                            backend,
                            model.num_ctx,
                            messages,
                            tools=tools,
                            tool_policy=tool_policy,
                            options=options,
                            span_attrs=trace_attrs,
                        ),
                        timeout=_slow_after(deadline),
                    )
                    llm_upstream_latency_seconds.labels(
                        logical_model=model.name, backend=backend.name
                    ).observe(_now() - start)
                    _observe_ollama_measurements(model.name, backend, result)
                    if attempt_span is not None:
                        attempt_span.set_attribute(
                            "gen_ai.usage.input_tokens", result.prompt_eval_count
                        )
                        attempt_span.set_attribute("gen_ai.usage.output_tokens", result.eval_count)
                        attempt_span.set_attribute(
                            "response.finish_reasons",
                            [result.done_reason] if result.done_reason else [],
                        )
                except TimeoutError as exc:
                    # wait_for already cancelled the attempt, so the upstream
                    # connection is closed rather than left generating.
                    if _out_of_budget(deadline):
                        raise _deadline_exceeded(model, trace_ctx, backend, attempt_span) from exc
                    # Slow, not spent: the backend is the problem, so advance
                    # the chain. docs/saturation-failover.md.
                    last_error = str(_saturated(model, trace_ctx, backend, attempt_span))
                    break
                except UpstreamStatusError as exc:
                    if not is_retryable_status(exc.status_code):
                        # Settled about a body that will not change, and the
                        # backend answered. docs/upstream-error-classification.md.
                        breakers.record_success(backend)
                        log.warning(
                            "dispatch.request_rejected",
                            **request_log_fields(
                                trace_ctx,
                                backend=backend.name,
                                attempt=attempt,
                                outcome="request-rejected",
                                upstream_status=exc.status_code,
                            ),
                        )
                        if attempt_span is not None:
                            attempt_span.set_attribute("agentproxy.outcome", "request-rejected")
                            attempt_span.set_attribute(
                                "agentproxy.upstream.status_code", exc.status_code
                            )
                        raise UpstreamRequestRejected(
                            f"{model.name}: upstream rejected the request ({exc})",
                            status_code=exc.status_code,
                            body=exc.body,
                        ) from exc
                    breakers.record_failure(backend)
                    last_error = str(exc)
                    log.warning(
                        "dispatch.transport_error",
                        backend=backend.name,
                        attempt=attempt,
                        error=str(exc),
                        upstream_status=exc.status_code,
                    )
                    if attempt_span is not None:
                        record_error("upstream_transport_failed", attempt_span)
                        attempt_span.set_attribute("agentproxy.outcome", "failed")
                        attempt_span.set_attribute(
                            "agentproxy.upstream.status_code", exc.status_code
                        )
                    if attempt < settings.max_retries and not _out_of_budget(deadline):
                        llm_retries_total.labels(
                            logical_model=model.name, backend=backend.name
                        ).inc()
                        log.info(
                            "dispatch.retry",
                            **request_log_fields(
                                trace_ctx, backend=backend.name, attempt=attempt, outcome="retry"
                            ),
                        )
                        await asyncio.sleep(settings.retry_base_delay * (2**attempt))
                        continue
                    break  # exhausted this backend's retries -> fall back
                except UpstreamError as exc:
                    breakers.record_failure(backend)
                    last_error = str(exc)
                    log.warning(
                        "dispatch.transport_error",
                        backend=backend.name,
                        attempt=attempt,
                        error=str(exc),
                    )
                    if attempt_span is not None:
                        record_error("upstream_transport_failed", attempt_span)
                        attempt_span.set_attribute("agentproxy.outcome", "failed")
                    if attempt < settings.max_retries:
                        llm_retries_total.labels(
                            logical_model=model.name, backend=backend.name
                        ).inc()
                        log.info(
                            "dispatch.retry",
                            **request_log_fields(
                                trace_ctx, backend=backend.name, attempt=attempt, outcome="retry"
                            ),
                        )
                        await asyncio.sleep(settings.retry_base_delay * (2**attempt))
                        continue
                    break  # exhausted this backend's retries -> fall back

                result.served_by = backend.name
                result.served_regime = backend.regime
                ok, reason = validate_response(result)
                if ok:
                    breakers.record_success(backend)
                    _verify_delivered_context(
                        model, backend, result, prompt_tokens_sent, trace_ctx, attempt_span
                    )
                    log.info(
                        "dispatch.ok",
                        **request_log_fields(
                            trace_ctx, backend=backend.name, attempt=attempt, outcome="ok"
                        ),
                    )
                    if attempt_span is not None:
                        attempt_span.set_attribute("agentproxy.outcome", "ok")
                        attempt_span.set_attribute(
                            "gen_ai.usage.input_tokens", result.prompt_eval_count
                        )
                        attempt_span.set_attribute("gen_ai.usage.output_tokens", result.eval_count)
                        attempt_span.set_attribute(
                            "response.finish_reasons",
                            [result.done_reason] if result.done_reason else [],
                        )
                    return result

                # Bad generation: reroll on this live backend, do not trip the breaker.
                llm_validation_failures_total.labels(logical_model=model.name, reason=reason).inc()
                last_error = f"validation:{reason}"
                log.warning(
                    "dispatch.validation_failed",
                    backend=backend.name,
                    reason=reason,
                    attempt=attempt,
                )
                record_error("response_validation_failed", attempt_span)
                if attempt_span is not None:
                    attempt_span.set_attribute("agentproxy.outcome", "validation-failure")
                    attempt_span.set_attribute("agentproxy.validation_reason", reason)
                if attempt < settings.max_retries:
                    llm_retries_total.labels(logical_model=model.name, backend=backend.name).inc()
                    log.info(
                        "dispatch.retry",
                        **request_log_fields(
                            trace_ctx,
                            backend=backend.name,
                            attempt=attempt,
                            outcome="validation-failure",
                        ),
                    )
                    await asyncio.sleep(settings.retry_base_delay * (2**attempt))
                    continue
                breakers.record_success(backend)  # backend is alive, just unlucky
                outcome = (
                    "truncation-avoided" if reason == "truncation_garbage" else "validation-failure"
                )
                log.info(
                    "dispatch.validation_terminal",
                    **request_log_fields(
                        trace_ctx,
                        backend=backend.name,
                        attempt=attempt,
                        outcome=outcome,
                        reason=reason,
                    ),
                )
                if attempt_span is not None:
                    attempt_span.set_attribute("agentproxy.outcome", "validation-failure")
                    attempt_span.set_attribute("agentproxy.validation_reason", reason)
                    attempt_span.set_attribute(
                        "gen_ai.usage.input_tokens", result.prompt_eval_count
                    )
                    attempt_span.set_attribute("gen_ai.usage.output_tokens", result.eval_count)
            except asyncio.CancelledError:
                cancellation_fields = request_log_fields(
                    trace_ctx,
                    backend=backend.name,
                    attempt=attempt,
                    outcome="cancelled",
                )
                log.info("dispatch.cancelled", **cancellation_fields)
                if attempt_span is not None:
                    attempt_span.set_attribute("agentproxy.outcome", "cancelled")
                    attempt_span.add_event("dispatch.cancelled", cancellation_fields)
                raise
            finally:
                if attempt_span_cm is not None:
                    attempt_span_cm.__exit__(None, None, None)

        if idx + 1 < len(model.backends):
            llm_fallbacks_total.labels(logical_model=model.name, backend=backend.name).inc()
            log.warning(
                "dispatch.fallback",
                **request_log_fields(trace_ctx, backend=backend.name, outcome="fallback"),
            )

    raise _chain_exhausted(model, attempted_backends, last_error)


def _chain_exhausted(
    model: LogicalModel, attempted: list[str], last_error: str
) -> BackendUnavailable:
    """Name the failure after what was actually attempted (issue #114)."""
    if len(attempted) > 1:
        return AllBackendsFailed(
            f"{model.name}: all {len(attempted)} backends failed ({last_error})"
        )
    if attempted:
        return BackendUnavailable(f"{model.name}: backend {attempted[0]} failed ({last_error})")
    return BackendUnavailable(f"{model.name}: no backend was available ({last_error})")


async def dispatch_resilient(
    model_name: str,
    messages: list[dict[str, Any]],
    *,
    tools: list[dict[str, Any]] | None = None,
    tool_policy: upstream.ToolPolicy | None = None,
    options: dict[str, Any] | None = None,
    trace_ctx: RequestTraceContext | None = None,
    deadline: float | None = None,
) -> UpstreamResult:
    """Tag-based front door to the resilient dispatch engine (leg 04 step 10).

    Resolves the real ollama ``model_name`` against the backend catalog (deriving
    its safe ``num_ctx`` and fallback chain) and runs :func:`dispatch`:
    retry-with-backoff on each live backend (``llm_retries_total``), fallback to
    the next backend on exhaustion (``llm_fallbacks_total``), and response
    validation on every attempt. A backend that always fails walks the chain and
    then raises :class:`AllBackendsFailed` - a clean error, never a hang.

    Callers that already hold a resolved :class:`LogicalModel` - the worker, which
    resolves it at the route boundary for the 404 guard - call :func:`dispatch`
    directly; callers that only carry the tag string use this entry point.
    """
    model = await resolve(model_name)
    if model is None:
        raise UnknownModel(model_name)
    # The engine's retry/fallback logging always reads a trace context; a bare
    # caller that only had the name gets a minimal one built from it here.
    if trace_ctx is None:
        trace_ctx = RequestTraceContext(
            logical_model=model.name, request_model=model_name, request_kind="chat"
        )
    return await dispatch(
        model,
        messages,
        tools=tools,
        tool_policy=tool_policy,
        options=options,
        trace_ctx=trace_ctx,
        deadline=deadline,
    )


async def dispatch_stream(
    model: LogicalModel,
    messages: list[dict[str, Any]],
    *,
    tools: list[dict[str, Any]] | None = None,
    tool_policy: upstream.ToolPolicy | None = None,
    options: dict[str, Any] | None = None,
    trace_ctx: RequestTraceContext | None = None,
    deadline: float | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Stream a chat with connect-time fallback across the chain.

    Full content validation cannot apply to a token stream, so streaming gets the
    fallback chain and the circuit breaker (a backend that errors *before* the
    first chunk falls back), but not the reroll. Harnesses that want the full
    resilience guarantees use the non-streaming path.
    """
    last_error = "no backends"
    attempted_backends: list[str] = []
    trace_attrs = trace_ctx.attrs() if trace_ctx else None
    candidates = len(model.backends)
    for index, backend in enumerate(model.backends):
        if not breakers.allow(backend):
            continue
        attempted_backends.append(backend.name)
        # A silent retry storm is indistinguishable from one slow attempt from
        # outside. docs/sse-heartbeats.md.
        yield _state_chunk(
            "attempt",
            n=index + 1,
            of=candidates,
            backend=backend.name,
            regime=backend.regime,
        )
        try:
            first = True
            if _out_of_budget(deadline):
                raise _deadline_exceeded(model, trace_ctx, backend, None)
            stream = upstream.chat_stream(
                backend,
                model.num_ctx,
                messages,
                tools=tools,
                tool_policy=tool_policy,
                options=options,
                span_attrs=trace_attrs,
            )
            # Only time to the first chunk is bounded. Generation that has
            # started is progress. docs/saturation-failover.md.
            async for chunk in _first_chunk_bounded(stream, _slow_after(deadline)):
                if first:
                    first = False
                    yield _state_chunk("upstream_started", backend=backend.name)
                elif _out_of_budget(deadline):
                    raise _deadline_exceeded(model, trace_ctx, backend, None)
                if chunk.get("done"):
                    result = upstream.parse_stream_result(chunk, backend.ollama_tag)
                    _observe_ollama_measurements(model.name, backend, result)
                yield chunk
            breakers.record_success(backend)
            return
        except TimeoutError as exc:
            if _out_of_budget(deadline):
                raise _deadline_exceeded(model, trace_ctx, backend, None) from exc
            last_error = str(_saturated(model, trace_ctx, backend, None))
            yield _state_chunk("backend_saturated", backend=backend.name, failing_over=True)
            continue
        except asyncio.CancelledError:
            log.info(
                "stream.cancelled",
                **request_log_fields(trace_ctx, backend=backend.name, outcome="cancelled"),
            )
            raise
        except UpstreamStatusError as exc:
            if not is_retryable_status(exc.status_code):
                # Settled rejection of this body: falling back would only ask a
                # second backend the same invalid question (issue #114).
                breakers.record_success(backend)
                log.warning(
                    "stream.request_rejected",
                    **request_log_fields(
                        trace_ctx,
                        backend=backend.name,
                        outcome="request-rejected",
                        upstream_status=exc.status_code,
                    ),
                )
                raise UpstreamRequestRejected(
                    f"{model.name}: upstream rejected the request ({exc})",
                    status_code=exc.status_code,
                    body=exc.body,
                ) from exc
            breakers.record_failure(backend)
            last_error = str(exc)
            log.warning(
                "stream.transport_error",
                **request_log_fields(
                    trace_ctx, backend=backend.name, error=str(exc), outcome="failed"
                ),
            )
            if not first:
                raise _chain_exhausted(model, attempted_backends, last_error) from exc
            continue
        except UpstreamError as exc:
            breakers.record_failure(backend)
            last_error = str(exc)
            log.warning(
                "stream.transport_error",
                **request_log_fields(
                    trace_ctx, backend=backend.name, error=str(exc), outcome="failed"
                ),
            )
            # Only safe to fall back if nothing was emitted yet.
            if not first:
                raise BackendUnavailable(
                    f"{model.name}: stream broke mid-flight ({last_error})"
                ) from exc
            continue
    raise _chain_exhausted(model, attempted_backends, last_error)
