"""Verify the pinned Inno Setup compiler input and seal its build provenance."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

INNO_COMPONENT = "inno-setup"
INNO_VERSION = "6.7.3"
INNO_LICENSE_NAME = "Inno Setup License"
INNO_HOMEPAGE = "https://jrsoftware.org/isinfo.php"
INNO_REPOSITORY = "jrsoftware/issrc"
INNO_TAG = "is-6_7_3"
INNO_TAG_OBJECT = "c7af86b4b2fd03371185df2b09a1dca8d472ab70"
INNO_SOURCE_COMMIT = "4adf37ed7f3fd2bd11c6836ba056e3de170fbabf"
INNO_INSTALLER_FILENAME = "innosetup-6.7.3.exe"
INNO_INSTALLER_URL = (
    "https://github.com/jrsoftware/issrc/releases/download/"
    "is-6_7_3/innosetup-6.7.3.exe"
)
INNO_INSTALLER_SIZE = 10_592_232
INNO_INSTALLER_SHA256 = (
    "9c73c3bae7ed48d44112a0f48e66742c00090bdb5bef71d9d3c056c66e97b732"
)
INNO_SOURCE_FILENAME = "issrc-is-6_7_3.zip"
INNO_SOURCE_URL = (
    "https://github.com/jrsoftware/issrc/archive/refs/tags/is-6_7_3.zip"
)
INNO_SOURCE_SIZE = 5_749_717
INNO_SOURCE_SHA256 = (
    "db98c24bc0280278c708f9bb1b42794216f0c8fe0d28b60eaa0e2f293bdaddd3"
)
INNO_LICENSE_URL = (
    "https://raw.githubusercontent.com/jrsoftware/issrc/is-6_7_3/license.txt"
)
INNO_LICENSE_SIZE = 1_521
INNO_LICENSE_SHA256 = (
    "2e5346868c2a18434489824e11d65c3031620f792fefc415d05f19cd441abf5c"
)
INNO_PACKAGED_LICENSE_SHA256 = (
    "0c81595601bce47eeef8d865d5da7f9ca2c6a12235b7482b29f5ab23ed02ee5a"
)
INNO_SIGNER_SUBJECT = "CN=Pyrsys B.V., O=Pyrsys B.V., S=Noord-Holland, C=NL"
INNO_SIGNER_THUMBPRINT = "E0AB19C8D38CBF9C44709925122A7A02F8C70CB7"
INNO_COPYRIGHT = (
    "Copyright © 1997-2026 Jordan Russell. "
    "Portions Copyright © 2000-2026 Martijn Laan."
)
INNO_WEBSITE = "https://www.innosetup.com"
INNO_FILE_DESCRIPTION = f"LoL Replay Tool Setup - Inno Setup {INNO_WEBSITE}"
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
EXPECTED_DISTRIBUTION_BOUNDARY = {
    "published_artifact": "LoLReplayTool-Setup-<version>.exe",
    "compiler_only_inputs": [
        "ISCC.exe",
        "ISCmplr.dll",
        "ISPP.dll",
        "ISPPBuiltins.iss",
        "Default.isl",
        "Languages/Japanese.isl",
        "islzma.dll",
        "islzma32.exe",
        "islzma64.exe",
        "ISSigTool.exe",
    ],
    "embedded_code_inputs": [
        "SetupLdr.e32",
        "SetupCustomStyle.e32",
        "LZMA decompression code carried by the selected Setup stub",
    ],
    "uninstaller_origin": "SetupCustomStyle.e32",
    "compiler_files_copied_to_application_root": False,
    "source_evidence": [
        "Projects/Src/Compiler.SetupCompiler.pas",
        "Projects/Src/Setup.dpr",
        "Projects/Src/SetupLdr.dpr",
        "Projects/Src/Compression.LZMACompressor.pas",
        "Projects/Src/Compression.LZMADecompressor.pas",
        "Projects/Src/Compression.LZMADecompressor/Lzma2Decode/ISLzmaDec.c",
    ],
}

EXPECTED_PUBLIC_KEYS: tuple[dict[str, Any], ...] = (
    {
        "filename": "def01.ispublickey",
        "url": (
            "https://raw.githubusercontent.com/jrsoftware/issrc/"
            "is-6_7_3/def01.ispublickey"
        ),
        "size": 248,
        "sha256": (
            "3b0b7e1e478c9ab327746a990512e2b245c9231e88a7fdd60d7874dd439db562"
        ),
        "key_id": (
            "def0147c3bbc17ab99bf7b7a9c2de1390283f38972152418d7c2a4a7d7131a38"
        ),
    },
    {
        "filename": "def02.ispublickey",
        "url": (
            "https://raw.githubusercontent.com/jrsoftware/issrc/"
            "is-6_7_3/def02.ispublickey"
        ),
        "size": 248,
        "sha256": (
            "32bea6bceb4ac7c4e6b3becdf3fb38de77378c5e76d494ab907d87cfab9e597b"
        ),
        "key_id": (
            "def020edee3c4835fd54d85eff8b66d4d899b22a777353ca4a114b652e5e7a28"
        ),
    },
)

EXPECTED_TOOLCHAIN_FILES: tuple[dict[str, Any], ...] = (
    {
        "path": "ISCC.exe",
        "role": "command-line compiler entry point",
        "size": 1_456_272,
        "sha256": "0a8757031b33777e4c9cbffee40f11a5062b36d25cbe144c1db73b6102b80ad7",
        "authenticode": True,
    },
    {
        "path": "ISCmplr.dll",
        "role": "compiler engine",
        "size": 1_524_880,
        "sha256": "85a1e3090d3a5b85319f001b7c8f9ecfad45f37eff030a67bbe29ef58b7aa2c3",
        "authenticode": True,
        "issig_key": "def02.ispublickey",
    },
    {
        "path": "ISCmplr.dll.issig",
        "role": "compiler engine upstream signature",
        "size": 367,
        "sha256": "5c4b4b0fc6958934918c611eb509ecf63827a2bb36ce61fcb538fc5897b3a7ea",
    },
    {
        "path": "ISPP.dll",
        "role": "preprocessor engine",
        "size": 1_006_736,
        "sha256": "bde04be7f4a55ca56afb6b06600d7cbe8326fac468ac3f2885b80eefb3ed2a7a",
        "authenticode": True,
        "issig_key": "def02.ispublickey",
    },
    {
        "path": "ISPP.dll.issig",
        "role": "preprocessor engine upstream signature",
        "size": 364,
        "sha256": "5f66c7d86af3be12522f044541b747f293c8131268e16d27c622bfc14f7250c9",
    },
    {
        "path": "ISPPBuiltins.iss",
        "role": "preprocessor built-ins",
        "size": 11_302,
        "sha256": "bc765e1121a95dc602ca1248b484418cecc6db9d4b6f4dae58e9a2c35262527c",
    },
    {
        "path": "Setup.e32",
        "role": "classic Setup and Uninstall stub",
        "size": 4_427_264,
        "sha256": "a80d75ba8d8c336050c37f7707de8bb017cc93597329e0ea18d474156b3e0da5",
        "issig_key": "def02.ispublickey",
    },
    {
        "path": "Setup.e32.issig",
        "role": "classic Setup and Uninstall stub upstream signature",
        "size": 365,
        "sha256": "09dbd2458c48a249eeda5ae2705a4315192cc96ecd1b233136f11799d5dff175",
    },
    {
        "path": "SetupCustomStyle.e32",
        "role": "selected modern-style Setup and Uninstall stub",
        "size": 5_810_688,
        "sha256": "9675a2a5c78c66b691cc80270031da42bebad5725fbf60853cb4e5f90251bd5e",
        "issig_key": "def02.ispublickey",
    },
    {
        "path": "SetupCustomStyle.e32.issig",
        "role": "modern-style Setup and Uninstall stub upstream signature",
        "size": 376,
        "sha256": "cb513ff3e47a9c52341620ebd787e968f9c71fd012c6836e78fb039013e8256f",
    },
    {
        "path": "SetupLdr.e32",
        "role": "selected x86 Setup loader",
        "size": 950_272,
        "sha256": "5475964893adbb33ccc420ad4d88bbdff27dfaaca7580cd6058ea2798893490d",
        "issig_key": "def02.ispublickey",
    },
    {
        "path": "SetupLdr.e32.issig",
        "role": "x86 Setup loader upstream signature",
        "size": 367,
        "sha256": "12441b9477b0363993f06e325767f8b925a20e895e364fa8460754ee39ca6c08",
    },
    {
        "path": "Default.isl",
        "role": "default compiler messages",
        "size": 21_993,
        "sha256": "42a5f6f7dbbddf26cc278f67db5d894235ce1d126a6856702e31bd02023a1316",
    },
    {
        "path": "Languages/Japanese.isl",
        "role": "selected installer language",
        "size": 28_186,
        "sha256": "d5450537bb128112347bf86a4bdc3a4be0605414df0bc4fc90f55aaef1ba369b",
    },
    {
        "path": "islzma.dll",
        "role": "LZMA compression library",
        "size": 139_408,
        "sha256": "8af307ce3738ab1d37cc7e76b2f76615e2045597d4367c3e40d52b5f2fbd353d",
        "authenticode": True,
        "issig_key": "def01.ispublickey",
    },
    {
        "path": "islzma.dll.issig",
        "role": "LZMA compression library upstream signature",
        "size": 365,
        "sha256": "c49d4b2cc6499846118dc376c9c66081744531edc932ffa979698d68ec0662b2",
    },
    {
        "path": "islzma32.exe",
        "role": "x86 LZMA worker fallback",
        "size": 203_408,
        "sha256": "fd977c3ac44c56997d2b4fd6647e97e2457c43991a6ae26a4b2252c0eabdfebb",
        "authenticode": True,
        "issig_key": "def01.ispublickey",
    },
    {
        "path": "islzma32.exe.issig",
        "role": "x86 LZMA worker upstream signature",
        "size": 367,
        "sha256": "81039bd05963f8b02e1396a6fb5f3f7df7fd1ce1655f7a960f6c0b549489c562",
    },
    {
        "path": "islzma64.exe",
        "role": "selected x64 LZMA worker",
        "size": 226_448,
        "sha256": "87a50495aff580b78cad57906a765d3cf28ca508cfa95c24c1247c286ddf71b2",
        "authenticode": True,
        "issig_key": "def01.ispublickey",
    },
    {
        "path": "islzma64.exe.issig",
        "role": "x64 LZMA worker upstream signature",
        "size": 367,
        "sha256": "e74ea022afb90bbff710d9952a1069767c970484b1be596771ae7d109932f716",
    },
    {
        "path": "ISSigTool.exe",
        "role": "upstream signature verifier used only during build preparation",
        "size": 919_184,
        "sha256": "aea490d45665a88c0c832d25647d21c1b87962efedb25668caec05678e0fd7c6",
        "authenticode": True,
    },
    {
        "path": "license.txt",
        "role": "upstream Inno Setup license",
        "size": 1_521,
        "sha256": "2e5346868c2a18434489824e11d65c3031620f792fefc415d05f19cd441abf5c",
    },
)


class InnoSetupProvenanceError(RuntimeError):
    """The pinned Inno Setup build input could not be verified."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_json_sha256(payload: object) -> str:
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()


