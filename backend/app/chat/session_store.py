"""In-memory chat session store.

Implements CHAT-SESS-01/02/03 from C1-9 spec:
- SessionState dataclass (CHAT-SESS-01)
- TTL=1800s lazy GC, max_sessions=500 over-cap log warn (CHAT-SESS-02)
- threading.RLock thread safety (CHAT-SESS-03)

TODO(CHAT-CFG-01): Once A-5 lands, import TTL and max_sessions from
    settings (get_settings().chat_session_ttl_s / chat_max_sessions).
    Currently uses hardcoded defaults that match the spec.
"""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from ..config import get_settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# session_id validation pattern (C1-5 / CHAT-SEC-01)
# ---------------------------------------------------------------------------

# C1-9:CHAT-SEC-01
# P1-6: canonical session_id pattern — imported by routes.py and models/api.py
# to avoid pattern drift across three different locations.
SESSION_ID_PATTERN: str = r"^[A-Za-z0-9_-]{8,64}$"
_SESSION_ID_RE = re.compile(SESSION_ID_PATTERN)


def _validate_session_id(sid: str) -> None:
    """Raise ValueError if sid does not match the allowed pattern.

    Pattern: ^[A-Za-z0-9_-]{8,64}$  (same as C1-5 ChatRequest).
    """
    if not _SESSION_ID_RE.match(sid):
        raise ValueError(f"invalid session_id: {sid!r}")


# ---------------------------------------------------------------------------
# SessionState
# ---------------------------------------------------------------------------

# C1-9:CHAT-SESS-01
@dataclass
class SessionState:
    """Per-session chat state shared by the chat dispatcher and LangGraph.

    ``session_id`` is used both as the chat history key and as the
    LangGraph MemorySaver ``thread_id``; the two ends MUST share the same id
    so HITL resume can find the graph state (CHAT-SESS-01 rationale).

    Fields
    ------
    session_id:
        Unique identifier, pattern ``^[A-Za-z0-9_-]{8,64}$``.
    history:
        Ordered list of chat messages.  Each entry is a dict with at least
        ``{"role": "user"|"assistant"|"tool", "content": str}`` and an
        optional ``"tool_call_id"`` for tool messages.
    created_at:
        UTC timestamp of session creation (immutable after creation).
    updated_at:
        UTC timestamp of the most recent ``update()`` call.  Used by the
        TTL GC to decide whether a session has expired.
    awaiting_plan_approval:
        True while the graph is paused at ``await_plan_approval`` (Gate-2
        HITL).  The dispatcher uses this flag to inject the plan-pending
        block into the system prompt.
    pending_plan_summary:
        Cached plan text set when Gate-2 fires; cleared on confirm/reject.
        Lets the chat LLM show the plan without re-invoking the graph.
    """

    session_id: str
    # P2-13: use Any for values because tool_calls is list and content may be str|list
    history: list[dict[str, Any]] = field(default_factory=list)
    created_at: datetime = field(
        default_factory=lambda: datetime.now(UTC)
    )
    updated_at: datetime = field(
        default_factory=lambda: datetime.now(UTC)
    )
    awaiting_plan_approval: bool = False
    pending_plan_summary: str | None = None


# ---------------------------------------------------------------------------
# SessionStore
# ---------------------------------------------------------------------------

# P1-7: fallback defaults; the factory function (get_session_store) reads
# actual values from Settings so CHAT_SESSION_TTL_S / CHAT_MAX_SESSIONS env
# vars take effect at runtime. Module-level defaults kept as documentation only.
_DEFAULT_TTL_S: float = 1800.0
_DEFAULT_MAX_SESSIONS: int = 500
_DEFAULT_GC_INTERVAL_S: float = 60.0


