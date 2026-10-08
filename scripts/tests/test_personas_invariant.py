"""Amendment 30 PR-5: the persona-artefact invariant can fail."""
import pathlib
import shutil
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import repo_invariants

REPO = pathlib.Path(__file__).resolve().parents[2]


def test_live_repo_passes():
    result = repo_invariants.check_personas_are_repository_artefacts(REPO)
    assert result.passed, [v for v in result.violations]


def _fixture_repo(tmp_path: pathlib.Path) -> pathlib.Path:
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    (root / "docs" / "agents").mkdir(parents=True)
    shutil.copyfile(REPO / "scripts" / "materialize_agents.py",
                    root / "scripts" / "materialize_agents.py")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    return root


def test_missing_persona_fails(tmp_path):
    root = _fixture_repo(tmp_path)
    result = repo_invariants.check_personas_are_repository_artefacts(root)
    assert not result.passed
    assert any("does not exist" in v.message for v in result.violations)


def test_tracked_claude_agents_content_fails(tmp_path):
    root = _fixture_repo(tmp_path)
    for name in ("polecat-developer.md", "architect-sme.md"):
        shutil.copyfile(REPO / "docs" / "agents" / name, root / "docs" / "agents" / name)
    agents = root / ".claude" / "agents"
    agents.mkdir(parents=True)
    (agents / "rogue.md").write_text("untracked persona becomes truth\n")
    subprocess.run(["git", "-C", str(root), "add", "-f", ".claude/"], check=True)
    result = repo_invariants.check_personas_are_repository_artefacts(root)
    assert not result.passed
    assert any("never be tracked" in v.message for v in result.violations)


def test_tracked_claude_skills_content_passes(tmp_path):
    # Release-gate finding, 2026-08-17 (PR #425 x PR #420): .claude/skills/
    # is versioned, gate-reviewed harness configuration. A parent-wide
    # ls-files on .claude/ would accuse it; the invariant's subject is
    # materialized personas under .claude/agents/ only.
    root = _fixture_repo(tmp_path)
    for name in ("polecat-developer.md", "architect-sme.md"):
        shutil.copyfile(REPO / "docs" / "agents" / name, root / "docs" / "agents" / name)
    skills = root / ".claude" / "skills" / "platform-doctrine"
    skills.mkdir(parents=True)
    (skills / "SKILL.md").write_text("versioned harness configuration\n")
    subprocess.run(["git", "-C", str(root), "add", "-f", ".claude/skills/"], check=True)
    result = repo_invariants.check_personas_are_repository_artefacts(root)
    assert result.passed, [v for v in result.violations]
