"""OPS-61 (Amendment 37 carried-forward clause 1): the gate's read-only
boundary is containment, not a prompt. Each probe demonstrates one denial by
running it and watching it fail, not by asserting it in prose - and the
control probe demonstrates the boundary is enforcing rather than decorative
by running the same class of operation where the profile permits it.
"""
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import containment


def _sandbox_apply_available() -> tuple[bool, str]:
    """Whether this process can itself call sandbox_apply.

    A process already running under an outer sandbox-exec profile cannot
    apply a second one - not a matter of the inner profile's content, the
    exact `codex exec --sandbox workspace-write` nesting failure decision
    record D6 (docs/plans/2026-09-19-decision-record-audit-against-the-north-star.md)
    documents (rc=71, "sandbox_apply: Operation not permitted"). A dev.task worker
    testing this very containment module is itself dispatched inside the
    dispatcher's own deny-write profile, so it can hit that identical
    nesting wall while developing OS-level probes for a *different*
    profile. Detected once per run rather than assumed from ``sys.platform``
    alone, so these probes skip with a clear reason in that shape of
    environment and run for real everywhere else - including wherever a
    gate is actually dispatched, which is not nested this way.
    """
    probe = subprocess.run(
        ["sandbox-exec", "-f", "/dev/stdin", "/usr/bin/true"],
        input="(version 1)\n(allow default)\n",
        capture_output=True,
        text=True,
    )
    if probe.returncode == 0:
        return True, ""
    return False, (probe.stderr or "").strip()


def test_gate_profile_denies_checkout_and_allows_scratch(tmp_path):
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    text = containment.render_gate_profile(tmp_path / "scratch")
    assert str(checkout) not in text
    assert str((tmp_path / "scratch").resolve()) in text
    assert "(deny network*)" in text
    assert "@SCRATCH@" not in text


def test_prepare_gate_containment_writes_profile_and_scratch_beside_checkout(tmp_path):
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    profile, scratch = containment.prepare_gate_containment(checkout)
    resolved_parent = checkout.resolve().parent
    assert profile.parent == resolved_parent and profile.is_file()
    assert scratch.parent == resolved_parent and scratch.is_dir()
    assert not (checkout / containment.GATE_PROFILE_NAME).exists()
    text = profile.read_text()
    assert str(scratch) in text
    # The checkout itself must never appear as a write allowance - that is
    # the entire difference from the dev-worker write profile.
    assert str(checkout.resolve()) not in text


@pytest.mark.skipif(sys.platform != "darwin", reason="sandbox-exec is macOS-only")
class TestGateContainmentProbes:
    """Each test is a probe against the real sandbox-exec profile, not a
    unit test of Python logic - the mechanism under test is the OS, not this
    module."""

    @pytest.fixture(autouse=True)
    def _require_sandbox_apply(self):
        available, reason = _sandbox_apply_available()
        if not available:
            pytest.skip(
                "sandbox_apply unavailable to this process (likely already "
                f"nested inside another sandbox-exec profile): {reason}"
            )

    @pytest.fixture()
    def rig(self, tmp_path):
        checkout = tmp_path / "checkout"
        checkout.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=checkout, check=True)
        subprocess.run(
            ["git", "config", "user.email", "gate@example.com"], cwd=checkout, check=True
        )
        subprocess.run(["git", "config", "user.name", "gate"], cwd=checkout, check=True)
        (checkout / "README.md").write_text("reviewed checkout\n")
        subprocess.run(["git", "add", "README.md"], cwd=checkout, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=checkout, check=True)
        profile, scratch = containment.prepare_gate_containment(checkout)
        return checkout, profile, scratch

    def test_control_write_denied_in_checkout_allowed_in_scratch(self, rig):
        """Negative control (Amendment 36's lesson): the same class of
        operation - a file write - must succeed where the profile permits
        it and fail where it does not, or a universal denial would pass
        this test suite for the wrong reason."""
        checkout, profile, scratch = rig
        denied = subprocess.run(
            ["sandbox-exec", "-f", str(profile), "/usr/bin/touch", str(checkout / "x.txt")],
            capture_output=True,
        )
        allowed = subprocess.run(
            ["sandbox-exec", "-f", str(profile), "/usr/bin/touch", str(scratch / "x.txt")],
            capture_output=True,
        )
        assert denied.returncode != 0 and not (checkout / "x.txt").exists()
        assert allowed.returncode == 0 and (scratch / "x.txt").exists()

    def test_git_commit_against_reviewed_checkout_is_denied(self, rig):
        checkout, profile, _scratch = rig
        before = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=checkout, capture_output=True, text=True
        ).stdout.strip()
        (checkout / "README.md").write_text("edited\n")
        result = subprocess.run(
            [
                "sandbox-exec",
                "-f",
                str(profile),
                "/usr/bin/git",
                "commit",
                "-am",
                "should be denied",
            ],
            cwd=checkout,
            capture_output=True,
        )
        after = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=checkout, capture_output=True, text=True
        ).stdout.strip()
        assert result.returncode != 0
        assert after == before, "commit must not have landed against the reviewed checkout"

    def test_git_push_to_local_remote_is_denied(self, rig):
        checkout, profile, _scratch = rig
        remote = checkout.parent / "remote.git"
        subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
        result = subprocess.run(
            [
                "sandbox-exec",
                "-f",
                str(profile),
                "/usr/bin/git",
                "push",
                str(remote),
                "HEAD:refs/heads/main",
            ],
            cwd=checkout,
            capture_output=True,
        )
        assert result.returncode != 0
        empty = subprocess.run(
            ["git", "-C", str(remote), "rev-parse", "--verify", "refs/heads/main"],
            capture_output=True,
        )
        assert empty.returncode != 0, "push must not have reached the remote"

    def test_network_access_is_denied(self, rig):
        """gh, `git push` over http(s)/ssh, and reaching for a production API
        (the 2026-08-29 A36 incident) all go through the same syscall this
        probes directly, without depending on live internet reachability
        from the test host."""
        import socket

        _checkout, profile, _scratch = rig
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        probe = (
            "import socket, sys\n"
            "s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)\n"
            "s.settimeout(2)\n"
            f"s.connect(('127.0.0.1', {port}))\n"
            "sys.exit(0)\n"
        )
        try:
            denied = subprocess.run(
                ["sandbox-exec", "-f", str(profile), sys.executable, "-c", probe],
                capture_output=True,
            )
            allowed = subprocess.run(
                [sys.executable, "-c", probe],
                capture_output=True,
            )
        finally:
            server.close()
        assert denied.returncode != 0
        assert allowed.returncode == 0, allowed.stderr

    def test_read_only_verification_still_works(self, rig):
        """The gate must still be able to read the reviewed checkout and run
        the declared read-only verification (git log/diff/show)."""
        checkout, profile, _scratch = rig
        for command in (
            ["/usr/bin/git", "log", "--oneline"],
            ["/usr/bin/git", "show", "HEAD"],
            ["/usr/bin/git", "diff", "HEAD"],
        ):
            result = subprocess.run(
                ["sandbox-exec", "-f", str(profile), *command],
                cwd=checkout,
                capture_output=True,
            )
            assert result.returncode == 0, (command, result.stderr)
