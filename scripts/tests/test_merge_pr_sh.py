"""End-to-end tests for scripts/merge-pr.sh's release-gate verdict check.

``gh`` is stubbed on PATH; the real python3 (and the real
scripts/gate_markers.py) run unmodified, so the recognizer under test is the
one merge-pr.sh actually shells out to. No gh, no network — see
scripts/tests/test_rollback.py for the same PATH-stub pattern applied to
another bash entry point.
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import textwrap


REPO = pathlib.Path(__file__).resolve().parents[2]
MERGE_PR_SH = REPO / "scripts" / "merge-pr.sh"


def _write_gh_stub(
    bin_dir: pathlib.Path,
    *,
    title_file,
    body_file,
    comments_file,
    merge_log_file,
    head_sha: str | None,
    fail_head_lookup: bool,
) -> None:
    gh = bin_dir / "gh"
    if fail_head_lookup:
        head_lookup_body = 'echo "gh stub: simulated headRefOid lookup failure" >&2; exit 1'
    elif head_sha is not None:
        head_lookup_body = f'printf \'%s\\n\' "{head_sha}"'
    else:
        head_lookup_body = 'echo "gh stub: unexpected --json field: headRefOid" >&2; exit 64'
    gh.write_text(
        textwrap.dedent(
            f"""\
            #!/usr/bin/env bash
            set -euo pipefail

            if [ "$1" = "pr" ] && [ "$2" = "view" ]; then
              shift 2
              shift  # pr number
              json_field=""
              while [ $# -gt 0 ]; do
                case "$1" in
                  --json) json_field="$2"; shift 2 ;;
                  -q) shift 2 ;;
                  *) shift ;;
                esac
              done
              case "$json_field" in
                title) cat "{title_file}" ;;
                body) cat "{body_file}" ;;
                comments) cat "{comments_file}" ;;
                headRefOid) {head_lookup_body} ;;
                *) echo "gh stub: unexpected --json field: $json_field" >&2; exit 64 ;;
              esac
              exit 0
            fi

            if [ "$1" = "pr" ] && [ "$2" = "merge" ]; then
              shift 2
              printf '%s\\0' "$@" > "{merge_log_file}"
              exit 0
            fi

            echo "gh stub: unexpected invocation: $*" >&2
            exit 64
            """
        )
    )
    os.chmod(gh, 0o755)


def _run(
    tmp_path: pathlib.Path,
    *args: str,
    title: str = "Some PR title",
    body: str = "Summary.\n\nChange kind: structural\n\nRelease-gate: MERGE\n",
    comments: list | None = None,
    head_sha: str | None = "unused-head-sha",
    fail_head_lookup: bool = False,
) -> tuple[subprocess.CompletedProcess[str], pathlib.Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)

    title_file = tmp_path / "title.txt"
    body_file = tmp_path / "body.txt"
    comments_file = tmp_path / "comments.json"
    merge_log_file = tmp_path / "merge-call.log"

    title_file.write_text(title)
    body_file.write_text(body)
    comments_file.write_text(json.dumps(comments or []))

    _write_gh_stub(
        bin_dir,
        title_file=title_file,
        body_file=body_file,
        comments_file=comments_file,
        merge_log_file=merge_log_file,
        head_sha=head_sha,
        fail_head_lookup=fail_head_lookup,
    )

    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    # merge-pr.sh now calls post-merge-health-check.py after a successful
    # merge. With FACTORY_REPO exported -- as it is on the factory worker
    # host -- resolve_repo() returns before reaching the gh stub, the stub
    # exits 64, and announce_unreachable() reaches the REAL Discord webhook
    # and the REAL ~/.factory-dispatcher/alert-state.json. That state file
    # records what this host has already alerted about, so a test run can
    # SUPPRESS a later genuine unreachable alert -- the failure is in the
    # bad direction. Mirrors the isolation in
    # test_merge_pr_sh_post_merge_health.py, which defends the same path.
    env.pop("FACTORY_REPO", None)
    env.pop("DISCORD_WEBHOOK_URL", None)
    env["FACTORY_ALERT_STATE_PATH"] = str(tmp_path / "alert-state.json")

    result = subprocess.run(
        ["bash", str(MERGE_PR_SH), "500", *args],
        capture_output=True,
        env=env,
        text=True,
    )
    return result, merge_log_file


def _merge_call_args(merge_log_file: pathlib.Path) -> list[str]:
    raw = merge_log_file.read_bytes()
    parts = raw.split(b"\0")
    if parts and parts[-1] == b"":
        parts.pop()
    return [part.decode() for part in parts]


# --- no recognizable verdict: refused before any merge call -----------------


def test_no_verdict_refuses_before_merge_and_names_the_gap(tmp_path):
    result, merge_log_file = _run(
        tmp_path,
        body="Summary.\n\nChange kind: structural\n",
    )

    assert result.returncode != 0
    assert not merge_log_file.exists()
    assert "no recognizable release-gate verdict" in result.stderr
    assert "Release-gate: MERGE" in result.stderr
    assert "Release-gate: MERGE-WITH-CHANGES" in result.stderr
    assert "Release-gate: DO-NOT-MERGE" in result.stderr


def test_malformed_verdict_line_is_treated_as_no_verdict(tmp_path):
    result, merge_log_file = _run(
        tmp_path,
        body="Summary.\n\nChange kind: structural\n\nRelease-gate: MAYBE\n",
    )

    assert result.returncode != 0
    assert not merge_log_file.exists()
    assert "no recognizable release-gate verdict" in result.stderr


# --- dev.finding 807baee1, AC-3: the refusal says WHERE a verdict belongs --


def test_no_verdict_refusal_names_the_comment_only_rule_for_a_dispatcher_shaped_body(tmp_path):
    """A PR that declares a dev.task bead id (a factory PR) must be told a
    verdict is recorded only as a PR comment, never in the body -- the body
    is the worker's own writable surface (dev.finding 807baee1)."""
    bead_id = "11111111-1111-4111-8111-111111111111"
    result, merge_log_file = _run(
        tmp_path,
        body=f"Dispatched-bead id: {bead_id}\n\nChange kind: structural\n",
    )

    assert result.returncode != 0
    assert not merge_log_file.exists()
    assert "no recognizable release-gate verdict" in result.stderr
    assert "a verdict is recorded only as a PR comment" in result.stderr
    assert "never in the body" in result.stderr


