"""A hand-rolled Substrate double whose method is a bare ``*args``/``**kwargs``
accepts any call shape at all -- including one the real dependency would
reject outright. CLAUDE.md names the incident this reproduces:
``FakeSubstrate.add_note`` took ``**kwargs`` and ignored them, so
``--bind-pr`` shipped with no provenance, passed every test, and 422'd on its
first real call, leaving a partial write behind.

The two sibling checks in ``test_store_double_call_surface.py`` ask different
questions and both pass on exactly this shape: ``test_every_hand_rolled_double_
covers_what_its_own_file_calls`` asks whether a double covers what its own
file calls (a double spelled ``add_note(self, *args, **kwargs)`` covers
everything, since it accepts any call), and ``test_the_hand_rolled_double_
count_has_not_grown`` only counts doubles, never inspects a signature. Neither
asks whether the signature accepts a call ``substrate.Substrate`` itself would
reject. This file adds that third, missing question, and only that one -- it
does not change either sibling's answer.

The check: for every hand-rolled double (the same population
``test_store_double_call_surface.py`` already derives -- never a second,
hand-kept list) and every method it defines that shares a name with a
``substrate.Substrate`` method, count each side's REQUIRED parameters (no
default, excluding ``self``, excluding the ``*args``/``**kwargs`` catch-alls
themselves) -- the real side via ``inspect.signature`` against the live,
imported class, the double side from its own AST (so inspecting a double
never imports, and therefore never runs, the test module that defines it). If
the double requires fewer arguments than the real method does, some call
exists that the double accepts and the real dependency would reject -- e.g.
zero arguments, when the double is ``(self, *args, **kwargs)`` and the real
method requires four positional arguments. That is the defect.

A double's own ``**extra`` (however named) mirroring a real variadic keyword
parameter never lowers its required-parameter count, so a double that spells
out every required name and *also* forwards arbitrary extra keywords -- the
correct shape -- is not flagged for having a catch-all. Only a signature
missing required names is.

No substrate, no network: pure source analysis for the doubles, plus
``inspect.signature`` on the real, imported ``substrate.Substrate`` class --
exactly like its siblings.
"""

from __future__ import annotations

import ast
import inspect
import sys
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
REPO_ROOT = APP.parents[1]
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

import substrate  # noqa: E402

from test_store_double_call_surface import (  # noqa: E402
    NOT_DOUBLES_HERE,
    _hand_rolled_double_nodes_from_source,
    _tracked_test_files,
)

#: Files whose "hand-rolled double" -- per the shared, name-based,
#: two-method-overlap census in test_store_double_call_surface.py -- stands
#: in for a Substrate class OTHER than apps/factory-dispatcher/substrate.py's.
#: Comparing such a double's signature against substrate.Substrate is a
#: category error, not evidence of the incident this file exists to catch.
#: Named here, visibly, rather than silently filtered -- distinct from (and
#: not a change to) NOT_DOUBLES_HERE above, which excludes protocol
#: conformance fixtures for a different reason and is shared with the
#: coverage/count checks this file must not perturb.
#:
#: test_ea_apply.py's FakeSubstrate and FakeDependencySubstrate mirror
#: scripts/ea-load.py's OWN internal Substrate client (that file's own
#: docstring: "mirrors exactly the six calls ea-load.py's own Substrate class
#: makes") and only coincidentally share two method NAMES -- list_beads,
#: add_link -- with the dispatcher's BeadStore protocol, which is what trips
#: the shared census's name-based overlap heuristic. list_beads(bead_type,
#: limit) and add_link(source_id, target_id, link_type[, created_by]) are
#: that OTHER class's real, narrower contract, not a loosened copy of this
#: one's -- flagging them here would be a false positive from the census
#: heuristic already acknowledged as a judgment call, not a real instance of
#: the pattern.
#:
#: ADDED 2026-09-19, when test_store_double_call_surface.py's
#: ``DOUBLE_SCAN_TREES`` widened ``_tracked_test_files()`` from
#: apps/factory-dispatcher/tests alone to also scripts/tests (this file
#: inherits that scan unchanged, since it imports ``_tracked_test_files``
#: rather than keeping a second list): test_ea_derive_dependencies.py's
#: FakeSubstrate mirrors scripts/ea-derive.py's OWN ``Substrate`` class,
#: which that class's own docstring calls out as "Deliberately separate from
#: scripts/ea-load.py's Substrate" -- a third, still narrower contract, not
#: either of the above. test_ea_load.py's FakeSubstrate is the same category
#: error as test_ea_apply.py's pair above: it mirrors ea-load.py's own
#: Substrate client directly rather than a loosened copy of the dispatcher's
#: BeadStore. Both share list_beads(bead_type, limit=1000) and an add_link
#: missing dispatcher's BeadStore.add_link's four required parameters for the
#: same reason test_ea_apply.py's does -- the real dependency they stand in
#: for never had those parameters to begin with.
#:
#: ADDED 2026-09-19 (OPS-181/OPS-182): activities/status_bead.py's
#: StatusSubstrate protocol gained find_bead (the standing-status lookup
#: that replaced a page scan), and every double implementing it gained the
#: method too. find_bead is also a name on the dispatcher's own BeadStore
#: protocol -- a second coincidence of naming, same shape as
#: list_beads/add_link above -- so FakeSub in test_requirements_apply.py and
#: _RecordingSubstrate in test_status_bead.py now overlap BeadStore on two
#: names and would otherwise be checked against it. Both mirror
#: scripts/substrate_client.py's Substrate (via requirements-load.py's and
#: status_bead.py's own callers): list_beads(bead_type, limit=1000) and
#: find_bead(namespace, type, content_ref) against THAT narrower contract,
#: not the dispatcher's own BeadStore.list_beads(namespace, type,
#: state=None, limit=200). test_status_bead_lookup.py's new
#: FixtureSubstrate mirrors the same arch-side contract for the same reason
#: and is exempted here too.
NOT_THIS_SUBSTRATE_HERE = {
    "test_ea_apply.py",
    "test_ea_derive_dependencies.py",
    "test_ea_load.py",
    "test_requirements_apply.py",
    "test_status_bead.py",
    "test_status_bead_lookup.py",
}

