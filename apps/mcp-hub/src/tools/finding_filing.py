"""Files a `dev.finding` bead by calling ``file_finding.file_finding`` from
the real dispatcher checkout this service ships alongside — never a
re-derived filing path (PRIN-005, see ``.factory/design.md``). Mirrors
``tools.task_filing``'s own shape for ``dev.task`` filing (S55-10a is this
bead's predecessor).

The ``FACTORY_DISPATCHER_ROOT`` lookup and the ``import dispatch``-before-
``import file_finding`` circular-import fix are shared with
``tools.task_filing`` and live in ``tools.dispatcher_loader`` — see that
module's docstring for the full explanation.

``prompt_ref`` travels inside ``spec`` here, not as a separate parameter:
the gateway route accepts the JSON object ``file_finding.py``'s CLI accepts
plus a ``prompt_ref`` key, and :func:`file_dev_finding` pops it back out
before forwarding the rest to ``file_finding.file_finding`` — the same shape
``file_finding.py``'s own ``_validate_and_build_content`` already uses to
keep ``state`` out of the bead's content. ``prompt_ref`` is provenance, never
content, so it must not ride into the bead the way a plain extra key would.

The store is constructed LAZILY, via :class:`_IdCapturingStore`, not eagerly
in this function. ``file_finding.file_finding`` validates ``spec`` before it
ever touches a store (``_validate_and_build_content`` runs before
``sub.create_bead`` — apps/factory-dispatcher/file_finding.py:169-173): a
refused spec must never construct a real ``Substrate()``, the same
refusal-before-write guarantee the CLI gives (PRIN-005 parity). Constructing
eagerly here — ``store if store is not None else file_finding_mod.Substrate()``
before calling ``file_finding.file_finding`` at all — breaks that ordering: a
verification environment with no ``SUBSTRATE_URL``/``SUBSTRATE_API_KEY`` (the
dispatcher's own sandboxed test run, deliberately) would raise a bare
``KeyError`` from ``Substrate.__init__`` before validation ever ran, even for
a spec that was always going to be refused. See ``.factory/design.md`` for
the full incident this ordering fix exists to prevent.
"""

from __future__ import annotations

from typing import Any

from tools.dispatcher_loader import Unavailable, load_dispatcher_module  # noqa: F401

__all__ = ["Unavailable", "FilingRefused", "PartialWrite", "file_dev_finding"]


class FilingRefused(RuntimeError):
    """file_finding.file_finding refused the spec (SystemExit) — a
    business-rule refusal (bad kind/disposition/severity, a non-empty-string
    check failing, a blocking finding with no evidence, no admitted
    disposition state, missing created_by/prompt_ref, etc.), not a transport
    or configuration failure. Carries file_finding's own message text
    unchanged, so the HTTP surface can refuse with the SAME reason CLI
    filing would."""


class PartialWrite(RuntimeError):
    """``file_finding.file_finding`` created the dev.finding bead (``id``)
    but failed to transition it to its disposition state — the
    ``SystemExit(1)`` its own ``transition_state`` except-block raises,
    carrying no reason text beyond exit code ``1``. Collapsing that into
    ``FilingRefused`` would read as an ordinary validation refusal with the
    unhelpful detail ``'1'`` and lose the one fact that matters: a bead WAS
    created and is sitting there pending, findable, needing attention by
    hand — not nothing happened. Carries ``id`` so a caller can report
    exactly that instead, and its own message says so rather than reading
    as the bare, unhelpful ``'1'`` ``str(SystemExit(1))`` would otherwise
    surface as."""

    def __init__(self, id: str, reason: str) -> None:
        self.id = id
        self.reason = reason
        super().__init__(
            f"dev.finding {id} was created and stays pending; it failed to "
            f"reach its disposition state (file_finding.py exit {reason!r})"
        )


def _load_file_finding() -> Any:
    """Import and return the real ``file_finding`` module. See
    ``tools.dispatcher_loader.load_dispatcher_module`` for the load-bearing
    import order this relies on."""
    return load_dispatcher_module("file_finding")


class _IdCapturingStore:
    """Wraps the store ``file_dev_finding`` will use so it is constructed
    lazily — on first method call, never in ``__init__`` — and so a
    successful ``create_bead``'s id survives even if a later call on this
    same store raises. See this module's docstring for why laziness here is
    load-bearing, not an optimisation.

    ``factory`` is a zero-argument callable: either ``lambda: test_double``
    (an injected store — still deferred, so an injected double's own
    construction cost, if any, is paid at the same point a real one would
    be) or ``lambda: file_finding_mod.Substrate()``.
    """

    def __init__(self, factory: Any) -> None:
        self._factory = factory
        self._real: Any = None
        self.captured_id: str | None = None

    def _real_store(self) -> Any:
        if self._real is None:
            self._real = self._factory()
        return self._real

    def create_bead(self, *args: Any, **kwargs: Any) -> Any:
        created = self._real_store().create_bead(*args, **kwargs)
        self.captured_id = created.get("id") if isinstance(created, dict) else None
        return created

    def transition_state(self, *args: Any, **kwargs: Any) -> Any:
        return self._real_store().transition_state(*args, **kwargs)


def file_dev_finding(
    spec: dict[str, Any], created_by: str, *, store: Any | None = None
) -> dict[str, Any]:
    """File ``spec`` as a dev.finding bead over HTTP.

    ``spec`` is the JSON object ``file_finding.py``'s CLI accepts (kind,
    disposition, severity, summary, state, optional evidence/reproduction,
    extras) plus a ``prompt_ref`` key, which is popped out here and passed
    to ``file_finding.file_finding`` as its own keyword rather than left to
    ride into content.

    ``store`` lets tests inject a fake in place of a real ``Substrate()`` —
    mirrors ``tools.task_filing.file_dev_task``'s own ``store`` parameter.
    Even when given, it is wrapped in :class:`_IdCapturingStore` and not
    touched until ``file_finding.file_finding`` actually calls a method on
    it, so the validate-before-construct property holds for injected doubles
    too, not just the real ``Substrate()`` path.

    Raises :class:`FilingRefused` for a validation refusal before any write,
    :class:`PartialWrite` when the bead was created but failed to reach its
    disposition state, and :class:`Unavailable` when the store itself
    couldn't be built or reached (missing credentials, a live failure).
    """
    file_finding_mod = _load_file_finding()
    spec = dict(spec)
    prompt_ref = spec.pop("prompt_ref", None)

    wrapped = _IdCapturingStore(
        lambda: store if store is not None else file_finding_mod.Substrate()
    )
    try:
        filed = file_finding_mod.file_finding(
            spec, created_by, prompt_ref=prompt_ref, sub=wrapped
        )
    except SystemExit as exc:
        reason = str(exc.code) if exc.code is not None else str(exc)
        if wrapped.captured_id is not None:
            raise PartialWrite(wrapped.captured_id, reason) from exc
        raise FilingRefused(reason) from exc
    except (KeyError, RuntimeError) as exc:
        # Store construction (missing SUBSTRATE_URL/SUBSTRATE_API_KEY) or a
        # live store call failed outside file_finding.py's own SystemExit
        # mapping (e.g. create_bead's HTTP call itself) — report as
        # unavailable, never an unhandled traceback.
        raise Unavailable(f"dev.finding store unavailable: {exc}") from exc
    return {
        "id": filed.id,
        "state": filed.state,
        "kind": spec.get("kind"),
        "severity": spec.get("severity"),
    }
