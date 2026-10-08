import importlib.util
import os
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC_PATH = ROOT / "openapi.json"
GENERATOR_PATH = ROOT / "scripts" / "generate_openapi.py"


def _load_generator():
    spec = importlib.util.spec_from_file_location("generate_openapi", GENERATOR_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_committed_spec_matches_the_app():
    generated = _load_generator().openapi_json()
    committed = SPEC_PATH.read_text()
    assert committed == generated, (
        "apps/substrate/openapi.json is out of sync with the app's routes -- "
        "regenerate with: python scripts/generate_openapi.py > openapi.json"
    )


def test_generator_output_does_not_depend_on_the_ci_env(tmp_path):
    """The generator sets placeholders for the three import-time variables when
    they are absent, so the remedy the failure message prints works in a bare
    shell without truncating the committed file first (the #814 gate, F1). The
    document must carry none of their values."""
    import subprocess
    import sys

    env = {k: v for k, v in os.environ.items() if k not in {"DATABASE_URL", "SUBSTRATE_API_KEY", "SUBSTRATE_ENCRYPTION_KEY"}}
    out = subprocess.run(
        [sys.executable, str(GENERATOR_PATH)], cwd=str(GENERATOR_PATH.parent.parent), env=env,
        capture_output=True, text=True, check=True,
    ).stdout
    assert out == SPEC_PATH.read_text()
    assert "openapi-generator-placeholder" not in out
