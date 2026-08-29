import copy
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import inno_setup_provenance as inno
from scripts.collect_licenses import collect_installer_component_license

REPO_ROOT = Path(__file__).resolve().parents[1]
COMPONENTS_FILE = REPO_ROOT / "compliance" / "components.json"


def _component_lock() -> dict[str, object]:
    return json.loads(COMPONENTS_FILE.read_text(encoding="utf-8"))


def _valid_provenance(component: dict[str, object]) -> dict[str, object]:
    signer = {
        "status": "Valid",
        "subject": inno.INNO_SIGNER_SUBJECT,
        "thumbprint": inno.INNO_SIGNER_THUMBPRINT,
    }
    files = []
    for record in component["toolchain_files"]:
        item = {
            "path": record["path"],
            "role": record["role"],
            "size": record["size"],
            "sha256": record["sha256"],
        }
        if record.get("authenticode") is True:
            item["authenticode"] = copy.deepcopy(signer)
        if record.get("issig_key") is not None:
            item["issig"] = {
                "key": record["issig_key"],
                "verified": True,
            }
        files.append(item)
    return {
        "schema_version": 1,
        "component": inno.INNO_COMPONENT,
        "version": inno.INNO_VERSION,
        "source": {
            "repository": inno.INNO_REPOSITORY,
            "tag": inno.INNO_TAG,
            "tag_object": inno.INNO_TAG_OBJECT,
            "commit": inno.INNO_SOURCE_COMMIT,
            "archive": component["source_archives"][0],
        },
        "official_installer": {
            "filename": inno.INNO_INSTALLER_FILENAME,
            "url": inno.INNO_INSTALLER_URL,
            "size": inno.INNO_INSTALLER_SIZE,
            "sha256": inno.INNO_INSTALLER_SHA256,
            "release_attestation": {
                "repository": inno.INNO_REPOSITORY,
                "tag": inno.INNO_TAG,
                "asset": inno.INNO_INSTALLER_FILENAME,
                "verified": True,
            },
            "authenticode": signer,
        },
        "toolchain_files": files,
        "generated_installer_markers": component["generated_installer_markers"],
        "distribution_boundary": component["distribution_boundary"],
    }


def test_repository_inno_lock_and_local_license_are_exact():
    lock = _component_lock()

    component = inno.validate_component_lock(lock)
    material = component["license_materials"][0]
    local_license = REPO_ROOT / material["path"]

    assert component["version"] == "6.7.3"
    assert component["official_installer"]["url"] == inno.INNO_INSTALLER_URL
    assert component["source_tag"]["verified_signature"] is True
    assert local_license.is_file()
    assert hashlib.sha256(local_license.read_bytes()).hexdigest() == material["sha256"]


@pytest.mark.parametrize(
    ("path", "replacement"),
    [
        (("version",), "6.7.4"),
        (("license",), "Unknown"),
        (("homepage",), "https://example.invalid"),
        (("source_tag", "commit"), "0" * 40),
        (("official_installer", "url"), "https://example.invalid/setup.exe"),
        (("official_installer", "sha256"), "0" * 64),
        (("license_materials", 0, "sha256"), "0" * 64),
        (("toolchain_files", 0, "sha256"), "0" * 64),
        (("generated_installer_markers", "legal_copyright"), "Unknown"),
        (("generated_installer_markers", "file_description"), "Unknown"),
        (("distribution_boundary", "uninstaller_origin"), "Setup.e32"),
    ],
)
def test_inno_component_lock_rejects_identity_drift(path, replacement):
    lock = _component_lock()
    target = lock["installer_components"][0]
    for segment in path[:-1]:
        target = target[segment]
    target[path[-1]] = replacement

    with pytest.raises(inno.InnoSetupProvenanceError):
        inno.validate_component_lock(lock)


def test_build_provenance_binds_installer_signatures_and_toolchain():
    lock = _component_lock()
    component = inno.validate_component_lock(lock)
    provenance = _valid_provenance(component)
    identity = inno.validate_provenance(provenance, component)
    build_provenance = {
        "schema_version": 1,
        "inno_setup": provenance,
        "inno_setup_provenance_sha256": identity,
    }

    assert inno.validate_build_provenance(build_provenance, lock) == identity

    tampered_signature = copy.deepcopy(build_provenance)
    tampered_signature["inno_setup"]["official_installer"]["authenticode"][
        "thumbprint"
    ] = "0" * 40
    with pytest.raises(inno.InnoSetupProvenanceError):
        inno.validate_build_provenance(tampered_signature, lock)

    tampered_toolchain = copy.deepcopy(build_provenance)
    tampered_toolchain["inno_setup"]["toolchain_files"][0]["sha256"] = "0" * 64
    with pytest.raises(inno.InnoSetupProvenanceError):
        inno.validate_build_provenance(tampered_toolchain, lock)

    for marker_field in ("legal_copyright", "file_description"):
        tampered_marker = copy.deepcopy(build_provenance)
        tampered_marker["inno_setup"]["generated_installer_markers"][
            marker_field
        ] = "tampered"
        with pytest.raises(inno.InnoSetupProvenanceError):
            inno.validate_build_provenance(tampered_marker, lock)

    tampered_seal = copy.deepcopy(build_provenance)
    tampered_seal["inno_setup_provenance_sha256"] = "0" * 64
    with pytest.raises(inno.InnoSetupProvenanceError):
        inno.validate_build_provenance(tampered_seal, lock)


