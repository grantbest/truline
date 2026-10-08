from __future__ import annotations

import os
import pathlib
import subprocess
import sys


REPO = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))

import repo_invariants as inv  # noqa: E402


def _write(path: pathlib.Path, text: str, mode: int | None = None) -> pathlib.Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    if mode is not None:
        os.chmod(path, mode)
    return path


def _git(root: pathlib.Path, *args: str) -> None:
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=Test User",
            "-c",
            "user.email=test@example.invalid",
            *args,
        ],
        check=True,
        capture_output=True,
        text=True,
    )


def _commit(root: pathlib.Path, message: str) -> None:
    _git(root, "add", "-A")
    _git(root, "commit", "--allow-empty", "-m", message)


def _rollout(root: pathlib.Path, *entries: str) -> None:
    quoted = "\n".join(f'  "{entry}"' for entry in entries)
    _write(
        root / "scripts" / "rollout-status.sh",
        "#!/usr/bin/env bash\nDEPLOYMENTS=(\n" + quoted + "\n)\n",
        0o755,
    )


def _deployment(
    root: pathlib.Path,
    namespace: str,
    name: str,
    replicas: int = 1,
    rel: str = "infrastructure/k8s/app.yaml",
) -> None:
    _write(
        root / rel,
        f"""apiVersion: apps/v1
kind: Deployment
metadata:
  name: {name}
  namespace: {namespace}
spec:
  replicas: {replicas}
""",
    )


def _kube_linter(root: pathlib.Path, *paths: str) -> None:
    entries = "\n".join(f'    - "{path}"' for path in paths)
    _write(root / ".kube-linter.yml", "checks:\n  ignorePaths:\n" + entries + "\n")


def _workflow(root: pathlib.Path, body: str) -> None:
    _write(root / ".github" / "workflows" / "lint.yml", body)


def _migration(root: pathlib.Path, filename: str, revision: str, down_revision: str | None) -> None:
    down = "None" if down_revision is None else repr(down_revision)
    _write(
        root / "apps" / "substrate" / "migrations" / "versions" / filename,
        f"revision = {revision!r}\ndown_revision = {down}\n",
    )


def _minimal_repo(root: pathlib.Path) -> None:
    _git(root, "init", "-q")
    _rollout(root, "platform/app")
    _deployment(root, "platform", "app")
    ignored = "infrastructure/k8s/ignored.yaml"
    _write(root / ignored, "kind: ConfigMap\n")
    _kube_linter(root, ignored)
    _write(root / "scripts" / "ok.sh", "#!/usr/bin/env bash\n", 0o755)
    _workflow(root, "jobs:\n  test:\n    steps:\n      - run: bash scripts/ok.sh\n")
    _migration(root, "0001_base.py", "0001_base", None)
    _migration(root, "0002_next.py", "0002_next", "0001_base")
    _commit(root, "Initial fixture")


def _messages(result: inv.RuleResult) -> str:
    return "\n".join(v.message for v in result.violations)


def test_rule1_passes_when_rollout_deployment_exists(tmp_path):
    _minimal_repo(tmp_path)

    result = inv.check_rollout_deployments_exist(tmp_path)

    assert result.passed


def test_rule1_reports_home_vikunja_regression(tmp_path):
    _minimal_repo(tmp_path)
    _rollout(tmp_path, "home/vikunja")

    result = inv.check_rollout_deployments_exist(tmp_path)

    assert not result.passed
    assert "home/vikunja" in _messages(result)
    assert "scripts/rollout-status.sh" in _messages(result)


def test_rule2_passes_when_rollout_deployment_has_replicas(tmp_path):
    _minimal_repo(tmp_path)

    result = inv.check_rollout_deployments_nonzero(tmp_path)

    assert result.passed


def test_rule2_reports_zero_replica_false_green(tmp_path):
    _minimal_repo(tmp_path)
    _rollout(tmp_path, "infra-network/pihole")
    _deployment(tmp_path, "infra-network", "pihole", replicas=0)

    result = inv.check_rollout_deployments_nonzero(tmp_path)

    assert not result.passed
    assert "infra-network/pihole" in _messages(result)
    assert "replicas: 0" in _messages(result)


def test_rule3_passes_when_ignore_paths_exist(tmp_path):
    _minimal_repo(tmp_path)

    result = inv.check_kube_linter_ignore_paths(tmp_path)

    assert result.passed


