"""B-110 — the closed edge vocabulary must admit the edges its own writers write.

BEAD_LINK_TYPES is the single gate every ``bead_link`` row passes through
(``BeadLinkCreate.normalize_link_type``, `src/schemas.py`). Two real writers
sit behind other services' scope and cannot be touched from here — ``scripts/
ea-load.py`` and ``apps/factory-dispatcher/activities/knowledge_ingestion.py``
— so these tests read the real constant/literal those writers use, not a
hand-copied guess of it. A copy can drift from its source silently; an import
(or, where a real import would pull in dependencies this suite must not
require — no substrate, no cluster, no network — an AST read of the literal)
cannot.
"""

import ast
import re
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from src.schemas import BEAD_LINK_TYPES, BeadLinkCreate

REPO_ROOT = Path(__file__).resolve().parents[3]

# The admitted types (docs/architecture/bead-object-inventory.md "Edge
# vocabulary" is canonical). Spelled out here, not derived from
# BEAD_LINK_TYPES, so a stray unadmitted type is still caught. Sixteen were
# decided for B-110; `delivers` is the seventeenth, admitted 2026-08-25 with
# arch.release; `threatens` and `accepted_by` are the eighteenth and
# nineteenth, admitted 2026-09-13 with arch.risk (R26.09/O-5).
ADMITTED_LINK_TYPES = (
    "designs",
    "supersedes",
    "gates",
    "regresses",
    "found_by",
    "affects",
    "measures",
    "supports",
    "realizes",
    "depends_on",
    "consumes",
    "applies",
    "derived_from",
    "enforced_by",
    "caused_by",
    "resolved_by",
    "delivers",
    "threatens",
    "accepted_by",
)


def test_exactly_nineteen_types_are_admitted():
    assert len(ADMITTED_LINK_TYPES) == 19
    assert BEAD_LINK_TYPES == frozenset(ADMITTED_LINK_TYPES)


@pytest.mark.parametrize("link_type", ADMITTED_LINK_TYPES)
def test_each_admitted_type_is_accepted_by_the_real_validator(link_type):
    link = BeadLinkCreate(target_id=uuid4(), link_type=link_type)
    assert link.link_type == link_type


def test_unadmitted_link_type_is_rejected_with_the_allowed_set_named():
    with pytest.raises(ValidationError) as exc_info:
        BeadLinkCreate(target_id=uuid4(), link_type="produces")

    message = str(exc_info.value)
    for link_type in ADMITTED_LINK_TYPES:
        assert link_type in message


# --- scripts/ea-load.py: the EA reconciler writes supports/realizes/depends_on/
# measures on every reconcile ------------------------------------------------

EA_LOAD = REPO_ROOT / "scripts" / "ea-load.py"


def _ea_load_edge_types():
    """The real EDGES keys, read statically — not a copy of them.

    Read by AST rather than imported, for the same reason the
    knowledge_ingestion check below is: ea-load.py imports ``yaml`` at module
    scope and exits when it is absent, and this suite's environment is
    apps/substrate/requirements.txt, which does not carry PyYAML. Executing
    the module here made a substrate test depend on an undeclared dependency
    of another tree — green wherever the dispatcher's bootstrap had installed
    pyyaml, red in CI's isolated substrate venv (run 32655475145). The
    constant is still read from the real file, so a drifting copy is still
    impossible; only the import is avoided.
    """
    tree = ast.parse(EA_LOAD.read_text())
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not any(
            isinstance(t, ast.Name) and t.id == "EDGES" for t in node.targets
        ):
            continue
        assert isinstance(node.value, ast.Dict), "EDGES is no longer a dict literal"
        keys = [k.value for k in node.value.keys if isinstance(k, ast.Constant)]
        assert len(keys) == len(node.value.keys), "EDGES keys are no longer literals"
        return keys
    raise AssertionError("EDGES not found in scripts/ea-load.py")


def test_ea_load_edge_types_are_all_admitted():
    """Reads the real EDGES constant — not a copy of it.

    Fails against today's seven-type BEAD_LINK_TYPES: ``supports``,
    ``realizes`` and ``depends_on`` are not in it, and every reconcile ea-load
    runs writes at least one of them.
    """
    assert set(_ea_load_edge_types()) <= BEAD_LINK_TYPES


# --- knowledge_ingestion.py: land_knowledge_principles links a landed
# principle back to its extraction task with `derived_from` --------------------

KNOWLEDGE_INGESTION = (
    REPO_ROOT / "apps" / "factory-dispatcher" / "activities" / "knowledge_ingestion.py"
)


def _knowledge_engine_landing_link_type() -> str:
    """The link_type land_knowledge_principles passes to store.create_link.

    knowledge_ingestion.py imports `dispatch` and `temporalio`, neither
    installed in this suite's environment, so a real ``import
    knowledge_ingestion`` would violate "no substrate, no cluster, no
    network" before it ever reached the assertion. Reading the literal via AST
    is the real source, not a hand-copy of it — the value comes from parsing
    the actual file on disk, so a future edit to that call site is still
    caught here.
    """
    tree = ast.parse(KNOWLEDGE_INGESTION.read_text())
    create_link_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "create_link"
    ]
    assert len(create_link_calls) == 1, (
        "expected exactly one store.create_link call in knowledge_ingestion.py "
        "to inspect — the extraction point needs updating if this changed"
    )
    link_type_arg = create_link_calls[0].args[-1]
    assert isinstance(link_type_arg, ast.Constant), "link_type is no longer a literal"
    return link_type_arg.value


def test_knowledge_engine_landing_link_type_is_admitted():
    """Fails against today's frozenset: `derived_from` is not in it, so every
    principle-landing call in knowledge_ingestion.py 422s at HEAD."""
    landing_link_type = _knowledge_engine_landing_link_type()
    assert landing_link_type == "derived_from"
    assert landing_link_type in BEAD_LINK_TYPES


# --- canonical doc table -----------------------------------------------------

def test_schema_link_types_match_canonical_edge_table():
    doc = (REPO_ROOT / "docs/architecture/bead-object-inventory.md").read_text()
    edge_section = doc.split("## Edge vocabulary", 1)[1].split("\n## ", 1)[0]
    admitted_section = edge_section.split("### Excluded", 1)[0]
    documented_types = frozenset(
        re.findall(r"^\| `[^`|]+ --([a-z_]+)--> [^`|]+` \|", admitted_section, re.MULTILINE)
    )

    assert documented_types == BEAD_LINK_TYPES


def test_produces_and_reads_are_recorded_as_deliberately_excluded():
    doc = (REPO_ROOT / "docs/architecture/bead-object-inventory.md").read_text()
    edge_section = doc.split("## Edge vocabulary", 1)[1].split("\n## ", 1)[0]
    excluded_section = edge_section.split("### Excluded", 1)[1]

    assert "produces" not in BEAD_LINK_TYPES
    assert "reads" not in BEAD_LINK_TYPES
    assert "--produces-->" in excluded_section
    assert "--reads-->" in excluded_section
