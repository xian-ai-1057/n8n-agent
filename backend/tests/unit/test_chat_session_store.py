"""Unit tests for SessionStore (C1-9:CHAT-SESS-01/02/03).

Baseline coverage:
- Happy path: create, get, update, delete lifecycle
- TTL expiry with frozen clock
- max_sessions over-capacity log warn
- Concurrent create thread safety

test-engineer will extend with full scenario matrix per the spec.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from app.chat.session_store import (
    SessionState,
    SessionStore,
    _validate_session_id,
    get_session_store,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _store(**kwargs) -> SessionStore:
    """Return a fresh SessionStore (not the singleton)."""
    return SessionStore(**kwargs)


def _future(store: SessionStore, seconds: float) -> datetime:
    """Return a datetime `seconds` in the future relative to now."""
    return datetime.now(timezone.utc) + timedelta(seconds=seconds)


# ===========================================================================
# CHAT-SESS-01: SessionState structure & lifecycle
# ===========================================================================


class TestCHATSESS01Lifecycle:
    """Happy path + basic lifecycle (CHAT-SESS-01)."""

    def test_chat_sess_01_create_with_uuid(self):
        """store.create() without id → 16-char hex id, pattern-valid."""
        store = _store()
        sess = store.create()

        assert isinstance(sess, SessionState)
        assert len(sess.session_id) == 16
        # Pattern ^[A-Za-z0-9_-]{8,64}$
        import re
        assert re.match(r"^[A-Za-z0-9_-]{8,64}$", sess.session_id)

    def test_chat_sess_01_create_with_explicit_id(self):
        """store.create(id) → session stored under that id."""
        store = _store()
        sess = store.create("valid_id_01")

        assert sess.session_id == "valid_id_01"
        fetched = store.get("valid_id_01")
        assert fetched is not None
        assert fetched.session_id == "valid_id_01"

    def test_chat_sess_01_create_invalid_id_raises(self):
        """store.create('ab') → ValueError (< 8 chars)."""
        store = _store()
        with pytest.raises(ValueError, match="invalid session_id"):
            store.create("ab")

    def test_chat_sess_01_create_invalid_id_special_chars(self):
        """store.create id with forbidden chars → ValueError."""
        store = _store()
        with pytest.raises(ValueError, match="invalid session_id"):
            store.create("abc!@#$%^")

    def test_chat_sess_01_get_returns_none_when_missing(self):
        """get on non-existent id → None, no exception."""
        store = _store()
        result = store.get("nonexistent_id_x")
        assert result is None

    def test_chat_sess_01_update_advances_updated_at(self):
        """update() refreshes updated_at to a time >= before the call."""
        store = _store()
        sess = store.create()
        original_updated_at = sess.updated_at

        # Ensure measurable time passes (at least microsecond-level)
        sess.awaiting_plan_approval = True
        store.update(sess)

        stored = store.get(sess.session_id)
        assert stored is not None
        assert stored.updated_at >= original_updated_at

    def test_chat_sess_01_delete_removes_session(self):
        """delete() removes a session; subsequent get returns None."""
        store = _store()
        sess = store.create()
        sid = sess.session_id

        store.delete(sid)
        assert store.get(sid) is None

    def test_chat_sess_01_delete_idempotent(self):
        """delete() twice on same id does not raise."""
        store = _store()
        sess = store.create()
        sid = sess.session_id

        store.delete(sid)
        store.delete(sid)  # should not raise

    def test_chat_sess_01_session_fields_defaults(self):
        """Newly created session has expected default field values."""
        store = _store()
        sess = store.create()

        assert sess.history == []
        assert sess.awaiting_plan_approval is False
        assert sess.pending_plan_summary is None
        assert sess.created_at.tzinfo is not None
        assert sess.updated_at.tzinfo is not None

    def test_chat_sess_01_singleton_get_session_store(self):
        """get_session_store() returns the same object on repeated calls."""
        a = get_session_store()
        b = get_session_store()
        assert a is b

    def test_validate_session_id_valid(self):
        """_validate_session_id passes for valid ids."""
        _validate_session_id("abc12345")           # min 8 chars
        _validate_session_id("a" * 64)             # max 64 chars
        _validate_session_id("valid-id_ABC123")    # mixed case + separators

    def test_validate_session_id_too_short(self):
        with pytest.raises(ValueError):
            _validate_session_id("abc")

    def test_validate_session_id_too_long(self):
        with pytest.raises(ValueError):
            _validate_session_id("a" * 65)

    def test_validate_session_id_path_traversal(self):
        """../../etc/passwd must be rejected."""
        with pytest.raises(ValueError):
            _validate_session_id("../../etc/passwd")


# ===========================================================================
# CHAT-SESS-02: TTL / GC / max_sessions
# ===========================================================================


class TestCHATSESS02TTLAndCapacity:
    """TTL expiry, lazy GC throttle, over-capacity warn (CHAT-SESS-02)."""

    def test_chat_sess_02_ttl_expiry_session_gone(self):
        """Session expired by TTL is removed on next get after GC trigger."""
        store = _store(ttl_s=1800.0, gc_interval_s=0.0)  # gc always fires
        sess = store.create()
        sid = sess.session_id

        # Simulate clock 1801 seconds in the future
        future_now = _future(store, 1801)
        store.gc_expired(now=future_now)

        assert store.get(sid) is None

    def test_chat_sess_02_active_session_not_gced(self):
        """Session updated within TTL is NOT removed by gc."""
        store = _store(ttl_s=1800.0, gc_interval_s=0.0)
        sess = store.create()
        sid = sess.session_id

        # Update session to reset its updated_at
        store.update(sess)

        # GC 900 seconds later — within TTL
        future_now = _future(store, 900)
        deleted = store.gc_expired(now=future_now)

        assert deleted == 0
        assert store.get(sid) is not None

    def test_chat_sess_02_gc_count_returned(self):
        """gc_expired() returns the count of deleted sessions."""
        store = _store(ttl_s=1800.0, gc_interval_s=0.0)
        ids = [store.create().session_id for _ in range(3)]

        future_now = _future(store, 1801)
        deleted = store.gc_expired(now=future_now)

        assert deleted == 3
        for sid in ids:
            assert store.get(sid) is None

    def test_chat_sess_02_lazy_gc_not_retriggered_too_soon(self):
        """_maybe_gc does not call gc_expired twice within gc_interval_s."""
        store = _store(ttl_s=1800.0, gc_interval_s=60.0)
        sess = store.create()

        # First GC fires (gc_interval_s=60, store was just created so
        # _last_gc_at is ~ now; force it to be old so first call fires)
        store._last_gc_at = datetime.now(timezone.utc) - timedelta(seconds=61)

        with patch.object(store, "gc_expired", wraps=store.gc_expired) as mock_gc:
            store.get(sess.session_id)   # fires GC
            store.get(sess.session_id)   # should NOT re-fire (< 60s since last)

        # gc_expired called exactly once
        assert mock_gc.call_count == 1

    def test_chat_sess_02_over_capacity_logs_warn(self, caplog):
        """Building 501st session logs a warning but still creates it."""
        import logging

        store = _store(max_sessions=500, gc_interval_s=9999.0)

        # Create exactly 500 sessions (skip GC by using a large gc_interval)
        for i in range(500):
            store.create(f"sess_{i:04d}_ab")  # 12 chars, pattern valid

        with caplog.at_level(logging.WARNING, logger="app.chat.session_store"):
            extra_sess = store.create()  # 501st

        # Session was still created
        assert store.get(extra_sess.session_id) is not None
        # Warning was emitted
        assert any("over_capacity" in r.message for r in caplog.records)

    def test_chat_sess_02_len_reflects_current_count(self):
        """__len__ returns accurate live session count."""
        store = _store()
        assert len(store) == 0
        store.create()
        store.create()
        assert len(store) == 2


# ===========================================================================
# CHAT-SESS-03: Thread safety (RLock)
# ===========================================================================


class TestCHATSESS03ThreadSafety:
    """Concurrent create / get without races (CHAT-SESS-03)."""

    def test_chat_sess_03_concurrent_create_100_unique_ids(self):
        """50 threads each create 2 sessions → 100 unique ids, no errors."""
        store = _store()
        created_ids: list[str] = []
        errors: list[Exception] = []
        lock = threading.Lock()

        def worker():
            try:
                for _ in range(2):
                    sess = store.create()
                    with lock:
                        created_ids.append(sess.session_id)
            except Exception as exc:
                with lock:
                    errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)

        assert errors == [], f"Errors during concurrent create: {errors}"
        assert len(created_ids) == 100
        assert len(set(created_ids)) == 100, "Duplicate session ids detected"

    def test_chat_sess_03_concurrent_get_during_create(self):
        """Mixed concurrent reads and writes complete without deadlock."""
        store = _store()
        # Pre-populate some sessions
        sids = [store.create().session_id for _ in range(10)]

        errors: list[Exception] = []
        lock = threading.Lock()

        def reader():
            try:
                for sid in sids:
                    store.get(sid)
            except Exception as exc:
                with lock:
                    errors.append(exc)

        def writer():
            try:
                store.create()
            except Exception as exc:
                with lock:
                    errors.append(exc)

        threads = [
            threading.Thread(target=reader if i % 2 == 0 else writer)
            for i in range(20)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)

        assert errors == [], f"Errors during concurrent get/create: {errors}"
