"""Unit tests for chat-layer Settings fields (C1-9:CHAT-CFG-01).

Verifies default values and env-override behaviour for the five new
CHAT_* environment variables added to backend/app/config.py.
"""

from __future__ import annotations

import pytest

from app.config import Settings


# ---------------------------------------------------------------------------
# Default value tests
# ---------------------------------------------------------------------------


def test_chat_cfg01_chat_model_defaults_to_none() -> None:
    """chat_model raw field is None when env not set."""
    s = Settings()
    assert s.chat_model is None


def test_chat_cfg01_chat_model_defaults_to_planner() -> None:
    """effective_chat_model falls back to planner_model (or llm_model)."""
    # planner_model defaults to "" which triggers fallback to llm_model
    s = Settings()
    assert s.effective_chat_model == s.llm_model


def test_chat_cfg01_chat_temperature_default() -> None:
    """chat_temperature default is 0.3."""
    s = Settings()
    assert s.chat_temperature == pytest.approx(0.3)


def test_chat_cfg01_chat_max_history_default() -> None:
    """chat_max_history default is 40."""
    s = Settings()
    assert s.chat_max_history == 40


def test_chat_cfg01_chat_session_ttl_s_default() -> None:
    """chat_session_ttl_s default is 1800."""
    s = Settings()
    assert s.chat_session_ttl_s == 1800


def test_chat_cfg01_chat_max_sessions_default() -> None:
    """chat_max_sessions default is 500."""
    s = Settings()
    assert s.chat_max_sessions == 500


# ---------------------------------------------------------------------------
# Override tests (construct Settings directly with keyword args)
# ---------------------------------------------------------------------------


def test_chat_cfg01_chat_model_override() -> None:
    """When chat_model is set, effective_chat_model uses it."""
    s = Settings(chat_model="gpt-4o-mini")
    assert s.effective_chat_model == "gpt-4o-mini"


def test_chat_cfg01_effective_chat_model_prefers_chat_model_over_planner() -> None:
    """chat_model takes priority over planner_model."""
    s = Settings(chat_model="gpt-4o-mini", planner_model="gpt-4o")
    assert s.effective_chat_model == "gpt-4o-mini"


def test_chat_cfg01_effective_chat_model_falls_back_to_planner_when_chat_unset() -> None:
    """When chat_model is None, effective_chat_model uses planner_model."""
    s = Settings(chat_model=None, planner_model="gpt-4o")
    assert s.effective_chat_model == "gpt-4o"


def test_chat_cfg01_chat_temperature_override() -> None:
    """chat_temperature accepts custom float."""
    s = Settings(chat_temperature=0.7)
    assert s.chat_temperature == pytest.approx(0.7)


def test_chat_cfg01_chat_max_history_int_coercion() -> None:
    """chat_max_history stores an int (Pydantic coerces "20" → 20)."""
    s = Settings(chat_max_history=20)
    assert isinstance(s.chat_max_history, int)
    assert s.chat_max_history == 20


def test_chat_cfg01_chat_session_ttl_int_coercion() -> None:
    """chat_session_ttl_s stores an int."""
    s = Settings(chat_session_ttl_s=60)
    assert isinstance(s.chat_session_ttl_s, int)
    assert s.chat_session_ttl_s == 60


def test_chat_cfg01_chat_max_sessions_override() -> None:
    """chat_max_sessions accepts an overridden value."""
    s = Settings(chat_max_sessions=200)
    assert s.chat_max_sessions == 200
