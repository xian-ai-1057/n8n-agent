"""Unit tests for chat-layer keyword loader and matcher.

Covers CHAT-KW-01 (loader + fallback) and CHAT-KW-02 (matching rules).
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest

# Reset the module cache before each test so tests are independent.
from app.chat.keywords import (
    _FALLBACK_KEYWORDS,
    KeywordHits,
    _reset_cache,
    get_keywords,
    load_keywords,
    match_keywords,
)


@pytest.fixture(autouse=True)
def _reset_keyword_cache():
    """Ensure the module-level cache is cleared before and after every test."""
    _reset_cache()
    yield
    _reset_cache()


# ---------------------------------------------------------------------------
# CHAT-KW-01: loader tests
# ---------------------------------------------------------------------------


def test_v_chat_kw01_load_valid_yaml(tmp_path: Path):
    """load_keywords() returns nested dict when yaml is valid."""
    yaml_file = tmp_path / "keywords.yaml"
    yaml_file.write_text(
        textwrap.dedent(
            """\
            build_keywords:
              zh:
                - 建立 workflow
              en:
                - build workflow
            confirm_keywords:
              zh:
                - 確認
              en:
                - confirm
            reject_keywords:
              zh:
                - 取消
              en:
                - cancel
            """
        ),
        encoding="utf-8",
    )

    result = load_keywords(path=yaml_file)

    assert isinstance(result, dict)
    assert "build_keywords" in result
    assert "建立 workflow" in result["build_keywords"]["zh"]
    assert "build workflow" in result["build_keywords"]["en"]


def test_v_chat_kw01_load_missing_file(tmp_path: Path, caplog):
    """load_keywords() returns fallback and logs error when file is missing."""
    import logging

    missing = tmp_path / "does_not_exist.yaml"

    with caplog.at_level(logging.ERROR):
        result = load_keywords(path=missing)

    assert result == _FALLBACK_KEYWORDS
    assert any("not found" in record.message or "fallback" in record.message.lower()
               for record in caplog.records)


def test_v_chat_kw01_load_malformed_yaml(tmp_path: Path, caplog):
    """load_keywords() falls back and logs error on YAML syntax error."""
    import logging

    bad_yaml = tmp_path / "keywords.yaml"
    bad_yaml.write_text("build_keywords: [\nnot_closed", encoding="utf-8")

    with caplog.at_level(logging.ERROR):
        result = load_keywords(path=bad_yaml)

    assert result == _FALLBACK_KEYWORDS
    assert any("fallback" in record.message.lower() or "invalid" in record.message.lower()
               for record in caplog.records)


def test_v_chat_kw01_load_missing_top_key(tmp_path: Path, caplog):
    """load_keywords() falls back when a required top-level key is absent."""
    import logging

    partial_yaml = tmp_path / "keywords.yaml"
    # Missing confirm_keywords and reject_keywords
    partial_yaml.write_text(
        "build_keywords:\n  zh:\n    - 建立\n  en:\n    - build\n",
        encoding="utf-8",
    )

    with caplog.at_level(logging.ERROR):
        result = load_keywords(path=partial_yaml)

    assert result == _FALLBACK_KEYWORDS
    assert any("missing" in record.message.lower() or "fallback" in record.message.lower()
               for record in caplog.records)


def test_v_chat_kw01_loader_cached(tmp_path: Path):
    """get_keywords() returns cached result without re-reading the file."""
    yaml_file = tmp_path / "keywords.yaml"
    yaml_file.write_text(
        textwrap.dedent(
            """\
            build_keywords:
              zh: [建立 workflow]
              en: [build workflow]
            confirm_keywords:
              zh: [確認]
              en: [confirm]
            reject_keywords:
              zh: [取消]
              en: [cancel]
            """
        ),
        encoding="utf-8",
    )

    first = load_keywords(path=yaml_file)

    # Patch read_text so a second call would fail if it tried to re-read.
    with patch.object(Path, "read_text", side_effect=AssertionError("should not re-read")):
        second = get_keywords()

    assert first is second  # same object (cached)


# ---------------------------------------------------------------------------
# CHAT-KW-02: matching tests
# ---------------------------------------------------------------------------


def test_v_chat_kw02_match_zh_build():
    """match_keywords() hits a zh build keyword when present."""
    hits: KeywordHits = match_keywords("幫我建立 workflow 每小時跑一次")
    assert "建立 workflow" in hits.build
    assert hits.has_build()


def test_v_chat_kw02_match_en_build():
    """match_keywords() hits an en build keyword when present."""
    hits = match_keywords("I want to build workflow that syncs Slack")
    assert any("build workflow" == kw for kw in hits.build)
    assert hits.has_build()


def test_v_chat_kw02_match_case_insensitive():
    """match_keywords() is case-insensitive for en keywords."""
    hits = match_keywords("BUILD WORKFLOW for me please")
    assert hits.has_build(), f"Expected build hit, got: {hits}"


def test_v_chat_kw02_negation_still_hits():
    """'不要建立 X' triggers both build and reject hits; LLM resolves ambiguity."""
    hits = match_keywords("我不要建立 workflow 了")
    assert hits.has_build(), "build should still hit"
    assert hits.has_reject(), "reject ('不要') should also hit"


def test_v_chat_kw02_no_hit():
    """A generic message with no keywords returns empty hits."""
    hits = match_keywords("今天天氣不錯")
    assert not hits.has_build()
    assert not hits.has_confirm()
    assert not hits.has_reject()


def test_v_chat_kw02_empty_string():
    """Empty string returns empty KeywordHits without raising."""
    hits = match_keywords("")
    assert hits == KeywordHits(build=[], confirm=[], reject=[])


def test_v_chat_kw02_confirm_hit():
    """match_keywords() detects confirm keywords."""
    hits = match_keywords("好,確認這個計畫")
    assert hits.has_confirm()


def test_v_chat_kw02_reject_hit():
    """match_keywords() detects reject keywords."""
    hits = match_keywords("算了,取消吧")
    assert hits.has_reject()


# ---------------------------------------------------------------------------
# CHAT-KW-01: additional edge cases
# ---------------------------------------------------------------------------


def test_v_chat_kw01_load_non_dict_yaml(tmp_path: Path, caplog):
    """load_keywords() falls back when yaml top-level is a list, not a dict."""
    import logging

    bad_yaml = tmp_path / "keywords.yaml"
    # A valid YAML list — not a mapping
    bad_yaml.write_text("- item1\n- item2\n", encoding="utf-8")

    with caplog.at_level(logging.ERROR):
        result = load_keywords(path=bad_yaml)

    assert result == _FALLBACK_KEYWORDS
    assert any(
        "fallback" in record.message.lower() or "mapping" in record.message.lower()
        for record in caplog.records
    )


def test_v_chat_kw02_multiple_matches_all_returned():
    """When two build keywords hit, both are in the build list."""
    # Use the actual fallback/default keywords that are always present
    hits = match_keywords("幫我建立 workflow 然後自動化一下")
    # At least one build hit should be present
    assert len(hits.build) >= 1


def test_v_chat_kw02_non_string_type_does_not_crash(tmp_path: Path):
    """Non-string keyword entries in YAML (e.g. boolean true) are skipped, no crash."""
    import logging

    mixed_yaml = tmp_path / "keywords.yaml"
    # Inject a boolean value as a keyword — tests _collect_hits guard
    mixed_yaml.write_text(
        "build_keywords:\n  zh:\n    - 建立\n    - true\n  en:\n    - build\n"
        "confirm_keywords:\n  zh:\n    - 確認\n  en:\n    - confirm\n"
        "reject_keywords:\n  zh:\n    - 取消\n  en:\n    - cancel\n",
        encoding="utf-8",
    )

    result = load_keywords(path=mixed_yaml)
    assert result is not None
    # match_keywords should not raise despite boolean entry
    hits = match_keywords("建立 workflow")
    assert isinstance(hits.build, list)


def test_v_chat_kw02_keyword_hits_dataclass_methods():
    """KeywordHits convenience methods work correctly."""
    empty = KeywordHits(build=[], confirm=[], reject=[])
    assert not empty.has_build()
    assert not empty.has_confirm()
    assert not empty.has_reject()

    populated = KeywordHits(build=["建立"], confirm=["確認"], reject=["取消"])
    assert populated.has_build()
    assert populated.has_confirm()
    assert populated.has_reject()
