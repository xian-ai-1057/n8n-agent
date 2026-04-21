"""Prompt package for the agent (Implements R2-3).

Files live as raw markdown next to this module; use `loader.load_prompt(name)`
to fetch the plain-text body.
"""

from .loader import load_prompt

__all__ = ["load_prompt"]