def test_rule3_reports_dead_kube_linter_exclusions_from_pr226(tmp_path):
    _minimal_repo(tmp_path)
    _write(
        tmp_path / "infrastructure" / "k8s" / "base" / "automations" / "automations-worker.yaml",
        "kind: Deployment\n",
    )
    _kube_linter(
        tmp_path,
        "infrastructure/k8s/automations/automations-worker.yaml",
        "infrastructure/k8s/automations/automations-worker.yaml",
    )

    result = inv.check_kube_linter_ignore_paths(tmp_path)

    assert len(result.violations) == 2
    assert "infrastructure/k8s/automations/automations-worker.yaml" in _messages(result)
    assert all(v.edit_file == pathlib.Path(".kube-linter.yml") for v in result.violations)


def test_rule4_passes_when_workflow_scripts_exist_and_are_executable(tmp_path):
    _minimal_repo(tmp_path)

    result = inv.check_workflow_script_paths(tmp_path)

    assert result.passed


def test_rule4_allows_python_interpreter_to_run_non_executable_python_file(tmp_path):
    _minimal_repo(tmp_path)
    _write(tmp_path / "scripts" / "checker.py", "print('ok')\n", 0o644)
    _workflow(tmp_path, "jobs:\n  test:\n    steps:\n      - run: python scripts/checker.py\n")

    result = inv.check_workflow_script_paths(tmp_path)

    assert result.passed


def test_rule4_reports_missing_and_non_executable_workflow_scripts(tmp_path):
    _minimal_repo(tmp_path)
    _write(tmp_path / "scripts" / "not-exec.sh", "#!/usr/bin/env bash\n", 0o644)
    _workflow(
        tmp_path,
        """jobs:
  test:
    steps:
      - run: bash scripts/not-exec.sh
      - run: bash scripts/missing.sh
""",
    )

    result = inv.check_workflow_script_paths(tmp_path)

    assert len(result.violations) == 2
    messages = _messages(result)
    assert "scripts/not-exec.sh" in messages
    assert "not executable" in messages
    assert "scripts/missing.sh" in messages
    assert "missing" in messages


def test_rule5_passes_for_linear_migrations_and_filename_revision_mismatch(tmp_path):
    _minimal_repo(tmp_path)
    versions = tmp_path / "apps" / "substrate" / "migrations" / "versions"
    for path in versions.glob("*.py"):
        path.unlink()
    _migration(tmp_path, "0001_baseline.py", "0001_baseline", None)
    _migration(tmp_path, "0002_unique_plaid_id.py", "0002_unique_plaid_id", "0001_baseline")
    _migration(tmp_path, "0003_unique_account_mask.py", "0003", "0002_unique_plaid_id")
    _migration(tmp_path, "0004_bead_link.py", "0004_bead_link", "0003")
    _migration(tmp_path, "0005_decrypt_non_finance.py", "0005_decrypt_non_finance", "0004_bead_link")
    _migration(tmp_path, "0006_unique_arch_ref.py", "0006_unique_arch_ref", "0005_decrypt_non_finance")

    result = inv.check_alembic_heads(tmp_path)

    assert result.passed


def test_rule5_reports_multiple_heads(tmp_path):
    _minimal_repo(tmp_path)
    versions = tmp_path / "apps" / "substrate" / "migrations" / "versions"
    for path in versions.glob("*.py"):
        path.unlink()
    _migration(tmp_path, "0001_base.py", "0001_base", None)
    _migration(tmp_path, "0002_a.py", "0002_a", "0001_base")
    _migration(tmp_path, "0003_b.py", "0003_b", "0002_a")
    _migration(tmp_path, "0004_orphan.py", "0004_orphan", "0003_missing")

    result = inv.check_alembic_heads(tmp_path)

    assert not result.passed
    assert "expected exactly one Alembic head" in _messages(result)


def test_rule5_reports_duplicate_down_revision(tmp_path):
    _minimal_repo(tmp_path)
    versions = tmp_path / "apps" / "substrate" / "migrations" / "versions"
    for path in versions.glob("*.py"):
        path.unlink()
    _migration(tmp_path, "0001_base.py", "0001_base", None)
    _migration(tmp_path, "0002_a.py", "0002_a", "0001_base")
    _migration(tmp_path, "0002_b.py", "0002_b", "0001_base")

    result = inv.check_alembic_heads(tmp_path)

    assert not result.passed
    assert "multiple migrations share down_revision 0001_base" in _messages(result)


