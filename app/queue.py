"""
In-memory queue and worker pool - the resilience core (leg 02 "in-memory queue",
leg 04 step 3).

The web layer accepts a request, enqueues a job, and awaits its future. A worker
pops the job and dispatches it through the resilience policies. A bounded queue
gives backpressure: when it is full the route returns 429 instead of unbounded
buffering. This decouples client-accept from upstream health and is per-pod and
ephemeral by design (no shared state, leg 02 topology).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from opentelemetry import context as otel_context

from . import resilience
from .config import get_settings
from .models import LogicalModel
from .obs import (
    RequestTraceContext,
    llm_queue_depth,
    llm_queue_rejected_total,
    log,
    record_error,
    request_log_fields,
    get_tracer,
)
from .upstream import ToolPolicy, UpstreamResult


class QueueBusy(Exception):
    """The bounded queue is full - the route should answer 429."""


@dataclass
class Job:
    model: LogicalModel
    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]] | None
    options: dict[str, Any] | None
    tool_policy: ToolPolicy | None = None
    trace_ctx: RequestTraceContext | None = None
    # Defaulted to None only so it can trail trace_ctx in the dataclass; submit()
    # always constructs a Job with a live future, so the awaiting paths guard it.
    future: "asyncio.Future[UpstreamResult] | None" = field(default=None)
    otel_context: Any | None = None
    # Set at accept time, so queue wait spends the same budget as generation.
    deadline: float | None = None
    # Resolved the moment a worker claims the job, which is what closes the
    # queue.wait span. See docs/proxy-request-path.md.
    dequeued: "asyncio.Future[None] | None" = field(default=None)


class WorkQueue:
    """A bounded ``asyncio.Queue`` fronting a fixed worker pool."""

    def __init__(self, maxsize: int, worker_count: int) -> None:
        # The queue is created in start() so it binds to the loop that actually
        # runs the workers (a module-singleton must not bind at import time).
        self._maxsize = maxsize
        self._queue: asyncio.Queue[Job] | None = None
        self._worker_count = worker_count
        self._workers: list[asyncio.Task] = []
        self._running = False

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._queue = asyncio.Queue(maxsize=self._maxsize)
        loop = asyncio.get_running_loop()
        self._workers = [loop.create_task(self._worker(i)) for i in range(self._worker_count)]
        log.info("queue.start", workers=self._worker_count, maxsize=self._maxsize)

    async def stop(self) -> None:
        self._running = False
        for w in self._workers:
            w.cancel()
        for w in self._workers:
            try:
                await w
            except asyncio.CancelledError:
                pass
        self._workers = []
        self._queue = None
        log.info("queue.stop")

    async def submit(
        self,
        model: LogicalModel,
        messages,
        tools,
        options,
        *,
        tool_policy: ToolPolicy | None = None,
        trace_ctx: RequestTraceContext | None = None,
        deadline: float | None = None,
    ) -> UpstreamResult:
        """Enqueue a job and await its result. Raises ``QueueBusy`` when full."""
        if self._queue is None:
            raise RuntimeError("queue not started")
        loop = asyncio.get_running_loop()
        future: asyncio.Future[UpstreamResult] = loop.create_future()
        dequeued: asyncio.Future[None] = loop.create_future()
        job = Job(
            model=model,
            messages=messages,
            tools=tools,
            options=options,
            tool_policy=tool_policy,
            trace_ctx=trace_ctx,
            future=future,
            otel_context=otel_context.get_current(),
            deadline=deadline,
            dequeued=dequeued,
        )
        try:
            self._queue.put_nowait(job)
        except asyncio.QueueFull:
            llm_queue_rejected_total.inc()
            if trace_ctx is not None:
                log.warning("queue.rejected", **request_log_fields(trace_ctx, outcome="rejected"))
            raise QueueBusy(model.name)

        def discard_if_cancelled(completed: asyncio.Future[UpstreamResult]) -> None:
            if completed.cancelled():
                self._discard_cancelled_job(job)

        future.add_done_callback(discard_if_cancelled)
        llm_queue_depth.set(self._queue.qsize())
        tracer = get_tracer()
        if tracer is None:
            return await future
        # Driven manually so the span closes at dequeue, not at completion.
        # Issue #105 and docs/proxy-request-path.md.
        span_cm = tracer.start_as_current_span("queue.wait")
        span = span_cm.__enter__()
        open_span = True
        try:
            span.set_attribute("agentproxy.logical_model", model.name)
            if trace_ctx is not None:
                for key, value in trace_ctx.attrs().items():
                    span.set_attribute(key, value)
            # `future` joins the wait so a job that fails before any worker
            # claims it cannot leave this hanging.
            waiters: set[asyncio.Future[Any]] = {dequeued, future}
            await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
            span.set_attribute("agentproxy.queue.admitted", dequeued.done())
            span_cm.__exit__(None, None, None)
            open_span = False
            return await future
        except asyncio.CancelledError:
            # asyncio.wait does not pass cancellation to the futures it watches,
            # so the job has to be cancelled by hand or its queue slot leaks.
            if not future.done():
                future.cancel()
            if open_span:
                span.set_attribute("agentproxy.outcome", "cancelled")
                span.add_event("queue.cancelled", {"outcome": "cancelled"})
            raise
        finally:
            if open_span:
                span_cm.__exit__(None, None, None)

    def _discard_cancelled_job(self, job: Job) -> None:
        """Remove a cancelled job that has not yet been claimed by a worker."""

        queue = self._queue
        if queue is None:
            return

        retained: list[Job] = []
        removed = False
        while True:
            try:
                queued = queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            queue.task_done()
            if queued is job and not removed:
                removed = True
                continue
            retained.append(queued)

        for queued in retained:
            queue.put_nowait(queued)

        llm_queue_depth.set(queue.qsize())
        if removed:
            log.info(
                "queue.cancelled",
                **request_log_fields(job.trace_ctx, outcome="cancelled"),
            )

    async def _worker(self, idx: int) -> None:
        # start() creates the queue before spawning any worker, so it is bound
        # for the worker's whole lifetime; bind it locally so the type narrows.
        queue = self._queue
        assert queue is not None
        while True:
            job = await queue.get()
            if job.dequeued is not None and not job.dequeued.done():
                job.dequeued.set_result(None)
            llm_queue_depth.set(queue.qsize())
            context_token = (
                otel_context.attach(job.otel_context) if job.otel_context is not None else None
            )
            try:
                future = job.future
                if future is None or future.cancelled():
                    continue
                dispatch_task = asyncio.create_task(
                    resilience.dispatch(
                        job.model,
                        job.messages,
                        tools=job.tools,
                        tool_policy=job.tool_policy,
                        options=job.options,
                        trace_ctx=job.trace_ctx,
                        deadline=job.deadline,
                    )
                )

                def cancel_dispatch(completed: asyncio.Future[UpstreamResult]) -> None:
                    if completed.cancelled() and not dispatch_task.done():
                        dispatch_task.cancel()

                future.add_done_callback(cancel_dispatch)
                try:
                    result = await dispatch_task
                except asyncio.CancelledError:
                    if future.cancelled():
                        log.info(
                            "queue.cancelled",
                            **request_log_fields(job.trace_ctx, outcome="cancelled"),
                        )
                        continue
                    raise
                finally:
                    future.remove_done_callback(cancel_dispatch)
                if not future.done():
                    future.set_result(result)
            except Exception as exc:  # deliver the failure to the awaiting route
                record_error("queue_worker_failed")
                if job.future is not None and not job.future.done():
                    job.future.set_exception(exc)
            finally:
                if context_token is not None:
                    otel_context.detach(context_token)
                queue.task_done()


_queue: WorkQueue | None = None


def get_queue() -> WorkQueue:
    """Process-wide queue singleton (constructed lazily on first access)."""
    global _queue
    if _queue is None:
        s = get_settings()
        _queue = WorkQueue(maxsize=s.queue_maxsize, worker_count=s.worker_count)
    return _queue
