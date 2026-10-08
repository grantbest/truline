#!/usr/bin/env bash
# Squash-merge a PR so the durable commit message keeps what the PR body
# declared. `gh pr merge --squash --subject "..."` silently drops the body,
# which lost the Tidy-First `Change kind:` line on twelve merges across three
# separate days (2026-08-13, 2026-08-16, 2026-08-18) — each one an append to
# scripts/change-kind-grandfathered.txt. RULE 7 fails the NEXT branched PR,
# not the offending one, so by the time it fires the history is immutable.
#
# Usage: scripts/merge-pr.sh <pr-number> [--override-verdict "<reason>"]
#
# This wrapper is the merge path; a bare `gh pr merge` is the mistake.
# It deliberately does NOT pass --delete-branch: a PR whose branch is the
# base of a stacked PR gets closed forever by branch deletion (#328/#329).
# Delete branches explicitly after confirming nothing stacks on them.
#
# The JUDGMENT behind a release-gate verdict cannot be mechanized here — that
# stays human, per CLAUDE.md. But the EXISTENCE of a recorded verdict can be,
# and PC-FAC-005/AC-1 named the gap where it wasn't: this script refuses to
# merge when the PR carries no recognisable `Release-gate:` record (or a
# recorded DO-NOT-MERGE), using the single recognizer scripts/gate_markers.py
# defines. CLAUDE.md reserves bypassing a no-verdict or red merge to the Operator;
# --override-verdict is that escape hatch, and it requires a written reason
# that is stamped into the durable squash commit body.
#
# A no-verdict refusal also runs the PR through gate_markers.py's near-miss
# diagnostic before it exits: a decorated verdict (code span, bold, trailing
# PR number) parses as "no verdict" the same as a genuinely missing one, and
# without this the refusal gave no hint which of those it was. The
# diagnostic only ever adds to the printed refusal text — it is never
# consulted when deciding whether to merge.
#
# A recognised MERGE/MERGE-WITH-CHANGES verdict is also checked for
# staleness: gate_markers.py's `stale-near-miss` command looks for a line
# recorded AFTER that verdict (same body-then-comments order) that mentions
# a release gate but doesn't parse as one. That shape means a later verdict
# may have been attempted and lost to decoration — the recognised verdict is
# superseded-and-unreadable, not superseding, and this refuses rather than
# proceeding on a verdict that may no longer be current.
#
# A MERGE or MERGE-WITH-CHANGES verdict recorded with an adjacent
# `Release-gate-revision:` line (see gate_markers.py's module docstring for
# the convention) is additionally checked against the PR's current head —
# but the two verdicts want opposite answers to that comparison, because
# they assert opposite things about the tree:
#
#   MERGE-WITH-CHANGES asserts "make the changes and re-verify before
#   merging" per CLAUDE.md, not "merge and follow up" — so it is refused
#   when the revision STILL MATCHES head: the tree hasn't moved since the
#   verdict was judged, so the required changes cannot have been made.
#
#   MERGE asserts "this exact tree is fit to merge" — so it is refused
#   when the revision NO LONGER MATCHES head: the tree has moved since the
#   verdict was judged, so the verdict reviewed a tree that no longer
#   exists, not the one about to be merged.
#
# Either check only ever runs when a revision was actually recorded: a
# verdict with no revision line proceeds exactly as it always has (the
# compatibility case, since no verdict recorded before this convention
# existed has one), and an unresolvable current head skips the check
# rather than guessing.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() {
  echo "usage: scripts/merge-pr.sh <pr-number> [--override-verdict \"<reason>\"]" >&2
}

same_revision() {
  python3 "$SCRIPT_DIR/gate_markers.py" same-revision "$1" "$2"
}

pr=""
override_reason=""
while [ $# -gt 0 ]; do
  case "$1" in
    --override-verdict)
      if [ $# -lt 2 ] || [ -z "$2" ]; then
        echo "ERROR: --override-verdict requires a written reason." >&2
        exit 1
      fi
      override_reason="$2"
      shift 2
      ;;
    --override-verdict=*)
      override_reason="${1#--override-verdict=}"
      if [ -z "$override_reason" ]; then
        echo "ERROR: --override-verdict requires a written reason." >&2
        exit 1
      fi
      shift
      ;;
    -*)
      echo "ERROR: unrecognized flag: $1" >&2
      usage
      exit 1
      ;;
    *)
      if [ -n "$pr" ]; then
        echo "ERROR: unexpected extra argument: $1" >&2
        usage
        exit 1
      fi
      pr="$1"
      shift
      ;;
  esac
