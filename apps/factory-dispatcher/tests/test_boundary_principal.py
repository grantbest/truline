"""Root-cause track control C-4 (docs/plans/2026-09-30-root-cause-track-
security-findings-2026-09-26.md): a spec whose scope touches a trust boundary
must declare the principal it runs as and the credentials that principal can
reach. file_task.BOUNDARY_PATHS is the one declared list; _paths_intersect
decides mechanically whether a spec's scope.paths touches it;
_check_boundary_principal, called first in build_content, refuses a boundary
spec missing principal/reaches.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

HERE = pathlib.Path(__file__).resolve().parent
DISPATCHER = HERE.parents[0]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(DISPATCHER))

from spawn_scan import find_spawn_calls  # noqa: E402

import file_task  # noqa: E402


# ---------------------------------------------------------------------------
# AC-1: the spawn-site half of BOUNDARY_PATHS stays complete.
# ---------------------------------------------------------------------------


def test_every_spawn_call_site_is_on_the_boundary_list():
    """A new file under apps/factory-dispatcher/ (non-test) that spawns a
    process must land on file_task.BOUNDARY_PATHS or this test goes red --
    the same AST walk test_spawn_inventory.py drives off of (spawn_scan.py),
    applied here to list-completeness rather than to the credential-ledger
    ratchet."""
    calls = find_spawn_calls(DISPATCHER)
    offenders = sorted(
        {call.path for call in calls}
        - {
            call.path
            for call in calls
            if file_task._first_boundary_intersection(
                [f"apps/factory-dispatcher/{call.path}"]
            )
            is not None
        }
    )
    assert offenders == [], (
        f"spawn call(s) found in file(s) not on file_task.BOUNDARY_PATHS: {offenders}"
    )


# ---------------------------------------------------------------------------
# AC-2: the intersection trigger is mechanical and component-wise.
# ---------------------------------------------------------------------------

_INTERSECTING = (
    "apps/factory-dispatcher/",
    "apps/factory-dispatcher/dispatch.py",
    "infrastructure/k8s/infra-ai/litellm.yaml",
    "apps/mcp-hub/src/*.py",
    "./apps/factory-dispatcher/containment.py",
    "apps/",
)

_NOT_INTERSECTING = (
    "apps/factory-dispatcher/tests/",
    "apps/factory-dispatcher/file_task.py",
    "docs/plans/x.md",
    "apps/factory-dispatcher/dispatch.py.bak",
    "apps/factory-dispatcher/launchd_agent_helpers.py",
    "infrastructure-notes/",
)


@pytest.mark.parametrize("scope_path", _INTERSECTING)
def test_scope_path_intersects_a_boundary_pattern(scope_path):
    assert file_task._first_boundary_intersection([scope_path]) is not None


@pytest.mark.parametrize("scope_path", _NOT_INTERSECTING)
def test_scope_path_does_not_intersect_any_boundary_pattern(scope_path):
    assert file_task._first_boundary_intersection([scope_path]) is None


# ---------------------------------------------------------------------------
# AC-3: the refusal.
# ---------------------------------------------------------------------------

_BOUNDARY_SCOPE = {"paths": ["apps/factory-dispatcher/dispatch.py"]}


def _spec(**overrides):
    spec = {
        "lane": "code-health",
        "title": "t",
        "intent": "i",
        "acceptance": ["THE thing SHALL happen"],
        "scope": {"paths": ["apps/x/"]},
        "risk_class": "behavioral",
        "requirement_refs_waived": "test fixture; exercises C-4 only",
        "release_ref_waived": "test fixture; exercises C-4 only",
    }
    spec.update(overrides)
    return spec


def test_boundary_spec_with_neither_field_is_refused():
    with pytest.raises(SystemExit) as excinfo:
        file_task.build_content(_spec(scope=_BOUNDARY_SCOPE))

    message = str(excinfo.value)
    assert "control C-4" in message
    assert "'apps/factory-dispatcher/dispatch.py'" in message
    assert "must declare principal and reaches" in message


def test_boundary_spec_with_only_principal_is_refused_naming_reaches():
    with pytest.raises(SystemExit) as excinfo:
        file_task.build_content(_spec(scope=_BOUNDARY_SCOPE, principal="grantbest"))

    message = str(excinfo.value)
    assert "must declare reaches" in message
    assert "must declare principal" not in message


def test_boundary_spec_with_only_reaches_is_refused_naming_principal():
    with pytest.raises(SystemExit) as excinfo:
        file_task.build_content(_spec(scope=_BOUNDARY_SCOPE, reaches=["none"]))

    message = str(excinfo.value)
    assert "must declare principal" in message
    assert "must declare reaches" not in message


def test_boundary_spec_with_reaches_as_a_string_is_refused():
    with pytest.raises(SystemExit) as excinfo:
        file_task.build_content(
            _spec(scope=_BOUNDARY_SCOPE, principal="grantbest", reaches="SUBSTRATE_API_KEY")
        )

    assert "must declare reaches" in str(excinfo.value)


def test_boundary_spec_with_an_empty_reaches_list_is_refused():
    with pytest.raises(SystemExit) as excinfo:
        file_task.build_content(_spec(scope=_BOUNDARY_SCOPE, principal="grantbest", reaches=[]))

    assert "must declare reaches" in str(excinfo.value)


def test_boundary_spec_with_an_empty_string_element_in_reaches_is_refused():
    with pytest.raises(SystemExit) as excinfo:
        file_task.build_content(
            _spec(scope=_BOUNDARY_SCOPE, principal="grantbest", reaches=["SUBSTRATE_API_KEY", "   "])
        )

    assert "must declare reaches" in str(excinfo.value)


def test_boundary_spec_with_both_fields_valid_is_accepted():
    content = file_task.build_content(
        _spec(scope=_BOUNDARY_SCOPE, principal="grantbest", reaches=["none"])
    )

    assert content["principal"] == "grantbest"
    assert content["reaches"] == ["none"]


def test_a_non_boundary_spec_without_the_fields_files_unchanged():
    content = file_task.build_content(_spec())

    assert "principal" not in content
    assert "reaches" not in content


# ---------------------------------------------------------------------------
# AC-4: passthrough.
# ---------------------------------------------------------------------------


def test_principal_and_reaches_round_trip_unchanged():
    content = file_task.build_content(
        _spec(
            scope=_BOUNDARY_SCOPE,
            principal="grantbest",
            reaches=["SUBSTRATE_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"],
        )
    )

    assert content["principal"] == "grantbest"
    assert content["reaches"] == ["SUBSTRATE_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"]


def test_a_non_boundary_spec_that_carries_the_fields_is_accepted_and_passed_through():
    content = file_task.build_content(_spec(principal="grantbest", reaches=["none"]))

    assert content["principal"] == "grantbest"
    assert content["reaches"] == ["none"]


# ---------------------------------------------------------------------------
# AC-5: the list catches what it exists for.
# ---------------------------------------------------------------------------


def test_dispatch_py_is_a_boundary_path():
    """a0166920: long-lived factory processes held the write key in their exec
    environment; dispatch.py is the process that spawns them."""
    assert file_task._first_boundary_intersection(["apps/factory-dispatcher/dispatch.py"])


def test_containment_py_is_a_boundary_path():
    """79db3113: dispatcher git in a worker-written clone ran with hooks and
    repo config enabled, reading containment.py's clone-content handling."""
    assert file_task._first_boundary_intersection(["apps/factory-dispatcher/containment.py"])