def test_no_verdict_refusal_names_the_comment_only_rule_for_an_attended_pr_too(tmp_path):
    """The attended-PR case is unchanged: the refusal still fires, and the
    notice (which is the same text regardless of PR shape) still carries the
    rule -- this is the control for the test above, proving the new line's
    presence does not depend on, or otherwise disturb, the attended path."""
    result, merge_log_file = _run(
        tmp_path,
        body="Summary.\n\nChange kind: structural\n",
    )

    assert result.returncode != 0
    assert not merge_log_file.exists()
    assert "no recognizable release-gate verdict" in result.stderr
    assert "a verdict is recorded only as a PR comment" in result.stderr


# --- AC-4: the refusal explains itself when a near miss exists --------------


def test_decorated_verdict_refuses_and_explains_the_near_miss(tmp_path):
    """A decorated verdict (code span) refuses exactly like a genuinely
    missing one -- AC-4 changes what is printed, never what is permitted."""
    result, merge_log_file = _run(
        tmp_path,
        body="Summary.\n\nChange kind: structural\n\n`Release-gate: MERGE`\n",
    )

    assert result.returncode != 0
    assert not merge_log_file.exists()
    assert "no recognizable release-gate verdict" in result.stderr
    assert "found:    `Release-gate: MERGE`" in result.stderr
    assert "required: Release-gate: MERGE" in result.stderr


def test_trailing_pr_number_verdict_refuses_and_explains_the_near_miss(tmp_path):
    result, merge_log_file = _run(
        tmp_path,
        body="Summary.\n\nChange kind: structural\n\nRelease-gate: MERGE-WITH-CHANGES #853\n",
    )

    assert result.returncode != 0
    assert not merge_log_file.exists()
    assert "found:    Release-gate: MERGE-WITH-CHANGES #853" in result.stderr
    assert "required: Release-gate: MERGE-WITH-CHANGES" in result.stderr


def test_illegal_value_verdict_refuses_without_a_guessed_value(tmp_path):
    """LGTM never mentions a legal value, so the near-miss block names the
    line but never guesses a verdict for it (AC-3 applied to the shell
    path)."""
    result, merge_log_file = _run(
        tmp_path,
        body="Summary.\n\nChange kind: structural\n\nRelease-gate: LGTM\n",
    )

    assert result.returncode != 0
    assert not merge_log_file.exists()
    assert "no recognizable release-gate verdict" in result.stderr
    assert "Release-gate: LGTM" in result.stderr
    assert "no recognised release-gate verdict value" in result.stderr


# --- AC-2: the generic worked example pins the revision, adjacently --------


