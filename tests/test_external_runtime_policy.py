from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts import external_runtime_policy as policy


@pytest.mark.parametrize(
    "relative",
    [
        "obs-portable/bin/64bit/obs64.exe",
        r"Vendor\OBS-Studio\data.txt",
        "OBS-PORTABLE",
        "tools/OBS64.EXE",
        "tools/ffmpeg.exe",
        r"tools\FFPROBE.EXE",
        "tools/ffplay.exe",
        "downloads/OBS-Studio-32.1.2-Windows-x64.zip",
        "downloads/obs-studio-32.1.2-Windows-x64-Installer.EXE",
        "downloads/OBS-Studio-32.1.2-Windows-x64.msi",
        "downloads/OBS-Studio-32.1.2-Windows-x64.7z",
        "downloads/ffmpeg-8.1.1-essentials_build.zip",
        "downloads/FFMPEG-release-essentials.7Z",
    ],
)
def test_user_provided_runtime_paths_are_rejected(relative):
    assert policy.is_user_provided_runtime_path(relative) is True


@pytest.mark.parametrize(
    "relative",
    [
        "tests/fixtures/opencv_videoio_ffmpeg4140_64.dll",
        r"_internal\cv2\opencv_videoio_ffmpeg4140_64.dll",
        "src/ffmpeg.py",
        "docs/OBS-Studio-setup.md",
        "downloads/ffmpeg-release-essentials.tar.xz",
    ],
)
def test_non_standalone_runtime_paths_are_allowed(relative):
    assert policy.is_user_provided_runtime_path(relative) is False


def _run_git(repository: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def test_tracked_source_check_ignores_untracked_generated_runtime(tmp_path):
    repository = tmp_path / "repository"
    repository.mkdir()
    _run_git(repository, "init", "--quiet")
    (repository / "README.md").write_text("test\n", encoding="utf-8")
    _run_git(repository, "add", "README.md")

    untracked_runtime = repository / "ffmpeg.exe"
    untracked_runtime.write_bytes(b"local generated runtime")

    assert policy.check_tracked_source(repository) == []

    _run_git(repository, "add", "--force", "ffmpeg.exe")

    assert policy.check_tracked_source(repository) == ["ffmpeg.exe"]


def test_tracked_source_check_rejects_repository_subdirectory(tmp_path):
    repository = tmp_path / "repository"
    nested = repository / "nested"
    nested.mkdir(parents=True)
    _run_git(repository, "init", "--quiet")
    (repository / "ffmpeg.exe").write_bytes(b"tracked runtime outside nested path")
    (nested / "README.md").write_text("test\n", encoding="utf-8")
    _run_git(repository, "add", "--force", "ffmpeg.exe", "nested/README.md")

    with pytest.raises(
        policy.ExternalRuntimePolicyError,
        match="must be the canonical Git top level",
    ):
        policy.check_tracked_source(nested)


def test_tracked_source_check_accepts_linked_worktree_with_windows_path(tmp_path):
    repository = tmp_path / "primary repository 日本語"
    linked_worktree = tmp_path / "linked worktree 日本語"
    repository.mkdir()
    _run_git(repository, "init", "--quiet")
    (repository / "README.md").write_text("test\n", encoding="utf-8")
    _run_git(repository, "add", "README.md")
    _run_git(
        repository,
        "-c",
        "user.name=Test User",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "--quiet",
        "-m",
        "test",
    )
    _run_git(repository, "worktree", "add", "--quiet", "--detach", str(linked_worktree))

    assert policy.git_tracked_paths(linked_worktree) == ["README.md"]


def test_git_inventory_failure_is_fail_closed(monkeypatch, tmp_path):
    def fail_git(*_args, **_kwargs):
        return subprocess.CompletedProcess(
            args=["git"],
            returncode=128,
            stdout=b"",
            stderr=b"fatal: not a git repository",
        )

    monkeypatch.setattr(policy.subprocess, "run", fail_git)

    with pytest.raises(policy.ExternalRuntimePolicyError, match="exit code 128"):
        policy.git_tracked_paths(tmp_path)


@pytest.mark.parametrize("raw", [b"ffmpeg.exe", b"bad-\xff\0", b"ok\0\0"])
def test_malformed_git_inventory_is_fail_closed(raw):
    with pytest.raises(policy.ExternalRuntimePolicyError):
        policy._parse_git_tracked_paths(raw)


def test_cli_reports_every_forbidden_tracked_path(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(
        policy,
        "check_tracked_source",
        lambda _root: ["obs-portable/bin/64bit/obs64.exe", "tools/ffmpeg.exe"],
    )

    assert policy.main(["--repository-root", str(tmp_path)]) == 1

    captured = capsys.readouterr()
    assert "User-provided OBS/standalone FFmpeg" in captured.err
    assert "obs-portable/bin/64bit/obs64.exe" in captured.err
    assert "tools/ffmpeg.exe" in captured.err


def test_normal_ci_checks_tracked_runtime_before_dependency_install():
    workflow = (Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )
    lint_job = workflow.split("  lint-test:", maxsplit=1)[1].split(
        "  test-windows:", maxsplit=1
    )[0]
    command = "python -m scripts.external_runtime_policy --repository-root ."

    assert workflow.count(command) == 1
    assert lint_job.index("- name: Set up Python") < lint_job.index(command)
    assert lint_job.index(command) < lint_job.index("- name: Install dependencies")
