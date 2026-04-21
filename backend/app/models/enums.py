"""Enum constants shared across models (Implements D0-2 §2)."""

from __future__ import annotations

from enum import StrEnum


class StepIntent(StrEnum):
    TRIGGER = "trigger"
    ACTION = "action"
    CONDITION = "condition"
    TRANSFORM = "transform"
    OUTPUT = "output"


class ConnectionType(StrEnum):
    MAIN = "main"
    AI_LANGUAGE_MODEL = "ai_languageModel"
    AI_MEMORY = "ai_memory"
    AI_TOOL = "ai_tool"


class ValidationSeverity(StrEnum):
    ERROR = "error"  # blocks deploy
    WARNING = "warning"  # non-blocking