def test_verdict_notice_worked_example_shows_adjacent_revision_line(tmp_path):
    """The generic notice's only copy-pasteable command used to be
    --override-verdict, next to an example with no revision line at all --
    exactly the shape that merges silently and leaves no trace. The example
    must now show `Release-gate-revision:` on the line immediately after
    `Release-gate: MERGE`, since the parser only reads the very next line
    (a blank line in between would teach the wrong thing)."""
    result, merge_log_file = _run(
        tmp_path,
        body="Summary.\n\nChange kind: structural\n",
    )

    assert result.returncode != 0
    assert not merge_log_file.exists()
    assert "  Release-gate: MERGE\n  Release-gate-revision: <sha>\n" in result.stderr


# --- AC-3: no recorded revision still merges, unchanged, no new trace ------


def test_no_recorded_revision_still_merges_silently_with_no_new_stamp(tmp_path):
    """824c352e's AC-2 carried forward verbatim: every verdict recorded
    before the Release-gate-revision convention existed has no revision
    line, and refusing those would strand the historical backlog. This bead
    changes which remedy is recommended on a refusal -- it must not add a
    warning or a stamp to a verdict that still has nothing to compare."""
    result, merge_log_file = _run(
        tmp_path,
        body="Summary.\n\nChange kind: structural\n\nRelease-gate: MERGE\n",
        head_sha="ac3584d8fabc0000000000000000000000000000",
    )

    assert result.returncode == 0
    args = _merge_call_args(merge_log_file)
    merge_body = args[args.index("--body") + 1]
    assert merge_body == "Change kind: structural"
    assert "Verdict-override" not in merge_body
    # Assert the ABSENCE OF WHAT AC-3 NAMES, not that stderr is globally empty.
    # `assert result.stderr == ""` asserted a property of every subsystem that
    # writes to stderr, not the property under test, and it broke the moment
    # #924 added the post-merge health check -- whose skip line ("could not
    # determine OWNER/REPO ... skipping.") is DELIBERATE and is itself pinned by
    # test_merge_pr_sh_post_merge_health.py's
    # test_the_health_check_never_turns_a_successful_merge_into_a_failure. That
    # notice is a "this check did not run" statement, which CLAUDE.md requires;
    # silencing it, or setting FACTORY_REPO to dodge it, would re-open the
    # hazard the harness pops FACTORY_REPO to avoid (reaching the real Discord
    # webhook and the real alert-state file).
    assert "WARNING" not in result.stderr
    assert "Release-gate" not in result.stderr
    assert "Verdict-override" not in result.stderr


# --- DO-NOT-MERGE: refused, naming the verdict -------------------------------


def test_do_not_merge_verdict_refuses_before_merge_and_names_it(tmp_path):
    result, merge_log_file = _run(
        tmp_path,
        body="Summary.\n\nChange kind: structural\n\nRelease-gate: DO-NOT-MERGE\n",
    )

    assert result.returncode != 0
    assert not merge_log_file.exists()
    assert "DO-NOT-MERGE" in result.stderr


def test_do_not_merge_recorded_in_a_comment_also_refuses(tmp_path):
    result, merge_log_file = _run(
        tmp_path,
        body="Summary.\n\nChange kind: structural\n",
        comments=[{"body": "Release-gate: DO-NOT-MERGE"}],
    )

    assert result.returncode != 0
    assert not merge_log_file.exists()
    assert "DO-NOT-MERGE" in result.stderr


# --- MERGE / MERGE-WITH-CHANGES: proceeds exactly as today ------------------


def test_merge_verdict_proceeds_and_squash_body_unchanged(tmp_path):
    result, merge_log_file = _run(
        tmp_path,
        body=(
            "Summary.\n\nChange kind: behavioral\n\n"
            "Outer-loop: claude/foo\nDispatched-bead id: bead-123\n\n"
            "Release-gate: MERGE\n"
        ),
    )

    assert result.returncode == 0
    args = _merge_call_args(merge_log_file)
    assert "--body" in args
    merge_body = args[args.index("--body") + 1]
    assert merge_body == (
        "Change kind: behavioral\nOuter-loop: claude/foo\nDispatched-bead id: bead-123"
    )
    assert "--delete-branch" not in args


def test_merge_with_changes_verdict_in_a_comment_proceeds(tmp_path):
    result, merge_log_file = _run(
        tmp_path,
        body="Summary.\n\nChange kind: structural\n",
        comments=[{"body": "Release-gate: MERGE-WITH-CHANGES"}],
    )

    assert result.returncode == 0
    args = _merge_call_args(merge_log_file)
    merge_body = args[args.index("--body") + 1]
    assert merge_body == "Change kind: structural"
    assert "Verdict-override" not in merge_body