def _write_migration_source(root: pathlib.Path, filename: str, source: str) -> None:
    _write(root / "apps" / "substrate" / "migrations" / "versions" / filename, source)


def test_parse_migrations_reads_annotated_revision_same_as_plain(tmp_path):
    """`revision: str = "..."` (ast.AnnAssign) must parse identically to the
    plain `revision = "..."` (ast.Assign) form -- the widening changes which
    node types are examined, not what counts as a valid revision.
    """
    (tmp_path / "apps" / "substrate" / "migrations" / "versions").mkdir(parents=True)
    _write_migration_source(
        tmp_path,
        "0001_plain.py",
        'revision = "0001_plain"\ndown_revision = None\n',
    )
    _write_migration_source(
        tmp_path,
        "0002_annotated.py",
        'revision: str = "0002_annotated"\n'
        'down_revision: str = "0001_plain"\n',
    )

    migrations = {m.path.name: m for m in inv.parse_migrations(tmp_path)}

    assert migrations["0001_plain.py"].revision == "0001_plain"
    assert migrations["0001_plain.py"].down_revisions == (None,)
    assert migrations["0002_annotated.py"].revision == "0002_annotated"
    assert migrations["0002_annotated.py"].down_revisions == ("0001_plain",)


def test_parse_migrations_bare_annotation_with_no_value_is_skipped_not_raised(tmp_path):
    """`revision: str` alone is a valid AnnAssign whose `.value` is None --
    the parser must not crash on it, and must treat the migration as if it
    declared no revision at all.
    """
    (tmp_path / "apps" / "substrate" / "migrations" / "versions").mkdir(parents=True)
    _write_migration_source(
        tmp_path,
        "0001_bare.py",
        "revision: str\ndown_revision: str\n",
    )

    migrations = inv.parse_migrations(tmp_path)  # must not raise

    assert len(migrations) == 1
    assert migrations[0].revision is None
    assert migrations[0].down_revisions == ()


def test_rule6_passes_when_retired_vikunja_sync_paths_are_absent(tmp_path):
    _minimal_repo(tmp_path)

    result = inv.check_retired_application_paths_absent(tmp_path)

    assert result.passed


def test_rule6_reports_retired_vikunja_sync_paths_that_return(tmp_path):
    _minimal_repo(tmp_path)
    _write(tmp_path / "apps" / "substrate-sync-vikunja" / "README.md", "retired\n")
    _write(
        tmp_path / "infrastructure" / "k8s" / "base" / "substrate" / "substrate-sync-vikunja.yaml",
        "kind: Deployment\n",
    )

    result = inv.check_retired_application_paths_absent(tmp_path)

    assert len(result.violations) == 2
    messages = _messages(result)
    assert "apps/substrate-sync-vikunja" in messages
    assert "infrastructure/k8s/base/substrate/substrate-sync-vikunja.yaml" in messages


def test_rule7_passes_when_pr_merge_commit_preserves_change_kind(tmp_path):
    _minimal_repo(tmp_path)
    _write(tmp_path / "docs" / "example.md", "fixture\n")
    _commit(
        tmp_path,
        "fix(example): compliant durable declaration (#999)\n\nChange kind: behavioral",
    )

    result = inv.check_pr_merge_commit_change_kind(tmp_path, baseline=None)

    assert result.passed
    assert "1 PR merge commit checked" in result.summary


def test_rule7_reports_factory_authored_squash_commit_without_change_kind(tmp_path):
    _minimal_repo(tmp_path)
    _write(tmp_path / "docs" / "factory.md", "fixture\n")
    _commit(
        tmp_path,
        "fix(factory): dispatched task without durable declaration (#1000)\n\n"
        "Dispatched from dev.task 00000000-0000-0000-0000-000000000000 by "
        "factory-dispatcher/codex.",
    )

    result = inv.check_pr_merge_commit_change_kind(tmp_path, baseline=None)

    assert not result.passed
    messages = _messages(result)
    assert "PR #1000" in messages
    assert "Change kind: structural" in messages
    assert "Change kind: behavioral" in messages