def test_process_env_py_is_a_boundary_path():
    """a0166920 (RC-2, the credential-ledger half): process_env.child_env is
    where every spawned child's credential grant is declared."""
    assert file_task._first_boundary_intersection(["apps/factory-dispatcher/process_env.py"])


def test_launchd_agent_py_is_a_boundary_path():
    """a0166920: launchd_agent.py rendered the launch commands that sourced
    the env file (`set -a; . <env_file>; set +a`), a0166920's actual route."""
    assert file_task._first_boundary_intersection(["apps/factory-dispatcher/launchd_agent.py"])


def test_dispatch_steps_py_is_a_boundary_path():
    """dd648709: a Temporal payload could choose the dispatcher's Config; the
    merged narrowing (1c586a20) lives in activities/dispatch_steps.py."""
    assert file_task._first_boundary_intersection(
        ["apps/factory-dispatcher/activities/dispatch_steps.py"]
    )


def test_openapi_app_py_is_a_boundary_path():
    """52deaf69: mcp-hub trusted caller-supplied identity headers; that trust
    lived in apps/mcp-hub/src/openapi_app.py."""
    assert file_task._first_boundary_intersection(["apps/mcp-hub/src/openapi_app.py"])


def test_access_auth_py_is_a_boundary_path():
    """4208f26f: mcp-hub let an identity-less request through; check_scope
    lives in apps/mcp-hub/src/access_auth.py."""
    assert file_task._first_boundary_intersection(["apps/mcp-hub/src/access_auth.py"])


def test_substrate_routes_py_is_a_boundary_path():
    """RC-4 (caller authentication): apps/substrate/src/routes.py holds the
    substrate API-key check."""
    assert file_task._first_boundary_intersection(["apps/substrate/src/routes.py"])


def test_worker_identity_py_is_a_boundary_path():
    """part B design §8 row B1: worker_identity.py builds the sudo launch call
    and the worker's credential payload."""
    assert file_task._first_boundary_intersection(
        ["apps/factory-dispatcher/worker_identity.py"]
    )