def test_comment_verdict_with_matching_revision_on_a_dispatcher_shaped_pr_proceeds(tmp_path):
    """AC-4: the revision binding (#936) still works when the verdict and
    revision are recorded as a comment on a PR that declares a dev.task bead
    id -- the only shape such a PR's verdict may now take (AC-2)."""
    bead_id = "11111111-1111-4111-8111-111111111111"
    head_sha = "ac3584d8fabc0000000000000000000000000000"
    result, merge_log_file = _run(
        tmp_path,
        body=f"Dispatched-bead id: {bead_id}\n\nChange kind: structural\n",
        comments=[{"body": f"Release-gate: MERGE\nRelease-gate-revision: {head_sha}"}],
        head_sha=head_sha,
    )

    assert result.returncode == 0
    args = _merge_call_args(merge_log_file)
    merge_body = args[args.index("--body") + 1]
    assert merge_body == f"Change kind: structural\nDispatched-bead id: {bead_id}"


# --- recorded revision: a MERGE-WITH-CHANGES verdict judged against the
# --- PR's still-current head is refused; every other shape proceeds --------


def test_merge_with_changes_revision_equal_to_head_refuses(tmp_path):
    """The tree has not moved since the verdict was judged, so the changes
    it required cannot have been made."""
    result, merge_log_file = _run(
        tmp_path,
        body=(
            "Summary.\n\nChange kind: structural\n\n"
            "Release-gate: MERGE-WITH-CHANGES\n"
            "Release-gate-revision: ac3584d8fabc0000000000000000000000000000\n"
        ),
        head_sha="ac3584d8fabc0000000000000000000000000000",
    )

    assert result.returncode != 0
    assert not merge_log_file.exists()
    assert "MERGE-WITH-CHANGES" in result.stderr
    assert "ac3584d8fabc0000000000000000000000000000" in result.stderr
    assert "has not moved" in result.stderr


def test_merge_with_changes_revision_equal_to_head_names_re_gate_remedy(tmp_path):
    """AC-1/AC-5: the refusal's only actionable line must not be the bare
    override flag -- it must name the traceable remedy (push the changes,
    re-gate the resulting head, pin it with an adjacent revision line), and
    that instruction must be checkable in the rendered text, not just by
    exit code."""
    result, merge_log_file = _run(
        tmp_path,
        body=(
            "Summary.\n\nChange kind: structural\n\n"
            "Release-gate: MERGE-WITH-CHANGES\n"
            "Release-gate-revision: ac3584d8fabc0000000000000000000000000000\n"
        ),
        head_sha="ac3584d8fabc0000000000000000000000000000",
    )

    assert result.returncode != 0
    assert not merge_log_file.exists()
    assert "re-gate the new head" in result.stderr
    assert "Release-gate-revision:" in result.stderr


def test_merge_with_changes_revision_matches_head_by_abbreviated_prefix(tmp_path):
    """A gate report already writes an abbreviated sha; matching is by
    prefix, not exact equality."""
    result, merge_log_file = _run(
        tmp_path,
        body=(
            "Summary.\n\nChange kind: structural\n\n"
            "Release-gate: MERGE-WITH-CHANGES\n"
            "Release-gate-revision: ac3584d8\n"
        ),
        head_sha="ac3584d8fabc0000000000000000000000000000",
    )

    assert result.returncode != 0
    assert not merge_log_file.exists()
    assert "ac3584d8" in result.stderr


def test_merge_with_changes_revision_different_from_head_proceeds(tmp_path):
    """The tree HAS moved since the verdict was judged, so the changes may
    genuinely have been made -- this is judgment, not this check's job."""
    result, merge_log_file = _run(
        tmp_path,
        body=(
            "Summary.\n\nChange kind: structural\n\n"
            "Release-gate: MERGE-WITH-CHANGES\n"
            "Release-gate-revision: ac3584d8\n"
        ),
        head_sha="bb37e27eaaaa0000000000000000000000000000",
    )

    assert result.returncode == 0
    args = _merge_call_args(merge_log_file)
    merge_body = args[args.index("--body") + 1]
    assert merge_body == "Change kind: structural"


def test_merge_with_changes_no_recorded_revision_proceeds(tmp_path):
    """The compatibility case: every verdict recorded before this convention
    existed has no revision line and must remain fully valid."""
    result, merge_log_file = _run(
        tmp_path,
        body="Summary.\n\nChange kind: structural\n\nRelease-gate: MERGE-WITH-CHANGES\n",
        head_sha="ac3584d8fabc0000000000000000000000000000",
    )

    assert result.returncode == 0
    assert merge_log_file.exists()