def _load_json(path: Path, *, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InnoSetupProvenanceError(f"Cannot read {label}: {exc}") from exc
    if not isinstance(payload, dict):
        raise InnoSetupProvenanceError(f"{label} must be a JSON object.")
    return payload


def _is_link_or_reparse(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise InnoSetupProvenanceError(f"Cannot inspect {path}: {exc}") from exc
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0) & reparse_flag
    )


def _require_regular_file(path: Path, *, label: str) -> None:
    if _is_link_or_reparse(path) or not path.is_file():
        raise InnoSetupProvenanceError(f"{label} must be a regular file: {path}")


def _path_key(path: Path) -> str:
    try:
        resolved = path.resolve(strict=False)
    except OSError:
        resolved = path.absolute()
    return os.path.normcase(str(resolved))


def _is_within(root: Path, candidate: Path) -> bool:
    try:
        candidate.resolve(strict=False).relative_to(root.resolve(strict=False))
    except (OSError, ValueError):
        return False
    return True


def _validate_attestation_paths(
    *,
    components_file: Path,
    installer: Path,
    install_root: Path,
    signature_report_path: Path,
    output_provenance: Path,
    build_provenance_path: Path | None,
) -> None:
    if os.path.lexists(output_provenance):
        raise InnoSetupProvenanceError(
            f"Inno Setup provenance output already exists: {output_provenance}"
        )
    writable = {"output provenance": output_provenance}
    if build_provenance_path is not None:
        writable["build provenance"] = build_provenance_path
    inputs = {
        "component lock": components_file,
        "official installer": installer,
        "signature report": signature_report_path,
    }
    writable_items = list(writable.items())
    for index, (label, path) in enumerate(writable_items):
        if _is_within(install_root, path):
            raise InnoSetupProvenanceError(
                f"{label.capitalize()} must be outside the Inno Setup root."
            )
        for other_label, other_path in inputs.items():
            if _path_key(path) == _path_key(other_path):
                raise InnoSetupProvenanceError(
                    f"{label.capitalize()} aliases {other_label}."
                )
        for other_label, other_path in writable_items[index + 1 :]:
            if _path_key(path) == _path_key(other_path):
                raise InnoSetupProvenanceError(
                    f"{label.capitalize()} aliases {other_label}."
                )


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.path.lexists(path) and _is_link_or_reparse(path):
        raise InnoSetupProvenanceError(
            f"Provenance output must not be a link: {path}"
        )
    serialized = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    handle, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
        text=True,
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as target:
            target.write(serialized)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary_name, path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise


