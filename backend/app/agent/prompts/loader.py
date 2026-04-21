"""Prompt file loader (Implements R2-3).

Prompts are stored as verbatim .md next to this module. We deliberately avoid
`str.format` (the fix/builder few-shots contain literal `{}` that would
otherwise require escaping) — callers render via `render_prompt` which does a
plain `str.replace` per placeholder.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

_PROMPTS_DIR = Path(__file__).resolve().parent


def load_prompt(name: str) -> str:
    """Read `<name>.md` next to this module and return its raw body.

    Accepts either `"planner"` or `"planner.md"`.
    """
    stem = name[:-3] if name.endswith(".md") else name
    path = _PROMPTS_DIR / f"{stem}.md"
    if not path.is_file():
        raise FileNotFoundError(f"prompt file not found: {path}")
    return path.read_text(encoding="utf-8")


def render_prompt(name: str, variables: dict[str, Any]) -> str:
    """Load a prompt and substitute `{var}` placeholders.

    Uses `str.replace`, NOT `str.format`, so literal `{}` in few-shot blocks
    are preserved as-is.
    """
    text = load_prompt(name)
    for key, value in variables.items():
        text = text.replace("{" + key + "}", str(value))
    return text
