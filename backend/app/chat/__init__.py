"""Chat layer public API re-exports.

Exports the session store singleton factory and the SessionState dataclass
so callers can do:
    from app.chat import get_session_store, SessionState
"""

# C1-9:CHAT-SESS-01
from app.chat.session_store import SessionState, SessionStore, get_session_store

__all__ = [
    "SessionState",
    "SessionStore",
    "get_session_store",
]
