"""Amendment 30 PR-5: the materializer writes only where told."""
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

materialize_agents = __import__("materialize_agents")


def test_materializes_both_personas(tmp_path):
    written = materialize_agents.materialize(tmp_path)
    names = sorted(p.name for p in written)
    assert names == ["architect-sme.md", "polecat-developer.md"]
    for p in written:
        assert p.parent == tmp_path / ".claude" / "agents"
        assert p.read_text().startswith("---")


def test_refuses_nonexistent_destination(tmp_path):
    with pytest.raises(SystemExit):
        materialize_agents.materialize(tmp_path / "nope")


def test_is_idempotent(tmp_path):
    materialize_agents.materialize(tmp_path)
    written = materialize_agents.materialize(tmp_path)
    assert len(written) == 2