def test_attestation_rejects_output_or_build_provenance_input_aliases(tmp_path):
    lock_copy = tmp_path / "components.json"
    lock_copy.write_bytes(COMPONENTS_FILE.read_bytes())
    placeholder = tmp_path / "placeholder"
    placeholder.write_bytes(b"placeholder")
    install_root = tmp_path / "toolchain"
    install_root.mkdir()

    with pytest.raises(inno.InnoSetupProvenanceError, match="already exists"):
        inno.attest_install(
            components_file=lock_copy,
            installer=placeholder,
            install_root=install_root,
            signature_report_path=placeholder,
            output_provenance=lock_copy,
        )

    output = tmp_path / "provenance.json"
    with pytest.raises(inno.InnoSetupProvenanceError, match="aliases component lock"):
        inno.attest_install(
            components_file=lock_copy,
            installer=placeholder,
            install_root=install_root,
            signature_report_path=placeholder,
            output_provenance=output,
            build_provenance_path=lock_copy,
        )

    nested_output = install_root / "provenance.json"
    with pytest.raises(inno.InnoSetupProvenanceError, match="outside"):
        inno.attest_install(
            components_file=lock_copy,
            installer=placeholder,
            install_root=install_root,
            signature_report_path=placeholder,
            output_provenance=nested_output,
        )


def test_installer_component_license_is_copied_from_the_locked_repository_file(
    tmp_path,
):
    component = inno.validate_component_lock(_component_lock())
    destination = tmp_path / "custom-license-destination"

    manifest = collect_installer_component_license(
        component,
        destination,
        seen_targets={},
    )

    relative = "inno-setup/LICENSE.txt"
    copied = destination / relative
    assert manifest["component"] == "inno-setup"
    assert manifest["license_files"] == [relative]
    assert copied.read_bytes() == (
        REPO_ROOT / "licenses" / "inno-setup" / "LICENSE.txt"
    ).read_bytes()
    assert manifest["license_file_sha256"][relative] == inno.sha256_file(copied)
    assert not (tmp_path / "licenses" / relative).exists()


def test_windows_workflows_prepare_and_reverify_the_pinned_toolchain():
    release_workflow = (REPO_ROOT / ".github" / "workflows" / "release.yml").read_text(
        encoding="utf-8"
    )
    ci_workflow = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )
    prepare_script = (REPO_ROOT / "scripts" / "prepare_inno_setup.ps1").read_text(
        encoding="utf-8"
    )
    build_script = (REPO_ROOT / "scripts" / "build_installer.ps1").read_text(
        encoding="utf-8"
    )
    installer_script = (REPO_ROOT / "installer" / "LoLReplayTool.iss").read_text(
        encoding="utf-8"
    )

    for workflow in (release_workflow, ci_workflow):
        assert "choco install innosetup" not in workflow
        assert ".\\scripts\\prepare_inno_setup.ps1" in workflow
        assert "-InnoSetupRoot $env:INNO_SETUP_ROOT" in workflow
        assert "-InnoSetupProvenance $env:INNO_SETUP_PROVENANCE" in workflow
    assert (
        "SEALED_BUILD_PROVENANCE_SHA256: "
        "${{ steps.inno_environment.outputs.build_provenance_sha256 }}"
        in release_workflow
    )
    assert "release verify-asset" in prepare_script
    assert "Get-AuthenticodeSignature" in prepare_script
    assert '"--key-file=$keyPath" verify $path' in prepare_script
    gh_verification = prepare_script.index("& $ghCommand.Source release verify-asset")
    token_clear = prepare_script.index(
        '[Environment]::SetEnvironmentVariable("GH_TOKEN", $null, "Process")',
        gh_verification,
    )
    installer_execution = prepare_script.index("$installProcess = Start-Process")
    signature_execution = prepare_script.index('& $signatureTool "--key-file=$keyPath"')
    assert gh_verification < token_clear < installer_execution < signature_execution
    assert prepare_script.index("} finally {", token_clear) > signature_execution
    assert '"verify",' in build_script
    assert "$resolvedBuildProvenance -and -not $resolvedInnoSetupRoot" in build_script
    assert "VersionInfoCopyright=" + inno.INNO_COPYRIGHT in installer_script
    assert "VersionInfoDescription=" + inno.INNO_FILE_DESCRIPTION in installer_script
    assert "VersionInfoCompany=Inno Setup" not in installer_script
    assert "LZMAUseSeparateProcess=yes" in installer_script
    assert "UseSetupLdr=x86" in installer_script
    assert (
        REPO_ROOT / ".gitattributes"
    ).read_text(encoding="utf-8").splitlines() == [
        "LICENSE text eol=lf",
        "licenses/inno-setup/LICENSE.txt text eol=lf",
        "licenses/python-packages/PyQt6-Qt6/qt-6.10.2/** -text -whitespace",
    ]


def test_prepare_wrapper_rejects_output_alias_before_network_or_install(tmp_path):
    if os.name != "nt":
        pytest.skip("PowerShell path safety is Windows-specific")
    pwsh = shutil.which("pwsh")
    if pwsh is None:
        pytest.skip("PowerShell 7 is not installed")
    download_root = tmp_path / "download"
    install_root = tmp_path / "install"

    result = subprocess.run(
        [
            pwsh,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(REPO_ROOT / "scripts" / "prepare_inno_setup.ps1"),
            "-PythonExe",
            sys.executable,
            "-Components",
            str(COMPONENTS_FILE),
            "-DownloadDirectory",
            str(download_root),
            "-InstallDirectory",
            str(install_root),
            "-OutputProvenance",
            str(COMPONENTS_FILE),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "must not already exist" in (result.stdout + result.stderr)
    assert not download_root.exists()
    assert not install_root.exists()
