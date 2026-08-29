from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import installer_content_audit as audit

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
AUDIT_SCRIPT = REPOSITORY_ROOT / "scripts" / "audit_installer_contents.ps1"
POWERSHELL = shutil.which("pwsh")


def _write_distribution(root: Path) -> None:
    files = {
        "LICENSE": b"GPL-3.0-only\n",
        "THIRD_PARTY_NOTICES.md": b"third party notices\n",
        "SOURCE_OFFER.md": b"source offer\n",
        "QT_RELINKING.md": b"Qt relinking\n",
        "VERSION": b"0.5.2\n",
        "licenses/python-packages.json": b'{"schema_version": 1}\n',
        "licenses/distribution-manifest.json": (
            json.dumps({"schema_version": 1, "files": []}).encode("utf-8") + b"\n"
        ),
        "_internal/space directory/日本語 data.bin": b"payload bytes\n",
        "LoLReplayTool.exe": b"application\n",
    }
    for relative, content in files.items():
        path = root / Path(*relative.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)


def _matching_roots(tmp_path: Path) -> tuple[Path, Path]:
    distribution = tmp_path / "検査済み dist with spaces"
    installed = tmp_path / "installer 展開 content"
    _write_distribution(distribution)
    shutil.copytree(distribution, installed)
    return distribution, installed


@pytest.fixture(autouse=True)
def _accept_synthetic_compliance(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(audit, "validate_distribution", lambda _root: [])


def test_matching_payload_accepts_spaces_and_non_ascii_paths(tmp_path: Path) -> None:
    distribution, installed = _matching_roots(tmp_path)

    assert audit.audit_installer_payload(distribution, installed) == []


@pytest.mark.parametrize("difference", ["extra", "missing", "content"])
def test_payload_comparison_is_bidirectional_and_hash_locked(
    tmp_path: Path,
    difference: str,
) -> None:
    distribution, installed = _matching_roots(tmp_path)
    target = installed / "_internal" / "space directory" / "日本語 data.bin"
    if difference == "extra":
        (installed / "extra.bin").write_bytes(b"unexpected")
    elif difference == "missing":
        target.unlink()
    else:
        target.write_bytes(b"same size?????\n")

    errors = audit.audit_installer_payload(distribution, installed)

    expected_fragment = {
        "extra": "contains an extra file",
        "missing": "is missing distribution file",
        "content": "content SHA256 differs",
    }[difference]
    assert any(expected_fragment in error for error in errors)


def test_required_license_and_distribution_manifest_are_hash_checked(
    tmp_path: Path,
) -> None:
    distribution, installed = _matching_roots(tmp_path)
    (installed / "LICENSE").unlink()
    (installed / "licenses" / "distribution-manifest.json").write_text(
        '{"schema_version": 999, "files": []}\n',
        encoding="utf-8",
    )

    errors = audit.audit_installer_payload(distribution, installed)

    assert any("required file is missing: LICENSE" in error for error in errors)
    assert any("distribution-manifest.json SHA256 differs" in error for error in errors)


@pytest.mark.parametrize(
    "relative",
    [
        "obs-portable/bin/64bit/obs64.exe",
        "bin/OBS-Studio/data/plugin.dll",
        "managed/ffmpeg/bin/codec.dll",
        "tools/ffmpeg.exe",
        "downloads/OBS-Studio-31.0.0-Windows-x64-Installer.exe",
        "downloads/ffmpeg-release-full.7z",
        "downloads/ffmpeg-release-installer.exe",
        "downloads/ffmpeg.zip",
        "downloads/OBS-STUDIO.EXE",
        "managed/ffmpeg_8.1_win64/bin/codec.dll",
    ],
)
def test_payload_rejects_external_runtime_files_archives_and_managed_directories(
    tmp_path: Path,
    relative: str,
) -> None:
    distribution, installed = _matching_roots(tmp_path)
    target = installed / Path(*relative.split("/"))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"must remain user-provided")

    errors = audit.audit_installer_payload(distribution, installed)

    assert any("forbidden user-provided runtime" in error for error in errors)