done

if [ -z "$pr" ]; then
  usage
  exit 1
fi

title=$(gh pr view "$pr" --json title -q .title)
body=$(gh pr view "$pr" --json body -q .body)
comments_json=$(gh pr view "$pr" --json comments -q .comments)

verdict=$(printf '%s' "$body" | python3 "$SCRIPT_DIR/gate_markers.py" verdict --comments-json "$comments_json")

verdict_notice="Record a release-gate verdict as a bare line in a PR comment or the PR body, with the revision it judged on the very next line:
  Release-gate: MERGE
  Release-gate-revision: <sha>
The revision line is optional; MERGE-WITH-CHANGES and DO-NOT-MERGE are also valid without it:
  Release-gate: MERGE-WITH-CHANGES
  Release-gate: DO-NOT-MERGE
On a PR that declares a dev.task bead (a factory PR), a verdict is recorded only as a PR comment -- never in the body, which the worker's own report populates (dev.finding 807baee1).
Bypassing this check is reserved to the Operator per CLAUDE.md:
  scripts/merge-pr.sh ${pr} --override-verdict \"<reason>\""

if [ -z "$verdict" ]; then
  if [ -n "$override_reason" ]; then
    echo "WARNING: PR #${pr} carries no recognizable release-gate verdict record — proceeding under override." >&2
  else
    echo "ERROR: PR #${pr} carries no recognizable release-gate verdict record." >&2
    echo "$verdict_notice" >&2
    near_misses=$(printf '%s' "$body" | python3 "$SCRIPT_DIR/gate_markers.py" near-misses --comments-json "$comments_json")
    if [ -n "$near_misses" ]; then
      echo "" >&2
      echo "Found a line that mentions a release gate but doesn't parse as one:" >&2
      echo "$near_misses" >&2
    fi
    exit 1
  fi
elif [ "$verdict" = "DO-NOT-MERGE" ]; then
  if [ -n "$override_reason" ]; then
    echo "WARNING: PR #${pr}'s recorded release-gate verdict is DO-NOT-MERGE — proceeding under override." >&2
  else
    echo "ERROR: PR #${pr}'s recorded release-gate verdict is DO-NOT-MERGE." >&2
    echo "$verdict_notice" >&2
    exit 1
  fi
else
  stale_diagnostic=""
  if ! stale_diagnostic=$(printf '%s' "$body" | python3 "$SCRIPT_DIR/gate_markers.py" stale-near-miss --comments-json "$comments_json"); then
    if [ -n "$override_reason" ]; then
      echo "WARNING: PR #${pr}'s recognized release-gate verdict (${verdict}) is followed by a later line that mentions a release gate but does not parse as one — proceeding under override." >&2
    else
      echo "ERROR: PR #${pr}'s recognized release-gate verdict (${verdict}) is followed by a later line that mentions a release gate but does not parse as one." >&2
      echo "A later verdict may have been recorded here and lost to decoration:" >&2
      echo "$stale_diagnostic" >&2
      echo "$verdict_notice" >&2
      exit 1
    fi
  fi

  if [ "$verdict" = "MERGE-WITH-CHANGES" ] || [ "$verdict" = "MERGE" ]; then
    revision=$(printf '%s' "$body" | python3 "$SCRIPT_DIR/gate_markers.py" revision --comments-json "$comments_json")
    if [ -n "$revision" ]; then
      head_sha=$(gh pr view "$pr" --json headRefOid -q .headRefOid 2>/dev/null || true)
      if [ -n "$head_sha" ]; then
        revision_matches_head=false
        if same_revision "$revision" "$head_sha"; then
          revision_matches_head=true
        fi

        if [ "$verdict" = "MERGE-WITH-CHANGES" ] && [ "$revision_matches_head" = true ]; then
          if [ -n "$override_reason" ]; then
            echo "WARNING: PR #${pr}'s MERGE-WITH-CHANGES verdict was recorded against revision ${revision}, and the current head is still ${head_sha} — the tree has not moved, so the required changes cannot have been made — proceeding under override." >&2
          else
            echo "ERROR: PR #${pr}'s MERGE-WITH-CHANGES verdict was recorded against revision ${revision}, and the current head is still ${head_sha}." >&2
            echo "The tree has not moved since that verdict was judged, so the changes it required cannot have been made." >&2
            echo "Push the required changes, then re-gate the new head and record a new verdict with the revision line immediately after it:" >&2
            echo "  Release-gate: MERGE" >&2
            echo "  Release-gate-revision: <sha>" >&2
            echo "$verdict_notice" >&2
            exit 1
          fi
        elif [ "$verdict" = "MERGE" ] && [ "$revision_matches_head" = false ]; then
          if [ -n "$override_reason" ]; then
            echo "WARNING: PR #${pr}'s MERGE verdict was recorded against revision ${revision}, and the current head is now ${head_sha} — the tree has moved since the verdict was judged, so the tested tree is not the tree being merged — proceeding under override." >&2
          else
            echo "ERROR: PR #${pr}'s MERGE verdict was recorded against revision ${revision}, and the current head is now ${head_sha}." >&2
            echo "The tree has moved since that verdict was judged: the tested tree is not the tree being merged." >&2
            echo "Re-gate head ${head_sha} and record a new verdict with the revision line immediately after it:" >&2
            echo "  Release-gate: MERGE" >&2
            echo "  Release-gate-revision: ${head_sha}" >&2
            echo "$verdict_notice" >&2
            exit 1
          fi
        fi
      fi
    fi
  fi