def _inno_component(lock: dict[str, Any]) -> dict[str, Any]:
    components = lock.get("installer_components")
    if not isinstance(components, list):
        raise InnoSetupProvenanceError(
            "Component lock has no installer_components list."
        )
    matches = [
        item
        for item in components
        if isinstance(item, dict) and item.get("component") == INNO_COMPONENT
    ]
    if len(matches) != 1:
        raise InnoSetupProvenanceError(
            "Component lock must contain exactly one inno-setup entry."
        )
    return matches[0]


def _require_exact(actual: object, expected: object, *, label: str) -> None:
    if actual != expected:
        raise InnoSetupProvenanceError(f"Pinned Inno Setup {label} differs.")


def validate_component_lock(lock: dict[str, Any]) -> dict[str, Any]:
    """Return the pinned component after checking every immutable upstream input."""

    component = _inno_component(lock)
    _require_exact(component.get("version"), INNO_VERSION, label="version")
    _require_exact(component.get("license"), INNO_LICENSE_NAME, label="license")
    _require_exact(component.get("homepage"), INNO_HOMEPAGE, label="homepage")
    _require_exact(
        component.get("packaged_in_distribution"),
        True,
        label="distribution boundary",
    )
    _require_exact(
        component.get("source_status"),
        "verified_corresponding_source",
        label="source status",
    )
    _require_exact(
        component.get("source_tag"),
        {
            "repository": INNO_REPOSITORY,
            "tag": INNO_TAG,
            "tag_object": INNO_TAG_OBJECT,
            "commit": INNO_SOURCE_COMMIT,
            "verified_signature": True,
        },
        label="signed source tag",
    )
    _require_exact(
        component.get("source_archives"),
        [
            {
                "filename": INNO_SOURCE_FILENAME,
                "url": INNO_SOURCE_URL,
                "sha256": INNO_SOURCE_SHA256,
                "size": INNO_SOURCE_SIZE,
            }
        ],
        label="source archive",
    )
    _require_exact(
        component.get("upstream_license"),
        {
            "url": INNO_LICENSE_URL,
            "sha256": INNO_LICENSE_SHA256,
            "size": INNO_LICENSE_SIZE,
        },
        label="upstream license",
    )
    _require_exact(
        component.get("official_installer"),
        {
            "filename": INNO_INSTALLER_FILENAME,
            "url": INNO_INSTALLER_URL,
            "sha256": INNO_INSTALLER_SHA256,
            "size": INNO_INSTALLER_SIZE,
            "release_repository": INNO_REPOSITORY,
            "release_tag": INNO_TAG,
            "release_attestation_required": True,
            "authenticode": {
                "status": "Valid",
                "subject": INNO_SIGNER_SUBJECT,
                "thumbprint": INNO_SIGNER_THUMBPRINT,
            },
        },
        label="official installer",
    )
    _require_exact(
        component.get("public_keys"),
        list(EXPECTED_PUBLIC_KEYS),
        label="public keys",
    )
    _require_exact(
        component.get("toolchain_files"),
        list(EXPECTED_TOOLCHAIN_FILES),
        label="toolchain inventory",
    )
    _require_exact(
        component.get("distribution_boundary"),
        EXPECTED_DISTRIBUTION_BOUNDARY,
        label="distribution boundary",
    )
    markers = component.get("generated_installer_markers")
    _require_exact(
        markers,
        {
            "legal_copyright": INNO_COPYRIGHT,
            "file_description": INNO_FILE_DESCRIPTION,
        },
        label="generated installer markers",
    )
    materials = component.get("license_materials")
    if not isinstance(materials, list) or len(materials) != 1:
        raise InnoSetupProvenanceError(
            "Pinned Inno Setup license material must contain exactly one file."
        )
    material = materials[0]
    if not isinstance(material, dict):
        raise InnoSetupProvenanceError("Pinned Inno Setup license material is invalid.")
    _require_exact(
        material.get("path"),
        "licenses/inno-setup/LICENSE.txt",
        label="packaged license path",
    )
    _require_exact(
        material.get("sha256"),
        INNO_PACKAGED_LICENSE_SHA256,
        label="packaged license SHA256",
    )
    return component


