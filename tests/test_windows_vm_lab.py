from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

POWERSHELL = shutil.which("pwsh")
WINDOWS_POWERSHELL = shutil.which("powershell")
LAB_SCRIPT = Path("scripts/windows_vm_lab.ps1").resolve()
GUEST_SCRIPT = Path("scripts/windows_vm_lab_guest.ps1").resolve()
BOOTSTRAP_SCRIPT = Path("scripts/windows_vm_lab_bootstrap.ps1").resolve()
ISO_BUILD_SCRIPT = Path("scripts/build_windows_vm_lab_iso.ps1").resolve()
ENVIRONMENT_B_SCRIPT = Path("scripts/windows_vm_lab_environment_b.ps1").resolve()


def test_clr0400_evidence_boundary_is_present_and_fail_closed():
    bootstrap = BOOTSTRAP_SCRIPT.read_text(encoding="utf-8")
    guest = GUEST_SCRIPT.read_text(encoding="utf-8")
    host = LAB_SCRIPT.read_text(encoding="utf-8")
    docs = Path("docs/windows-vm-lab.md").read_text(encoding="utf-8")
    names = (
        "msvcp140_clr0400.dll",
        "vcruntime140_clr0400.dll",
        "vcruntime140_1_clr0400.dll",
    )
    for name in names:
        assert bootstrap.count(name) >= 1
        assert guest.count(name) >= 1
        assert name in docs
    for source in (bootstrap, guest):
        assert "Get-AuthenticodeSignature" in source
        assert "OriginalFilename" in source
        assert "hardlink list $path" in source
        assert "/verifyfile=$path" in source
        assert "sfc_exit_code" in source
        assert "amd64_netfx4-" in source
    assert "clr0400_evidence" in host
    assert "schema_version -ne 3" in host
    assert "Test-Clr0400EvidencePolicy" in bootstrap
    assert "Compare-Object" in bootstrap
    assert 'expectedGuestSchema = if ($GuestAction -ceq "Inspect") { 3 } else { 1 }' in host
    assert "Test-Clr0400EvidencePolicy -Evidence $Inspection.bootstrap.clr0400_evidence" in host


@pytest.mark.skipif(POWERSHELL is None, reason="pwsh is unavailable")
@pytest.mark.parametrize("script", [BOOTSTRAP_SCRIPT, GUEST_SCRIPT])
def test_clr0400_policy_helper_rejects_incomplete_or_invalid_sets(script: Path):
    command = r'''
$tree = $null; $errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($env:TARGET_SCRIPT, [ref]$tree, [ref]$errors)
$fn = $ast.Find({ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq "Test-Clr0400EvidencePolicy" }, $true)
. ([scriptblock]::Create($fn.Extent.Text))
$names = @("msvcp140_clr0400.dll","vcruntime140_clr0400.dll","vcruntime140_1_clr0400.dll")
$cases = @(
  @([pscustomobject]@{ valid=$true; exact_set=$true; observed_names=$names }, $true),
  @([pscustomobject]@{ valid=$false; exact_set=$true; observed_names=$names }, $false),
  @([pscustomobject]@{ valid=$true; exact_set=$false; observed_names=@() }, $false),
  @([pscustomobject]@{ valid=$true; exact_set=$true; observed_names=@($names[0]) }, $false),
  @([pscustomobject]@{ valid=$true; exact_set=$true; observed_names=@($names + "extra.dll") }, $false)
)
foreach ($case in $cases) { if ((Test-Clr0400EvidencePolicy -Evidence $case[0]) -ne $case[1]) { exit 1 } }
'''
    result = subprocess.run(
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", command],
        env={**os.environ, "TARGET_SCRIPT": str(script)},
        capture_output=True, text=True, check=False, timeout=30,
    )
    assert result.returncode == 0, result.stderr


@pytest.fixture
def external_temp() -> Iterator[Path]:
    runner_temp = os.environ.get("RUNNER_TEMP")
    parent = runner_temp if runner_temp and Path(runner_temp).is_dir() else None
    with tempfile.TemporaryDirectory(
        prefix="lol-vm-lab-test-",
        dir=parent,
    ) as directory:
        path = Path(directory).resolve()
        assert not path.is_relative_to(Path.cwd().resolve())
        yield path


def _config(external_temp: Path, **overrides: object) -> tuple[Path, dict[str, object]]:
    vmrun = external_temp / "vmrun.exe"
    vmrun.write_bytes(b"test vmrun placeholder\n")
    vm_name = "LoLReplayTool-VC-Runtime-Lab"
    vmx = external_temp / vm_name / f"{vm_name}.vmx"
    vmx.parent.mkdir()
    iso = external_temp / "payload.iso"
    iso.write_bytes(b"fixed test ISO payload\n")
    vmx.write_text(
        "\n".join(
            (
                f'displayName = "{vm_name}"',
                'uuid.bios = "56 4d 11 22 33 44 55 66-77 88 99 aa bb cc dd ee"'.replace(
                    "-", " "
                ),
                'vmx.encryptionType = "partial"',
                'encryption.keySafe = "fixed-test-key-safe"',
                'encryption.data = "fixed-test-encryption-data"',
                'vtpm.present = "TRUE"',
                'virtualHW.version = "23"',
                'guestOS = "windows11-64"',
                'firmware = "efi"',
                'uefi.secureBoot.enabled = "TRUE"',
                'memsize = "4096"',
                'numvcpus = "2"',
                'nvme0.present = "TRUE"',
                'nvme0:0.present = "TRUE"',
                f'nvme0:0.fileName = "{vm_name}-000001.vmdk"',
                'usb.present = "TRUE"',
                'ethernet0.present = "TRUE"',
                'ethernet0.connectionType = "custom"',
                'ethernet0.vnet = "VMnet1"',
                'ethernet0.startConnected = "TRUE"',
                'ethernet0.generatedAddress = "00:0c:29:11:22:33"',
                'sata0:1.present = "TRUE"',
                'sata0:1.deviceType = "cdrom-image"',
                f'sata0:1.fileName = "{iso}"',
                'sata0:1.startConnected = "TRUE"',
                'isolation.tools.copy.disable = "TRUE"',
                'isolation.tools.paste.disable = "TRUE"',
                'isolation.tools.dnd.disable = "TRUE"',
                'isolation.tools.hgfs.disable = "TRUE"',
                'sharedFolder.maxNum = "0"',
                "",
            )
        ),
        encoding="utf-8",
    )
    (vmx.parent / f"{vm_name}.vmdk").write_text(
        "# Disk DescriptorFile\n"
        "version=1\n"
        "CID=11111111\n"
        "parentCID=ffffffff\n"
        'createType="monolithicSparse"\n',
        encoding="ascii",
    )
    (vmx.parent / f"{vm_name}-000001.vmdk").write_text(
        "# Disk DescriptorFile\n"
        "version=1\n"
        "CID=22222222\n"
        "parentCID=11111111\n"
        'createType="monolithicSparse"\n'
        f'parentFileNameHint="{vm_name}.vmdk"\n',
        encoding="ascii",
    )
    snapshot_state = vmx.with_name(f"{vm_name}-Snapshot1.vmsn")
    snapshot_state.write_bytes(b"fixed powered-off snapshot state\n")
    vmx.with_suffix(".vmsd").write_text(
        "\n".join(
            (
                'snapshot.numSnapshots = "1"',
                'snapshot.current = "1"',
                'snapshot0.uid = "1"',
                'snapshot0.displayName = "A0-runtime-absent"',
                'snapshot0.parent = ""',
                f'snapshot0.filename = "{snapshot_state.name}"',
                'snapshot0.createTimeHigh = "123"',
                'snapshot0.createTimeLow = "456"',
                'snapshot0.numDisks = "1"',
                f'snapshot0.disk0.fileName = "{vm_name}.vmdk"',
                'snapshot0.disk0.node = "nvme0:0"',
                'snapshot0.type = "1"',
                "",
            )
        ),
        encoding="utf-8",
    )
    dhcp_config = external_temp / "vmnetdhcp.conf"
    dhcp_config.write_text(
        "subnet 192.168.20.0 netmask 255.255.255.0 {\n}\n"
        "host vmnet1 {\n  fixed-address 192.168.20.1;\n}\n",
        encoding="utf-8",
    )
    nat_config = external_temp / "vmnetnat.conf"
    nat_config.write_text("device = vmnet8\n", encoding="utf-8")
    values: dict[str, object] = {
        "schema_version": 3,
        "vmrun_path": str(vmrun),
        "vmx_path": str(vmx),
        "vm_encryption_credential_path": str(
            external_temp / "vm-encryption.credential.xml"
        ),
        "expected_vm_uuid": "capture",
        "expected_vm_encryption_type": "capture",
        "expected_guest_mac": "capture",
        "vmx_file_sha256": "capture",
        "vm_definition_fingerprint_sha256": "capture",
        "snapshot_a": "A0-runtime-absent",
        "snapshot_uid": "capture",
        "snapshot_fingerprint_sha256": "capture",
        "vmware_network": "vmnet1",
        "vmware_dhcp_config_path": str(dhcp_config),
        "vmware_nat_config_path": str(nat_config),
        "host_address": "192.168.20.1",
        "guest_address": "192.168.20.10",
        "guest_credential_path": str(external_temp / "guest.credential.xml"),
        "payload_iso_path": str(iso),
        "payload_iso_sha256": hashlib.sha256(iso.read_bytes()).hexdigest(),
        "payload_volume_label": "LOLVC134",
        "runtime_installer_relative_path": "runtime/vc_redist.x64.exe",
        "installer_relative_path": "installer/LoLReplayTool-Setup-0.5.2.exe",
        "installer_sha256": "f" * 64,
        "runtime_installer_sha256": "1" * 64,
        "minimum_runtime_version": "14.44.35211.0",
        "app_relative_path": "app/LoLReplayTool.exe",
        "app_sha256": "2" * 64,
        "environment_b_script_relative_path": "02-test-environment-b.ps1",
        "environment_b_script_sha256": "3" * 64,
        "payload_commit": "1d5f79209646edda33911470ed132a9d5f4d440c",
        "artifact_root": str(external_temp / "evidence"),
    }
    config = external_temp / "lab.json"
    config.write_text(json.dumps(values), encoding="utf-8")
    capture = _json_output(_run_lab(config, "Capture"))
    values.update(capture["replacement_values"])
    values.update(overrides)
    config.write_text(json.dumps(values), encoding="utf-8")
    return config, values


