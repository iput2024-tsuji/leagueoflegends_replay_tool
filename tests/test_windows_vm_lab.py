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
    (vmx.parent / f"{vm_name}.vmdk").write_bytes(b"fixed base disk\n")
    (vmx.parent / f"{vm_name}-000001.vmdk").write_bytes(b"active child disk\n")
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
        "schema_version": 2,
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


@pytest.mark.skipif(os.name != "nt", reason="IMAPI2FS is Windows-only")
def test_iso_builder_creates_hashed_media_without_mutating_source(
    external_temp: Path,
):
    if WINDOWS_POWERSHELL is None:
        pytest.skip("Windows PowerShell is unavailable")
    source = external_temp / "source-kit"
    (source / "LoLReplayTool-external-build").mkdir(parents=True)
    (source / "evidence").mkdir()
    files = {
        "vc_redist.x64.exe": b"runtime",
        "LoLReplayTool-external-build/LoLReplayTool.exe": b"app",
        "02-test-environment-b.ps1": b"environment b",
        "run_packaged_self_check.ps1": b"runner",
        "evidence/package-sha256.csv": b"manifest",
        "evidence/pe-runtime-audit.json": b"pe audit",
        "evidence/external-vc-runtime-wheel-provenance.json": b"provenance",
        "create-test-iso.ps1": b"local-only builder",
    }
    for relative_path, content in files.items():
        (source / relative_path).write_bytes(content)
    output = external_temp / "media" / "managed.iso"
    output.parent.mkdir()

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
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=False,
    )

    built = _json_output(result)
    assert output.is_file()
    assert built["sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()
    assert built["volume_label"] == "LOL_VC_PR134"
    assert (source / "create-test-iso.ps1").read_bytes() == b"local-only builder"
    assert not (source / "00-Bootstrap-VM-Lab.cmd").exists()


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
        for path in (LAB_SCRIPT, GUEST_SCRIPT, BOOTSTRAP_SCRIPT, ISO_BUILD_SCRIPT)
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


@pytest.mark.parametrize("script", [GUEST_SCRIPT, BOOTSTRAP_SCRIPT])
def test_firewall_audit_is_active_store_batched_and_fail_closed(script: Path):
    source = script.read_text(encoding="utf-8-sig")

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
        "install fixed Microsoft-signed Redistributable from the hashed ISO",
        "verify Environment B Runtime version",
        "run the fixed packaged self-check with isolated data",
        "write JSON evidence",
        "request soft VM shutdown",
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
        '# Disk DescriptorFile\nRW 2048 SPARSE "LoLReplayTool-VC-Runtime-Lab-s001.vmdk"\n',
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


def test_plan_rejects_any_vmx_file_change_even_outside_semantic_keys(
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
def test_guest_inspection_returns_machine_readable_schema():
    if POWERSHELL is None:
        pytest.skip("pwsh is unavailable")
    result = subprocess.run(
        [
            POWERSHELL,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(GUEST_SCRIPT),
            "-Action",
            "Inspect",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )

    inspection = _json_output(result)
    assert inspection["schema_version"] == 1
    assert inspection["action"] == "Inspect"
    assert inspection["runtime"]["registry_view"] == "Registry64"
    assert isinstance(inspection["runtime"]["installed"], bool)
    assert isinstance(inspection["vmware_tools"]["present"], bool)
    assert isinstance(inspection["ipv4_addresses"], list)
    assert isinstance(inspection["default_routes"], list)
    assert inspection["default_route_source"] in {
        "route-table",
        "gateway-fallback",
    }
    assert isinstance(inspection["system_runtime_inventory"], list)
    assert isinstance(inspection["winrm_firewall"]["available"], bool)