def validate_component_file(components_file: Path) -> dict[str, Any]:
    lock = _load_json(components_file, label="component lock")
    if lock.get("schema_version") != 1:
        raise InnoSetupProvenanceError("Unsupported component lock schema.")
    return validate_component_lock(lock)


def _signature_report_entry(
    signature_report: dict[str, Any],
    path: str,
) -> dict[str, Any]:
    signatures = signature_report.get("authenticode")
    if not isinstance(signatures, dict) or not isinstance(signatures.get(path), dict):
        raise InnoSetupProvenanceError(
            f"Authenticode verification report is missing for {path}."
        )
    entry = signatures[path]
    _require_exact(entry.get("status"), "Valid", label=f"{path} signature status")
    _require_exact(
        entry.get("subject"),
        INNO_SIGNER_SUBJECT,
        label=f"{path} signer subject",
    )
    _require_exact(
        entry.get("thumbprint"),
        INNO_SIGNER_THUMBPRINT,
        label=f"{path} signer thumbprint",
    )
    return {
        "status": "Valid",
        "subject": INNO_SIGNER_SUBJECT,
        "thumbprint": INNO_SIGNER_THUMBPRINT,
    }


def _verify_regular_locked_file(
    root: Path,
    record: dict[str, Any],
) -> dict[str, Any]:
    relative = PurePosixPath(str(record["path"]))
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise InnoSetupProvenanceError(
            f"Unsafe Inno Setup toolchain path: {record['path']}"
        )
    path = root.joinpath(*relative.parts)
    _require_regular_file(path, label=f"Inno Setup toolchain file {relative.as_posix()}")
    actual_size = path.stat().st_size
    actual_hash = sha256_file(path)
    if actual_size != record["size"] or actual_hash != record["sha256"]:
        raise InnoSetupProvenanceError(
            f"Pinned Inno Setup toolchain file differs: {relative.as_posix()}"
        )
    return {
        "path": relative.as_posix(),
        "role": str(record["role"]),
        "size": actual_size,
        "sha256": actual_hash,
    }