def test_rule7_skips_when_the_tree_carries_no_git_history(tmp_path):
    """A guard that cannot observe must skip, not accuse.

    The checker runs against fixture directories and exported trees with no git
    history. Treating that as a violation manufactures a finding about commits
    that do not exist, and when this rule first shipped it did exactly that:
    every CLI meta-test built on a plain temp directory began failing, including
    one belonging to an unrelated rule, which turned `main` red.
    """
    plain_tree = tmp_path / "no-git-here"
    plain_tree.mkdir()  # deliberately not a git work tree

    result = inv.check_pr_merge_commit_change_kind(plain_tree, baseline=None)

    assert result.passed
    assert not result.violations
    assert "skipped" in result.summary


def test_rule7_still_bites_inside_a_work_tree_after_the_skip_was_added(tmp_path):
    """The skip must be narrow: no git history at all, and nothing wider.

    Without this, the fix for the red build is indistinguishable from disabling
    the rule.
    """
    _minimal_repo(tmp_path)
    _write(tmp_path / "docs" / "example.md", "fixture\n")
    _commit(tmp_path, "fix(example): no durable declaration (#1001)")

    result = inv.check_pr_merge_commit_change_kind(tmp_path, baseline=None)

    assert not result.passed
    assert "PR #1001" in _messages(result)


def _head_sha(root) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def test_rule7_excuses_a_commit_named_in_the_grandfather_ratchet(tmp_path):
    """A merge that already lost its declaration cannot be corrected in place.

    Rewriting the message means rewriting `main`, and substrate records cite
    those shas as operational truth, so the correction would trade a wrong
    commit message for a CMDB pointing at commits that no longer exist. The
    admission is recorded by sha instead, next to the reason.
    """
    _minimal_repo(tmp_path)
    _write(tmp_path / "docs" / "excused.md", "fixture\n")
    _commit(tmp_path, "fix(example): declaration lost at merge (#1002)")
    sha = _head_sha(tmp_path)
    _write(
        tmp_path / "scripts" / "change-kind-grandfathered.txt",
        f"# reason recorded here\n{sha}\n",
    )

    result = inv.check_pr_merge_commit_change_kind(tmp_path, baseline=None)

    assert result.passed
    assert "1 grandfathered" in result.summary


def test_rule7_grandfather_ratchet_excuses_only_the_shas_it_names(tmp_path):
    """The ratchet must not become a blanket amnesty.

    An entry admits one specific commit. A later merge that loses its
    declaration has to be caught, or the file becomes a way to turn the rule
    off one line at a time.

    A real baseline is required here, not ``baseline=None``: that resolves to
    ``HEAD -n 1`` and examines only the newest commit, which would let this test
    pass while proving nothing about the excused one.
    """
    _minimal_repo(tmp_path)
    baseline = _head_sha(tmp_path)
    _write(tmp_path / "docs" / "excused.md", "fixture\n")
    _commit(tmp_path, "fix(example): declaration lost at merge (#1003)")
    excused = _head_sha(tmp_path)
    _write(
        tmp_path / "scripts" / "change-kind-grandfathered.txt",
        f"{excused}\n",
    )
    _write(tmp_path / "docs" / "later.md", "fixture\n")
    _commit(tmp_path, "fix(example): a later merge, not excused (#1004)")

    result = inv.check_pr_merge_commit_change_kind(tmp_path, baseline=baseline)

    assert not result.passed
    assert "2 PR merge commits checked" in result.summary
    assert "1 grandfathered" in result.summary
    messages = _messages(result)
    assert "PR #1004" in messages
    assert "PR #1003" not in messages


def test_rule7_a_grandfather_sha_unreachable_in_the_audited_range_fails_as_dead(tmp_path):
    """A grandfather entry that names no commit the audited range ever sees is
    a standing hole nothing else would report closed. It must fail by name,
    not be tolerated silently.
    """
    _minimal_repo(tmp_path)
    baseline = _head_sha(tmp_path)
    _write(tmp_path / "docs" / "compliant.md", "fixture\n")
    _commit(
        tmp_path,
        "fix(example): compliant durable declaration (#1005)\n\nChange kind: behavioral",
    )
    _write(
        tmp_path / "scripts" / "change-kind-grandfathered.txt",
        "0" * 40 + "\n",
    )

    result = inv.check_pr_merge_commit_change_kind(tmp_path, baseline=baseline)

    assert not result.passed
    assert "1 grandfathered entry(ies) dead" in result.summary
    messages = _messages(result)
    assert "0" * 40 in messages
    assert "delete" in messages