# C1-9:CHAT-SESS-01
# C1-9:CHAT-SESS-02
# C1-9:CHAT-SESS-03
class SessionStore:
    """Process-local in-memory chat session store.

    Thread-safe via a single ``threading.RLock`` (CHAT-SESS-03).  The RLock
    (re-entrant) is required because ``_maybe_gc`` acquires the lock and then
    calls ``gc_expired``, which also acquires the same lock.

    Lazy GC (CHAT-SESS-02): expired sessions are purged when ``get`` or
    ``create`` is called and at least ``_gc_interval_s`` seconds have passed
    since the last GC pass.  No background thread is started.

    Over-capacity (CHAT-SESS-02): when ``len(store) >= max_sessions`` and
    ``create`` is called, a WARNING is logged but the session is still created
    (MVP decision — see spec rationale).
    """

    # C1-9:CHAT-SESS-03
    def __init__(
        self,
        *,
        ttl_s: float = _DEFAULT_TTL_S,
        max_sessions: int = _DEFAULT_MAX_SESSIONS,
        gc_interval_s: float = _DEFAULT_GC_INTERVAL_S,
    ) -> None:
        self._sessions: dict[str, SessionState] = {}
        self._lock = threading.RLock()
        self._last_gc_at: datetime = datetime.now(UTC)
        self._ttl_s = ttl_s
        self._max_sessions = max_sessions
        self._gc_interval_s = gc_interval_s

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    # C1-9:CHAT-SESS-02
    def _maybe_gc(self) -> None:
        """Run gc_expired if the GC interval has elapsed since last run."""
        now = datetime.now(UTC)
        elapsed = (now - self._last_gc_at).total_seconds()
        if elapsed >= self._gc_interval_s:
            self.gc_expired(now=now)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    # C1-9:CHAT-SESS-01
    def create(self, session_id: str | None = None) -> SessionState:
        """Create a new session and return it.

        Parameters
        ----------
        session_id:
            If ``None``, a 16-character hex id (``uuid4().hex[:16]``) is
            generated automatically.  Otherwise, the supplied id is validated
            against ``^[A-Za-z0-9_-]{8,64}$`` and a ``ValueError`` is raised
            if it does not match.

        Notes
        -----
        If the store is at or above ``max_sessions`` capacity a WARNING is
        logged, but the session is still created (CHAT-SESS-02).
        """
        with self._lock:
            self._maybe_gc()

            if session_id is None:
                session_id = uuid4().hex[:16]
            else:
                _validate_session_id(session_id)

            # C1-9:CHAT-SESS-02
            current_count = len(self._sessions)
            if current_count >= self._max_sessions:
                logger.warning(
                    "session_store_over_capacity",
                    extra={
                        "event": "session_store_over_capacity",
                        "count": current_count,
                        "max_sessions": self._max_sessions,
                    },
                )

            session = SessionState(session_id=session_id)
            self._sessions[session_id] = session
            return session

    # C1-9:CHAT-SESS-01
    def get(self, session_id: str) -> SessionState | None:
        """Return the session for ``session_id``, or ``None`` if not found.

        Triggers lazy GC before the lookup.  Does NOT raise ``KeyError``;
        callers are responsible for handling a ``None`` return.
        """
        with self._lock:
            self._maybe_gc()
            return self._sessions.get(session_id)

    # C1-9:CHAT-SESS-01
    def update(self, session: SessionState) -> None:
        """Persist an updated session back to the store.

        Advances ``session.updated_at`` to the current UTC time so that the
        TTL GC resets for this session.
        """
        with self._lock:
            session.updated_at = datetime.now(UTC)
            self._sessions[session.session_id] = session

    # C1-9:CHAT-SESS-01
    def delete(self, session_id: str) -> None:
        """Remove a session by id.  Idempotent — does not raise if missing."""
        with self._lock:
            self._sessions.pop(session_id, None)

    # C1-9:CHAT-SESS-02
    def gc_expired(self, *, now: datetime | None = None) -> int:
        """Delete sessions whose ``updated_at`` is older than the TTL.

        Parameters
        ----------
        now:
            Reference timestamp; defaults to ``datetime.now(timezone.utc)``.
            Pass a fixed value in tests to simulate time passing without
            actually sleeping.

        Returns
        -------
        int
            Number of sessions deleted.
        """
        with self._lock:
            if now is None:
                now = datetime.now(UTC)
            self._last_gc_at = now

            expired_ids = [
                sid
                for sid, sess in self._sessions.items()
                if (now - sess.updated_at).total_seconds() > self._ttl_s
            ]
            for sid in expired_ids:
                del self._sessions[sid]

            if expired_ids:
                logger.debug(
                    "session_store_gc",
                    extra={
                        "event": "session_store_gc",
                        "deleted_count": len(expired_ids),
                    },
                )
            return len(expired_ids)

    def __len__(self) -> int:
        """Return the current number of live sessions."""
        with self._lock:
            return len(self._sessions)


# ---------------------------------------------------------------------------
# Process-local singleton
# ---------------------------------------------------------------------------

_store_instance: SessionStore | None = None
_store_lock = threading.Lock()


# C1-9:CHAT-SESS-01
# C1-9:CHAT-CFG-01
def get_session_store() -> SessionStore:
    """Return the process-local ``SessionStore`` singleton.

    Lazy-initialised on first call.  Thread-safe via a module-level lock.
    TTL and max_sessions are read from Settings (CHAT-CFG-01) so the env
    vars CHAT_SESSION_TTL_S and CHAT_MAX_SESSIONS are honoured at runtime.
    """
    global _store_instance
    if _store_instance is None:
        with _store_lock:
            if _store_instance is None:
                cfg = get_settings()
                _store_instance = SessionStore(
                    ttl_s=float(cfg.chat_session_ttl_s),
                    max_sessions=cfg.chat_max_sessions,
                )
    return _store_instance
