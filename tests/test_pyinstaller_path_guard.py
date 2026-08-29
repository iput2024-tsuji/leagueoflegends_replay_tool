import os
import subprocess
import sys
from pathlib import Path


def test_pyinstaller_path_guard_resets_process_path(tmp_path):
    expected = os.pathsep.join(
        [str(tmp_path / "locked-python"), str(tmp_path / "system")]
    )
    environment = os.environ.copy()
    environment["LOL_REPLAY_PYINSTALLER_PATH"] = expected
    environment["PYTHONPATH"] = str(
        Path("scripts/pyinstaller_sitecustomize").resolve()
    )
    environment["PATH"] = str(tmp_path / "host-contamination")

    result = subprocess.run(
        [sys.executable, "-c", "import os; print(os.environ['PATH'])"],
        check=True,
        capture_output=True,
        env=environment,
        text=True,
    )

    assert result.stdout.strip() == expected


def test_pyinstaller_path_guard_reaches_isolated_child(tmp_path):
    expected = os.pathsep.join(
        [str(tmp_path / "locked-python"), str(tmp_path / "system")]
    )
    environment = os.environ.copy()
    environment["LOL_REPLAY_PYINSTALLER_PATH"] = expected
    environment["PYTHONPATH"] = str(
        Path("scripts/pyinstaller_sitecustomize").resolve()
    )
    environment["PATH"] = str(tmp_path / "host-contamination")
    script = (
        "import os; from PyInstaller import isolated; "
        "child = isolated.call("
        "lambda: __import__('os').environ['PATH']"
        "); print(os.environ['PATH']); print(child)"
    )

    result = subprocess.run(
        [sys.executable, "-c", script],
        check=True,
        capture_output=True,
        env=environment,
        text=True,
    )

    assert result.stdout.splitlines() == [expected, expected]