def attest_install(
    *,
    components_file: Path,
    installer: Path,
    install_root: Path,
    signature_report_path: Path,
    output_provenance: Path,
    build_provenance_path: Path | None = None,
) -> dict[str, Any]:
    """Verify actual compiler bytes/signatures and write sealed provenance."""

    _validate_attestation_paths(
        components_file=components_file,
        installer=installer,
        install_root=install_root,
        signature_report_path=signature_report_path,
        output_provenance=output_provenance,
        build_provenance_path=build_provenance_path,
    )
    component = validate_component_file(components_file)
    _require_regular_file(installer, label="Inno Setup official installer")
    if (
        installer.name != INNO_INSTALLER_FILENAME
        or installer.stat().st_size != INNO_INSTALLER_SIZE
        or sha256_file(installer) != INNO_INSTALLER_SHA256
    ):
        raise InnoSetupProvenanceError("Pinned Inno Setup official installer differs.")
    if _is_link_or_reparse(install_root) or not install_root.is_dir():
        raise InnoSetupProvenanceError(
            f"Inno Setup root must be a regular directory: {install_root}"
        )
    signature_report = _load_json(
        signature_report_path,
        label="Inno Setup signature report",
    )
    if signature_report.get("schema_version") != 1:
        raise InnoSetupProvenanceError("Unsupported Inno Setup signature report schema.")
    _require_exact(
        signature_report.get("release_attestation"),
        {
            "repository": INNO_REPOSITORY,
            "tag": INNO_TAG,
            "asset": INNO_INSTALLER_FILENAME,
            "verified": True,
        },
        label="GitHub Release attestation",
    )
    installer_signature = _signature_report_entry(signature_report, "official_installer")
    issig_report = signature_report.get("issig")
    if not isinstance(issig_report, dict):
        raise InnoSetupProvenanceError("ISSig verification report is missing.")

    files: list[dict[str, Any]] = []
    for locked_file in component["toolchain_files"]:
        actual = _verify_regular_locked_file(install_root, locked_file)
        path = actual["path"]
        if locked_file.get("authenticode") is True:
            actual["authenticode"] = _signature_report_entry(signature_report, path)
        key = locked_file.get("issig_key")
        if key is not None:
            report = issig_report.get(path)
            _require_exact(
                report,
                {"key": key, "verified": True},
                label=f"{path} ISSig verification",
            )
            actual["issig"] = report
        files.append(actual)

    provenance = {
        "schema_version": 1,
        "component": INNO_COMPONENT,
        "version": INNO_VERSION,
        "source": {
            "repository": INNO_REPOSITORY,
            "tag": INNO_TAG,
            "tag_object": INNO_TAG_OBJECT,
            "commit": INNO_SOURCE_COMMIT,
            "archive": component["source_archives"][0],
        },
        "official_installer": {
            "filename": installer.name,
            "url": INNO_INSTALLER_URL,
            "size": installer.stat().st_size,
            "sha256": sha256_file(installer),
            "release_attestation": signature_report["release_attestation"],
            "authenticode": installer_signature,
        },
        "toolchain_files": files,
        "generated_installer_markers": component["generated_installer_markers"],
        "distribution_boundary": component.get("distribution_boundary"),
    }
    _atomic_write_json(output_provenance, provenance)
    provenance_sha256 = canonical_json_sha256(provenance)
    if build_provenance_path is not None:
        _require_regular_file(build_provenance_path, label="build provenance")
        build_provenance = _load_json(
            build_provenance_path,
            label="build provenance",
        )
        if build_provenance.get("schema_version") != 1:
            raise InnoSetupProvenanceError("Unsupported build provenance schema.")
        if "inno_setup" in build_provenance or "inno_setup_provenance_sha256" in (
            build_provenance
        ):
            raise InnoSetupProvenanceError(
                "Build provenance already contains Inno Setup provenance."
            )
        build_provenance["inno_setup"] = provenance
        build_provenance["inno_setup_provenance_sha256"] = provenance_sha256
        _atomic_write_json(build_provenance_path, build_provenance)
    return provenance


