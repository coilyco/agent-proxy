"""Bounded queue backpressure (leg 04 step 3)."""

import asyncio
import time

import pytest

from app.models import LogicalModel, Backend
from app.queue import Job, QueueBusy, WorkQueue


def _model():
    return LogicalModel("fast", 32768, [Backend("b", "http://x", "t")])


async def test_full_queue_raises_queue_busy():
    # No workers, maxsize 1: the first put fills the queue, the next submit 429s.
    q = WorkQueue(maxsize=1, worker_count=0)
    q._queue = asyncio.Queue(maxsize=1)  # bind on this test's loop, no workers.
    loop = asyncio.get_running_loop()
    q._queue.put_nowait(Job(_model(), [], None, None, loop.create_future()))
    with pytest.raises(QueueBusy):
        await q.submit(_model(), [], None, None)


async def test_cancelled_waiting_job_releases_queue_slot():
    q = WorkQueue(maxsize=1, worker_count=0)
    q._queue = asyncio.Queue(maxsize=1)  # bind on this test's loop, no workers.

    waiting = asyncio.create_task(q.submit(_model(), [], None, None))
    await asyncio.sleep(0)
    assert q._queue.qsize() == 1

    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    await asyncio.sleep(0)

    assert q._queue.qsize() == 0

    replacement = asyncio.create_task(q.submit(_model(), [], None, None))
    await asyncio.sleep(0)
    assert q._queue.qsize() == 1
    replacement.cancel()
    with pytest.raises(asyncio.CancelledError):
        await replacement


async def test_worker_delivers_result(monkeypatch):
    from app import resilience
    from app.upstream import UpstreamResult

    async def fake_dispatch(
        model, messages, tools=None, tool_policy=None, options=None, trace_ctx=None, deadline=None
    ):
        return UpstreamResult(model="t", content="hello")

    monkeypatch.setattr(resilience, "dispatch", fake_dispatch)
    q = WorkQueue(maxsize=4, worker_count=1)
    await q.start()
    try:
        result = await asyncio.wait_for(q.submit(_model(), [], None, None), timeout=5)
        assert result.content == "hello"
    finally:
        await q.stop()


async def test_worker_restores_submitter_trace_context(monkeypatch):
    from opentelemetry import trace
    from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags

    from app import resilience
    from app.upstream import UpstreamResult

    observed = []

    async def fake_dispatch(
        model, messages, tools=None, tool_policy=None, options=None, trace_ctx=None, deadline=None
    ):
        observed.append(trace.get_current_span().get_span_context())
        return UpstreamResult(model="t", content="hello")

    monkeypatch.setattr(resilience, "dispatch", fake_dispatch)
    monkeypatch.setattr("app.queue.get_tracer", lambda: None)
    parent = NonRecordingSpan(
        SpanContext(
            trace_id=0x123,
            span_id=0x456,
            is_remote=False,
            trace_flags=TraceFlags(0x01),
        )
    )
    q = WorkQueue(maxsize=4, worker_count=1)
    await q.start()
    try:
        with trace.use_span(parent, end_on_exit=False):
            await asyncio.wait_for(q.submit(_model(), [], None, None), timeout=5)
    finally:
        await q.stop()

    assert len(observed) == 1
    assert observed[0].trace_id == 0x123
    assert observed[0].span_id == 0x456


async def test_queue_wait_ends_at_dequeue_not_at_completion():
    """Issue #105: the span tracked request.chat to within a millisecond."""
    from app import obs, resilience
    from app.upstream import UpstreamResult

    finished = asyncio.Event()
    spans: list[tuple[str, dict, float]] = []

    class _Span:
        def __init__(self, name):
            self.name = name
            self.attrs: dict = {}
            self.closed_at: float | None = None

        def set_attribute(self, key, value):
            self.attrs[key] = value

        def add_event(self, name, attrs=None):
            return None

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            self.closed_at = time.monotonic()
            spans.append((self.name, self.attrs, self.closed_at))
            return False

    class _Tracer:
        def start_as_current_span(self, name):
            return _Span(name)

    async def slow_dispatch(
        model, messages, tools=None, tool_policy=None, options=None, trace_ctx=None, deadline=None
    ):
        await finished.wait()
        return UpstreamResult(model="t", content="done")

    original = obs.get_tracer
    obs.get_tracer = lambda: _Tracer()
    import app.queue as queue_module

    queue_module.get_tracer = lambda: _Tracer()
    resilience_dispatch = resilience.dispatch
    resilience.dispatch = slow_dispatch
    q = WorkQueue(maxsize=4, worker_count=1)
    await q.start()
    try:
        pending = asyncio.create_task(q.submit(_model(), [], None, None))
        # The worker has claimed the job, so the wait is over even though the
        # request is not.
        await asyncio.sleep(0.05)
        assert [name for name, _attrs, _closed in spans] == ["queue.wait"]
        assert spans[0][1]["agentproxy.queue.admitted"] is True

        finished.set()
        result = await asyncio.wait_for(pending, timeout=1)
        assert result.content == "done"
        # Still exactly one, and it closed before the response existed.
        assert len(spans) == 1
    finally:
        resilience.dispatch = resilience_dispatch
        obs.get_tracer = original
        queue_module.get_tracer = original
        await q.stop()