def test_merge_verdict_with_revision_equal_to_head_proceeds(tmp_path):
    """MERGE means this exact tree is fit to merge: an unmoved tree is
    exactly what the verdict approved, so it proceeds. Matching is by
    prefix, not exact equality, same as the MERGE-WITH-CHANGES check."""
    result, merge_log_file = _run(
        tmp_path,
        body=(
            "Summary.\n\nChange kind: structural\n\n"
            "Release-gate: MERGE\n"
            "Release-gate-revision: ac3584d8\n"
        ),
        head_sha="ac3584d8fabc0000000000000000000000000000",
    )

    assert result.returncode == 0
    assert merge_log_file.exists()


# --- MERGE verdict against a recorded revision: the mirror image of the
# --- MERGE-WITH-CHANGES check above. MERGE asserts "this exact tree is fit
# --- to merge" -- a later push means the tested tree is not the tree being
# --- merged, so this refuses when MERGE-WITH-CHANGES would proceed. -------


def test_merge_verdict_no_recorded_revision_proceeds(tmp_path):
    """The compatibility case: every verdict recorded before this
    convention existed has no revision line and must remain fully valid."""
    result, merge_log_file = _run(
        tmp_path,
        body="Summary.\n\nChange kind: structural\n\nRelease-gate: MERGE\n",
        head_sha="bb37e27eaaaa0000000000000000000000000000",
    )

    assert result.returncode == 0
    assert merge_log_file.exists()


def test_merge_verdict_revision_different_from_head_refuses(tmp_path):
    """The tree has moved since the verdict was judged: the gate approved a
    tree that no longer exists, not the one about to be merged."""
    result, merge_log_file = _run(
        tmp_path,
        body=(
            "Summary.\n\nChange kind: structural\n\n"
            "Release-gate: MERGE\n"
            "Release-gate-revision: 6ac343fc\n"
        ),
        head_sha="902eb427aaaa0000000000000000000000000000",
    )

    assert result.returncode != 0
    assert not merge_log_file.exists()
    assert "MERGE" in result.stderr
    assert "6ac343fc" in result.stderr
    assert "902eb427aaaa0000000000000000000000000000" in result.stderr
    assert "has moved" in result.stderr


def test_merge_verdict_revision_different_from_head_names_re_gate_remedy(tmp_path):
    """AC-1/AC-5: same shape as the MERGE-WITH-CHANGES case, but here the new
    head is already known, so the remedy names it directly rather than with
    a placeholder."""
    result, merge_log_file = _run(
        tmp_path,
        body=(
            "Summary.\n\nChange kind: structural\n\n"
            "Release-gate: MERGE\n"
            "Release-gate-revision: 6ac343fc\n"
        ),
        head_sha="902eb427aaaa0000000000000000000000000000",
    )

    assert result.returncode != 0
    assert not merge_log_file.exists()
    assert "Re-gate head 902eb427aaaa0000000000000000000000000000" in result.stderr
    assert "Release-gate-revision: 902eb427aaaa0000000000000000000000000000" in result.stderr


def test_merge_verdict_revision_different_override_proceeds_and_stamps_reason(tmp_path):
    result, merge_log_file = _run(
        tmp_path,
        "--override-verdict",
        "the Operator: confirmed the follow-up push is still what was reviewed, see PR discussion",
        body=(
            "Summary.\n\nChange kind: structural\n\n"
            "Release-gate: MERGE\n"
            "Release-gate-revision: 6ac343fc\n"
        ),
        head_sha="902eb427aaaa0000000000000000000000000000",
    )

    assert result.returncode == 0
    args = _merge_call_args(merge_log_file)
    merge_body = args[args.index("--body") + 1]
    assert (
        "Verdict-override: the Operator: confirmed the follow-up push is still what was reviewed, see PR discussion"
        in merge_body
    )
    assert "proceeding under override" in result.stderr


def test_merge_verdict_revision_check_skipped_when_head_unresolvable(tmp_path):
    """An unmeasured condition must not become a refusal: when the current
    head can't be determined, the check is skipped rather than guessed."""
    result, merge_log_file = _run(
        tmp_path,
        body=(
            "Summary.\n\nChange kind: structural\n\n"
            "Release-gate: MERGE\n"
            "Release-gate-revision: 6ac343fc\n"
        ),
        fail_head_lookup=True,
    )

    assert result.returncode == 0
    assert merge_log_file.exists()


def test_merge_verdict_revision_check_never_makes_a_network_call_without_a_revision(tmp_path):
    """No revision recorded means the headRefOid lookup must never be
    attempted at all -- proven by making that lookup itself fail loudly,
    which would surface as a merge-pr.sh failure if it were ever called."""
    result, merge_log_file = _run(
        tmp_path,
        body="Summary.\n\nChange kind: structural\n\nRelease-gate: MERGE\n",
        fail_head_lookup=True,
    )

    assert result.returncode == 0
    assert merge_log_file.exists()