def validate_provenance(
    provenance: object,
    component: dict[str, Any],
) -> str:
    """Validate sealed Inno provenance and return its canonical identity."""

    if not isinstance(provenance, dict) or provenance.get("schema_version") != 1:
        raise InnoSetupProvenanceError("Inno Setup provenance schema is invalid.")
    _require_exact(provenance.get("component"), INNO_COMPONENT, label="component")
    _require_exact(provenance.get("version"), INNO_VERSION, label="version")
    source = provenance.get("source")
    _require_exact(
        source,
        {
            "repository": INNO_REPOSITORY,
            "tag": INNO_TAG,
            "tag_object": INNO_TAG_OBJECT,
            "commit": INNO_SOURCE_COMMIT,
            "archive": component["source_archives"][0],
        },
        label="source provenance",
    )
    installer = provenance.get("official_installer")
    if not isinstance(installer, dict):
        raise InnoSetupProvenanceError("Inno Setup installer provenance is missing.")
    _require_exact(installer.get("filename"), INNO_INSTALLER_FILENAME, label="installer filename")
    _require_exact(installer.get("url"), INNO_INSTALLER_URL, label="installer URL")
    _require_exact(installer.get("size"), INNO_INSTALLER_SIZE, label="installer size")
    _require_exact(installer.get("sha256"), INNO_INSTALLER_SHA256, label="installer SHA256")
    _require_exact(
        installer.get("release_attestation"),
        {
            "repository": INNO_REPOSITORY,
            "tag": INNO_TAG,
            "asset": INNO_INSTALLER_FILENAME,
            "verified": True,
        },
        label="release attestation",
    )
    _require_exact(
        installer.get("authenticode"),
        {
            "status": "Valid",
            "subject": INNO_SIGNER_SUBJECT,
            "thumbprint": INNO_SIGNER_THUMBPRINT,
        },
        label="installer Authenticode identity",
    )
    files = provenance.get("toolchain_files")
    if not isinstance(files, list):
        raise InnoSetupProvenanceError("Inno Setup toolchain provenance is missing.")
    expected_by_path = {
        str(item["path"]): item for item in component["toolchain_files"]
    }
    if {item.get("path") for item in files if isinstance(item, dict)} != set(
        expected_by_path
    ) or len(files) != len(expected_by_path):
        raise InnoSetupProvenanceError("Inno Setup toolchain provenance set differs.")
    for item in files:
        if not isinstance(item, dict):
            raise InnoSetupProvenanceError("Inno Setup toolchain provenance is invalid.")
        expected = expected_by_path[str(item["path"])]
        for field in ("role", "size", "sha256"):
            _require_exact(
                item.get(field),
                expected[field],
                label=f"{item['path']} {field}",
            )
        if expected.get("authenticode") is True:
            _require_exact(
                item.get("authenticode"),
                {
                    "status": "Valid",
                    "subject": INNO_SIGNER_SUBJECT,
                    "thumbprint": INNO_SIGNER_THUMBPRINT,
                },
                label=f"{item['path']} Authenticode identity",
            )
        elif "authenticode" in item:
            raise InnoSetupProvenanceError(
                f"Unexpected Authenticode record for {item['path']}."
            )
        expected_key = expected.get("issig_key")
        if expected_key is not None:
            _require_exact(
                item.get("issig"),
                {"key": expected_key, "verified": True},
                label=f"{item['path']} ISSig identity",
            )
        elif "issig" in item:
            raise InnoSetupProvenanceError(
                f"Unexpected ISSig record for {item['path']}."
            )
    _require_exact(
        provenance.get("generated_installer_markers"),
        component["generated_installer_markers"],
        label="generated installer markers",
    )
    _require_exact(
        provenance.get("distribution_boundary"),
        component.get("distribution_boundary"),
        label="distribution boundary",
    )
    return canonical_json_sha256(provenance)


