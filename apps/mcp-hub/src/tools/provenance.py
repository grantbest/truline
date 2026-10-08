"""The one place mcp-hub builds a bead's ``provenance`` dict.

``apps/substrate``'s ``BeadProvenance`` (schemas.py) is ``extra="forbid"``
and requires all six keys (``worker``, ``model``, ``prompt_ref``, ``tokens``,
``cost_usd``, ``duration_s``) present, non-nullable except ``tokens``/
``cost_usd``. Every writer under ``src/`` builds its provenance dict through
:func:`build_provenance` instead of hand-assembling one -- see
``tests/test_provenance_ratchet.py``, which refuses any dict literal built
elsewhere.

This module must not import anything under ``apps/substrate`` at runtime --
the six-key contract is proven by ``tests/test_provenance_contract.py``
driving each writer's real POST body through the real schema, not by a
runtime dependency here.
"""

from __future__ import annotations

from typing import Optional

__all__ = ["build_provenance", "prompt_ref_for"]


def build_provenance(
    *,
    worker: str,
    model: Optional[str],
    prompt_ref: str,
    tokens: Optional[int] = None,
    cost_usd: Optional[float] = None,
    duration_s: Optional[float] = None,
) -> dict:
    """Return a ``BeadProvenance``-shaped dict: exactly the six keys, no others.

    ``model`` empty/``None`` becomes the literal ``"none"`` -- a model ran,
    or didn't, is a fact the writer must state, not omit. When ``model`` is
    ``"none"``, ``tokens``/``cost_usd`` default to ``0``/``0.0`` (no model
    ran, so there is nothing to measure); otherwise they default to ``None``
    (a model ran but its usage was not measured) -- a value the caller passes
    is always kept either way. ``duration_s`` defaults to ``0.0``. A negative
    ``tokens``, ``cost_usd``, or ``duration_s`` raises ``ValueError``, as does
    an empty (post-``strip()``) ``worker`` or ``prompt_ref``.
    """
    worker_clean = (worker or "").strip()
    if not worker_clean:
        raise ValueError("build_provenance: worker must be a non-empty string")

    prompt_ref_clean = (prompt_ref or "").strip()
    if not prompt_ref_clean:
        raise ValueError("build_provenance: prompt_ref must be a non-empty string")

    resolved_model = (model or "").strip() or "none"

    if resolved_model == "none":
        tokens = 0 if tokens is None else tokens
        cost_usd = 0.0 if cost_usd is None else cost_usd

    duration = 0.0 if duration_s is None else float(duration_s)

    for field_name, value in (("tokens", tokens), ("cost_usd", cost_usd), ("duration_s", duration)):
        if value is not None and value < 0:
            raise ValueError(f"build_provenance: {field_name} must not be negative")

    return {
        "worker": worker_clean,
        "model": resolved_model,
        "prompt_ref": prompt_ref_clean,
        "tokens": tokens,
        "cost_usd": cost_usd,
        "duration_s": duration,
    }


def prompt_ref_for(prompt_hash: Optional[str], fallback: str) -> str:
    """``prompt-sha256:<hash>`` when a hash was computed, else ``fallback``."""
    if isinstance(prompt_hash, str) and prompt_hash:
        return f"prompt-sha256:{prompt_hash}"
    return fallback