def _run_lab(
    config: Path,
    action: str,
    *arguments: str,
    powershell: str | None = POWERSHELL,
) -> subprocess.CompletedProcess[str]:
    if powershell is None:
        pytest.skip("requested PowerShell runtime is unavailable")
    return subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(LAB_SCRIPT),
            "-Action",
            action,
            "-ConfigPath",
            str(config),
            *arguments,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )


def _json_output(result: subprocess.CompletedProcess[str]) -> dict[str, object]:
    assert result.returncode == 0, result.stdout + "\n" + result.stderr
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert lines, result.stdout + "\n" + result.stderr
    return json.loads(lines[-1])


def test_schema_v2_and_invalid_installer_fields_fail_closed(external_temp: Path):
    config, values = _config(external_temp)
    values["schema_version"] = 2
    config.write_text(json.dumps(values), encoding="utf-8")
    rejected = _run_lab(config, "Plan")
    assert rejected.returncode != 0
    values["schema_version"] = 3
    values.pop("installer_sha256")
    config.write_text(json.dumps(values), encoding="utf-8")
    rejected = _run_lab(config, "Plan")
    assert rejected.returncode != 0


@pytest.mark.parametrize(
    "installer_path",
    ["../setup.exe", "C:/setup.exe", "C:setup.exe", r"\setup.exe", "//server/setup.exe"],
)
def test_installer_path_must_be_relative_without_parent(external_temp: Path, installer_path: str):
    config, values = _config(external_temp)
    values["installer_relative_path"] = installer_path
    config.write_text(json.dumps(values), encoding="utf-8")
    rejected = _run_lab(config, "Plan")
    assert rejected.returncode != 0


def test_installer_actions_are_forwarded_and_host_validated():
    host = LAB_SCRIPT.read_text(encoding="utf-8")
    guest = GUEST_SCRIPT.read_text(encoding="utf-8")
    assert '"InstallerEnvironmentA"' in host
    assert '"InstallerEnvironmentB"' in host
    assert "$Config.installer_relative_path" in host
    assert "$Config.installer_sha256" in host
    assert "$result.controlled_cleanup" in host
    assert "$result.uninstall_exit_code -ne 0" in host
    assert "ProcessStartInfo" in guest
    assert "$startInfo.Arguments =" in guest
    assert "ArgumentList.Add" not in guest
    assert "GetRelativePath" not in guest
    assert "RandomNumberGenerator]::Fill" not in guest
    assert "ConvertTo-NativeArgument" not in guest
    assert "process argumentに空白またはquote" in guest
    assert "$result.result.log_cleanup" in host
    assert 'Where-Object { $_.path -ceq "vm-a-sentinel.bin" }' in host
    assert 'Where-Object { $_.path -ceq "vm-b-update-sentinel.bin" }' in host
    assert "InstallerEnvironmentA" in guest and "InstallerEnvironmentB" in guest
    assert "$installerB.a_sentinel_sha256 -cne $installerA.sentinel_sha256" in host


