from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest


REPO = pathlib.Path(__file__).resolve().parents[2]
FIXTURES = REPO / "scripts" / "tests" / "fixtures" / "release_manifest"


def _load_release_manifest():
    spec = importlib.util.spec_from_file_location(
        "release_manifest",
        REPO / "scripts" / "release-manifest.py",
    )
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_fixture_manifest_contains_scope_excerpts_stub_and_boilerplate(capsys):
    manifest = _load_release_manifest()

    code = manifest.main(["501", "502", "--fixture-dir", str(FIXTURES)])

    assert code == 0
    output = capsys.readouterr().out
    assert "| PR | Title | Bead | Lane | Risk | Change kind |" in output
    assert "| #501 | Generate release manifest | 11111111-1111-4111-8111-111111111111 | platform | medium | behavioral |" in output
    assert "| #502 | Tidy manifest fixtures | 33333333-3333-4333-8333-333333333333 | factory | low | structural |" in output
    assert "### PR #501 - Generate release manifest" in output
    assert "- Intent: Build a release manifest from queued PRs." in output
    assert "Emit the gate-prompt markdown skeleton." in output
    assert "in `designs` dev.design `22222222-2222-4222-8222-222222222222`" in output
    assert "## Merge Order" in output
    assert "SRE fills this section before handoff." in output
    assert "**Read it into a variable and never print it:**" in output
    assert "Never interpolate the key into a command you write out, and never paste its value into a later command" in output


def test_outer_loop_pr_renders_attended_and_exits_zero(capsys):
    manifest = _load_release_manifest()

    code = manifest.main(["505", "--fixture-dir", str(FIXTURES)])

    assert code == 0
    output = capsys.readouterr().out
    assert "| #505 | Outer-loop attended change | outer-loop (attended) | - | - | behavioral |" in output
    assert "This PR is explicitly marked outer-loop (attended)." in output
    assert "MISSING" not in output
    assert "handoff defect" not in output


def test_factory_template_pr_renders_bead_and_exits_zero(capsys):
    manifest = _load_release_manifest()

    code = manifest.main(["506", "--fixture-dir", str(FIXTURES)])

    assert code == 0
    output = capsys.readouterr().out
    assert (
        "| #506 | fix(platform): the gate reads the dispatcher's own handwriting "
        "| 55555555-5555-4555-8555-555555555555 | platform | low | behavioral |"
    ) in output
    assert "MISSING" not in output
    assert "handoff defect" not in output


def test_shared_markers_are_the_same_objects_gate_prepass_uses():
    manifest = _load_release_manifest()
    spec = importlib.util.spec_from_file_location(
        "gate_prepass_for_marker_check",
        REPO / "scripts" / "gate-prepass.py",
    )
    assert spec is not None and spec.loader is not None
    prepass = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = prepass
    spec.loader.exec_module(prepass)

    assert manifest.OUTER_LOOP_RE is prepass.OUTER_LOOP_RE
    assert manifest.find_bead_id is prepass.find_bead_id


def test_shared_substrate_client_is_the_same_object_gate_prepass_uses():
    manifest = _load_release_manifest()
    spec = importlib.util.spec_from_file_location(
        "gate_prepass_for_substrate_check",
        REPO / "scripts" / "gate-prepass.py",
    )
    assert spec is not None and spec.loader is not None
    prepass = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = prepass
    spec.loader.exec_module(prepass)

    # One client implementation, asserted at module identity: both scripts
    # now hold the shared substrate_client module (gate-prepass through the
    # get-only reader facade), so neither carries a class alias to compare.
    assert manifest._substrate is prepass._substrate
    assert manifest.SECRET_ENV_VARS == ("SUBSTRATE_URL", "SUBSTRATE_API_KEY")


def test_missing_bead_id_is_explicit_and_nonzero(capsys):
    manifest = _load_release_manifest()

    code = manifest.main(["503", "--fixture-dir", str(FIXTURES)])

    assert code == 1
    output = capsys.readouterr().out
    assert "MISSING - PR body carries no originating dev.task bead id" in output
    assert "This PR carries no originating dev.task bead id in its body. This is a handoff defect." in output


def test_substrate_env_values_are_scrubbed_from_output(monkeypatch, capsys):
    manifest = _load_release_manifest()
    url = "https://sentinel-substrate.example.invalid"
    key = "sentinel-substrate-api-key-value"
    monkeypatch.setenv("SUBSTRATE_URL", url)
    monkeypatch.setenv("SUBSTRATE_API_KEY", key)

    code = manifest.main(["504", "--fixture-dir", str(FIXTURES)])

    assert code == 0
    output = capsys.readouterr().out
    assert url not in output
    assert key not in output
    assert "SUBSTRATE_URL" in output
    assert "SUBSTRATE_API_KEY" in output


def test_fixture_mode_does_not_call_live_clients(monkeypatch):
    manifest = _load_release_manifest()

    def fail_live_provider():
        raise AssertionError("fixture mode must not construct the live provider")

    monkeypatch.setattr(manifest, "LiveProvider", fail_live_provider)

    assert manifest.main(["501", "--fixture-dir", str(FIXTURES)]) == 0


# --- --release selection -------------------------------------------------------


def test_release_selects_prs_from_delivers_edges(capsys):
    manifest = _load_release_manifest()

    code = manifest.main(["--release", "R90.01", "--fixture-dir", str(FIXTURES)])

    assert code == 0
    output = capsys.readouterr().out
    assert "| #509 | First delivering PR |" in output
    assert "| #510 | Second delivering PR |" in output
    assert "PRs under review" in output
    assert "509" in output.splitlines() and "510" in output.splitlines()


def test_resolve_release_prs_sorts_and_dedupes():
    manifest = _load_release_manifest()
    provider = manifest.FixtureProvider(FIXTURES)

    assert manifest.resolve_release_prs("R90.01", provider) == [509, 510]


def test_release_and_positional_prs_are_mutually_exclusive(capsys):
    manifest = _load_release_manifest()

    with pytest.raises(SystemExit):
        manifest.main(["501", "--release", "R90.01", "--fixture-dir", str(FIXTURES)])

    with pytest.raises(SystemExit):
        manifest.main(["--fixture-dir", str(FIXTURES)])


def test_unknown_release_refuses_rather_than_emitting_an_empty_manifest(capsys):
    manifest = _load_release_manifest()

    code = manifest.main(["--release", "R00.99", "--fixture-dir", str(FIXTURES)])

    assert code == 2
    err = capsys.readouterr().err
    assert "R00.99" in err
    assert "not found" in err
