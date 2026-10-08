"""Tests for ``scripts/arch-source-class-backfill.py``.

The real substrate is deliberately not involved — no database, no cluster, no
network. The fake substrate double stands in for the HTTP client, but its
``patch`` validates the payload through the *real*
``apps/substrate/src/schemas.py`` content schemas (imported, not copied) —
the ``FakeSubstrate.add_note`` lesson (CLAUDE.md operating rules): a double
that accepts more than the live contract is a second implementation.
"""

from __future__ import annotations

import copy
import importlib.util
import pathlib
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]

sys.path.insert(0, str(REPO / "apps" / "substrate" / "src"))
from schemas import validate_bead_content  # noqa: E402


def _load():
    spec = importlib.util.spec_from_file_location(
        "arch_source_class_backfill", REPO / "scripts" / "arch-source-class-backfill.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


backfill = _load()


def _capability(ref: str, **overrides) -> dict:
    content = {
        "ref": ref,
        "name": "Account Visibility",
        "description": "A current view of every account balance.",
        "layer": "demand",
        "owner": "grant",
        "evidence": ["docs/reference/mcp-finance.md"],
        "assessed_at": "2026-07-30",
        "maturity": "operating",
    }
    content.update(overrides)
    return content


class ValidatingFakeSubstrate:
    """Rejects a patch payload the way the live ``arch`` schema does.

    ``validate_bead_content`` is imported from apps/substrate/src/schemas.py
    rather than re-implemented, so this double cannot drift looser than the
    server it stands in for.
    """

    def __init__(self):
        self.beads: dict[str, list[dict]] = {t: [] for t in backfill.ARCH_TYPES}
        self.patches: list[tuple[str, dict]] = []
        self._next_id = 1

    def _seed(self, bead_type: str, content: dict) -> dict:
        bead = {"id": f"bead-{self._next_id}", "type": bead_type, "content": content}
        self._next_id += 1
        self.beads[bead_type].append(bead)
        return bead

    def list_beads(self, bead_type: str, limit: int = 1000) -> list[dict]:
        return copy.deepcopy(self.beads.get(bead_type, []))

    def patch(self, bead_id: str, body: dict) -> dict:
        body = {**body, "created_by": backfill.CREATED_BY}
        self.patches.append((bead_id, body))
        content = body.get("content")
        if content is not None:
            for beads in self.beads.values():
                for bead in beads:
                    if bead["id"] == bead_id:
                        validate_bead_content("arch", bead["type"], content)
        for beads in self.beads.values():
            for bead in beads:
                if bead["id"] == bead_id:
                    bead.update(body)
                    return bead
        raise AssertionError(f"unknown bead id {bead_id}")


def test_the_double_rejects_a_payload_the_live_schema_would_reject():
    """If this ever passes, the double has drifted looser than the substrate."""
    sub = ValidatingFakeSubstrate()
    bead = sub._seed("capability", _capability("bc.finance.visibility"))

    with pytest.raises(Exception):
        sub.patch(bead["id"], {"content": {"name": "missing required fields"}})


def test_dry_run_classifies_nothing_and_writes_nothing():
    sub = ValidatingFakeSubstrate()
    sub._seed("capability", _capability("bc.finance.visibility"))
    sub._seed("service", {
        "ref": "svc.substrate.api",
        "name": "Substrate API",
        "description": "d",
        "layer": "supply",
        "owner": "grant",
        "evidence": ["apps/substrate/"],
        "assessed_at": "2026-07-30",
    })

    plan = backfill.reconcile(sub, apply=False)

    assert plan.dry_run is True
    assert len(plan.classified) == 2
    assert plan.already_classified == []
    assert sub.patches == []


def test_apply_classifies_beads_missing_source_class_as_authored():
    sub = ValidatingFakeSubstrate()
    sub._seed("capability", _capability("bc.finance.visibility"))

    plan = backfill.reconcile(sub, apply=True)

    assert plan.classified == ["capability:bc.finance.visibility"]
    assert len(sub.patches) == 1
    bead_id, body = sub.patches[0]
    assert body["content"]["source_class"] == "authored"
    assert body["content"]["ref"] == "bc.finance.visibility"
    assert body["created_by"] == "arch-source-class-backfill"


def test_apply_preserves_every_other_content_field():
    sub = ValidatingFakeSubstrate()
    sub._seed("capability", _capability("bc.finance.visibility", notes="hand-written"))

    backfill.reconcile(sub, apply=True)

    patched = sub.beads["capability"][0]["content"]
    assert patched["notes"] == "hand-written"
    assert patched["maturity"] == "operating"
    assert patched["source_class"] == "authored"


def test_apply_skips_beads_that_already_carry_source_class():
    sub = ValidatingFakeSubstrate()
    sub._seed(
        "capability",
        _capability("bc.finance.visibility", source_class="derived"),
    )

    plan = backfill.reconcile(sub, apply=True)

    assert plan.classified == []
    assert plan.already_classified == ["capability:bc.finance.visibility"]
    assert sub.patches == []


def test_apply_twice_is_idempotent():
    sub = ValidatingFakeSubstrate()
    sub._seed("capability", _capability("bc.finance.visibility"))

    first = backfill.reconcile(sub, apply=True)
    second = backfill.reconcile(sub, apply=True)

    assert len(first.classified) == 1
    assert second.classified == []
    assert second.already_classified == ["capability:bc.finance.visibility"]
    assert len(sub.patches) == 1


def test_backfill_never_deletes():
    """The module docstring promises "No delete path exists in this script".

    This guard asserts over every attribute whose name STARTS WITH "delete",
    not the single literal name "delete". The narrower form went on passing
    after the promise became false: subclassing the shared client inherited
    `delete_bead` and `delete_link`, and neither is named exactly "delete", so
    the guard could no longer fail for the reason it exists. The protocol half
    still checks for absence, because `SubstrateClient` is a Protocol that
    declares no delete at all; the concrete half checks for REFUSAL, because
    the shared client legitimately implements deletes and this script's
    subclass overrides them to raise.
    """
    protocol_deletes = [
        name for name in dir(backfill.SubstrateClient) if name.startswith("delete")
    ]
    assert protocol_deletes == [], (
        f"SubstrateClient protocol now declares a delete path: {protocol_deletes}"
    )

    # Point this at the PARENT, not at backfill.Substrate. The subclass defines
    # delete_bead/delete_link itself, so a list taken from it is non-empty
    # unconditionally and the assertion could never fire -- which is the exact
    # vacuous-guard shape this whole test exists to prevent, and an earlier
    # version of these very lines had it. Proven by deleting both names from
    # substrate_client.Substrate: the subclass still reported both.
    inherited = [
        name for name in dir(backfill._Substrate) if name.startswith("delete")
    ]
    assert inherited, (
        "the shared client no longer declares a delete path; this guard was "
        "written around inheriting one and needs rewriting rather than silently passing"
    )

    concrete_deletes = [
        name for name in dir(backfill.Substrate) if name.startswith("delete")
    ]
    for name in concrete_deletes:
        with pytest.raises(NotImplementedError):
            getattr(backfill.Substrate, name)(object.__new__(backfill.Substrate), "x")


def test_write_failures_are_reported_without_leaking_response_bodies():
    class RejectingSubstrate(ValidatingFakeSubstrate):
        def patch(self, bead_id, body):
            raise backfill.SubstrateError(422, "body containing secret-token", "/beads/x")

    sub = RejectingSubstrate()
    sub._seed("capability", _capability("bc.finance.visibility"))

    plan = backfill.reconcile(sub, apply=True)

    assert plan.errors
    output = "\n".join(plan.errors)
    assert "secret-token" not in output
    assert "422" in output


def test_main_defaults_to_dry_run_and_makes_no_writes(monkeypatch, capsys):
    sub = ValidatingFakeSubstrate()
    sub._seed("capability", _capability("bc.finance.visibility"))

    monkeypatch.setattr(backfill, "Substrate", lambda: sub)
    monkeypatch.setattr(sys, "argv", ["arch-source-class-backfill.py"])

    exit_code = backfill.main()

    assert exit_code == 0
    assert sub.patches == []
    assert "DRY RUN" in capsys.readouterr().out


def test_main_apply_flag_writes(monkeypatch, capsys):
    sub = ValidatingFakeSubstrate()
    sub._seed("capability", _capability("bc.finance.visibility"))

    monkeypatch.setattr(backfill, "Substrate", lambda: sub)
    monkeypatch.setattr(sys, "argv", ["arch-source-class-backfill.py", "--apply"])

    exit_code = backfill.main()

    assert exit_code == 0
    assert len(sub.patches) == 1
    assert "DRY RUN" not in capsys.readouterr().out
