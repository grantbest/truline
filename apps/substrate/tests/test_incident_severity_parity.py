"""``ArchIncidentContent.severity`` must never drift from ``ALERT_INVENTORY``.

2026-09-12 architecture review §3 named this pair as the smallest instance of
an unenforced principle: a contract copied across a module boundary carries a
parity test that re-executes the source, or the copy is a defect. The two
copies are ``ArchIncidentContent.severity`` here (``src/schemas.py``) and
``AlertSeverity`` in mcp-hub's ``apps/mcp-hub/src/tools/notify.py`` — the
docstrings on both sides already say they mirror each other; nothing before
this test made that true.

Pattern precedent: ``apps/factory-dispatcher/tests/test_dev_task_contract_parity.py``.
That test imports ``bead_rules.py`` directly because it is a zero-import,
store-agnostic module; the same is true of ``notify.py`` here for our
purposes — its only non-stdlib import is ``httpx``, already a substrate
dependency (requirements.txt), and its one relative import
(``from . import incidents``) is guarded by ``TYPE_CHECKING`` so it never
executes. So this is a direct import, not an AST re-parse: the fragile
first-draft AST approach dev_task_contract_parity.py's own docstring
describes went stale the moment the parsed file's shape changed.

Both mutation-direction tests re-execute SOURCE TEXT with one entry
added, never a file on disk (``apps/mcp-hub/**`` is out of scope for this
change) -- the same technique dev_task_contract_parity.py uses to show a
substrate-side drift is caught without touching apps/substrate/**.
"""

from __future__ import annotations

import sys
import typing
from pathlib import Path

from src.schemas import ArchIncidentContent

NOTIFY_PY = (
    Path(__file__).resolve().parents[2] / "mcp-hub" / "src" / "tools" / "notify.py"
)
sys.path.insert(0, str(NOTIFY_PY.parent))

import notify  # noqa: E402  (path inserted above, mirrors dev_task_contract_parity.py)


def _schema_severities() -> frozenset[str]:
    return frozenset(typing.get_args(ArchIncidentContent.model_fields["severity"].annotation))


def test_incident_severity_matches_alert_inventory():
    assert _schema_severities() == notify.ALERT_SEVERITIES, (
        "ArchIncidentContent.severity (src/schemas.py) has drifted from "
        "ALERT_SEVERITIES in apps/mcp-hub/src/tools/notify.py -- update both "
        "together, in the same change."
    )


def test_the_source_actually_declares_a_plausible_vocabulary():
    """Both sides emptied together would pass equality vacuously; pin the
    source to something real."""
    assert len(notify.ALERT_SEVERITIES) >= 2
    assert "urgent" in notify.ALERT_SEVERITIES


def test_a_mutated_schema_side_is_caught():
    drifted = _schema_severities() | {"catastrophic"}
    assert drifted != notify.ALERT_SEVERITIES


def test_a_mutated_notify_side_is_caught():
    """Re-execute notify.py's SOURCE TEXT with one severity added (the file on
    disk is never touched) and show the equality would fail."""
    source = NOTIFY_PY.read_text()
    drifted_source = source.replace(
        'INFORMATIONAL = "informational"',
        'INFORMATIONAL = "informational"\n    COSMETIC = "cosmetic"',
        1,
    )
    assert drifted_source != source, (
        "the substitution did not match notify.py's current text -- its "
        "formatting changed, update this probe"
    )
    namespace: dict = {"__name__": "notify_drifted_probe"}
    code = compile(drifted_source, str(NOTIFY_PY), "exec", dont_inherit=True)
    exec(code, namespace)
    assert namespace["ALERT_SEVERITIES"] != _schema_severities()