def validate_build_provenance(
    build_provenance: dict[str, Any],
    lock: dict[str, Any],
) -> str:
    """Validate the embedded Inno section and return its canonical identity."""

    component = validate_component_lock(lock)
    provenance = build_provenance.get("inno_setup")
    identity = validate_provenance(provenance, component)
    sealed_sha256 = build_provenance.get("inno_setup_provenance_sha256")
    if not isinstance(sealed_sha256, str) or SHA256_PATTERN.fullmatch(
        sealed_sha256
    ) is None:
        raise InnoSetupProvenanceError(
            "Build provenance has no sealed Inno Setup provenance SHA256."
        )
    if sealed_sha256 != identity:
        raise InnoSetupProvenanceError(
            "Build provenance Inno Setup identity differs from its sealed SHA256."
        )
    return identity


def verify_installed_provenance(
    *,
    components_file: Path,
    install_root: Path,
    provenance_path: Path,
    build_provenance_path: Path | None = None,
) -> str:
    """Recheck installed bytes against a previously sealed signature report."""

    component = validate_component_file(components_file)
    provenance = _load_json(provenance_path, label="Inno Setup provenance")
    identity = validate_provenance(provenance, component)
    if _is_link_or_reparse(install_root) or not install_root.is_dir():
        raise InnoSetupProvenanceError(
            f"Inno Setup root must be a regular directory: {install_root}"
        )
    for locked_file in component["toolchain_files"]:
        _verify_regular_locked_file(install_root, locked_file)
    if build_provenance_path is not None:
        build_provenance = _load_json(
            build_provenance_path,
            label="build provenance",
        )
        lock = _load_json(components_file, label="component lock")
        embedded_identity = validate_build_provenance(build_provenance, lock)
        if embedded_identity != identity or build_provenance.get("inno_setup") != provenance:
            raise InnoSetupProvenanceError(
                "Standalone and embedded Inno Setup provenance differ."
            )
    return identity