def test_rule7_a_grandfather_sha_whose_commit_now_carries_the_declaration_fails_as_dead(tmp_path):
    """A sha that WAS a violation but no longer is (its commit already
    carries exactly one declaration) must not stay excused forever."""
    _minimal_repo(tmp_path)
    baseline = _head_sha(tmp_path)
    _write(tmp_path / "docs" / "compliant.md", "fixture\n")
    _commit(
        tmp_path,
        "fix(example): compliant durable declaration (#1006)\n\nChange kind: behavioral",
    )
    sha = _head_sha(tmp_path)
    _write(
        tmp_path / "scripts" / "change-kind-grandfathered.txt",
        f"{sha}\n",
    )

    result = inv.check_pr_merge_commit_change_kind(tmp_path, baseline=baseline)

    assert not result.passed
    assert "1 grandfathered entry(ies) dead" in result.summary
    assert sha in _messages(result)


def test_run_checks_reports_all_rule_violations_in_one_run(tmp_path):
    _minimal_repo(tmp_path)
    _rollout(tmp_path, "home/vikunja")
    _kube_linter(tmp_path, "infrastructure/k8s/dead.yaml")

    results = inv.run_checks(tmp_path)

    failing_rules = {result.rule_id for result in results if result.violations}
    assert "RULE 1" in failing_rules
    assert "RULE 3" in failing_rules
    assert len(failing_rules) >= 2


def test_format_results_includes_remediation_file(tmp_path):
    _minimal_repo(tmp_path)
    _rollout(tmp_path, "home/vikunja")
    result = inv.check_rollout_deployments_exist(tmp_path)

    output = inv.format_results([result])

    assert "Remediation: edit scripts/rollout-status.sh." in output


def test_rule7_does_not_accuse_live_entries_when_the_baseline_is_unreachable(tmp_path):
    """With the baseline unreachable (a shallow clone), the audited range
    degrades to HEAD alone and every live grandfather entry would read as
    dead. A guard that cannot observe must say so rather than accuse: the
    staleness pass is skipped and the summary declares it un-audited.
    """
    _minimal_repo(tmp_path)
    _write(tmp_path / "docs" / "excused.md", "fixture\n")
    _commit(tmp_path, "fix(example): declaration lost at merge (#1007)")
    excused = _head_sha(tmp_path)
    _write(
        tmp_path / "scripts" / "change-kind-grandfathered.txt",
        f"{excused}\n",
    )
    _write(tmp_path / "docs" / "later.md", "fixture\n")
    _commit(
        tmp_path,
        "fix(example): compliant HEAD commit (#1008)\n\nChange kind: behavioral",
    )

    result = inv.check_pr_merge_commit_change_kind(
        tmp_path, baseline="0" * 40
    )

    assert result.passed
    assert "dead" not in result.summary
    assert "grandfather staleness not audited (baseline unreachable)" in result.summary


def test_rule7_does_not_accuse_when_the_baseline_exists_but_is_not_an_ancestor(tmp_path):
    """The reused-CI-workspace shape that turned #713's own CI red: the
    baseline OBJECT is present from old history, but the checked-out head
    cannot traverse to it (a --depth=1 merge-ref fetch makes HEAD a shallow
    boundary). Existence is not observability -- the staleness pass must
    declare itself un-audited, not accuse every live entry.
    """
    _minimal_repo(tmp_path)
    baseline = _head_sha(tmp_path)
    _git(tmp_path, "checkout", "--orphan", "disconnected")
    _write(tmp_path / "docs" / "orphan.md", "fixture\n")
    _commit(
        tmp_path,
        "fix(example): disconnected head (#1009)\n\nChange kind: behavioral",
    )
    _write(
        tmp_path / "scripts" / "change-kind-grandfathered.txt",
        f"{baseline}\n",
    )

    result = inv.check_pr_merge_commit_change_kind(tmp_path, baseline=baseline)

    assert result.passed
    assert "dead" not in result.summary
    assert "grandfather staleness not audited (baseline unreachable)" in result.summary