fi

# Same shape the change-kind CI job validates: a bare declaration line,
# outside code fences. Extract it rather than re-validating — CI already
# guaranteed exactly one on the PR; this preserves it.
declaration=$(printf '%s\n' "$body" | awk '
  /^```/ { in_fence = !in_fence; next }
  in_fence { next }
  /^[[:space:]]*Change kind:[[:space:]]*(structural|behavioral)[[:space:]]*$/ { print; exit }
')
if [ -z "$declaration" ]; then
  echo "ERROR: PR #${pr} body carries no bare 'Change kind:' line — fix the body first." >&2
  exit 1
fi

# Carry provenance lines the gate tooling reads, when present.
provenance=$(printf '%s\n' "$body" | grep -iE '^\s*(Outer-loop:|Dispatched-bead id:)' | head -2 || true)

merge_body="$declaration"
if [ -n "$provenance" ]; then
  merge_body="${merge_body}
${provenance}"
fi
if [ -n "$override_reason" ]; then
  merge_body="${merge_body}
Verdict-override: ${override_reason}"
fi

if gh pr merge "$pr" --squash \
  --subject "${title} (#${pr})" \
  --body "$merge_body"; then
  merge_status=0
else
  merge_status=$?
fi

if [ "$merge_status" -eq 0 ]; then
  # A batch of individually-green PRs can still turn main red as a SET (OBSERVED 2026-09-17: 21
  # merges, every one CLEAN pairwise and green on its own branch, main red anyway on a tree-wide
  # count no per-PR check could see). scripts/post-merge-health-check.py is the tripwire: it reuses
  # workflow_run_health.py's own already-tested detection and declared-alert machinery, fired here
  # instead of waiting for that module's 15-minute schedule (or, in a fast batch, however many
  # further merges land before the next tick). Best-effort and never fatal: this line runs strictly
  # after the merge above has already succeeded, so a `gh`/network hiccup in the health check must
  # never be reported as a failed merge.
  #
  # workflow_run_health.py's own import chain reaches apps/factory-dispatcher/dispatch.py's
  # Temporal activities (worker_revision -> dispatch -> activities.dispatch_steps), so it needs an
  # interpreter with `temporalio` installed -- the bare `python3` an operator's shell resolves is
  # not guaranteed to be one. This repo's own venv/ (installed for exactly this dependency set) is
  # preferred when present; scripts/factory-redeploy.py's `--python` default applies the identical
  # preference for the same reason.
  post_merge_python3="python3"
  if [ -x "$SCRIPT_DIR/../venv/bin/python3" ]; then
    post_merge_python3="$SCRIPT_DIR/../venv/bin/python3"
  fi
  "$post_merge_python3" "$SCRIPT_DIR/post-merge-health-check.py" || true
fi

exit "$merge_status"
