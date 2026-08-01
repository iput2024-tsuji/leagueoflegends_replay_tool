from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

POWERSHELL = shutil.which("pwsh")
INSTALLER_SCRIPT = Path("scripts/build_installer.ps1").resolve()


def _write_installer_harness(tmp_path: Path) -> tuple[Path, Path, Path]:
    repository = tmp_path / "repository with spaces"
    scripts = repository / "scripts"
    scripts.mkdir(parents=True)
    installer = scripts / "build_installer.ps1"
    shutil.copy2(INSTALLER_SCRIPT, installer)
    capture = repository / "captured build arguments.json"
    (scripts / "build.ps1").write_text(
        """param(
  [string]$PythonExe = "",
  [string]$BuildProvenance = "",
  [string]$BuildProvenanceSha256 = ""
)

[ordered]@{
  PythonExe = $PythonExe
  BuildProvenance = $BuildProvenance
  BuildProvenanceSha256 = $BuildProvenanceSha256
} | ConvertTo-Json -Compress | Set-Content `
  -LiteralPath $env:INSTALLER_ARGUMENT_CAPTURE `
  -Encoding UTF8

throw "CAPTURED_BUILD_ARGUMENTS"
""",
        encoding="utf-8",
    )
    return repository, installer, capture


def _run_installer(
    installer: Path,
    capture: Path,
    *arguments: str,
) -> subprocess.CompletedProcess[str]:
    if POWERSHELL is None:
        pytest.skip("pwsh is unavailable")
    environment = os.environ.copy()
    environment["INSTALLER_ARGUMENT_CAPTURE"] = str(capture)
    return subprocess.run(
        [
            POWERSHELL,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(installer),
            "-Version",
            "1.2.3",
            "-SkipTests",
            *arguments,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
        env=environment,
    )


def _captured_arguments(
    capture: Path,
    result: subprocess.CompletedProcess[str],
) -> dict[str, str]:
    output = result.stdout + "\n" + result.stderr
    assert result.returncode != 0
    assert "CAPTURED_BUILD_ARGUMENTS" in output
    assert capture.is_file(), output
    return json.loads(capture.read_text(encoding="utf-8-sig"))


def _fake_python(repository: Path, relative: str) -> Path:
    python = repository / relative
    python.parent.mkdir(parents=True, exist_ok=True)
    python.write_bytes(b"test executable placeholder")
    return python.resolve()


def test_installer_forwards_explicit_python_as_named_argument(tmp_path):
    repository, installer, capture = _write_installer_harness(tmp_path)
    python = _fake_python(repository, "runtime with spaces/python custom.exe")

    result = _run_installer(
        installer,
        capture,
        "-PythonExe",
        str(python),
    )

    captured = _captured_arguments(capture, result)
    assert Path(captured["PythonExe"]).resolve() == python
    assert captured["BuildProvenance"] == ""
    assert captured["BuildProvenanceSha256"] == ""


def test_installer_forwards_python_and_provenance_by_name_with_spaced_paths(
    tmp_path,
):
    repository, installer, capture = _write_installer_harness(tmp_path)
    python = _fake_python(repository, "runtime with spaces/python custom.exe")
    provenance = repository / "provenance material" / "build provenance.json"
    provenance.parent.mkdir(parents=True)
    provenance.write_bytes(b'{"schema_version": 1}\n')
    provenance = provenance.resolve()
    provenance_sha256 = hashlib.sha256(provenance.read_bytes()).hexdigest()

    result = _run_installer(
        installer,
        capture,
        "-PythonExe",
        str(python),
        "-BuildProvenance",
        str(provenance),
        "-BuildProvenanceSha256",
        provenance_sha256,
    )

    captured = _captured_arguments(capture, result)
    assert Path(captured["PythonExe"]).resolve() == python
    assert Path(captured["BuildProvenance"]).resolve() == provenance
    assert captured["BuildProvenanceSha256"] == provenance_sha256


@pytest.mark.parametrize(
    "python_arguments",
    [
        pytest.param((), id="unspecified"),
        pytest.param(("-PythonExe", ""), id="explicit-empty"),
    ],
)
def test_installer_forwards_automatically_selected_venv_python(
    tmp_path,
    python_arguments,
):
    repository, installer, capture = _write_installer_harness(tmp_path)
    python = _fake_python(repository, "venv/Scripts/python.exe")

    result = _run_installer(installer, capture, *python_arguments)

    captured = _captured_arguments(capture, result)
    assert Path(captured["PythonExe"]).resolve() == python
    assert captured["BuildProvenance"] == ""
    assert captured["BuildProvenanceSha256"] == ""


def test_skip_build_does_not_invoke_build_script(tmp_path):
    repository, installer, capture = _write_installer_harness(tmp_path)
    _fake_python(repository, "venv/Scripts/python.exe")

    result = _run_installer(
        installer,
        capture,
        "-SkipBuild",
        "-SkipSelfCheck",
    )

    output = result.stdout + "\n" + result.stderr
    assert result.returncode != 0
    assert "CAPTURED_BUILD_ARGUMENTS" not in output
    assert not capture.exists()
    assert "LoLReplayTool.exe" in output
