"""Per-session context usage, read by the aterm context-token meter.

The proxy already maps ``x-agent-session-id`` to ``agent.session_id`` on every
request. This keeps one small rollup per session so a client can ask how full a
seat's context is without scanning the trajectory store, whose events carry no
session index. It lives in the process and is lost on restart: a seat's meter
goes blank until its next turn, which is honest, and a store here would hold
live state the trajectory contract already refuses to mutate.
"""

from __future__ import annotations

import re
import threading
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .obs import RequestTraceContext
from .route_registry import get_route_registry
from .upstream import UpstreamResult

FORMAT = "agent-proxy.session-usage.v1"
# The same alphabet as agent.origin, so a header cannot smuggle a path or a quote.
SESSION_ID_PATTERN = re.compile(r"[A-Za-z0-9._:/@-]{1,128}")
MAX_SESSIONS = 2048
MAX_IDS_PER_READ = 64


@dataclass
class SessionUsage:
    session_id: str
    model: str
    first_seen: datetime
    last_seen: datetime
    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    # Latest reported prompt plus completion, what the next prompt starts from.
    # Zero means nothing reported yet. See docs/operational-views.md.
    context_tokens: int = 0

    def view(self) -> dict[str, Any]:
        registry = get_route_registry()
        route = registry.routes.get(self.model) if registry is not None else None
        return {
            "session_id": self.session_id,
            "model": self.model,
            "requests": self.requests,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "context_tokens": self.context_tokens,
            "context_window": route.context_window if route is not None else None,
            "first_seen": self.first_seen.isoformat().replace("+00:00", "Z"),
            "last_seen": self.last_seen.isoformat().replace("+00:00", "Z"),
        }


class SessionUsageLedger:
    """Newest-touched sessions, bounded so a stream of one-off ids cannot grow it."""

    def __init__(self, max_sessions: int = MAX_SESSIONS) -> None:
        self._max = max_sessions
        self._lock = threading.Lock()
        self._sessions: OrderedDict[str, SessionUsage] = OrderedDict()

    def record(
        self,
        session_id: str,
        model: str,
        *,
        prompt_tokens: int,
        completion_tokens: int,
        now: datetime | None = None,
    ) -> None:
        if not SESSION_ID_PATTERN.fullmatch(session_id):
            return
        moment = now or datetime.now(timezone.utc)
        with self._lock:
            entry = self._sessions.get(session_id)
            if entry is None:
                entry = SessionUsage(session_id, model, moment, moment)
                self._sessions[session_id] = entry
            self._sessions.move_to_end(session_id)
            entry.model, entry.last_seen = model, moment
            entry.requests += 1
            entry.input_tokens += prompt_tokens
            entry.output_tokens += completion_tokens
            # A backend that reports no usage must not blank a good reading.
            if prompt_tokens > 0:
                entry.context_tokens = prompt_tokens + completion_tokens
            while len(self._sessions) > self._max:
                self._sessions.popitem(last=False)

    def read(self, session_ids: list[str]) -> dict[str, dict[str, Any]]:
        with self._lock:
            found = [self._sessions[i] for i in session_ids if i in self._sessions]
        return {entry.session_id: entry.view() for entry in found}

    def reset(self) -> None:
        with self._lock:
            self._sessions.clear()


_ledger = SessionUsageLedger()


def get_session_usage() -> SessionUsageLedger:
    return _ledger


def record_request(trace_context: RequestTraceContext, result: UpstreamResult) -> None:
    """Fold one succeeded request into its session, when the caller named one."""

    session_id = trace_context.extra.get("agent.session_id")
    if isinstance(session_id, str) and session_id:
        _ledger.record(
            session_id,
            trace_context.logical_model,
            prompt_tokens=result.prompt_eval_count,
            completion_tokens=result.eval_count,
        )