#: Every method substrate.Substrate itself defines, by name -- the live
#: contract a hand-rolled double's signature is checked against. Derived from
#: the class at import time, never a hard-coded list of method names: that
#: would be a second, hand-kept copy of the very contract this file exists to
#: stop doubles from silently diverging from.
SUBSTRATE_METHODS = {
    name: getattr(substrate.Substrate, name)
    for name in dir(substrate.Substrate)
    if not name.startswith("_") and callable(getattr(substrate.Substrate, name))
}


def _required_param_count(sig: inspect.Signature) -> int:
    """How many parameters ``sig`` requires a caller to supply: everything
    except ``self`` and a ``*args``/``**kwargs`` catch-all, which by
    definition can be satisfied with nothing."""
    count = 0
    for name, param in sig.parameters.items():
        if name == "self":
            continue
        if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
            continue
        if param.default is inspect.Parameter.empty:
            count += 1
    return count


#: Required-parameter count per substrate.Substrate method, computed once via
#: inspect.signature against the live, imported class -- not re-derived per
#: double, and never hand-copied.
REAL_REQUIRED_COUNTS = {
    name: _required_param_count(inspect.signature(method))
    for name, method in SUBSTRATE_METHODS.items()
}


def _required_param_count_ast(node: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    """The same count :func:`_required_param_count` derives from a live
    signature, derived instead from a double's own AST -- so checking a
    double's shape never imports, and therefore never runs, the test module
    that defines it."""
    args = node.args
    positional = args.posonlyargs + args.args
    # -1 excludes 'self', which is always first and never carries a default.
    n_positional_required = max(len(positional) - len(args.defaults) - 1, 0)
    n_kwonly_required = sum(1 for default in args.kw_defaults if default is None)
    return n_positional_required + n_kwonly_required


def _own_method_nodes(class_node: ast.ClassDef) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    return {
        n.name: n
        for n in class_node.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _signature_offenders_for(filename: str, source: str) -> list[tuple[str, str, int, int]]:
    """Hand-rolled doubles in ``source`` whose own method signature accepts a
    call ``substrate.Substrate``'s own method of the same name would reject:
    fewer required parameters than the real method has. Returns
    ``(filename, "Class.method", double_required, real_required)`` tuples.

    Only a double's OWN method bodies are inspected, not a locally-inherited
    base's -- a method the double inherits rather than redefines is the base
    class's shape, and the base is itself walked separately as its own
    result from ``_hand_rolled_double_nodes_from_source`` whenever it clears
    that function's own two-method threshold.
    """
    offenders: list[tuple[str, str, int, int]] = []
    for class_name, node, effective_methods in _hand_rolled_double_nodes_from_source(source):
        own_nodes = _own_method_nodes(node)
        for method_name in effective_methods & SUBSTRATE_METHODS.keys():
            method_node = own_nodes.get(method_name)
            if method_node is None:
                continue  # locally inherited; not this class's own signature
            double_required = _required_param_count_ast(method_node)
            real_required = REAL_REQUIRED_COUNTS[method_name]
            if double_required < real_required:
                offenders.append(
                    (filename, f"{class_name}.{method_name}", double_required, real_required)
                )
    return offenders


def _tracked_signature_offenders() -> list[tuple[str, str, int, int]]:
    offenders: list[tuple[str, str, int, int]] = []
    for path in _tracked_test_files():
        if path.name in NOT_DOUBLES_HERE or path.name in NOT_THIS_SUBSTRATE_HERE:
            continue
        offenders += _signature_offenders_for(str(path.relative_to(REPO_ROOT)), path.read_text())
    return offenders


def test_no_hand_rolled_double_accepts_a_call_the_real_substrate_would_reject():
    offenders = _tracked_signature_offenders()
    assert not offenders, (
        "hand-rolled Substrate doubles whose signature accepts a call "
        "substrate.Substrate would reject -- a bare *args/**kwargs (or too "
        "few named parameters) standing in for required arguments. This is "
        "the CLAUDE.md incident (FakeSubstrate.add_note took **kwargs and "
        "ignored them; --bind-pr shipped with no provenance, passed every "
        "test, and 422'd on its first real call) reproduced by copy-paste. "
        f"Offenders as (file, Class.method, double_required, real_required): {offenders}"
    )


def test_bare_args_add_note_is_flagged():
    """The incident shape, byte for byte: a double spelled ``(self, *args,
    **kwargs)`` accepts a zero-argument call; substrate.Substrate.add_note
    requires four."""
    source = (
        "class FakeSubstrate:\n"
        "    def list_tasks(self, state=None, limit=200):\n"
        "        return []\n"
        "    def add_note(self, *args, **kwargs):\n"
        "        pass\n"
    )
    offenders = _signature_offenders_for("test_x.py", source)
    assert len(offenders) == 1
    filename, qualified, double_required, real_required = offenders[0]
    assert filename == "test_x.py"
    assert qualified == "FakeSubstrate.add_note"
    assert double_required == 0
    assert real_required == 4


def test_double_spelling_out_the_real_signature_is_not_flagged():
    """Every required parameter named, matching the real method's arity --
    must pass. (A second protocol method, list_tasks, is present purely so
    this class clears the shared census's own two-method threshold and is
    actually examined -- without it, this would vacuously pass by never
    being treated as a double at all.)"""
    source = (
        "class FakeSubstrate:\n"
        "    def list_tasks(self, state=None, limit=200):\n"
        "        return []\n"
        "    def add_note(self, parent_id, kind, body, created_by):\n"
        "        pass\n"
    )
    assert _signature_offenders_for("test_x.py", source) == []


def test_double_mirroring_the_real_extra_kwarg_is_not_flagged():
    """A double that both spells out every required name AND forwards
    arbitrary extra keywords, mirroring substrate.Substrate.add_note's own
    ``**extra`` -- the shape #929's worker settled on once narrowed -- must
    not be penalised for the catch-all."""
    source = (
        "class FakeSubstrate:\n"
        "    def list_tasks(self, state=None, limit=200):\n"
        "        return []\n"
        "    def add_note(self, parent_id, kind, body, created_by,\n"
        "                 trust_tier='system', provenance=None, **extra):\n"
        "        pass\n"
    )
    assert _signature_offenders_for("test_x.py", source) == []


def test_double_omitting_a_method_entirely_is_untouched_by_this_check():
    """A double that never defines add_note at all is
    test_every_hand_rolled_double_covers_what_its_own_file_calls's question
    (does the double cover what its file calls), not this one -- this check
    must neither fire on it nor otherwise change that answer."""
    source = (
        "class FakeSubstrate:\n"
        "    def list_tasks(self, state=None, limit=200):\n"
        "        return []\n"
        "    def list_notes(self, parent_id, limit=500):\n"
        "        return []\n"
    )
    assert _signature_offenders_for("test_x.py", source) == []


def test_real_substrate_required_counts_match_the_live_signatures():
    """A parser that silently matched nothing, or a live class that lost a
    method, would pass every fixture above vacuously."""
    assert REAL_REQUIRED_COUNTS["add_note"] == 4  # parent_id, kind, body, created_by
    assert REAL_REQUIRED_COUNTS["patch_content"] == 3  # bead_id, content, created_by
    assert REAL_REQUIRED_COUNTS["set_state"] == 3  # bead_id, state, created_by
    assert REAL_REQUIRED_COUNTS["list_notes"] == 1  # parent_id (limit defaults)
    assert REAL_REQUIRED_COUNTS["create_task"] == 2  # content, created_by (trust_tier defaults)
    assert REAL_REQUIRED_COUNTS["add_link"] == 4  # source_id, target_id, link_type, created_by