def test_merge_with_changes_revision_check_skipped_when_head_unresolvable(tmp_path):
    """An unmeasured condition must not become a refusal: when the current
    head can't be determined, the check is skipped rather than guessed."""
    result, merge_log_file = _run(
        tmp_path,
        body=(
            "Summary.\n\nChange kind: structural\n\n"
            "Release-gate: MERGE-WITH-CHANGES\n"
            "Release-gate-revision: ac3584d8\n"
        ),
        fail_head_lookup=True,
    )

    assert result.returncode == 0
    assert merge_log_file.exists()


def test_merge_with_changes_revision_check_never_makes_a_network_call_without_a_revision(tmp_path):
    """No revision recorded means the headRefOid lookup must never be
    attempted at all -- proven by making that lookup itself fail loudly,
    which would surface as a merge-pr.sh failure if it were ever called."""
    result, merge_log_file = _run(
        tmp_path,
        body="Summary.\n\nChange kind: structural\n\nRelease-gate: MERGE-WITH-CHANGES\n",
        fail_head_lookup=True,
    )

    assert result.returncode == 0
    assert merge_log_file.exists()


def test_merge_with_changes_revision_line_is_not_mistaken_for_a_stale_near_miss(tmp_path):
    """F1/F4 regression: a compliant `Release-gate-revision:` line must not
    be read as an unparseable release-gate *mention* by the staleness
    diagnostic -- that would refuse every verdict recorded under this
    convention, whether or not the tree actually moved."""
    result, merge_log_file = _run(
        tmp_path,
        body=(
            "Summary.\n\nChange kind: structural\n\n"
            "Release-gate: MERGE-WITH-CHANGES\n"
            "Release-gate-revision: ac3584d8\n"
        ),
        head_sha="bb37e27eaaaa0000000000000000000000000000",
    )

    assert result.returncode == 0
    assert merge_log_file.exists()
    assert "does not parse as one" not in result.stderr


def test_merge_with_changes_revision_override_proceeds_and_stamps_reason(tmp_path):
    result, merge_log_file = _run(
        tmp_path,
        "--override-verdict",
        "the Operator: confirmed the changes were made in a follow-up push, see PR discussion",
        body=(
            "Summary.\n\nChange kind: structural\n\n"
            "Release-gate: MERGE-WITH-CHANGES\n"
            "Release-gate-revision: ac3584d8fabc0000000000000000000000000000\n"
        ),
        head_sha="ac3584d8fabc0000000000000000000000000000",
    )

    assert result.returncode == 0
    args = _merge_call_args(merge_log_file)
    merge_body = args[args.index("--body") + 1]
    assert (
        "Verdict-override: the Operator: confirmed the changes were made in a follow-up push, see PR discussion"
        in merge_body
    )
    assert "proceeding under override" in result.stderr


def test_later_recorded_verdict_supersedes_earlier_one(tmp_path):
    """An earlier DO-NOT-MERGE followed by a later MERGE must proceed — the most
    recent recorded judgment governs."""
    result, merge_log_file = _run(
        tmp_path,
        body="Summary.\n\nChange kind: structural\n",
        comments=[{"body": "Release-gate: DO-NOT-MERGE"}, {"body": "Release-gate: MERGE"}],
    )

    assert result.returncode == 0
    assert merge_log_file.exists()


# --- a later unreadable verdict blocks a merge that would otherwise proceed --


def test_later_decorated_verdict_after_a_readable_merge_refuses(tmp_path):
    """FACT AT FILING: this exact pair of comments merges on today's main.
    A later decorated DO-NOT-MERGE means a later verdict may have been
    recorded and lost to decoration — the recognised MERGE must not be
    trusted."""
    result, merge_log_file = _run(
        tmp_path,
        body="Summary.\n\nChange kind: structural\n",
        comments=[
            {"body": "Release-gate: MERGE"},
            {"body": "`Release-gate: DO-NOT-MERGE`"},
        ],
    )

    assert result.returncode != 0
    assert not merge_log_file.exists()
    assert "does not parse as one" in result.stderr
    assert "`Release-gate: DO-NOT-MERGE`" in result.stderr


def test_later_heading_form_retraction_after_a_readable_merge_refuses(tmp_path):
    """R1: a retraction written as a markdown heading (`## Release-gate:
    DO-NOT-MERGE`) must block a later merge exactly like the code-span form
    does -- the staleness path must not depend on the diagnostic-only
    _RELEASE_GATE_MENTION_RE, which #858 narrowed and would otherwise miss
    this shape entirely."""
    result, merge_log_file = _run(
        tmp_path,
        body="Summary.\n\nChange kind: structural\n",
        comments=[
            {"body": "Release-gate: MERGE"},
            {"body": "## Release-gate: DO-NOT-MERGE"},
        ],
    )

    assert result.returncode != 0
    assert not merge_log_file.exists()
    assert "does not parse as one" in result.stderr
    assert "## Release-gate: DO-NOT-MERGE" in result.stderr


