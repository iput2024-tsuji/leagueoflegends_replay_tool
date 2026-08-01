from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

POWERSHELL = shutil.which("pwsh")
CHECKER = Path("scripts/check_mpv_distribution.ps1").resolve()


def _run_checker(
    tmp_path: Path,
    relative_files: list[str],
) -> tuple[subprocess.CompletedProcess[str], list[Path]]:
    if POWERSHELL is None:
        pytest.skip("pwsh is unavailable")
    distribution = tmp_path / "distribution"
    distribution.mkdir()
    files = []
    for relative in relative_files:
        path = distribution / Path(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"test artifact")
        files.append(path)
    result = subprocess.run(
        [
            POWERSHELL,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(CHECKER),
            "-DistributionRoot",
            str(distribution),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=20,
        check=False,
    )
    return result, files


def _combined_output(result: subprocess.CompletedProcess[str]) -> str:
    return result.stdout + "\n" + result.stderr


@pytest.mark.parametrize(
    "relative",
    [
        pytest.param("libmpv-custom.dll", id="root-libmpv-wildcard"),
        pytest.param("plugins/video/mpv-2.dll", id="nested-mpv"),
        pytest.param("plugins/LIBMPV-NEXT.DLL", id="case-insensitive"),
    ],
)
def test_mpv_dll_in_distribution_fails_closed(tmp_path, relative):
    result, files = _run_checker(tmp_path, [relative])
    output = _combined_output(result)

    assert result.returncode != 0
    assert "利用者が用意するmpv DLLが成果物へ混入しています" in output
    assert str(files[0].resolve()).casefold() in output.casefold()
    assert r"%LOCALAPPDATA%\LoLReplayTool\bin" in output
    assert "手動配置" in output
    assert files[0].read_bytes() == b"test artifact"


def test_mpv_policy_reports_every_detected_file(tmp_path):
    result, files = _run_checker(
        tmp_path,
        [
            "mpv-release.dll",
            "plugins/libmpv-1.dll",
            "other/MPV-EXPERIMENTAL.DLL",
        ],
    )
    output = _combined_output(result)

    assert result.returncode != 0
    for path in files:
        assert str(path.resolve()).casefold() in output.casefold()
        assert path.read_bytes() == b"test artifact"


def test_similar_names_do_not_trigger_mpv_policy(tmp_path):
    relative_files = [
        "libmpv.dll",
        "mpv.dll",
        "xmpv-1.dll",
        "my-libmpv-1.dll",
        "libmpv_1.dll",
        "mpv-1.dll.bak",
        "libmpv-1.pdb",
    ]

    result, files = _run_checker(tmp_path, relative_files)
    output = _combined_output(result)

    assert result.returncode == 0, output
    assert [path.read_bytes() for path in files] == [b"test artifact"] * len(files)


def test_build_script_invokes_mpv_policy_without_silent_cleanup():
    script = Path("scripts/build.ps1").read_text(encoding="utf-8")

    assert 'Join-Path $scriptDir "check_mpv_distribution.ps1"' in script
    assert "-DistributionRoot $distRootDir" in script
    assert "Remove-Item -Path $dll.FullName" not in script
