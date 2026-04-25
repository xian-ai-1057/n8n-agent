"""Keyword loader and matcher for the chat-layer dispatcher.

Implements:
  - CHAT-KW-01: keywords.yaml schema + loader + fallback
  - CHAT-KW-02: match_keywords substring + case-insensitive

The YAML file lives next to this module at ``keywords.yaml``.
On startup the loader reads and caches the file once.  If the file is
missing or malformed it emits a log error and falls back to the builtin
fallback list so the process never crashes on import.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Default path: keywords.yaml sits in the same directory as this module.
# ---------------------------------------------------------------------------
_DEFAULT_YAML_PATH: Path = Path(__file__).with_name("keywords.yaml")

# ---------------------------------------------------------------------------
# Module-level cache.  None = not yet loaded.
# ---------------------------------------------------------------------------
_KEYWORDS: dict[str, dict[str, list[str]]] | None = None

# ---------------------------------------------------------------------------
# Hardcoded fallback (used when YAML is missing or malformed).
# Contains at least 1-2 entries per category per language so the dispatcher
# can always produce non-empty hints.
# ---------------------------------------------------------------------------
_FALLBACK_KEYWORDS: dict[str, dict[str, list[str]]] = {
    "build_keywords": {
        "zh": ["建立 workflow", "幫我做", "自動化", "n8n", "部署"],
        "en": ["build workflow", "automate", "set up", "deploy"],
    },
    "confirm_keywords": {
        "zh": ["確認", "好"],
        "en": ["confirm", "yes"],
    },
    "reject_keywords": {
        "zh": ["不要", "取消"],
        "en": ["cancel", "abort"],
    },
}

# Required top-level keys that a valid keywords.yaml must contain.
_REQUIRED_KEYS: frozenset[str] = frozenset(
    {"build_keywords", "confirm_keywords", "reject_keywords"}
)


# C1-9:CHAT-KW-01
def load_keywords(path: Path | None = None) -> dict[str, dict[str, list[str]]]:
    """Load keywords.yaml; cache result; fall back to builtin on any error.

    Parameters
    ----------
    path:
        Explicit path to a keywords YAML file.  Defaults to the
        ``keywords.yaml`` file co-located with this module.

    Returns
    -------
    dict
        Nested mapping ``{category: {lang: [keyword, ...]}}`` where
        *category* is one of ``build_keywords``, ``confirm_keywords``,
        ``reject_keywords``.
    """
    global _KEYWORDS  # noqa: PLW0603

    if _KEYWORDS is not None:
        return _KEYWORDS

    resolved = path or _DEFAULT_YAML_PATH

    try:
        raw_text = resolved.read_text(encoding="utf-8")
    except FileNotFoundError:
        logger.error(
            "keywords.yaml not found at %s — falling back to builtin keyword list",
            resolved,
        )
        _KEYWORDS = _FALLBACK_KEYWORDS
        return _KEYWORDS
    except OSError as exc:
        logger.error(
            "Failed to read keywords.yaml at %s (%s) — falling back to builtin",
            resolved,
            exc,
        )
        _KEYWORDS = _FALLBACK_KEYWORDS
        return _KEYWORDS

    try:
        data = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        logger.error(
            "keywords.yaml at %s contains invalid YAML (%s) — falling back to builtin",
            resolved,
            exc,
        )
        _KEYWORDS = _FALLBACK_KEYWORDS
        return _KEYWORDS

    if not isinstance(data, dict):
        logger.error(
            "keywords.yaml at %s must be a YAML mapping (got %s) — falling back",
            resolved,
            type(data).__name__,
        )
        _KEYWORDS = _FALLBACK_KEYWORDS
        return _KEYWORDS

    missing = _REQUIRED_KEYS - data.keys()
    if missing:
        logger.error(
            "keywords.yaml at %s is missing required top-level keys %s — falling back",
            resolved,
            missing,
        )
        _KEYWORDS = _FALLBACK_KEYWORDS
        return _KEYWORDS

    _KEYWORDS = data
    return _KEYWORDS


# C1-9:CHAT-KW-01
def get_keywords() -> dict[str, dict[str, list[str]]]:
    """Return the cached keyword dict, loading it on first call."""
    return load_keywords()


def _reset_cache() -> None:
    """Reset the module-level keyword cache.  For testing only."""
    global _KEYWORDS  # noqa: PLW0603
    _KEYWORDS = None


# ---------------------------------------------------------------------------
# KeywordHits dataclass and match_keywords function
# ---------------------------------------------------------------------------


# C1-9:CHAT-KW-02
@dataclass
class KeywordHits:
    """Container for the keywords that matched a user message.

    Each attribute holds the **matched keyword strings** (not the message
    fragments) so the dispatcher can embed them verbatim in the system-prompt
    hint.

    The class is intentionally lightweight — callers check ``has_build()`` /
    ``has_confirm()`` / ``has_reject()`` and embed ``hits`` into prompt text.
    """

    build: list[str]
    confirm: list[str]
    reject: list[str]

    def has_build(self) -> bool:
        """Return True when at least one build keyword was hit."""
        return bool(self.build)

    def has_confirm(self) -> bool:
        """Return True when at least one confirm keyword was hit."""
        return bool(self.confirm)

    def has_reject(self) -> bool:
        """Return True when at least one reject keyword was hit."""
        return bool(self.reject)


# C1-9:CHAT-KW-02
def match_keywords(text: str) -> KeywordHits:
    """Substring + case-insensitive match across zh+en keyword lists.

    The function never raises.  On an empty or non-string input it returns
    an empty ``KeywordHits``.

    Important design note
    ---------------------
    A hit does **not** trigger a tool call directly.  The result is injected
    as a soft hint into the chat LLM's system prompt (see CHAT-DISP-02).
    Negation handling (e.g. "不要建立 workflow") is intentionally left to the
    LLM — the matcher will report both a build hit and a reject hit and the
    LLM decides what to do.

    Parameters
    ----------
    text:
        The raw user message string.

    Returns
    -------
    KeywordHits
        Each list contains the matched keyword(s) in their original form
        (from the YAML list), **not** the lowercased version.
    """
    if not isinstance(text, str) or not text:
        return KeywordHits(build=[], confirm=[], reject=[])

    lower_text = text.lower()
    kw = get_keywords()

    def _collect_hits(category: str) -> list[str]:
        hits: list[str] = []
        lang_map: dict[str, list[str]] = kw.get(category, {})
        for _lang, keywords in lang_map.items():
            for kw_entry in keywords:
                if not isinstance(kw_entry, str):
                    # Guard against YAML booleans/nulls leaking through
                    logger.warning(
                        "Skipping non-string keyword entry %r in category %s",
                        kw_entry,
                        category,
                    )
                    continue
                if kw_entry.lower() in lower_text:
                    hits.append(kw_entry)
        return hits

    return KeywordHits(
        build=_collect_hits("build_keywords"),
        confirm=_collect_hits("confirm_keywords"),
        reject=_collect_hits("reject_keywords"),
    )