def _create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser(
        "validate-lock",
        help="Validate every immutable Inno Setup component-lock field.",
    )
    validate.add_argument("--components", required=True, type=Path)
    attest = commands.add_parser(
        "attest",
        help="Verify the installed toolchain and seal its provenance.",
    )
    attest.add_argument("--components", required=True, type=Path)
    attest.add_argument("--installer", required=True, type=Path)
    attest.add_argument("--install-root", required=True, type=Path)
    attest.add_argument("--signature-report", required=True, type=Path)
    attest.add_argument("--output-provenance", required=True, type=Path)
    attest.add_argument("--build-provenance", type=Path)
    verify = commands.add_parser(
        "verify",
        help="Recheck installed bytes against sealed Inno Setup provenance.",
    )
    verify.add_argument("--components", required=True, type=Path)
    verify.add_argument("--install-root", required=True, type=Path)
    verify.add_argument("--provenance", required=True, type=Path)
    verify.add_argument("--build-provenance", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _create_parser().parse_args(argv)
    try:
        if args.command == "validate-lock":
            validate_component_file(args.components)
            print(f"Verified Inno Setup component lock: {args.components}")
            return 0
        if args.command == "verify":
            verify_installed_provenance(
                components_file=args.components,
                install_root=args.install_root,
                provenance_path=args.provenance,
                build_provenance_path=args.build_provenance,
            )
            print(f"Verified installed Inno Setup provenance: {args.provenance}")
            return 0
        attest_install(
            components_file=args.components,
            installer=args.installer,
            install_root=args.install_root,
            signature_report_path=args.signature_report,
            output_provenance=args.output_provenance,
            build_provenance_path=args.build_provenance,
        )
    except InnoSetupProvenanceError as exc:
        print(f"ERROR: {exc}")
        return 1
    print(f"Verified Inno Setup provenance: {args.output_provenance}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
