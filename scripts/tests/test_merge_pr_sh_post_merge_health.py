"""End-to-end proof that scripts/merge-pr.sh's post-merge tripwire (AC3: per merge, not only at
the end) actually fires -- and only after a merge that happened.

Same PATH-stub pattern as scripts/tests/test_merge_pr_sh.py, extended to also answer `gh repo
view` and `gh workflow list`/`gh run list` (the calls scripts/post-merge-health-check.py itself
makes via apps/factory-dispatcher/workflow_run_health.py). No network: `FACTORY_ALERT_STATE_PATH`
is redirected to a throwaway file and `DISCORD_WEBHOOK_URL` is unset, so even the `UNREACHABLE`
path below -- which does reach the real declared alert policy -- never touches this host's real
dispatcher state or posts to a real webhook (`notify.post_discord` no-ops with no webhook URL
configured; see apps/mcp-hub/src/tools/notify.py).
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import textwrap


REPO = pathlib.Path(__file__).resolve().parents[2]
MERGE_PR_SH = REPO / "scripts" / "merge-pr.sh"

MERGEABLE_BODY = "Summary.\n\nChange kind: structural\n\nRelease-gate: MERGE\n"
REFUSED_BODY = "Summary.\n\nChange kind: structural\n"  # no verdict -- refused before any merge

GH_RUN = textwrap.dedent(
    """\
    #!/usr/bin/env bash
    set -euo pipefail

    read_json_field() {
      # Scans the remaining args for `--json <value>`, printing it (or "").
      while [ $# -gt 0 ]; do
        case "$1" in
          --json) printf '%s' "$2"; return 0 ;;
          *) shift ;;
        esac
      done
      printf ''
    }

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
        title) cat "__TITLE_FILE__" ;;
        body) cat "__BODY_FILE__" ;;
        comments) cat "__COMMENTS_FILE__" ;;
        headRefOid) printf '%s\\n' "unused-head-sha" ;;
        *) echo "gh stub: unexpected --json field: $json_field" >&2; exit 64 ;;
      esac
      exit 0
    fi

    if [ "$1" = "pr" ] && [ "$2" = "merge" ]; then
      shift 2
      printf '%s\\0' "$@" > "__MERGE_LOG_FILE__"
      exit 0
    fi

    if [ "$1" = "repo" ] && [ "$2" = "view" ]; then
      printf 'called\\n' >> "__REPO_VIEW_LOG_FILE__"
      shift 2
      json_field=$(read_json_field "$@")
      case "$json_field" in
        nameWithOwner) printf '"%s"\\n' "__REPO_SLUG__" ;;
        defaultBranchRef) printf '{"defaultBranchRef": {"name": "__DEFAULT_BRANCH__"}}\\n' ;;
        *) echo "gh stub: unexpected repo view --json field: $json_field" >&2; exit 64 ;;
      esac
      exit 0
    fi

    if [ "$1" = "workflow" ] && [ "$2" = "list" ]; then
      printf '%s\\n' '__WORKFLOW_NAMES_JSON__'
      exit 0
    fi

    if [ "$1" = "run" ] && [ "$2" = "list" ]; then
      shift 2
      workflow=""
      while [ $# -gt 0 ]; do
        case "$1" in
          --workflow) workflow="$2"; shift 2 ;;
          *) shift ;;
        esac
      done
      case "$workflow" in
    __RUN_LIST_CASES__
        *) echo "gh stub: unexpected --workflow: $workflow" >&2; exit 64 ;;
      esac
      exit 0
    fi

    echo "gh stub: unexpected invocation: $*" >&2
    exit 64
    """
)


def _write_gh_stub(
    bin_dir: pathlib.Path,
    *,
    title_file,
    body_file,
    comments_file,
    merge_log_file,
    repo_view_log_file,
    repo_slug: str = "example-owner/example-repo",
    default_branch: str = "main",
    workflow_names: list[str] = (),
    runs_by_workflow: dict[str, list[dict]] | None = None,
) -> None:
    gh = bin_dir / "gh"
    workflow_names_json = json.dumps([{"name": n} for n in workflow_names])
    runs_by_workflow = runs_by_workflow or {}
    cases = []
    for name, runs in runs_by_workflow.items():
        cases.append(f'        "{name}") printf \'%s\\n\' \'{json.dumps(runs)}\' ;;')
    for name in workflow_names:
        if name not in runs_by_workflow:
            cases.append(f'        "{name}") printf \'%s\\n\' \'[]\' ;;')
    run_list_cases = "\n".join(cases)

    script = (
        GH_RUN.replace("__TITLE_FILE__", str(title_file))
        .replace("__BODY_FILE__", str(body_file))
        .replace("__COMMENTS_FILE__", str(comments_file))
        .replace("__MERGE_LOG_FILE__", str(merge_log_file))
        .replace("__REPO_VIEW_LOG_FILE__", str(repo_view_log_file))
        .replace("__REPO_SLUG__", repo_slug)
        .replace("__DEFAULT_BRANCH__", default_branch)
        .replace("__WORKFLOW_NAMES_JSON__", workflow_names_json)
        .replace("__RUN_LIST_CASES__", run_list_cases)
    )
    gh.write_text(script)
    os.chmod(gh, 0o755)


def _run(
    tmp_path: pathlib.Path,
    *,
    body: str,
    workflow_names: list[str] = (),
    runs_by_workflow: dict[str, list[dict]] | None = None,
):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)

    title_file = tmp_path / "title.txt"
    body_file = tmp_path / "body.txt"
    comments_file = tmp_path / "comments.json"
    merge_log_file = tmp_path / "merge-call.log"
    repo_view_log_file = tmp_path / "repo-view.log"

    title_file.write_text("Some PR title")
    body_file.write_text(body)
    comments_file.write_text(json.dumps([]))

    _write_gh_stub(
        bin_dir,
        title_file=title_file,
        body_file=body_file,
        comments_file=comments_file,
        merge_log_file=merge_log_file,
        repo_view_log_file=repo_view_log_file,
        workflow_names=workflow_names,
        runs_by_workflow=runs_by_workflow,
    )

    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    env.pop("FACTORY_REPO", None)
    env.pop("DISCORD_WEBHOOK_URL", None)
    env["FACTORY_ALERT_STATE_PATH"] = str(tmp_path / "alert-state.json")

    result = subprocess.run(
        ["bash", str(MERGE_PR_SH), "500"],
        capture_output=True,
        env=env,
        text=True,
    )
    return result, merge_log_file, repo_view_log_file


def _completed_run(workflow_name: str, conclusion: str) -> dict:
    return {
        "databaseId": "1",
        "workflowName": workflow_name,
        "status": "completed",
        "conclusion": conclusion,
        "updatedAt": "2026-09-17T00:00:00Z",
        "url": "https://github.com/example/repo/actions/runs/1",
    }


def test_a_successful_merge_runs_the_post_merge_health_check(tmp_path):
    """AC3: the check fires per merge, evidenced by its own recognisable status line -- not
    deferred to a 15-minute schedule or to the end of a batch."""
    result, merge_log_file, repo_view_log_file = _run(tmp_path, body=MERGEABLE_BODY)

    assert result.returncode == 0
    assert merge_log_file.exists()
    assert repo_view_log_file.exists(), "post-merge health check must have called `gh repo view`"
    assert "[main-health]" in result.stderr


def test_a_repo_with_no_defined_workflows_reads_unmeasured_not_green(tmp_path):
    """Fail closed: zero observed workflows must never be reported as green."""
    result, merge_log_file, _ = _run(tmp_path, body=MERGEABLE_BODY, workflow_names=[])

    assert result.returncode == 0
    assert merge_log_file.exists()
    assert "[main-health] UNMEASURED" in result.stderr


def test_a_confirmed_green_workflow_reads_green(tmp_path):
    result, merge_log_file, _ = _run(
        tmp_path,
        body=MERGEABLE_BODY,
        workflow_names=["Lint & Validate"],
        runs_by_workflow={"Lint & Validate": [_completed_run("Lint & Validate", "success")]},
    )

    assert result.returncode == 0
    assert merge_log_file.exists()
    assert "[main-health] GREEN" in result.stderr


def test_a_failed_workflow_reads_red(tmp_path):
    """The shape of the 2026-09-17 incident: a required workflow's most recent completed run on
    main failed. Discord delivery is disabled in this test env (`DISCORD_WEBHOOK_URL` unset), so
    the real declared alert policy runs end-to-end and no-ops at the transport step only."""
    result, merge_log_file, _ = _run(
        tmp_path,
        body=MERGEABLE_BODY,
        workflow_names=["Lint & Validate"],
        runs_by_workflow={"Lint & Validate": [_completed_run("Lint & Validate", "failure")]},
    )

    assert result.returncode == 0, "a red post-merge check must never fail the merge itself"
    assert merge_log_file.exists()
    assert "[main-health] RED: Lint & Validate" in result.stderr


def test_a_refused_merge_never_invokes_gh_repo_view(tmp_path):
    """AC5: the existing refusal is untouched, and the new post-merge step -- which the refusal
    must never reach -- proves it by never calling `gh repo view` at all."""
    result, merge_log_file, repo_view_log_file = _run(tmp_path, body=REFUSED_BODY)

    assert result.returncode != 0
    assert not merge_log_file.exists()
    assert not repo_view_log_file.exists()
    assert "[main-health]" not in result.stderr


def test_the_health_check_never_turns_a_successful_merge_into_a_failure(tmp_path):
    """Even when the health check itself can't get past `gh repo view` (a bare, unstubbed
    failure exits 64), merge-pr.sh's own exit code must still reflect the merge, not the
    best-effort check tacked on after it."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    gh = bin_dir / "gh"
    title_file = tmp_path / "title.txt"
    body_file = tmp_path / "body.txt"
    comments_file = tmp_path / "comments.json"
    merge_log_file = tmp_path / "merge-call.log"
    title_file.write_text("Some PR title")
    body_file.write_text(MERGEABLE_BODY)
    comments_file.write_text("[]")
    gh.write_text(
        textwrap.dedent(
            f"""\
            #!/usr/bin/env bash
            set -euo pipefail
            if [ "$1" = "pr" ] && [ "$2" = "view" ]; then
              shift 2; shift
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
                headRefOid) printf '%s\\n' "unused-head-sha" ;;
                *) exit 64 ;;
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

    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    env.pop("FACTORY_REPO", None)
    env.pop("DISCORD_WEBHOOK_URL", None)
    env["FACTORY_ALERT_STATE_PATH"] = str(tmp_path / "alert-state.json")

    result = subprocess.run(
        ["bash", str(MERGE_PR_SH), "500"], capture_output=True, env=env, text=True
    )

    assert result.returncode == 0
    assert merge_log_file.exists()
    assert "could not determine OWNER/REPO" in result.stderr