def test_later_blockquote_form_retraction_after_a_readable_merge_refuses(tmp_path):
    """R1: same hazard, blockquote form."""
    result, merge_log_file = _run(
        tmp_path,
        body="Summary.\n\nChange kind: structural\n",
        comments=[
            {"body": "Release-gate: MERGE"},
            {"body": "> Release-gate: DO-NOT-MERGE"},
        ],
    )

    assert result.returncode != 0
    assert not merge_log_file.exists()
    assert "does not parse as one" in result.stderr
    assert "> Release-gate: DO-NOT-MERGE" in result.stderr


def test_decorated_verdict_before_a_readable_merge_still_proceeds(tmp_path):
    """Order decides, not presence: the same near miss, recorded BEFORE the
    readable MERGE, must not block it — a fix that refuses whenever any near
    miss exists anywhere would wrongly block this."""
    result, merge_log_file = _run(
        tmp_path,
        body="Summary.\n\nChange kind: structural\n",
        comments=[
            {"body": "`Release-gate: DO-NOT-MERGE`"},
            {"body": "Release-gate: MERGE"},
        ],
    )

    assert result.returncode == 0
    assert merge_log_file.exists()


def test_stale_near_miss_refusal_message_differs_from_no_verdict_refusal_message(tmp_path):
    """An operator must be able to tell the two refusals apart from stderr
    alone."""
    stale_result, _ = _run(
        tmp_path,
        body="Summary.\n\nChange kind: structural\n",
        comments=[
            {"body": "Release-gate: MERGE"},
            {"body": "`Release-gate: DO-NOT-MERGE`"},
        ],
    )
    no_verdict_result, _ = _run(
        tmp_path,
        body="Summary.\n\nChange kind: structural\n",
    )

    assert stale_result.returncode != 0
    assert no_verdict_result.returncode != 0
    assert "no recognizable release-gate verdict" not in stale_result.stderr
    assert "does not parse as one" not in no_verdict_result.stderr


def test_stale_near_miss_override_proceeds_and_warns(tmp_path):
    result, merge_log_file = _run(
        tmp_path,
        "--override-verdict",
        "the Operator: confirmed MERGE is still current, see PR discussion",
        body="Summary.\n\nChange kind: structural\n",
        comments=[
            {"body": "Release-gate: MERGE"},
            {"body": "`Release-gate: DO-NOT-MERGE`"},
        ],
    )

    assert result.returncode == 0
    assert merge_log_file.exists()
    assert "proceeding under override" in result.stderr


# --- regression corpus: refuses strictly more, merges no more ---------------


MERGED_TODAY = {
    "merge_verdict_in_body": (
        "Summary.\n\nChange kind: structural\n\nRelease-gate: MERGE\n",
        None,
    ),
    "merge_with_changes_in_comment": (
        "Summary.\n\nChange kind: structural\n",
        [{"body": "Release-gate: MERGE-WITH-CHANGES"}],
    ),
    "later_readable_verdict_supersedes_earlier_readable": (
        "Summary.\n\nChange kind: structural\n",
        [{"body": "Release-gate: DO-NOT-MERGE"}, {"body": "Release-gate: MERGE"}],
    ),
    "near_miss_before_readable_merge_does_not_block": (
        "Summary.\n\nChange kind: structural\n",
        [{"body": "`Release-gate: DO-NOT-MERGE`"}, {"body": "Release-gate: MERGE"}],
    ),
}

REFUSED_TODAY = {
    "no_verdict": ("Summary.\n\nChange kind: structural\n", None),
    "malformed_verdict": (
        "Summary.\n\nChange kind: structural\n\nRelease-gate: MAYBE\n",
        None,
    ),
    "decorated_verdict_only": (
        "Summary.\n\nChange kind: structural\n\n`Release-gate: MERGE`\n",
        None,
    ),
    "do_not_merge": (
        "Summary.\n\nChange kind: structural\n\nRelease-gate: DO-NOT-MERGE\n",
        None,
    ),
    "do_not_merge_in_comment": (
        "Summary.\n\nChange kind: structural\n",
        [{"body": "Release-gate: DO-NOT-MERGE"}],
    ),
    "no_change_kind_line": ("Summary.\n\nRelease-gate: MERGE\n", None),
}

