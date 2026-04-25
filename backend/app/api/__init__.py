"""API routers (Implements C1-5)."""

# C1-5:HITL-SHIP-01 — export in-process callable for C1-9 chat tool injection.
from .routes import _do_confirm_plan as do_confirm_plan  # noqa: F401