def test_failed_audit_does_not_modify_sibling_content(tmp_path: Path) -> None:
    distribution, installed = _matching_roots(tmp_path)
    outside = tmp_path / "outside audit roots" / "sentinel.txt"
    outside.parent.mkdir()
    outside.write_bytes(b"preserve exactly")
    before = outside.read_bytes()
    (installed / "unexpected.bin").write_bytes(b"failure trigger")

    errors = audit.audit_installer_payload(distribution, installed)

    assert errors
    assert outside.read_bytes() == before


@pytest.mark.skipif(
    os.name != "nt" or POWERSHELL is None,
    reason="Windows PowerShell installer boundary test",
)
def test_audit_runner_rejects_overlapping_temp_root_without_changes(
    tmp_path: Path,
) -> None:
    distribution = tmp_path / "distribution 日本語"
    _write_distribution(distribution)
    installer = tmp_path / "fake setup.exe"
    installer.write_bytes(b"must never execute")
    sentinel = tmp_path / "outside sentinel.bin"
    sentinel.write_bytes(b"preserve")

    result = subprocess.run(
        [
            POWERSHELL,
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(AUDIT_SCRIPT),
            "-InstallerPath",
            str(installer),
            "-DistributionRoot",
            str(distribution),
            "-TempRoot",
            str(distribution),
            "-PythonExe",
            sys.executable,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=30,
    )

    assert result.returncode != 0
    assert sentinel.read_bytes() == b"preserve"
    assert not list(distribution.glob("LoLReplayTool-installer-audit-*"))


def test_overlapping_roots_fail_closed(tmp_path: Path) -> None:
    distribution = tmp_path / "distribution"
    _write_distribution(distribution)
    installed = distribution / "nested payload"
    _write_distribution(installed)

    errors = audit.audit_installer_payload(distribution, installed)

    assert errors == [
        "Validated distribution root and installer payload root must be disjoint."
    ]


def test_reparse_point_in_payload_fails_closed(tmp_path: Path) -> None:
    distribution, installed = _matching_roots(tmp_path)
    external = tmp_path / "external"
    external.mkdir()
    link = installed / "linked-content"
    try:
        link.symlink_to(external, target_is_directory=True)
    except (NotImplementedError, OSError):
        pytest.skip("directory symlink creation is unavailable")

    errors = audit.audit_installer_payload(distribution, installed)

    assert any("Links and reparse points are forbidden" in error for error in errors)


def test_inno_audit_mode_disables_external_install_side_effects() -> None:
    audit_script = (
        REPOSITORY_ROOT / "scripts" / "audit_installer_contents.ps1"
    ).read_text(encoding="utf-8")

    assert audit.validate_inno_audit_guards(
        REPOSITORY_ROOT / "installer" / "LoLReplayTool.iss"
    ) == []
    assert '"/NOCLOSEAPPLICATIONS"' in audit_script


@pytest.mark.parametrize(
    ("section", "entry"),
    [
        ("Registry", 'Root: HKCU; Subkey: "Software\\Unsafe"'),
        ("INI", 'Filename: "{localappdata}\\unsafe.ini"; Section: "test"'),
        ("InstallDelete", 'Type: filesandordirs; Name: "{localappdata}\\Unsafe"'),
    ],
)
def test_inno_structure_rejects_unguarded_external_write_sections(
    tmp_path: Path,
    section: str,
    entry: str,
) -> None:
    source = (REPOSITORY_ROOT / "installer" / "LoLReplayTool.iss").read_text(
        encoding="utf-8"
    )
    candidate = tmp_path / "unsafe.iss"
    candidate.write_text(f"{source}\n[{section}]\n{entry}\n", encoding="utf-8")

    errors = audit.validate_inno_audit_guards(candidate)

    assert any("lacks explicit audit guard" in error for error in errors)


def test_inno_structure_accepts_explicitly_guarded_registry_entry(
    tmp_path: Path,
) -> None:
    source = (REPOSITORY_ROOT / "installer" / "LoLReplayTool.iss").read_text(
        encoding="utf-8"
    )
    candidate = tmp_path / "guarded.iss"
    candidate.write_text(
        source
        + "\n[Registry]\n"
        + 'Root: HKCU; Subkey: "Software\\Safe"; '
        + "Check: not IsContentAuditMode\n",
        encoding="utf-8",
    )

    assert audit.validate_inno_audit_guards(candidate) == []


@pytest.mark.parametrize(
    "replacement",
    [
        "Result := False;",
        "Result := ExpandConstant('{param:other|}') = '1';",
    ],
)
def test_inno_structure_rejects_changed_audit_mode_semantics(
    tmp_path: Path,
    replacement: str,
) -> None:
    source = (REPOSITORY_ROOT / "installer" / "LoLReplayTool.iss").read_text(
        encoding="utf-8"
    )
    original = "Result := ExpandConstant('{param:contentaudit|}') = '1';"
    assert original in source
    candidate = tmp_path / "unsafe-audit-mode.iss"
    candidate.write_text(source.replace(original, replacement), encoding="utf-8")

    errors = audit.validate_inno_audit_guards(candidate)

    assert any("fixed /CONTENTAUDIT parameter semantics" in error for error in errors)


@pytest.mark.parametrize("separator", ["\n", " "])
def test_inno_structure_rejects_new_install_time_code_event(
    tmp_path: Path,
    separator: str,
) -> None:
    source = (REPOSITORY_ROOT / "installer" / "LoLReplayTool.iss").read_text(
        encoding="utf-8"
    )
    candidate = tmp_path / "unsafe-code-event.iss"
    candidate.write_text(
        source
        + separator
        + "procedure CurStepChanged(CurStep: TSetupStep);\n"
        + "begin\n"
        + "  SaveStringToFile(\n"
        + "    ExpandConstant('{localappdata}\\unsafe.txt'),\n"
        + "    'unsafe', False);\n"
        + "end;\n",
        encoding="utf-8",
    )

    errors = audit.validate_inno_audit_guards(candidate)

    assert any("declarations differ from the audit-safe allowlist" in error for error in errors)


def test_inno_structure_requires_initialize_wizard_guard_before_side_effects(
    tmp_path: Path,
) -> None:
    source = (REPOSITORY_ROOT / "installer" / "LoLReplayTool.iss").read_text(
        encoding="utf-8"
    )
    original = "begin\n  if IsContentAuditMode then\n    Exit;"
    replacement = (
        "begin\n"
        "  SaveStringToFile(\n"
        "    ExpandConstant('{localappdata}\\unsafe.txt'), 'unsafe', False);\n"
        "  if IsContentAuditMode then\n"
        "    Exit;"
    )
    assert original in source
    candidate = tmp_path / "late-code-guard.iss"
    candidate.write_text(source.replace(original, replacement, 1), encoding="utf-8")

    errors = audit.validate_inno_audit_guards(candidate)

    assert any("exit before side effects" in error for error in errors)


def test_inno_structure_requires_prepare_to_install_guard_before_side_effects(
    tmp_path: Path,
) -> None:
    source = (REPOSITORY_ROOT / "installer" / "LoLReplayTool.iss").read_text(
        encoding="utf-8"
    )
    original = (
        "function PrepareToInstall(var NeedsRestart: Boolean): String;\n"
        "begin\n"
        "  if IsContentAuditMode then"
    )
    replacement = (
        "function PrepareToInstall(var NeedsRestart: Boolean): String;\n"
        "begin\n"
        "  Sleep(1);\n"
        "  if IsContentAuditMode then"
    )
    assert original in source
    candidate = tmp_path / "late-prepare-to-install-guard.iss"
    candidate.write_text(source.replace(original, replacement, 1), encoding="utf-8")

    errors = audit.validate_inno_audit_guards(candidate)

    assert any("PrepareToInstall must exit before side effects" in error for error in errors)


def test_inno_update_shutdown_protocol_is_fail_closed_and_never_targets_obs_by_name():
    source = (REPOSITORY_ROOT / "installer" / "LoLReplayTool.iss").read_text(
        encoding="utf-8"
    )

    assert "Result := RequestSafeUpdateShutdown;" in source
    assert "Local\\LoLReplayTool.UpdateShutdown" in source
    assert "Local\\LoLReplayTool.UpdateShutdownBlocked" in source
    assert "Local\\LoLReplayTool.UpdateShutdownComplete" in source
    assert "LoLReplayTool.SingleInstance" in source
    assert "試合終了後に更新を再試行" in source
    assert "if not ShutdownCompleted then" in source
    assert "if ShutdownCompleted then" in source
    assert "taskkill" not in source.casefold()
    assert "obs64" not in source.casefold()


def test_inno_runtime_prerequisite_is_checked_before_update_shutdown() -> None:
    source = (REPOSITORY_ROOT / "installer" / "LoLReplayTool.iss").read_text(
        encoding="utf-8"
    )

    assert "PrivilegesRequired=lowest" in source
    assert "VisualCppRuntimeKey = 'SOFTWARE\\Microsoft\\VisualStudio\\14.0\\VC\\Runtimes\\x64'" in source
    assert "VisualCppRuntimeMinimumVersion = '14.44.35211.0'" in source
    assert "RegQueryDWordValue(HKLM64" in source
    assert "RegQueryDWordValue(HKLM32" in source
    assert "Installed64 <> 1" in source
    assert "RegQueryStringValue(HKLM64" in source
    assert "StrToVersion(VersionText, PackedVersion)" in source
    assert "ComparePackedVersion(Version64Packed, MinimumVersionPacked) < 0" in source
    assert "RegKeyExists(HKLM32" in source
    assert "ComparePackedVersion(Version32Packed, Version64Packed) <> 0" in source
    assert "WizardSilent" in source
    assert "ShellExec('open', VisualCppRuntimeHelpURL" in source
    assert "vc_redist.x64.exe" not in source.casefold()
    assert "DownloadTemporaryFile" not in source
    assert "\n  Exec(" not in source
    prepare = source[source.index("function PrepareToInstall"):]
    assert prepare.index("Result := CheckVisualCppRuntime;") < prepare.index(
        "Result := RequestSafeUpdateShutdown;"
    )


def test_inno_runtime_prerequisite_code_is_in_audit_allowlist() -> None:
    assert audit.validate_inno_audit_guards(
        REPOSITORY_ROOT / "installer" / "LoLReplayTool.iss"
    ) == []


@pytest.mark.parametrize(
    "replacement",
    [
        "Result := CheckVisualCppRuntime;\n  Result := RequestSafeUpdateShutdown;",
        "Result := RequestSafeUpdateShutdown;\n  Result := CheckVisualCppRuntime;",
    ],
)
def test_inno_runtime_prerequisite_order_change_is_detectable(
    tmp_path: Path, replacement: str
) -> None:
    source = (REPOSITORY_ROOT / "installer" / "LoLReplayTool.iss").read_text(
        encoding="utf-8"
    )
    original = "Result := CheckVisualCppRuntime;\n  if Result <> '' then\n    Exit;\n  Result := RequestSafeUpdateShutdown;"
    assert original in source
    candidate = tmp_path / "runtime-order.iss"
    candidate.write_text(source.replace(original, replacement, 1), encoding="utf-8")

    assert audit.validate_inno_audit_guards(candidate)


@pytest.mark.parametrize(
    ("original", "replacement"),
    [
        (
            "StrToVersion(VersionText, PackedVersion)",
            "True",
        ),
        (
            "ComparePackedVersion(Version64Packed, MinimumVersionPacked) < 0",
            "ComparePackedVersion(Version64Packed, MinimumVersionPacked) >= 0",
        ),
        (
            "ComparePackedVersion(Version32Packed, Version64Packed) <> 0",
            "ComparePackedVersion(Version32Packed, Version64Packed) = 0",
        ),
        ("if not WizardSilent then", "if WizardSilent then"),
        (
            "ShellExec('open', VisualCppRuntimeHelpURL",
            "ShellExec('runas', VisualCppRuntimeHelpURL",
        ),
    ],
)
def test_inno_runtime_prerequisite_weakening_is_detectable(
    tmp_path: Path, original: str, replacement: str
) -> None:
    source = (REPOSITORY_ROOT / "installer" / "LoLReplayTool.iss").read_text(
        encoding="utf-8"
    )
    assert original in source
    candidate = tmp_path / "runtime-weakened.iss"
    candidate.write_text(source.replace(original, replacement, 1), encoding="utf-8")

    assert audit.validate_inno_audit_guards(candidate)


@pytest.mark.parametrize("call", ["Exec('vc_redist.x64.exe'", "DownloadTemporaryFile("])
def test_inno_runtime_automatic_acquisition_is_detectable(
    tmp_path: Path, call: str
) -> None:
    source = (REPOSITORY_ROOT / "installer" / "LoLReplayTool.iss").read_text(
        encoding="utf-8"
    )
    marker = "function CheckVisualCppRuntime: String;"
    assert marker in source
    candidate = tmp_path / "runtime-auto-install.iss"
    candidate.write_text(
        source.replace(marker, call + "\n" + marker, 1),
        encoding="utf-8",
    )

    assert audit.validate_inno_audit_guards(candidate)


@pytest.mark.parametrize(
    ("original", "replacement", "expected"),
    [
        (
            'Source: "..\\dist\\LoLReplayTool\\*"',
            'Source: "..\\downloads\\OBS-Studio.zip"',
            "source must be under validated dist",
        ),
        (
            'DestDir: "{app}"',
            'DestDir: "{localappdata}\\Outside"',
            "destination must stay under {app}",
        ),
        (
            "Flags: ignoreversion recursesubdirs createallsubdirs",
            "Flags: ignoreversion recursesubdirs createallsubdirs regserver",
            "audit-unsafe flags",
        ),
        (
            'DestDir: "{app}"',
            'DestDir: "{app}\\{code:Outside}"',
            "code constant cannot be audited",
        ),
        (
            "Flags: ignoreversion recursesubdirs createallsubdirs",
            "Flags: ignoreversion recursesubdirs createallsubdirs; "
            'DestName: "..\\outside.exe"',
            "DestName is unsafe",
        ),
        (
            "Flags: ignoreversion recursesubdirs createallsubdirs",
            "Flags: ignoreversion recursesubdirs createallsubdirs; "
            "AfterInstall: Escape()",
            "audit-unsafe fields",
        ),
    ],
)
def test_inno_structure_rejects_files_that_escape_audit_boundary(
    tmp_path: Path,
    original: str,
    replacement: str,
    expected: str,
) -> None:
    source = (REPOSITORY_ROOT / "installer" / "LoLReplayTool.iss").read_text(
        encoding="utf-8"
    )
    assert original in source
    candidate = tmp_path / "unsafe-files.iss"
    candidate.write_text(source.replace(original, replacement), encoding="utf-8")

    errors = audit.validate_inno_audit_guards(candidate)

    assert any(expected in error for error in errors)


def test_inno_structure_rejects_hidden_preprocessor_entries(tmp_path: Path) -> None:
    source = (REPOSITORY_ROOT / "installer" / "LoLReplayTool.iss").read_text(
        encoding="utf-8"
    )
    candidate = tmp_path / "included.iss"
    candidate.write_text(source + '\n#include "side-effects.iss"\n', encoding="utf-8")

    errors = audit.validate_inno_audit_guards(candidate)

    assert any("preprocessor directives differ" in error for error in errors)


def test_inno_structure_rejects_define_that_can_generate_registry_content(
    tmp_path: Path,
) -> None:
    source = (REPOSITORY_ROOT / "installer" / "LoLReplayTool.iss").read_text(
        encoding="utf-8"
    )
    original = '#define AppName "LoL Replay Tool"'
    generated_registry = (
        '#define AppName "LoL Replay Tool" + NewLine + "[Registry]" + '
        'NewLine + "Root: HKCU; Subkey: Software\\Unsafe"'
    )
    assert original in source
    candidate = tmp_path / "generated-registry.iss"
    candidate.write_text(
        source.replace(original, generated_registry, 1),
        encoding="utf-8",
    )

    errors = audit.validate_inno_audit_guards(candidate)

    assert any("preprocessor directive is not audit-safe" in error for error in errors)


def test_inno_structure_rejects_inline_expression_that_generates_registry(
    tmp_path: Path,
) -> None:
    source = (REPOSITORY_ROOT / "installer" / "LoLReplayTool.iss").read_text(
        encoding="utf-8"
    )
    candidate = tmp_path / "inline-generated-registry.iss"
    candidate.write_text(
        source
        + "\n[Messages]\n"
        + '{#"[Registry]"}\n'
        + 'Root: HKCU; Subkey: "Software\\Escape"\n',
        encoding="utf-8",
    )

    errors = audit.validate_inno_audit_guards(candidate)

    assert any("inline preprocessor expansions differ" in error for error in errors)


def test_inno_structure_rejects_commented_inline_expression_that_generates_registry(
    tmp_path: Path,
) -> None:
    source = (REPOSITORY_ROOT / "installer" / "LoLReplayTool.iss").read_text(
        encoding="utf-8"
    )
    candidate = tmp_path / "commented-inline-generated-registry.iss"
    candidate.write_text(
        source
        + "\n[Messages]\n"
        + '; {#NewLine + "[Registry]" + NewLine + '
        + '"Root: HKCU; Subkey: Software\\Escape"}\n',
        encoding="utf-8",
    )

    errors = audit.validate_inno_audit_guards(candidate)

    assert any("inline preprocessor expansions differ" in error for error in errors)


@pytest.mark.parametrize(
    "hidden_content",
    [
        '#emit "[Registry]"',
        "#for {Index = 0; Index < 1; Index++}",
        "[UnknownInstallSection]",
    ],
)
def test_inno_structure_fails_closed_on_dynamic_or_unknown_content(
    tmp_path: Path,
    hidden_content: str,
) -> None:
    source = (REPOSITORY_ROOT / "installer" / "LoLReplayTool.iss").read_text(
        encoding="utf-8"
    )
    candidate = tmp_path / "dynamic.iss"
    candidate.write_text(f"{source}\n{hidden_content}\n", encoding="utf-8")

    assert audit.validate_inno_audit_guards(candidate)


def test_inno_validation_cli_can_run_before_installer_extraction(
    capsys: pytest.CaptureFixture[str],
) -> None:
    script = REPOSITORY_ROOT / "installer" / "LoLReplayTool.iss"

    assert (
        audit.main(
            [
                "--inno-script",
                str(script),
                "--validate-inno-only",
            ]
        )
        == 0
    )
    assert "Inno audit structure validation passed" in capsys.readouterr().out


@pytest.mark.parametrize("workflow", ["ci.yml", "release.yml"])
def test_windows_workflows_run_finished_installer_audit(workflow: str) -> None:
    source = (
        REPOSITORY_ROOT / ".github" / "workflows" / workflow
    ).read_text(encoding="utf-8")

    assert "Audit finished installer contents" in source
    assert ".\\scripts\\audit_installer_contents.ps1" in source
    assert "Test installer audit failure isolation" in source
    assert ".\\scripts\\test_installer_audit_failure.ps1" in source
    assert "-TempRoot $env:RUNNER_TEMP" in source


def test_audit_runner_validates_inno_before_starting_setup() -> None:
    source = (
        REPOSITORY_ROOT / "scripts" / "audit_installer_contents.ps1"
    ).read_text(encoding="utf-8")

    preflight = source.index("--validate-inno-only")
    setup_start = source.index("$process.Start()")
    payload_audit = source.index("--distribution-root", preflight)

    assert preflight < setup_start < payload_audit
    assert '"/CONTENTAUDIT=1"' in source
    assert '"--output-receipt", $resolvedReceipt' in source


def test_release_workflow_binds_assets_to_finished_installer_audit() -> None:
    source = (
        REPOSITORY_ROOT / ".github" / "workflows" / "release.yml"
    ).read_text(encoding="utf-8")

    audit = source.index("name: Audit finished installer contents")
    create = source.index("name: Create and verify release assets")
    assert "-OutputReceipt $auditReceipt" in source[audit:create]
    assert "INSTALLER_AUDIT_RECEIPT=$auditReceipt" in source[audit:create]
    assert "--installer-audit-receipt $env:INSTALLER_AUDIT_RECEIPT" in source[
        create:
    ]


def test_failure_isolation_runner_accepts_only_payload_mismatch_failure() -> None:
    source = (
        REPOSITORY_ROOT / "scripts" / "test_installer_audit_failure.ps1"
    ).read_text(encoding="utf-8")

    assert "$expectedFailure.Exception.Message -cne $expectedFailureMessage" in source
    assert "意図したpayload不一致以外" in source


@pytest.mark.skipif(POWERSHELL is None, reason="PowerShell is unavailable")
@pytest.mark.parametrize(
    ("protected_flow", "expected_returncode"),
    [
        ("$global:LASTEXITCODE = 1\ntry { } finally { }", 0),
        ('try { throw "body failure" } finally { }', 1),
        ('try { } finally { throw "cleanup failure" }', 1),
    ],
)
def test_failure_isolation_runner_resets_exit_code_only_after_cleanup(
    tmp_path: Path,
    protected_flow: str,
    expected_returncode: int,
) -> None:
    source = (
        REPOSITORY_ROOT / "scripts" / "test_installer_audit_failure.ps1"
    ).read_text(encoding="utf-8")
    success_reset = "$global:LASTEXITCODE = 0"

    assert source.rstrip().endswith(success_reset)
    assert source.count(success_reset) == 1

    harness = tmp_path / "failure-isolation-exit-code.ps1"
    harness.write_text(
        '$ErrorActionPreference = "Stop"\n'
        + protected_flow
        + "\n"
        + success_reset
        + "\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-File", str(harness)],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    assert result.returncode == expected_returncode


def test_failure_isolation_runner_keeps_mismatch_dist_outside_audit_temp() -> None:
    source = (
        REPOSITORY_ROOT / "scripts" / "test_installer_audit_failure.ps1"
    ).read_text(encoding="utf-8")

    assert "$resolvedDistributionParent = Resolve-RealDirectory" in source
    assert (
        "$negativeRoot = Join-Path `\n"
        "  $resolvedDistributionParent `\n"
        in source
    )
    assert "Test-PathsOverlap -First $expectedMismatch" in source
    assert "-Second $resolvedTempRoot" in source
    assert (
        "-Path $negativeRoot `\n"
        "      -AllowedRoot $resolvedDistributionParent `\n"
        in source
    )
    assert (
        '$negativeRoot = Join-Path $resolvedTempRoot '
        '"LoLReplayTool-installer-negative-'
        not in source
    )
