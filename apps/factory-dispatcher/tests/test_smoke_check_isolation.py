"""dev.finding 32f631c1: smoke_check_python must not import clone-planted code.

py_compile.py -- or a compiled-only py_compile.pyc -- sitting in the clone's
own working directory shadows the stdlib py_compile module under plain
`python -m py_compile`, because `-m` puts the current directory first on
sys.path. `-I` (isolated mode) closes that: no module the clone's working
directory supplies is ever imported, in source or compiled form.
"""
import py_compile as _py_compile_stdlib
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import dispatch

MARKER_NAME = "marker.txt"


def _write_ok(clone: Path) -> None:
    (clone / "ok.py").write_text("print('hi')\n")


def _planted_source(clone: Path) -> None:
    """A py_compile.py in the clone that writes a marker file if imported."""
    (clone / "py_compile.py").write_text(
        f"with open({MARKER_NAME!r}, 'w') as f:\n    f.write('imported')\n"
    )


def _planted_compiled_only(clone: Path) -> None:
    """A py_compile.pyc in the clone, with no accompanying source, that
    writes a marker file if imported -- a sourceless module, built for the
    running interpreter, placed directly beside where the source would be
    (not under __pycache__) so the import system's bytecode-only fallback
    picks it up."""
    src = clone / "_marker_src.py"
    src.write_text(f"with open({MARKER_NAME!r}, 'w') as f:\n    f.write('imported')\n")
    _py_compile_stdlib.compile(
        str(src), cfile=str(clone / "py_compile.pyc"), doraise=True
    )
    src.unlink()


def _marker_written(clone: Path) -> bool:
    return (clone / MARKER_NAME).exists()


def test_planted_source_is_not_imported(tmp_path):
    clone = tmp_path
    _write_ok(clone)
    _planted_source(clone)

    result = dispatch.smoke_check_python(clone, ["ok.py"])

    assert result is None
    assert not _marker_written(clone)


def test_planted_compiled_only_module_is_not_imported(tmp_path):
    clone = tmp_path
    _write_ok(clone)
    _planted_compiled_only(clone)
    assert not (clone / "py_compile.py").exists(), "must be compiled-only, no source beside it"

    result = dispatch.smoke_check_python(clone, ["ok.py"])

    assert result is None
    assert not _marker_written(clone)


def test_control_planted_source_old_invocation_does_import(tmp_path):
    """Non-vacuousness for the source case: the pre-fix argv (no -I) DOES
    import the planted module, so the fixture is real and the fixed argv's
    silence above is not an artifact of an interpreter that never imported
    it in the first place."""
    clone = tmp_path
    _write_ok(clone)
    _planted_source(clone)

    proc = subprocess.run(
        [sys.executable, "-m", "py_compile", "ok.py"], cwd=clone, capture_output=True
    )

    assert proc.returncode == 0
    assert _marker_written(clone)


def test_control_planted_compiled_only_old_invocation_does_import(tmp_path):
    """Non-vacuousness for the compiled-only case: same control, sourceless
    module."""
    clone = tmp_path
    _write_ok(clone)
    _planted_compiled_only(clone)

    proc = subprocess.run(
        [sys.executable, "-m", "py_compile", "ok.py"], cwd=clone, capture_output=True
    )

    assert proc.returncode == 0
    assert _marker_written(clone)


def test_syntax_error_still_reported(tmp_path):
    clone = tmp_path
    (clone / "bad.py").write_text("def f(:\n    pass\n")

    result = dispatch.smoke_check_python(clone, ["bad.py"])

    assert result