@pytest.mark.skipif(
    WINDOWS_POWERSHELL is None, reason="Windows PowerShell is unavailable"
)
def test_uninstall_allows_only_empty_bin_directory_removal():
    command = r'''
$tokens = $null; $errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
  $env:TARGET_SCRIPT, [ref]$tokens, [ref]$errors
)
foreach ($name in @("ConvertTo-StateJson", "Assert-UserDataPreservedAfterUninstall")) {
  $fn = $ast.Find({ param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
    $node.Name -eq $name
  }, $true)
  if ($null -eq $fn) { throw "helper not found: $name" }
  . ([scriptblock]::Create($fn.Extent.Text))
}
function Assert-Accepts($before, $after) {
  Assert-UserDataPreservedAfterUninstall -Before $before -After $after -Label "case"
}
$base = [ordered]@{
  exists = $true
  entries = @(
    [ordered]@{ path = "recordings"; type = "directory" },
    [ordered]@{ path = "settings.json"; type = "file"; size = 1; sha256 = "a" },
    [ordered]@{ path = "bin"; type = "directory" }
  )
}
$exact = $base | ConvertTo-Json -Depth 10 | ConvertFrom-Json
Assert-Accepts $exact ($base | ConvertTo-Json -Depth 10 | ConvertFrom-Json)
$withoutBin = [ordered]@{
  exists = $true
  entries = @($base.entries | Where-Object { $_.path -cne "bin" })
}
Assert-Accepts $exact ($withoutBin | ConvertTo-Json -Depth 10 | ConvertFrom-Json)
$withBinChild = [ordered]@{
  exists = $true
  entries = @($base.entries + [ordered]@{ path = "bin/child.txt"; type = "file"; size = 1; sha256 = "b" })
}
$removedBinAndChild = [ordered]@{
  exists = $true
  entries = @($withBinChild.entries | Where-Object { $_.path -notlike "bin*" })
}
$failed = $false
try {
  Assert-UserDataPreservedAfterUninstall `
    -Before ($withBinChild | ConvertTo-Json -Depth 10 | ConvertFrom-Json) `
    -After ($removedBinAndChild | ConvertTo-Json -Depth 10 | ConvertFrom-Json) `
    -Label "removed bin subtree"
} catch { $failed = $true }
if (-not $failed) { throw "removed populated bin subtree was accepted" }
$badCases = @(
  [ordered]@{ exists = $true; entries = @($base.entries + [ordered]@{ path = "new.txt"; type = "file"; size = 1; sha256 = "b" }) },
  [ordered]@{ exists = $true; entries = @($base.entries | Where-Object { $_.path -cne "settings.json" }) },
  [ordered]@{ exists = $true; entries = @($base.entries + [ordered]@{ path = "bin\child.txt"; type = "file"; size = 1; sha256 = "b" }) },
  [ordered]@{ exists = $false; entries = @() }
)
foreach ($bad in $badCases) {
  $failed = $false
  try { Assert-UserDataPreservedAfterUninstall -Before $exact -After $bad -Label "bad" } catch { $failed = $true }
  if (-not $failed) { throw "invalid uninstall tree was accepted" }
}
'''
    result = subprocess.run(
        [WINDOWS_POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", command],
        env={**os.environ, "TARGET_SCRIPT": str(GUEST_SCRIPT)},
        capture_output=True, text=True, check=False, timeout=30,
    )
    assert result.returncode == 0, result.stdout + "\n" + result.stderr


@pytest.mark.skipif(
    WINDOWS_POWERSHELL is None, reason="Windows PowerShell is unavailable"
)
def test_invoke_installer_accepts_empty_arguments_in_windows_powershell():
    """PowerShell 5.1 treats a mandatory empty string array as unbound."""
    source = GUEST_SCRIPT.read_text(encoding="utf-8-sig")
    assert (
        "[Parameter(Mandatory = $true)][AllowEmptyCollection()][string[]]$Arguments"
        in source
    )
    command = r'''
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
  $env:TARGET_SCRIPT, [ref]$null, [ref]$null
)
$fn = $ast.Find({ param($node)
  $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
  $node.Name -eq "Invoke-Installer"
}, $true)
$paramBlock = $fn.Body.ParamBlock.Extent.Text
. ([scriptblock]::Create("function Test-EmptyArguments { $paramBlock; return `$Arguments.Count }"))
if ((Test-EmptyArguments -Paths ([pscustomobject]@{}) -Arguments @()) -ne 0) { exit 1 }
'''
    result = subprocess.run(
        [
            WINDOWS_POWERSHELL,
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            command,
        ],
        env={**os.environ, "TARGET_SCRIPT": str(GUEST_SCRIPT)},
        capture_output=True, text=True, check=False, timeout=30,
    )
    assert result.returncode == 0, result.stdout + "\n" + result.stderr


@pytest.mark.skipif(os.name != "nt", reason="IMAPI2FS is Windows-only")
def test_iso_builder_creates_hashed_media_without_mutating_source(
    external_temp: Path,
):
    if WINDOWS_POWERSHELL is None:
        pytest.skip("Windows PowerShell is unavailable")
    source = external_temp / "source-kit"
    (source / "LoLReplayTool-external-build").mkdir(parents=True)
    (source / "installer").mkdir()
    (source / "evidence").mkdir()
    files = {
        "vc_redist.x64.exe": b"runtime",
        "installer/LoLReplayTool-Setup-0.5.2.exe": b"installer",
        "LoLReplayTool-external-build/LoLReplayTool.exe": b"app",
        "02-test-environment-b.ps1": b"untrusted external environment b",
        "run_packaged_self_check.ps1": b"runner",
        "evidence/package-sha256.csv": b"manifest",
        "evidence/pe-runtime-audit.json": b"pe audit",
        "evidence/external-vc-runtime-wheel-provenance.json": b"provenance",
        "create-test-iso.ps1": b"local-only builder",
        "vm-lab-media-manifest.json": b"old read-only manifest",
    }
    for relative_path, content in files.items():
        (source / relative_path).write_bytes(content)
    environment_b = source / "02-test-environment-b.ps1"
    old_manifest = source / "vm-lab-media-manifest.json"
    read_only_inputs = (environment_b, old_manifest)
    for path in read_only_inputs:
        path.chmod(0o444)
    output = external_temp / "media" / "managed.iso"
    output.parent.mkdir()
    payload_commit = "1" * 40

    try:
        result = subprocess.run(
            [
                WINDOWS_POWERSHELL,
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(ISO_BUILD_SCRIPT),
                "-SourceKitPath",
                str(source),
                "-OutputPath",
                str(output),
                "-PayloadCommit",
                payload_commit,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            check=False,
        )
    finally:
        for path in read_only_inputs:
            path.chmod(0o666)

    built = _json_output(result)
    assert output.is_file()
    assert built["sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()
    assert built["volume_label"] == "LOL_VC_PR134"
    assert built["payload_commit"] == payload_commit
    tracked_environment_b = ENVIRONMENT_B_SCRIPT.read_bytes()
    normalized_environment_b = (
        b"\xef\xbb\xbf"
        + tracked_environment_b.decode("utf-8-sig").encode("utf-8")
    )
    assert built["environment_b_script_sha256"] == hashlib.sha256(
        normalized_environment_b
    ).hexdigest()
    assert (source / "create-test-iso.ps1").read_bytes() == b"local-only builder"
    assert environment_b.read_bytes() == b"untrusted external environment b"
    assert old_manifest.read_bytes() == b"old read-only manifest"
    assert not (source / "00-Bootstrap-VM-Lab.cmd").exists()


def test_iso_builder_pins_tracked_runner_and_windows_powershell_encoding():
    source = ISO_BUILD_SCRIPT.read_text(encoding="utf-8-sig")
    manifest_start = source.index("$manifestFiles")
    manifest_end = source.index("$mediaManifest", manifest_start)

    assert '"installer\\LoLReplayTool-Setup-0.5.2.exe"' in source
    assert '$selfCheckRunner = Join-Path $scriptDirectory "run_packaged_self_check.ps1"' in source
    assert '$environmentBScript = Join-Path $scriptDirectory "windows_vm_lab_environment_b.ps1"' in source
    assert 'Copy-Item -LiteralPath $environmentBScript' in source
    assert '"02-test-environment-b.ps1"' in source[manifest_start:manifest_end]
    assert "$environmentBManifestEntries.Count -ne 1" in source
    assert "Copy-Item -LiteralPath $selfCheckRunner" in source
    assert '$utf8WithBom = [Text.UTF8Encoding]::new($true)' in source
    for name in (
        "02-test-environment-b.ps1",
        "run_packaged_self_check.ps1",
        "windows_vm_lab_bootstrap.ps1",
    ):
        assert name in source


@pytest.mark.parametrize(
    "powershell",
    [
        pytest.param(POWERSHELL, id="pwsh"),
        pytest.param(WINDOWS_POWERSHELL, id="windows-powershell-5.1"),
    ],
)
def test_vm_lab_powershell_files_parse_without_errors(powershell: str | None):
    if powershell is None:
        pytest.skip("requested PowerShell runtime is unavailable")
    paths = ", ".join(
        f"'{str(path).replace(chr(39), chr(39) * 2)}'"
        for path in (
            LAB_SCRIPT,
            GUEST_SCRIPT,
            BOOTSTRAP_SCRIPT,
            ISO_BUILD_SCRIPT,
            ENVIRONMENT_B_SCRIPT,
        )
    )
    command = (
        f"$paths = @({paths}); $failed = $false; "
        "foreach ($path in $paths) { "
        "$tokens = $null; $errors = $null; "
        "[System.Management.Automation.Language.Parser]::ParseFile("
        "$path, [ref]$tokens, [ref]$errors) | Out-Null; "
        "if ($errors.Count -ne 0) { $errors | Out-String | Write-Error; "
        "$failed = $true } }; if ($failed) { exit 1 }"
    )
    result = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            command,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stdout + "\n" + result.stderr


@pytest.mark.parametrize(
    "powershell",
    [
        pytest.param(POWERSHELL, id="pwsh"),
        pytest.param(WINDOWS_POWERSHELL, id="windows-powershell-5.1"),
    ],
)
@pytest.mark.skipif(os.name != "nt", reason="Windows manifest path policy is Windows-only")
def test_environment_b_manifest_path_accepts_windows_separators_and_rejects_escape(
    powershell: str | None, external_temp: Path
):
    if powershell is None:
        pytest.skip("requested PowerShell runtime is unavailable")
    root = external_temp / "package-root"
    (root / "_internal").mkdir(parents=True)
    (root / "_internal" / "_asyncio.pyd").write_bytes(b"fixture")
    command = r'''
$tokens = $null; $errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
  $env:TARGET_SCRIPT, [ref]$tokens, [ref]$errors
)
if ($errors.Count -ne 0) { throw ($errors | Out-String) }
$fn = $ast.Find({ param($node)
  $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
  $node.Name -eq "Resolve-SafeManifestPath"
}, $true)
if ($null -eq $fn) { throw "Resolve-SafeManifestPath was not found" }
. ([scriptblock]::Create($fn.Extent.Text))
$root = $env:PACKAGE_ROOT
$positive = @(
  "_internal/_asyncio.pyd",
  "_internal\_asyncio.pyd"
)
foreach ($path in $positive) {
  $resolved = Resolve-SafeManifestPath -Root $root -RelativePath $path
  if (-not (Test-Path -LiteralPath $resolved -PathType Leaf)) {
    throw "Expected manifest path was rejected or unresolved: $path"
  }
}
$negative = @(
  "../escape.pyd", "..\escape.pyd", ".\_asyncio.pyd", "_internal//x.pyd",
  "C:\escape.pyd", "C:escape.pyd", "\escape.pyd", "/escape.pyd",
  "\\server\share\escape.pyd", "_internal\_asyncio.pyd:stream"
)
$negative += "_internal/" + [char]0 + "bad.pyd"
foreach ($path in $negative) {
  $accepted = $false
  try {
    Resolve-SafeManifestPath -Root $root -RelativePath $path | Out-Null
    $accepted = $true
  } catch { }
  if ($accepted) { throw "Unsafe manifest path was accepted: $path" }
}
exit 0
'''
    result = subprocess.run(
        [powershell, "-NoProfile", "-NonInteractive", "-Command", command],
        env={
            **os.environ,
            "TARGET_SCRIPT": str(ENVIRONMENT_B_SCRIPT),
            "PACKAGE_ROOT": str(root),
        },
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + "\n" + result.stderr


@pytest.mark.parametrize("script", [GUEST_SCRIPT, BOOTSTRAP_SCRIPT])
def test_firewall_audit_is_active_store_batched_and_fail_closed(script: Path):
    source = script.read_text(encoding="utf-8-sig")

    if script == BOOTSTRAP_SCRIPT:
        assert (
            'param([ValidateSet("ActiveStore", "PersistentStore")]'
            '[string]$PolicyStore = "ActiveStore")'
        ) in source
        evidence_start = source.index("function Get-WinRmFirewallEvidence")
        evidence_end = source.index("\nAssert-Administrator", evidence_start + 1)
        evidence_function = source[evidence_start:evidence_end]
        inventory_calls = [
            line.strip()
            for line in evidence_function.splitlines()
            if "Get-ActiveInboundFirewallInventory" in line
        ]
        assert inventory_calls == ["$inventory = Get-ActiveInboundFirewallInventory"]
    else:
        assert source.count("-PolicyStore ActiveStore") >= 5
    assert "| Get-NetFirewallPortFilter" not in source
    assert "| Get-NetFirewallServiceFilter" not in source
    assert "InstanceIDが空です" in source
    assert "InstanceIDが重複しています" in source
    assert "filterがありません" in source


def test_plan_is_non_mutating_and_keeps_a_b_order(external_temp: Path):
    config, _ = _config(external_temp)

    result = _run_lab(config, "Plan")

    plan = _json_output(result)
    assert plan["action"] == "Plan"
    assert plan["mutates_vm"] is False
    assert plan["payload_commit"] == "1d5f79209646edda33911470ed132a9d5f4d440c"
    assert plan["steps"] == [
        "require VM powered off",
        "require exact VMX identity, isolated NIC/CD settings, and A0 fingerprint",
        "revert A0-runtime-absent",
        "start VM without GUI",
        "verify WinRM over host-only vmnet1",
        (
            "verify Environment A has no x64 Redistributable, VMware Tools, "
            "or default route"
        ),
        "run the completed installer in Environment A and verify exit code 7 with no state changes",
        "install fixed Microsoft-signed Redistributable from the hashed ISO",
        "verify Environment B Runtime version",
        "run the fixed packaged self-check with isolated data",
        "install, update, and silently uninstall the completed installer in Environment B",
        "write JSON evidence",
        "request guest OS shutdown",
    ]
    assert not Path(str(_config_value(config, "artifact_root"))).exists()


def test_capture_reports_exact_vm_and_snapshot_identity_without_mutation(
    external_temp: Path,
):
    config, values = _config(external_temp)
    for key in (
        "expected_vm_uuid",
        "expected_vm_encryption_type",
        "expected_guest_mac",
        "vmx_file_sha256",
        "vm_definition_fingerprint_sha256",
        "snapshot_uid",
        "snapshot_fingerprint_sha256",
    ):
        values[key] = "capture"
    config.write_text(json.dumps(values), encoding="utf-8")

    result = _run_lab(config, "Capture")

    capture = _json_output(result)
    assert capture["schema_version"] == 2
    assert capture["action"] == "Capture"
    assert capture["mutates_vm"] is False
    replacements = capture["replacement_values"]
    assert replacements["expected_vm_uuid"].startswith("56 4d")
    assert replacements["expected_guest_mac"] == "00:0c:29:11:22:33"
    assert replacements["snapshot_uid"] == "1"
    assert len(replacements["vmx_file_sha256"]) == 64
    assert len(replacements["vm_definition_fingerprint_sha256"]) == 64
    assert len(replacements["snapshot_fingerprint_sha256"]) == 64
    assert not Path(str(values["artifact_root"])).exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell is Windows-only")
def test_plan_runs_on_windows_powershell_5_1(external_temp: Path):
    config, _ = _config(external_temp)

    result = _run_lab(
        config,
        "Plan",
        powershell=WINDOWS_POWERSHELL,
    )

    plan = _json_output(result)
    assert plan["action"] == "Plan"
    assert plan["mutates_vm"] is False


def _config_value(config: Path, name: str) -> object:
    return json.loads(config.read_text(encoding="utf-8"))[name]


def test_plan_rejects_unknown_inline_password_without_echoing_it(
    external_temp: Path,
):
    secret = "DO-NOT-ECHO-THIS-PASSWORD"
    config, values = _config(external_temp)
    values["guest_password"] = secret
    config.write_text(json.dumps(values), encoding="utf-8")

    result = _run_lab(config, "Plan")

    output = result.stdout + "\n" + result.stderr
    assert result.returncode != 0
    assert "guest_password" in output
    assert secret not in output


def test_plan_rejects_unpinned_capture_value(external_temp: Path):
    config, values = _config(external_temp)
    values["snapshot_fingerprint_sha256"] = "capture"
    config.write_text(json.dumps(values), encoding="utf-8")

    result = _run_lab(config, "Plan")

    output = result.stdout + "\n" + result.stderr
    assert result.returncode != 0
    assert "fingerprint" in output


def test_plan_rejects_payload_iso_hash_mismatch(external_temp: Path):
    config, _ = _config(external_temp, payload_iso_sha256="0" * 64)

    result = _run_lab(config, "Plan")

    output = result.stdout + "\n" + result.stderr
    assert result.returncode != 0
    assert "payload ISO" in output
    assert "SHA256" in output


@pytest.mark.parametrize(
    ("old", "new", "expected"),
    [
        (
            'displayName = "LoLReplayTool-VC-Runtime-Lab"',
            'displayName = "another VM"',
            "displayName",
        ),
        (
            'ethernet0.connectionType = "custom"',
            'ethernet0.connectionType = "nat"',
            "Custom VMnet1",
        ),
        (
            'isolation.tools.copy.disable = "TRUE"',
            'isolation.tools.copy.disable = "FALSE"',
            "isolation",
        ),
    ],
)
def test_plan_rejects_changed_vmx_security_identity(
    external_temp: Path,
    old: str,
    new: str,
    expected: str,
):
    config, values = _config(external_temp)
    vmx = Path(str(values["vmx_path"]))
    vmx.write_text(
        vmx.read_text(encoding="utf-8").replace(old, new),
        encoding="utf-8",
    )

    result = _run_lab(config, "Plan")

    output = result.stdout + "\n" + result.stderr
    assert result.returncode != 0
    assert expected in output


def test_plan_rejects_snapshot_state_fingerprint_change(external_temp: Path):
    config, values = _config(external_temp)
    vmx = Path(str(values["vmx_path"]))
    snapshot = vmx.with_name("LoLReplayTool-VC-Runtime-Lab-Snapshot1.vmsn")
    snapshot.write_bytes(snapshot.read_bytes() + b"unexpected mutation\n")

    result = _run_lab(config, "Plan")

    output = result.stdout + "\n" + result.stderr
    assert result.returncode != 0
    assert "snapshot UID/fingerprint" in output


def test_plan_rejects_snapshot_vmdk_content_change(external_temp: Path):
    config, values = _config(external_temp)
    vmx = Path(str(values["vmx_path"]))
    base_disk = vmx.with_name("LoLReplayTool-VC-Runtime-Lab.vmdk")
    base_disk.write_bytes(base_disk.read_bytes() + b"unexpected disk mutation\n")

    result = _run_lab(config, "Plan")

    output = result.stdout + "\n" + result.stderr
    assert result.returncode != 0
    assert "snapshot UID/fingerprint" in output


def test_snapshot_fingerprint_includes_vmdk_descriptor_extents(external_temp: Path):
    config, values = _config(external_temp)
    vmx = Path(str(values["vmx_path"]))
    base_disk = vmx.with_name("LoLReplayTool-VC-Runtime-Lab.vmdk")
    extent = vmx.with_name("LoLReplayTool-VC-Runtime-Lab-s001.vmdk")
    base_disk.write_text(
        "# Disk DescriptorFile\n"
        "CID=11111111\n"
        "parentCID=ffffffff\n"
        'RW 2048 SPARSE "LoLReplayTool-VC-Runtime-Lab-s001.vmdk"\n',
        encoding="ascii",
    )
    extent.write_bytes(b"fixed extent\n")
    values["snapshot_fingerprint_sha256"] = "capture"
    config.write_text(json.dumps(values), encoding="utf-8")
    capture = _json_output(_run_lab(config, "Capture"))
    values.update(capture["replacement_values"])
    config.write_text(json.dumps(values), encoding="utf-8")

    extent.write_bytes(extent.read_bytes() + b"unexpected extent mutation\n")
    result = _run_lab(config, "Plan")

    output = result.stdout + "\n" + result.stderr
    assert result.returncode != 0
    assert "snapshot UID/fingerprint" in output


@pytest.mark.skipif(os.name != "nt", reason="Windows reparse-point behavior")
def test_plan_rejects_vmdk_file_symlink(external_temp: Path):
    config, values = _config(external_temp)
    vmx = Path(str(values["vmx_path"]))
    base_disk = vmx.with_name("LoLReplayTool-VC-Runtime-Lab.vmdk")
    external_disk = external_temp / "outside.vmdk"
    external_disk.write_bytes(base_disk.read_bytes())
    base_disk.unlink()
    try:
        base_disk.symlink_to(external_disk)
    except OSError as exc:
        pytest.skip(f"file symlink creation is unavailable: {exc}")

    result = _run_lab(config, "Plan")

    output = result.stdout + "\n" + result.stderr
    assert result.returncode != 0
    assert "reparse point" in output


@pytest.mark.skipif(os.name != "nt", reason="Windows junction behavior")
def test_capture_rejects_vmdk_path_through_junction(external_temp: Path):
    if WINDOWS_POWERSHELL is None:
        pytest.skip("Windows PowerShell is unavailable")
    config, values = _config(external_temp)
    vmx = Path(str(values["vmx_path"]))
    outside = external_temp / "outside-disk"
    outside.mkdir()
    (outside / "outside.vmdk").write_bytes(b"external disk\n")
    junction = vmx.parent / "linked-disk"
    create = subprocess.run(
        [
            WINDOWS_POWERSHELL,
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            (
                "New-Item -ItemType Junction "
                "-Path $env:LOL_VM_LAB_JUNCTION_PATH "
                "-Target $env:LOL_VM_LAB_JUNCTION_TARGET | Out-Null"
            ),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
        env={
            **os.environ,
            "LOL_VM_LAB_JUNCTION_PATH": str(junction),
            "LOL_VM_LAB_JUNCTION_TARGET": str(outside),
        },
    )
    if create.returncode != 0:
        pytest.skip("junction creation is unavailable")
    vmsd = vmx.with_suffix(".vmsd")
    vmsd.write_text(
        vmsd.read_text(encoding="utf-8").replace(
            f'snapshot0.disk0.fileName = "{vmx.stem}.vmdk"',
            'snapshot0.disk0.fileName = "linked-disk\\outside.vmdk"',
        ),
        encoding="utf-8",
    )

    result = _run_lab(config, "Capture")

    output = result.stdout + "\n" + result.stderr
    assert result.returncode != 0
    assert "reparse point" in output


def test_firewall_audit_treats_any_range_or_service_rule_as_conflicting():
    guest_source = GUEST_SCRIPT.read_text(encoding="utf-8-sig")
    bootstrap_source = BOOTSTRAP_SCRIPT.read_text(encoding="utf-8-sig")

    for source in (guest_source, bootstrap_source):
        assert 'candidate -ceq "Any"' in source
        assert "-le 5985" in source
        assert "-ge 5985" in source
        assert "serviceAllowsWinRm" not in source
    assert "if (-not $portRelated -and -not $serviceRelated)" in guest_source
    assert "if ($portRelated -or $serviceRelated)" in bootstrap_source


def test_bootstrap_disables_only_persistent_rules_individually_and_fails_closed():
    source = BOOTSTRAP_SCRIPT.read_text(encoding="utf-8-sig")
    assert "function Disable-PersistentWinRmRules" in source
    assert "Get-WinRmRelatedFirewallRules -PolicyStore PersistentStore" in source
    assert "Disable-NetFirewallRule -PolicyStore PersistentStore -Name $name" in source
    assert "PersistentStore WinRM ruleのNameが空です" in source
    assert "PersistentStore WinRM ruleのNameが重複しています" in source
    assert "PersistentStore WinRM ruleのInstanceIDが空です" in source
    assert "PersistentStore WinRM ruleを無効化できませんでした" in source
    assert "PolicyStoreSourceType" in source
    assert "PolicyStoreSource" in source
    assert "Get-WinRmRelatedFirewallRules -PolicyStore PersistentStore |" not in source
    assert "Get-ActiveInboundFirewallInventory -PolicyStore $PolicyStore" in source
    assert "Get-WinRmFirewallEvidence" in source
    assert "$diagnostics" not in source
    assert "-PolicyStore PersistentStore -ErrorAction Stop" in source


@pytest.mark.skipif(POWERSHELL is None, reason="pwsh is unavailable")
def test_persistent_firewall_disable_helper_executes_fail_closed_cases():
    command = r'''
$tree = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
  $env:TARGET_SCRIPT,
  [ref]$tree,
  [ref]$errors
)
$fn = $ast.Find(
  {
    param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
      $node.Name -eq "Disable-PersistentWinRmRules"
  },
  $true
)
if ($errors.Count -ne 0 -or $null -eq $fn) { throw "helper parse failed" }
. ([scriptblock]::Create($fn.Extent.Text))
function Get-WinRmRelatedFirewallRules {
  param([string]$PolicyStore)
  $script:QueryStores += $PolicyStore
  return $script:Rules
}
function Disable-NetFirewallRule {
  param([string]$PolicyStore, [string]$Name, [string]$ErrorAction)
  $script:Calls += [pscustomobject]@{
    PolicyStore = $PolicyStore
    Name = $Name
    ErrorAction = $ErrorAction
  }
  if ($Name -eq $script:FailName) { throw "mock $Name failure" }
}

$valid = @(
  [pscustomobject]@{
    Name = "Rule1"
    InstanceID = "i1"
    PolicyStoreSourceType = "Local"
    PolicyStoreSource = "PersistentStore"
  },
  [pscustomobject]@{
    Name = "Rule2"
    InstanceID = "i2"
    PolicyStoreSourceType = "Local"
    PolicyStoreSource = "PersistentStore"
  }
)

$script:Rules = @()
$script:Calls = @()
$script:QueryStores = @()
$script:FailName = $null
Disable-PersistentWinRmRules | Out-Null
if ($script:Calls.Count -ne 0) { throw "zero case called Disable" }
if ($script:QueryStores.Count -ne 1 -or $script:QueryStores[0] -ne "PersistentStore") {
  throw "zero case queried the wrong store"
}

$script:Rules = $valid
$script:Calls = @()
$script:QueryStores = @()
$script:FailName = $null
$result = @(Disable-PersistentWinRmRules)
if ($result.Count -ne 2 -or $script:Calls.Count -ne 2) { throw "success count mismatch" }
if ($script:Calls[0].Name -ne "Rule1" -or $script:Calls[1].Name -ne "Rule2") {
  throw "success order mismatch"
}
foreach ($call in $script:Calls) {
  if ($call.PolicyStore -ne "PersistentStore" -or $call.ErrorAction -ne "Stop") {
    throw "success parameters mismatch"
  }
}
if ($script:QueryStores.Count -ne 1 -or $script:QueryStores[0] -ne "PersistentStore") {
  throw "success queried the wrong store"
}

function Assert-ValidationFailure {
  param([object[]]$InvalidRules)
  $script:Rules = @($InvalidRules)
  $script:Calls = @()
  $script:QueryStores = @()
  $failed = $false
  try { Disable-PersistentWinRmRules | Out-Null } catch { $failed = $true }
  if (-not $failed -or $script:Calls.Count -ne 0) {
    throw "validation case changed a rule"
  }
}
Assert-ValidationFailure -InvalidRules @(
  [pscustomobject]@{ Name = ""; InstanceID = "i1" }
)
Assert-ValidationFailure -InvalidRules @(
  [pscustomobject]@{ Name = "Rule1"; InstanceID = "" }
)
Assert-ValidationFailure -InvalidRules @(
  [pscustomobject]@{ Name = "Rule1"; InstanceID = "i1" },
  [pscustomobject]@{ Name = "rule1"; InstanceID = "i2" }
)

$script:Rules = $valid
$script:Calls = @()
$script:QueryStores = @()
$script:FailName = "Rule2"
$failureMessage = $null
try {
  Disable-PersistentWinRmRules | Out-Null
} catch {
  $failureMessage = $_.Exception.Message
}
if ($script:Calls.Count -ne 2 -or $script:Calls[0].Name -ne "Rule1") {
  throw "cmdlet failure call sequence mismatch"
}
if ($failureMessage -notmatch "Rule2" -or $failureMessage -notmatch "i2") {
  throw "cmdlet failure lacks rule diagnostics"
}
'''
    result = subprocess.run(
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", command],
        env={**os.environ, "TARGET_SCRIPT": str(BOOTSTRAP_SCRIPT)},
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(POWERSHELL is None, reason="pwsh is unavailable")
@pytest.mark.parametrize("script", [BOOTSTRAP_SCRIPT, GUEST_SCRIPT])
def test_active_default_route_helper_filters_and_propagates(script: Path):
    command = r'''
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
  $env:TARGET_SCRIPT,
  [ref]$tokens,
  [ref]$errors
)
$fn = $ast.Find(
  {
    param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
      $node.Name -eq "Get-ActiveDefaultIpv4Routes"
  },
  $true
)
if ($errors.Count -ne 0 -or $null -eq $fn) { throw "route helper parse failed" }
. ([scriptblock]::Create($fn.Extent.Text))
function Get-NetRoute {
  param([string]$AddressFamily, [string]$PolicyStore, [string]$ErrorAction)
  $script:Arguments = [pscustomobject]@{
    AddressFamily = $AddressFamily
    PolicyStore = $PolicyStore
    ErrorAction = $ErrorAction
  }
  if ($script:Mode -eq "known-not-found") {
    $record = [Management.Automation.ErrorRecord]::new(
      [Management.Automation.ItemNotFoundException]::new("no routes"),
      "CmdletizationQuery_NotFound,Get-NetRoute",
      [Management.Automation.ErrorCategory]::ObjectNotFound,
      "MSFT_NetRoute"
    )
    throw $record
  }
  if ($script:Mode -eq "unknown-not-found") {
    $record = [Management.Automation.ErrorRecord]::new(
      [Management.Automation.ItemNotFoundException]::new("different failure"),
      "Different_NotFound,Get-NetRoute",
      [Management.Automation.ErrorCategory]::ObjectNotFound,
      "MSFT_NetRoute"
    )
    throw $record
  }
  if ($script:Mode -eq "provider-failure") { throw "provider failed" }
  return $script:Routes
}

$script:Mode = "data"
$script:Routes = @([pscustomobject]@{ DestinationPrefix = "10.0.0.0/8" })
$routes = @(Get-ActiveDefaultIpv4Routes)
if ($routes.Count -ne 0) { throw "non-default route was retained" }

$script:Routes = @(
  [pscustomobject]@{ DestinationPrefix = "10.0.0.0/8" },
  [pscustomobject]@{ DestinationPrefix = "0.0.0.0/0" }
)
$routes = @(Get-ActiveDefaultIpv4Routes)
if ($routes.Count -ne 1 -or $routes[0].DestinationPrefix -ne "0.0.0.0/0") {
  throw "default route filter failed"
}
if (
  $script:Arguments.AddressFamily -ne "IPv4" -or
  $script:Arguments.PolicyStore -ne "ActiveStore" -or
  $script:Arguments.ErrorAction -ne "Stop"
) {
  throw "route query arguments failed"
}

$script:Mode = "known-not-found"
$routes = @(Get-ActiveDefaultIpv4Routes)
if ($routes.Count -ne 0) { throw "known route absence was not empty" }

$script:Mode = "unknown-not-found"
$unknownId = $null
try { Get-ActiveDefaultIpv4Routes | Out-Null } catch {
  $unknownId = $_.FullyQualifiedErrorId
}
if ($unknownId -ne "Different_NotFound,Get-NetRoute") {
  throw "unknown ObjectNotFound was swallowed"
}

$script:Mode = "provider-failure"
$providerFailed = $false
try { Get-ActiveDefaultIpv4Routes | Out-Null } catch { $providerFailed = $true }
if (-not $providerFailed) { throw "provider failure was swallowed" }
'''
    result = subprocess.run(
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", command],
        env={**os.environ, "TARGET_SCRIPT": str(script)},
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(POWERSHELL is None, reason="pwsh is unavailable")
def test_bootstrap_route_removal_control_flow_removes_only_matching_defaults():
    command = r'''
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
  $env:TARGET_SCRIPT,
  [ref]$tokens,
  [ref]$errors
)
$helper = $ast.Find(
  {
    param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
      $node.Name -eq "Get-ActiveDefaultIpv4Routes"
  },
  $true
)
$assignment = $ast.Find(
  {
    param($node)
    $node -is [System.Management.Automation.Language.AssignmentStatementAst] -and
      $node.Extent.Text -match '\$routesToRemove'
  },
  $true
)
$loop = $ast.Find(
  {
    param($node)
    $node -is [System.Management.Automation.Language.ForEachStatementAst] -and
      $node.Extent.Text -match '\$routesToRemove'
  },
  $true
)
if ($errors.Count -ne 0 -or $null -in @($helper, $assignment, $loop)) {
  throw "route removal AST extraction failed"
}
. ([scriptblock]::Create($helper.Extent.Text))
$removal = [scriptblock]::Create(
  $assignment.Extent.Text + [Environment]::NewLine + $loop.Extent.Text
)
function Get-NetRoute {
  param([string]$AddressFamily, [string]$PolicyStore, [string]$ErrorAction)
  return $script:Routes
}
function Remove-NetRoute {
  param($InputObject, $Confirm, $ErrorAction)
  $script:Removed += [pscustomobject]@{
    InputObject = $InputObject
    Confirm = $Confirm
    ErrorAction = $ErrorAction
  }
  if ($script:FailRemoval) { throw "remove failed" }
}

$InterfaceAlias = "lab"
$script:Routes = @(
  [pscustomobject]@{ DestinationPrefix = "10.0.0.0/8"; InterfaceAlias = "lab" },
  [pscustomobject]@{ DestinationPrefix = "0.0.0.0/0"; InterfaceAlias = "other" }
)
$script:Removed = @()
$script:FailRemoval = $false
. $removal
if ($script:Removed.Count -ne 0) { throw "non-matching route was removed" }

$target = [pscustomobject]@{
  DestinationPrefix = "0.0.0.0/0"
  InterfaceAlias = "lab"
  InterfaceIndex = 7
  NextHop = "0.0.0.0"
}
$script:Routes = @($target)
$script:Removed = @()
. $removal
if ($script:Removed.Count -ne 1) { throw "target route removal count mismatch" }
if (-not [object]::ReferenceEquals($script:Removed[0].InputObject, $target)) {
  throw "route removal did not use the enumerated object"
}
if ($script:Removed[0].Confirm -ne $false -or $script:Removed[0].ErrorAction -ne "Stop") {
  throw "route removal arguments failed"
}

$script:FailRemoval = $true
$removeFailed = $false
try { . $removal } catch { $removeFailed = $true }
if (-not $removeFailed) { throw "route removal failure was swallowed" }
'''
    result = subprocess.run(
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", command],
        env={**os.environ, "TARGET_SCRIPT": str(BOOTSTRAP_SCRIPT)},
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(POWERSHELL is None, reason="pwsh is unavailable")
def test_guest_default_route_state_propagates_provider_failures():
    command = r'''
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
  $env:TARGET_SCRIPT,
  [ref]$tokens,
  [ref]$errors
)
$fn = $ast.Find(
  {
    param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
      $node.Name -eq "Get-DefaultRouteState"
  },
  $true
)
if ($errors.Count -ne 0 -or $null -eq $fn) { throw "route state parse failed" }
. ([scriptblock]::Create($fn.Extent.Text))
function Get-ActiveDefaultIpv4Routes {
  if ($script:Fail) { throw "provider failed" }
  return $script:Routes
}

$script:Fail = $false
$script:Routes = @()
$state = Get-DefaultRouteState
if ($state.source -ne "route-table" -or @($state.routes).Count -ne 0) {
  throw "empty route state failed"
}

$script:Fail = $true
$failed = $false
try { Get-DefaultRouteState | Out-Null } catch { $failed = $true }
if (-not $failed) { throw "route state swallowed provider failure" }
'''
    result = subprocess.run(
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", command],
        env={**os.environ, "TARGET_SCRIPT": str(GUEST_SCRIPT)},
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


def test_plan_rejects_unknown_vmx_key_change(
    external_temp: Path,
):
    config, values = _config(external_temp)
    vmx = Path(str(values["vmx_path"]))
    vmx.write_text(
        vmx.read_text(encoding="utf-8") + 'annotation = "changed"\n',
        encoding="utf-8",
    )

    result = _run_lab(config, "Plan")

    output = result.stdout + "\n" + result.stderr
    assert result.returncode != 0
    assert "VMX identity/fingerprint" in output


def test_plan_accepts_snapshot_delta_rollover_and_volatile_vmx_changes(
    external_temp: Path,
):
    config, values = _config(external_temp)
    vmx = Path(str(values["vmx_path"]))
    vm_name = vmx.stem
    replacement = vmx.with_name(f"{vm_name}-000002.vmdk")
    replacement.write_text(
        "# Disk DescriptorFile\n"
        "version=1\n"
        "CID=33333333\n"
        "parentCID=11111111\n"
        'createType="monolithicSparse"\n'
        f'parentFileNameHint="{vm_name}.vmdk"\n',
        encoding="ascii",
    )
    vmx.write_text(
        vmx.read_text(encoding="utf-8")
        .replace(f'{vm_name}-000001.vmdk', replacement.name)
        .replace(
            'encryption.data = "fixed-test-encryption-data"',
            'encryption.data = "rotated-by-vmware"',
        )
        + 'vm.lastPowerRequestTimestamp = "123456"\n'
        + 'cleanShutdown = "TRUE"\n'
        + 'softPowerOff = "TRUE"\n',
        encoding="utf-8",
    )

    result = _run_lab(config, "Plan")

    assert result.returncode == 0, result.stdout + "\n" + result.stderr
    plan = _json_output(result)
    assert plan["action"] == "Plan"


def test_plan_rejects_active_disk_outside_snapshot_chain(external_temp: Path):
    config, values = _config(external_temp)
    vmx = Path(str(values["vmx_path"]))
    vm_name = vmx.stem
    unrelated = vmx.with_name("unrelated-000001.vmdk")
    unrelated.write_text(
        "# Disk DescriptorFile\n"
        "version=1\n"
        "CID=44444444\n"
        "parentCID=ffffffff\n"
        'createType="monolithicSparse"\n',
        encoding="ascii",
    )
    vmx.write_text(
        vmx.read_text(encoding="utf-8").replace(
            f'{vm_name}-000001.vmdk', unrelated.name
        ),
        encoding="utf-8",
    )

    result = _run_lab(config, "Plan")

    output = result.stdout + "\n" + result.stderr
    assert result.returncode != 0
    assert "active disk chain" in output


def test_doctor_reports_definition_change_as_not_ready(external_temp: Path):
    config, values = _config(external_temp)
    vmx = Path(str(values["vmx_path"]))
    vmx.with_suffix(".vmsd").write_text(
        vmx.with_suffix(".vmsd").read_text(encoding="utf-8").replace(
            'snapshot0.uid = "1"',
            'snapshot0.uid = "9"',
        ),
        encoding="utf-8",
    )

    result = _run_lab(config, "Doctor")

    doctor = _json_output(result)
    assert doctor["ready_for_run"] is False
    assert doctor["reason"] == "definition validation failed"
    assert "snapshot UID/fingerprint" in doctor["validation_error"]


def test_capture_rejects_second_present_network_adapter(external_temp: Path):
    config, values = _config(external_temp)
    vmx = Path(str(values["vmx_path"]))
    vmx.write_text(
        vmx.read_text(encoding="utf-8")
        + 'ethernet1.present = "TRUE"\n'
        + 'ethernet1.connectionType = "nat"\n',
        encoding="utf-8",
    )
    for key in (
        "expected_vm_uuid",
        "expected_vm_encryption_type",
        "expected_guest_mac",
        "vmx_file_sha256",
        "vm_definition_fingerprint_sha256",
        "snapshot_uid",
        "snapshot_fingerprint_sha256",
    ):
        values[key] = "capture"
    config.write_text(json.dumps(values), encoding="utf-8")

    result = _run_lab(config, "Capture")

    output = result.stdout + "\n" + result.stderr
    assert result.returncode != 0
    assert "network adapter" in output


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"snapshot_a": "A0"}, "A0-runtime-absent"),
        ({"vmware_network": "vmnet8"}, "host-only vmnet1"),
        ({"guest_address": "192.168.189.10"}, "private /24"),
        (
            {"runtime_installer_relative_path": "../vc_redist.x64.exe"},
            "相対path",
        ),
    ],
)
def test_plan_rejects_unsafe_environment_boundaries(
    external_temp: Path,
    overrides: dict[str, object],
    expected: str,
):
    config, _ = _config(external_temp, **overrides)

    result = _run_lab(config, "Plan")

    output = result.stdout + "\n" + result.stderr
    assert result.returncode != 0
    assert expected in output


def test_doctor_reports_missing_credentials_without_running_vm(
    external_temp: Path,
):
    config, _ = _config(external_temp)

    result = _run_lab(config, "Doctor")

    doctor = _json_output(result)
    assert doctor["action"] == "Doctor"
    assert doctor["ready_for_run"] is False
    assert doctor["reason"] == "required file is missing"
    assert doctor["files"]["vm_credential_exists"] is False
    assert doctor["files"]["guest_credential_exists"] is False


def test_run_requires_all_explicit_mutation_confirmations(external_temp: Path):
    config, _ = _config(external_temp)

    for arguments in (
        (),
        ("-ConfirmSnapshotRestore",),
        ("-ConfirmRuntimeInstall",),
        ("-ConfirmVmPasswordProcessExposure",),
        ("-ConfirmSnapshotRestore", "-ConfirmRuntimeInstall"),
    ):
        result = _run_lab(config, "Run", *arguments)
        output = result.stdout + "\n" + result.stderr
        assert result.returncode != 0
        assert "-ConfirmSnapshotRestore" in output
        assert "-ConfirmRuntimeInstall" in output
        assert "-ConfirmVmPasswordProcessExposure" in output
        assert not Path(str(_config_value(config, "artifact_root"))).exists()


@pytest.mark.skipif(os.name != "nt", reason="guest probe uses Windows cmdlets")
def test_guest_inspection_returns_machine_readable_schema(tmp_path: Path):
    if POWERSHELL is None:
        pytest.skip("pwsh is unavailable")
    module_dir = tmp_path / "NetTCPIP"
    module_dir.mkdir()
    (module_dir / "NetTCPIP.psm1").write_text(
        r'''
function Get-NetRoute {
  param([string]$AddressFamily, [string]$PolicyStore, [string]$ErrorAction)
  return @()
}
Export-ModuleMember -Function Get-NetRoute
''',
        encoding="utf-8-sig",
    )
    command = r'''& $env:TARGET_SCRIPT -Action Inspect'''
    result = subprocess.run(
        [
            POWERSHELL,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            command,
        ],
        env={
            **os.environ,
            "TARGET_SCRIPT": str(GUEST_SCRIPT),
            "PSModulePath": str(tmp_path)
            + os.pathsep
            + os.environ.get("PSModulePath", ""),
        },
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )

    inspection = _json_output(result)
    assert inspection["schema_version"] == 3
    assert inspection["action"] == "Inspect"
    assert inspection["runtime"]["registry_view"] == "Registry64"
    assert isinstance(inspection["runtime"]["installed"], bool)
    assert isinstance(inspection["vmware_tools"]["present"], bool)
    assert isinstance(inspection["ipv4_addresses"], list)
    assert isinstance(inspection["default_routes"], list)
    assert inspection["default_route_source"] == "route-table"
    assert "gateway-fallback" not in GUEST_SCRIPT.read_text(encoding="utf-8-sig")
    assert isinstance(inspection["system_runtime_inventory"], list)
    assert isinstance(inspection["winrm_firewall"]["available"], bool)


def test_bootstrap_repairs_private_profile_at_startup_and_records_task_evidence():
    source = BOOTSTRAP_SCRIPT.read_text(encoding="utf-8-sig")
    assert "New-ScheduledTaskPrincipal -UserId \"SYSTEM\"" in source
    assert "New-ScheduledTaskTrigger -AtStartup" in source
    assert "-StartupRepair" in source
    assert "Set-NetConnectionProfile" in source
    assert "-NetworkCategory Private" in source
    assert "Profile Private" in source
    assert 'Join-Path $env:ProgramData "LoLReplayToolVMLab"' in source
    assert "Copy-Item `" in source
    assert "Get-ActiveDefaultIpv4Routes" in source


def test_guest_derives_startup_script_evidence_from_task_action():
    source = GUEST_SCRIPT.read_text(encoding="utf-8-sig")

    assert "$fileArguments = [regex]::Matches(" in source
    assert "$taskScriptPath = $fileArguments[0].Groups[1].Value" in source
    assert 'script_path_source = "task-action"' in source
    assert "script_path_matches_expected" in source
    assert "marker_script_path_matches_action" in source
    assert (
        "script_path = if ($null -ne $bootstrapMarker)" not in source
    )


def test_cleanup_never_uses_vmrun_stop_and_requires_guest_session():
    source = Path("scripts/windows_vm_lab.ps1").read_text(encoding="utf-8-sig")
    assert 'Join-Path $env:WINDIR "System32\\shutdown.exe"' in source
    assert "& $shutdown /s /t 0" in source
    assert '$shutdownRequestSent = "unknown"' in source
    assert 'operation = "guest-shutdown-observed"' in source
    assert "manual_shutdown_required=true" in source
    assert 'Arguments @("stop", $Config.vmx_path' not in source


def test_lab_run_allows_three_minutes_for_cold_boot_winrm():
    source = LAB_SCRIPT.read_text(encoding="utf-8-sig")
    assert "[ValidateRange(1, 180)]" in source
    assert "[int]$TimeoutSeconds = 180" in source
    assert "-TimeoutSeconds 180" in source


def test_lab_run_records_inspections_before_policy_assertions():
    source = LAB_SCRIPT.read_text(encoding="utf-8-sig")
    run_source = source.split("function Invoke-LabRun", 1)[1]
    assert run_source.index('"environment-a.json"') < run_source.index(
        "Assert-EnvironmentA"
    )
    assert run_source.index('"environment-b.json"') < run_source.index(
        "Assert-EnvironmentB"
    )


def test_guest_self_check_captures_native_streams_without_remoting_errors():
    source = GUEST_SCRIPT.read_text(encoding="utf-8-sig")
    self_check = source.split("function Invoke-PackagedSelfCheck", 1)[1].split(
        "$result = switch", 1
    )[0]
    assert "Start-Process `" in self_check
    assert "-RedirectStandardOutput $validationStdoutPath" in self_check
    assert "-RedirectStandardError $validationStderrPath" in self_check
    assert "*>&1" not in self_check
    assert "validation_exit_code = $validationExitCode" in self_check
    assert "error_output = $validationErrorOutput" in self_check
    assert "Remove-ValidationCaptureFiles -Paths $validationCapturePaths" in self_check
    assert 'stdout = $validationOutput' in self_check
    assert 'stderr = $validationErrorOutput' in self_check


def test_guest_validation_capture_cleanup_is_fail_closed():
    source = GUEST_SCRIPT.read_text(encoding="utf-8-sig")
    cleanup = source.split("function Remove-ValidationCaptureFiles", 1)[1].split(
        "function Invoke-PackagedSelfCheck", 1
    )[0]
    assert "Remove-Item -LiteralPath $path -Force -ErrorAction Stop" in cleanup
    assert cleanup.count("Test-Path -LiteralPath $path") == 2
    assert "validation captureを削除できませんでした" in cleanup


@pytest.mark.parametrize(
    "powershell",
    [
        pytest.param(POWERSHELL, id="pwsh"),
        pytest.param(WINDOWS_POWERSHELL, id="windows-powershell-5.1"),
    ],
)
def test_guest_shutdown_request_executes_without_force_and_preserves_errors(
    powershell: str | None,
):
    if powershell is None:
        pytest.skip("requested PowerShell runtime is unavailable")
    command = r'''
$tokens = $null
$errors = $null
$utf8 = New-Object System.Text.UTF8Encoding($false, $true)
$source = [IO.File]::ReadAllText($env:TARGET_SCRIPT, $utf8)
$ast = [Management.Automation.Language.Parser]::ParseInput(
  $source,
  $env:TARGET_SCRIPT,
  [ref]$tokens,
  [ref]$errors
)
$fn = $ast.Find(
  {
    param($node)
    $node -is [Management.Automation.Language.FunctionDefinitionAst] -and
      $node.Name -eq "Request-GuestShutdown"
  },
  $true
)
if ($errors.Count -ne 0 -or $null -eq $fn) { throw "shutdown helper extraction failed" }
. ([scriptblock]::Create($fn.Extent.Text))
$script:mode = "success"
function Invoke-Command {
  param($Session, [scriptblock]$ScriptBlock, $ErrorAction)
  switch ($script:mode) {
    "success" { return [pscustomobject]@{ exit_code = 0; force_used = $false } }
    "nonzero" { return [pscustomobject]@{ exit_code = 1; force_used = $false } }
    "malformed" { return @() }
    "transport" { throw "simulated transport failure" }
  }
}
$result = Request-GuestShutdown -Session ([pscustomobject]@{})
if ($result.shutdown_request_sent -cne "confirmed" -or $result.shutdown_exit_code -ne 0 -or $result.force_used) {
  throw "successful shutdown request was not preserved"
}
$script:mode = "nonzero"
$result = Request-GuestShutdown -Session ([pscustomobject]@{})
if ($result.shutdown_request_sent -cne "confirmed" -or $result.shutdown_exit_code -ne 1 -or $result.force_used) {
  throw "nonzero shutdown result was not preserved"
}
foreach ($failureMode in @("malformed", "transport")) {
  $script:mode = $failureMode
  $failed = $false
  try { Request-GuestShutdown -Session ([pscustomobject]@{}) | Out-Null } catch { $failed = $true }
  if (-not $failed) { throw "$failureMode shutdown response was accepted" }
}
'''
    result = subprocess.run(
        [powershell, "-NoProfile", "-NonInteractive", "-Command", command],
        env={**os.environ, "TARGET_SCRIPT": str(LAB_SCRIPT)},
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + "\n" + result.stderr


@pytest.mark.parametrize(
    "required",
    [
        'task.principal -cne "SYSTEM"',
        'task.logon_type -cne "ServiceAccount"',
        'task.run_level -cne "Highest"',
        'task.enabled -cne "True"',
        '@($task.triggers).Count -ne 1',
        'MSFT_TaskBootTrigger',
        'task.action_count -ne 1',
        'task.info.last_task_result -ne 0',
        'startup_repair.result -cne "passed"',
    ],
)
def test_startup_repair_policy_rejects_missing_or_tampered_evidence(required: str):
    source = Path("scripts/windows_vm_lab.ps1").read_text(encoding="utf-8-sig")
    assert required in source


@pytest.mark.parametrize(
    "powershell",
    [
        pytest.param(POWERSHELL, id="pwsh"),
        pytest.param(WINDOWS_POWERSHELL, id="windows-powershell-5.1"),
    ],
)
def test_startup_repair_policy_executes_fail_closed_cases(
    powershell: str | None,
):
    if powershell is None:
        pytest.skip("requested PowerShell runtime is unavailable")
    command = r'''
$tokens = $null
$errors = $null
$utf8 = New-Object System.Text.UTF8Encoding($false, $true)
$source = [IO.File]::ReadAllText($env:TARGET_SCRIPT, $utf8)
$ast = [Management.Automation.Language.Parser]::ParseInput(
  $source,
  $env:TARGET_SCRIPT,
  [ref]$tokens,
  [ref]$errors
)
if ($errors.Count -ne 0) { throw "policy extraction failed" }
foreach ($functionName in @("ConvertTo-ExplicitDateTimeOffset", "Assert-StartupTask")) {
  $fn = $ast.Find(
    {
      param($node)
      $node -is [Management.Automation.Language.FunctionDefinitionAst] -and
        $node.Name -eq $functionName
    },
    $true
  )
  if ($null -eq $fn) { throw "policy extraction failed: $functionName" }
  . ([scriptblock]::Create($fn.Extent.Text))
}
$script:bootstrapScript = "fixed-bootstrap.ps1"
function Get-Sha256 { param([string]$Path) return "a" * 64 }
$expectedScript = Join-Path $env:ProgramData "LoLReplayToolVMLab\windows_vm_lab_bootstrap.ps1"
$expectedExecute = Join-Path $env:WINDIR "System32\WindowsPowerShell\v1.0\powershell.exe"
$expectedArguments = "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$expectedScript`" -StartupRepair -InterfaceAlias `"Ethernet0`" -GuestAddress `"192.168.20.10`" -HostAddress `"192.168.20.1`" -PrefixLength 24"
$valid = [ordered]@{
  captured_at_utc = "2026-08-28T05:00:03Z"
  bootstrap = [ordered]@{
    created_at_utc = "2026-08-28T05:00:00Z"
    interface_alias = "Ethernet0"
    guest_address = "192.168.20.10"
    host_address = "192.168.20.1"
    prefix_length = 24
    startup_task = [ordered]@{
      task_path = "\"
      script_path = $expectedScript
      script_sha256 = "a" * 64
      action = $expectedArguments
    }
  }
  startup_task = [ordered]@{
    present = $true
    name = "LoLReplayTool-VM-Lab-NetworkRepair"
    task_path = "\"
    principal = "SYSTEM"
    logon_type = "ServiceAccount"
    run_level = "Highest"
    enabled = "True"
    triggers = @("MSFT_TaskBootTrigger")
    action_count = 1
    action = [ordered]@{ execute = $expectedExecute; arguments = $expectedArguments }
    script_path_source = "task-action"
    script_path = $expectedScript
    script_path_matches_expected = $true
    marker_script_path_matches_action = $true
    script_exists = $true
    script_sha256 = "a" * 64
    info = [ordered]@{ last_task_result = 0; last_run_time = "2026-08-28T14:00:01+09:00" }
  }
  startup_repair = [ordered]@{
    result = "passed"
    interface_alias = "Ethernet0"
    guest_address = "192.168.20.10"
    host_address = "192.168.20.1"
    completed_at_utc = "2026-08-28T05:00:02Z"
  }
}
$validJson = $valid | ConvertTo-Json -Depth 12
Assert-StartupTask -Inspection ($validJson | ConvertFrom-Json)
$mutations = @(
  { param($x) $x.startup_task.present = $false },
  { param($x) $x.startup_task.name = "changed" },
  { param($x) $x.startup_task.task_path = "\Other\" },
  { param($x) $x.startup_task.principal = "user" },
  { param($x) $x.startup_task.logon_type = "Interactive" },
  { param($x) $x.startup_task.run_level = "Limited" },
  { param($x) $x.startup_task.enabled = "False" },
  { param($x) $x.startup_task.triggers = @("MSFT_TaskLogonTrigger") },
  { param($x) $x.startup_task.triggers = @("MSFT_TaskBootTrigger", "MSFT_TaskLogonTrigger") },
  { param($x) $x.startup_task.action_count = 2 },
  { param($x) $x.startup_task.action = $null },
  { param($x) $x.startup_task.action.execute = "powershell.exe" },
  { param($x) $x.startup_task.action.arguments = "changed" },
  { param($x) $x.startup_task.script_path_source = "marker" },
  { param($x) $x.startup_task.script_path = "C:\wrong.ps1" },
  { param($x) $x.startup_task.script_path_matches_expected = $false },
  { param($x) $x.startup_task.marker_script_path_matches_action = $false },
  { param($x) $x.startup_task.script_exists = $false },
  { param($x) $x.startup_task.script_sha256 = "b" * 64 },
  { param($x) $x.startup_task.info = $null },
  { param($x) $x.startup_task.info.last_task_result = 1 },
  { param($x) $x.startup_task.info.last_run_time = "not-a-time" },
  { param($x) $x.bootstrap.startup_task = $null },
  { param($x) $x.bootstrap.startup_task.task_path = "\Other\" },
  { param($x) $x.bootstrap.startup_task.script_path = "C:\wrong.ps1" },
  { param($x) $x.bootstrap.startup_task.script_sha256 = "b" * 64 },
  { param($x) $x.bootstrap.startup_task.action = "changed" },
  { param($x) $x.startup_repair = $null },
  { param($x) $x.startup_repair.result = "failed" },
  { param($x) $x.startup_repair.interface_alias = "Ethernet1" },
  { param($x) $x.startup_repair.guest_address = "192.168.20.11" },
  { param($x) $x.startup_repair.host_address = "192.168.20.2" },
  { param($x) $x.startup_repair.completed_at_utc = "not-a-time" },
  { param($x) $x.startup_task.info.last_run_time = "2026-08-28T05:00:01" },
  { param($x) $x.captured_at_utc = "2026-08-28T05:00:03" },
  { param($x) $x.startup_task.info.last_run_time = "2026-08-28T04:59:59Z" },
  { param($x) $x.startup_repair.completed_at_utc = "2026-08-28T05:00:04Z" }
)
foreach ($mutation in $mutations) {
  $candidate = $validJson | ConvertFrom-Json
  & $mutation $candidate
  $failed = $false
  try { Assert-StartupTask -Inspection $candidate } catch { $failed = $true }
  if (-not $failed) { throw "tampered startup evidence was accepted: $mutation" }
}
'''
    result = subprocess.run(
        [powershell, "-NoProfile", "-NonInteractive", "-Command", command],
        env={
            **os.environ,
            "TARGET_SCRIPT": str(LAB_SCRIPT),
            "ProgramData": os.environ.get("ProgramData") or "/ProgramData",
            "WINDIR": os.environ.get("WINDIR") or "/Windows",
        },
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + "\n" + result.stderr


def test_guest_self_check_runs_fixed_payload_with_process_execution_policy():
    script = GUEST_SCRIPT.read_text(encoding="utf-8-sig")

    assert "& $environmentBScript" not in script
    assert "System32\\WindowsPowerShell\\v1.0\\powershell.exe" in script
    assert "-ExecutionPolicy Bypass" in script
    assert "validation_exit_code = $validationExitCode" in script