# NEW as of this change: was in MERGED_TODAY before the fix, now refused.
NEWLY_REFUSED = {
    "decorated_do_not_merge_after_readable_merge": (
        "Summary.\n\nChange kind: structural\n",
        [{"body": "Release-gate: MERGE"}, {"body": "`Release-gate: DO-NOT-MERGE`"}],
    ),
    "heading_form_do_not_merge_after_readable_merge": (
        "Summary.\n\nChange kind: structural\n",
        [{"body": "Release-gate: MERGE"}, {"body": "## Release-gate: DO-NOT-MERGE"}],
    ),
    "blockquote_form_do_not_merge_after_readable_merge": (
        "Summary.\n\nChange kind: structural\n",
        [{"body": "Release-gate: MERGE"}, {"body": "> Release-gate: DO-NOT-MERGE"}],
    ),
}


def test_regression_corpus_merges_no_more_and_refuses_strictly_more(tmp_path):
    """PR body states the merged/refused split this proves: merged count is
    unchanged (4), refused count rises from 6 to 9."""
    merged_count = 0
    for name, (body, comments) in MERGED_TODAY.items():
        case_dir = tmp_path / f"merged-{name}"
        case_dir.mkdir()
        result, merge_log_file = _run(case_dir, body=body, comments=comments)
        assert result.returncode == 0
        assert merge_log_file.exists()
        merged_count += 1

    refused_count = 0
    for name, (body, comments) in {**REFUSED_TODAY, **NEWLY_REFUSED}.items():
        case_dir = tmp_path / f"refused-{name}"
        case_dir.mkdir()
        result, merge_log_file = _run(case_dir, body=body, comments=comments)
        assert result.returncode != 0
        assert not merge_log_file.exists()
        refused_count += 1

    assert merged_count == len(MERGED_TODAY) == 4
    assert refused_count == len(REFUSED_TODAY) + len(NEWLY_REFUSED) == 9


# --- --override-verdict: requires a reason, stamps it into the commit body --


def test_override_without_reason_refuses(tmp_path):
    result, merge_log_file = _run(
        tmp_path,
        "--override-verdict",
        body="Summary.\n\nChange kind: structural\n",
    )

    assert result.returncode != 0
    assert not merge_log_file.exists()
    assert "requires a written reason" in result.stderr


def test_override_with_empty_reason_refuses(tmp_path):
    result, merge_log_file = _run(
        tmp_path,
        "--override-verdict",
        "",
        body="Summary.\n\nChange kind: structural\n",
    )

    assert result.returncode != 0
    assert not merge_log_file.exists()
    assert "requires a written reason" in result.stderr


def test_override_with_reason_proceeds_when_no_verdict_and_stamps_reason(tmp_path):
    result, merge_log_file = _run(
        tmp_path,
        "--override-verdict",
        "the Operator: shipping the hotfix now, verdict to follow",
        body="Summary.\n\nChange kind: structural\n",
    )

    assert result.returncode == 0
    args = _merge_call_args(merge_log_file)
    merge_body = args[args.index("--body") + 1]
    assert merge_body == (
        "Change kind: structural\n"
        "Verdict-override: the Operator: shipping the hotfix now, verdict to follow"
    )


def test_override_with_reason_proceeds_on_do_not_merge_and_stamps_reason(tmp_path):
    result, merge_log_file = _run(
        tmp_path,
        "--override-verdict",
        "the Operator: overriding red verdict, see incident #999",
        body="Summary.\n\nChange kind: behavioral\n\nRelease-gate: DO-NOT-MERGE\n",
    )

    assert result.returncode == 0
    args = _merge_call_args(merge_log_file)
    merge_body = args[args.index("--body") + 1]
    assert "Verdict-override: the Operator: overriding red verdict, see incident #999" in merge_body
    assert merge_body.startswith("Change kind: behavioral")


def test_override_flag_equals_form_is_accepted(tmp_path):
    result, merge_log_file = _run(
        tmp_path,
        "--override-verdict=the Operator: reason via equals form",
        body="Summary.\n\nChange kind: structural\n",
    )

    assert result.returncode == 0
    args = _merge_call_args(merge_log_file)
    merge_body = args[args.index("--body") + 1]
    assert "Verdict-override: the Operator: reason via equals form" in merge_body


# --- existing refusal (no Change kind line) is unaffected by the new gate ---


def test_change_kind_refusal_still_happens_when_verdict_is_present(tmp_path):
    result, merge_log_file = _run(
        tmp_path,
        body="Summary.\n\nRelease-gate: MERGE\n",
    )

    assert result.returncode != 0
    assert not merge_log_file.exists()
    assert "no bare 'Change kind:' line" in result.stderr
