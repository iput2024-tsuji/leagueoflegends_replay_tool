"""Validate and inventory the files in a packaged application."""

from __future__ import annotations

import argparse
import ast
import fnmatch
import hashlib
import importlib.util
import io
import json
import marshal
import os
import re
import stat
import struct
import subprocess
import sys
import sysconfig
import types
import zipfile
from collections import Counter
from importlib import metadata
from pathlib import Path, PurePosixPath
from typing import Any

from scripts.collect_licenses import (
    COMPONENTS_FILE,
    PROJECT_DOCUMENTS,
    RUNTIME_REQUIREMENTS,
    canonicalize_distribution_name,
    is_meaningful_license_file,
    is_safe_regular_file,
    is_substantive_license_file,
    parse_requirement_pins,
    probe_python_native_runtime,
)
from scripts.external_runtime_policy import is_user_provided_runtime_path
from scripts.pyinstaller_runtime_policy import (
    RUNTIME_POLICY_AUDIT_FILENAME,
    is_root_vcomp_name,
    is_windows_os_runtime_name,
    validate_windows_runtime_policy_audit,
)
from src.license_info import validate_distribution_documents

MANIFEST_RELATIVE_PATH = "licenses/distribution-manifest.json"
GENERATED_ROOT_DOCUMENTS = {
    "LICENSE",
    "QT_RELINKING.md",
    "SOURCE_OFFER.md",
    "THIRD_PARTY_NOTICES.md",
    "VERSION",
}
NATIVE_SUFFIXES = {".dll", ".exe", ".pyd"}
FILE_ATTRIBUTE_REPARSE_POINT = 0x400
WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
INVALID_WINDOWS_CHARS = re.compile(r'[<>:"|?*\x00-\x1f]')

# Kept as a public constant for existing tests and downstream tooling.
REQUIRED_PACKAGE_LICENSES = {
    "PyQt6",
    "PyQt6-Qt6",
    "PyQt6-sip",
    "PyInstaller",
    "Python",
    "aiohttp",
    "numpy",
    "obsws-python",
    "opencv-python",
    "pandas",
    "python-mpv",
    "scikit-learn",
    "scipy",
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    return payload


def _safe_relative(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    normalized = value.replace("\\", "/")
    relative = PurePosixPath(normalized)
    if (
        relative.is_absolute()
        or re.match(r"^[A-Za-z]:", normalized)
        or any(
            part in {"", ".", ".."}
            or part.endswith((" ", "."))
            or part.split(".", 1)[0].upper() in WINDOWS_RESERVED_NAMES
            or INVALID_WINDOWS_CHARS.search(part)
            for part in relative.parts
        )
    ):
        return None
    return relative.as_posix()


def _within(root: Path, candidate: Path) -> bool:
    try:
        candidate.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    return True


def _is_reparse_point(metadata: os.stat_result) -> bool:
    return bool(
        stat.S_ISLNK(metadata.st_mode)
        or (
            getattr(metadata, "st_file_attributes", 0)
            & FILE_ATTRIBUTE_REPARSE_POINT
        )
    )


def _regular_nonempty_file(path: Path) -> bool:
    return is_safe_regular_file(path) and path.stat().st_size > 0


def _component_names(lock: dict[str, Any]) -> set[str]:
    return {
        *(str(component["component"]) for component in _component_entries(lock)),
        "license-materials",
    }


def _component_entries(lock: dict[str, Any]) -> list[dict[str, Any]]:
    entries = [lock["application"], lock["python"]]
    entries.extend(lock.get("runtime_components", []))
    entries.extend(lock.get("build_components", []))
    entries.extend(lock.get("installer_components", []))
    return entries


def _components_by_distribution(
    lock: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for component in _component_entries(lock):
        distribution = component.get("distribution")
        if not distribution:
            continue
        canonical = canonicalize_distribution_name(str(distribution))
        if canonical in result:
            raise ValueError(f"Duplicate distribution in component lock: {distribution}")
        result[canonical] = component
    return result


def validate_package_manifest(
    manifest_path: Path,
    component_lock: dict[str, Any] | None = None,
) -> list[str]:
    try:
        payload = _read_json(manifest_path)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        return [f"Cannot read license manifest: {exc}"]

    errors: list[str] = []
    if payload.get("schema_version") != 1:
        errors.append("Unsupported Python package license manifest schema.")
    packages = payload.get("packages")
    if not isinstance(packages, list):
        return [*errors, "License manifest does not contain a package list."]

    lock = component_lock
    if lock is None:
        lock_path = manifest_path.parent / "components.json"
        try:
            lock = _read_json(lock_path)
        except (OSError, json.JSONDecodeError, ValueError):
            lock = None

    expected: dict[str, dict[str, Any]] = {}
    installer_components: set[str] = set()
    if lock is not None:
        expected = _components_by_distribution(lock)
        for component in lock.get("installer_components", []):
            canonical = canonicalize_distribution_name(
                str(component.get("component", ""))
            )
            if not canonical or canonical in expected:
                errors.append(
                    "Duplicate or unnamed installer component in component lock."
                )
                continue
            expected[canonical] = component
            installer_components.add(canonical)
        expected["python"] = lock["python"]

    by_name: dict[str, dict[str, Any]] = {}
    seen_paths: dict[str, str] = {}
    for package in packages:
        if not isinstance(package, dict):
            errors.append("License manifest contains an invalid package entry.")
            continue
        package_name = str(package.get("name", "")).strip() or "<unknown>"
        canonical = canonicalize_distribution_name(package_name)
        if canonical in by_name:
            errors.append(f"Duplicate package in license manifest: {package_name}")
        by_name[canonical] = package

        expected_component = expected.get(canonical)
        if expected and expected_component is None:
            errors.append(f"Unexpected package in license manifest: {package_name}")
        elif expected_component is not None:
            if package.get("component") != expected_component.get("component"):
                errors.append(
                    f"Component differs from component lock for {package_name}."
                )
            expected_version = (
                payload.get("build_python_version")
                if canonical == "python"
                else expected_component.get("version")
            )
            if not isinstance(expected_version, str) or (
                package.get("version") != expected_version
            ):
                errors.append(
                    f"Package version differs from component lock for {package_name}: "
                    f"{package.get('version')} != {expected_version}"
                )
            if package.get("expected_license") != expected_component.get("license"):
                errors.append(
                    f"Expected license differs from component lock for {package_name}."
                )

        license_files = package.get("license_files")
        if not isinstance(license_files, list) or not license_files:
            errors.append(f"No license file was collected for package: {package_name}")
            continue
        substantive_paths: list[str] = []
        package_paths: set[str] = set()
        for value in license_files:
            relative_path = _safe_relative(value)
            if relative_path is None:
                errors.append(f"Unsafe license path for {package_name}: {value}")
                continue
            expected_installer_paths = set()
            if canonical in installer_components and expected_component is not None:
                for material in expected_component.get("license_materials", []):
                    if not isinstance(material, dict):
                        continue
                    locked_path = _safe_relative(material.get("path"))
                    if locked_path and locked_path.startswith("licenses/"):
                        expected_installer_paths.add(
                            locked_path.removeprefix("licenses/")
                        )
            if canonical in installer_components and (
                relative_path not in expected_installer_paths
            ):
                errors.append(
                    f"License path differs from installer component lock for "
                    f"{package_name}: {value}"
                )
                continue
            if (
                canonical not in installer_components
                and not relative_path.startswith("python-packages/")
            ):
                errors.append(
                    f"License path is outside python-packages for {package_name}: "
                    f"{value}"
                )
                continue
            collision_key = relative_path.casefold()
            previous = seen_paths.get(collision_key)
            if previous is not None:
                errors.append(
                    f"Duplicate or multiply referenced license path: "
                    f"{previous} / {relative_path}"
                )
            seen_paths[collision_key] = relative_path
            if collision_key in package_paths:
                continue
            package_paths.add(collision_key)
            target = manifest_path.parent / Path(*PurePosixPath(relative_path).parts)
            if Path(relative_path).suffix.casefold() in NATIVE_SUFFIXES:
                errors.append(
                    f"Native file cannot be license material for {package_name}: "
                    f"{value}"
                )
                continue
            if not _within(manifest_path.parent, target) or not is_safe_regular_file(
                target
            ):
                errors.append(
                    f"Collected license file is missing for {package_name}: {value}"
                )
                continue
            if target.stat().st_size == 0:
                errors.append(
                    f"Collected license file is empty for {package_name}: {value}"
                )
                continue
            if not is_meaningful_license_file(target):
                errors.append(
                    f"Collected license file is placeholder or not meaningful for "
                    f"{package_name}: {value}"
                )
                continue
            if is_substantive_license_file(Path(relative_path)):
                substantive_paths.append(relative_path)
        declared_hashes = package.get("license_file_sha256")
        if not isinstance(declared_hashes, dict):
            errors.append(f"License hash map is missing for package: {package_name}")
        else:
            if set(declared_hashes) != {
                value
                for value in license_files
                if isinstance(value, str) and _safe_relative(value) is not None
            }:
                errors.append(
                    f"License hash path set differs for package: {package_name}"
                )
            for relative_path, expected_hash in declared_hashes.items():
                safe_path = _safe_relative(relative_path)
                if safe_path is None:
                    continue
                target = manifest_path.parent / Path(
                    *PurePosixPath(safe_path).parts
                )
                if (
                    not isinstance(expected_hash, str)
                    or SHA256_PATTERN.fullmatch(expected_hash) is None
                    or not is_safe_regular_file(target)
                    or sha256_file(target) != expected_hash
                ):
                    errors.append(
                        f"License SHA256 differs for {package_name}: {safe_path}"
                    )
        declared_substantive = package.get("substantive_license_files")
        if not isinstance(declared_substantive, list):
            errors.append(
                f"Substantive license list is missing for package: {package_name}"
            )
        elif declared_substantive != substantive_paths:
            errors.append(
                f"Substantive license list differs from collected files for "
                f"{package_name}."
            )
        if not substantive_paths:
            errors.append(
                f"NOTICE, COPYRIGHT, or AUTHORS-only material is not a license for "
                f"package: {package_name}"
            )
        if expected_component is not None:
            expected_archive = expected_component.get("binary_archive")
            if isinstance(expected_archive, dict):
                expected_binary = {
                    key: expected_archive.get(key)
                    for key in ("filename", "sha256", "size")
                }
                if package.get("binary_archive") != expected_binary:
                    errors.append(
                        f"Binary archive provenance differs for {package_name}."
                    )
                if not isinstance(package.get("binary_install_verified"), bool):
                    errors.append(
                        f"Binary install verification flag is missing for "
                        f"{package_name}."
                    )
            elif "binary_archive" in package or "binary_install_verified" in package:
                errors.append(
                    f"Unexpected binary archive provenance for {package_name}."
                )

    required = (
        set(expected)
        if expected
        else {canonicalize_distribution_name(name) for name in REQUIRED_PACKAGE_LICENSES}
    )
    for canonical in sorted(required - set(by_name)):
        errors.append(f"Required package is missing from license manifest: {canonical}")

    if lock is not None:
        lock_path = manifest_path.parent / "components.json"
        if not _regular_nonempty_file(lock_path):
            errors.append("Packaged component lock is not a non-empty regular file.")
        else:
            actual_lock_hash = sha256_file(lock_path)
            if payload.get("component_lock_sha256") != actual_lock_hash:
                errors.append("License manifest component lock SHA256 does not match.")
        try:
            expected_requirements_hash = sha256_file(RUNTIME_REQUIREMENTS)
        except OSError as exc:
            errors.append(f"Cannot hash runtime requirements: {exc}")
        else:
            if payload.get("requirements_sha256") != expected_requirements_hash:
                errors.append("License manifest requirements SHA256 does not match.")
        if payload.get("release_python_version") != lock["python"].get(
            "release_version"
        ):
            errors.append("License manifest release Python version does not match.")
    if not isinstance(payload.get("build_python_version"), str):
        errors.append("License manifest build Python version is missing.")
    if lock is not None:
        try:
            observed_native_runtime = probe_python_native_runtime(lock)
        except RuntimeError as exc:
            errors.append(str(exc))
        else:
            if payload.get("python_native_runtime") != observed_native_runtime:
                errors.append(
                    "Python native runtime provenance differs from the current "
                    "verified runtime."
                )
    return errors


def parse_collect_toc(toc_path: Path) -> list[dict[str, str]]:
    try:
        payload = ast.literal_eval(toc_path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, ValueError) as exc:
        raise ValueError(f"Cannot parse PyInstaller COLLECT TOC: {exc}") from exc
    if (
        not isinstance(payload, tuple)
        or len(payload) != 1
        or not isinstance(payload[0], list)
    ):
        raise ValueError("Unexpected PyInstaller COLLECT TOC structure.")

    entries: list[dict[str, str]] = []
    seen: dict[str, str] = {}
    for raw_entry in payload[0]:
        if (
            not isinstance(raw_entry, tuple)
            or len(raw_entry) != 3
            or not all(isinstance(value, str) for value in raw_entry)
        ):
            raise ValueError("Invalid entry in PyInstaller COLLECT TOC.")
        toc_name, source, entry_type = raw_entry
        safe_name = _safe_relative(toc_name)
        if safe_name is None:
            raise ValueError(f"Unsafe path in PyInstaller COLLECT TOC: {toc_name}")
        final_path = (
            safe_name
            if entry_type == "EXECUTABLE"
            else f"_internal/{safe_name}"
        )
        collision_key = final_path.casefold()
        previous = seen.get(collision_key)
        if previous is not None and previous != final_path:
            raise ValueError(
                f"Case-insensitive TOC path collision: {previous} / {final_path}"
            )
        if previous is not None:
            raise ValueError(f"Duplicate path in PyInstaller COLLECT TOC: {final_path}")
        seen[collision_key] = final_path
        entries.append(
            {
                "toc_name": safe_name,
                "path": final_path,
                "source": source,
                "type": entry_type,
            }
        )
    return entries


PYINSTALLER_TOC_FILES = (
    "Analysis-00.toc",
    "PYZ-00.toc",
    "PKG-00.toc",
    "EXE-00.toc",
    "COLLECT-00.toc",
)


def _read_literal_toc(path: Path, label: str) -> Any:
    if not _regular_nonempty_file(path):
        raise ValueError(f"PyInstaller {label} TOC is missing or unsafe: {path}")
    try:
        return ast.literal_eval(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, ValueError) as exc:
        raise ValueError(f"Cannot parse PyInstaller {label} TOC: {exc}") from exc


def _typed_toc_entries(
    value: object,
    *,
    label: str,
    allowed_types: set[str],
    allow_namespace: bool = False,
) -> list[tuple[str, str, str]]:
    if not isinstance(value, list):
        raise ValueError(f"PyInstaller {label} TOC entries must be a list.")
    result: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for raw in value:
        if (
            not isinstance(raw, tuple)
            or len(raw) != 3
            or not all(isinstance(item, str) for item in raw)
        ):
            raise ValueError(f"PyInstaller {label} TOC contains an invalid entry.")
        name, source, entry_type = raw
        if not name or entry_type not in allowed_types:
            raise ValueError(
                f"PyInstaller {label} TOC entry has an invalid name or type: {raw!r}"
            )
        collision_key = name.casefold()
        if collision_key in seen:
            raise ValueError(
                f"PyInstaller {label} TOC contains a duplicate name: {name}"
            )
        seen.add(collision_key)
        if source == "-":
            if not allow_namespace or entry_type != "PYMODULE":
                raise ValueError(
                    f"PyInstaller {label} TOC has an unexpected namespace: {name}"
                )
        elif not source and not (
            entry_type == "OPTION" and name == "pyi-contents-directory _internal"
        ):
            raise ValueError(f"PyInstaller {label} TOC source is empty: {name}")
        result.append(raw)
    return result


def _parse_pyinstaller_tocs(collect_path: Path) -> dict[str, Any]:
    build_dir = collect_path.resolve().parent
    if collect_path.name != "COLLECT-00.toc":
        raise ValueError("The exact PyInstaller COLLECT-00.toc path is required.")
    paths = {name: build_dir / name for name in PYINSTALLER_TOC_FILES}
    payloads = {
        name: _read_literal_toc(path, name.removesuffix("-00.toc"))
        for name, path in paths.items()
    }

    analysis = payloads["Analysis-00.toc"]
    analysis_types = (
        list,
        list,
        list,
        list,
        dict,
        list,
        list,
        bool,
        dict,
        int,
        list,
        list,
        str,
        list,
        list,
        list,
        list,
        list,
        list,
        list,
    )
    if (
        not isinstance(analysis, tuple)
        or len(analysis) != len(analysis_types)
        or any(
            not isinstance(value, expected)
            for value, expected in zip(analysis, analysis_types, strict=True)
        )
    ):
        raise ValueError("Unexpected PyInstaller Analysis TOC structure.")
    if analysis[12] != sys.version:
        raise ValueError("PyInstaller Analysis Python version differs from the verifier.")
    repository_root = Path(__file__).resolve().parents[1]
    if (
        analysis[0] != [str(repository_root / "main.py")]
        or analysis[1] != [str(repository_root)]
        or analysis[2] != ["mpv"]
        or analysis[10] != []
    ):
        raise ValueError("PyInstaller Analysis application inputs differ from the spec.")
    if analysis[4:10] != ({}, [], [], False, {}, 0):
        raise ValueError("PyInstaller Analysis build options differ from the spec.")
    scripts = _typed_toc_entries(
        analysis[13], label="Analysis scripts", allowed_types={"PYSOURCE"}
    )
    expected_script_names = [
        "pyi_rth_inspect",
        "pyi_rth_pkgutil",
        "pyi_rth_multiprocessing",
        "pyi_rth_setuptools",
        "pyi_rth_pkgres",
        "pyi_rth_pyqt6",
        "main",
    ]
    if (
        [entry[0] for entry in scripts] != expected_script_names
        or scripts[-1] != ("main", str(repository_root / "main.py"), "PYSOURCE")
        or any(
            Path(source).name != f"{name}.py"
            for name, source, _entry_type in scripts[:-1]
        )
    ):
        raise ValueError("PyInstaller Analysis runtime/application scripts differ.")
    pure = _typed_toc_entries(
        analysis[14], label="Analysis pure", allowed_types={"PYMODULE"}, allow_namespace=True
    )
    binaries = _typed_toc_entries(
        analysis[15],
        label="Analysis binaries",
        allowed_types={"BINARY", "EXTENSION"},
    )
    unexpected_windows_runtime = [
        name
        for name, _source, _entry_type in binaries
        if is_windows_os_runtime_name(name) or is_root_vcomp_name(name)
    ]
    if unexpected_windows_runtime:
        raise ValueError(
            "PyInstaller Analysis contains excluded host Windows runtime binaries: "
            + ", ".join(sorted(unexpected_windows_runtime, key=str.casefold))
        )
    runtime_policy_audit_path = build_dir / RUNTIME_POLICY_AUDIT_FILENAME
    runtime_policy_summary = validate_windows_runtime_policy_audit(
        runtime_policy_audit_path,
        binaries,
    )
    datas = _typed_toc_entries(
        analysis[18], label="Analysis datas", allowed_types={"DATA"}
    )
    outside = _typed_toc_entries(
        analysis[19],
        label="Analysis outside-PYZ",
        allowed_types={"PYMODULE"},
        allow_namespace=True,
    )
    _typed_toc_entries(
        analysis[10],
        label="Analysis input binaries",
        allowed_types={"BINARY", "EXTENSION"},
    )
    _typed_toc_entries(
        analysis[11], label="Analysis input datas", allowed_types={"DATA"}
    )

    pyz = payloads["PYZ-00.toc"]
    if (
        not isinstance(pyz, tuple)
        or len(pyz) != 2
        or not isinstance(pyz[0], str)
    ):
        raise ValueError("Unexpected PyInstaller PYZ TOC structure.")
    pyz_entries = _typed_toc_entries(
        pyz[1], label="PYZ", allowed_types={"PYMODULE"}, allow_namespace=True
    )
    if Path(pyz[0]).resolve() != (build_dir / "PYZ-00.pyz").resolve():
        raise ValueError("PyInstaller PYZ TOC archive path differs.")

    pkg = payloads["PKG-00.toc"]
    pkg_types = (str, dict, list, str, bool, bool, bool, list, type(None), type(None), type(None))
    if (
        not isinstance(pkg, tuple)
        or len(pkg) != len(pkg_types)
        or any(
            not isinstance(value, expected)
            for value, expected in zip(pkg, pkg_types, strict=True)
        )
    ):
        raise ValueError("Unexpected PyInstaller PKG TOC structure.")
    if Path(pkg[0]).resolve() != (build_dir / "LoLReplayTool.pkg").resolve():
        raise ValueError("PyInstaller PKG TOC archive path differs.")
    pkg_entries = _typed_toc_entries(
        pkg[2],
        label="PKG",
        allowed_types={"OPTION", "PYZ", "PYMODULE", "PYSOURCE"},
    )
    expected_compression = {
        "BINARY": True,
        "DATA": True,
        "EXECUTABLE": True,
        "EXTENSION": True,
        "PYMODULE": True,
        "PYSOURCE": True,
        "PYZ": False,
        "SPLASH": True,
        "SYMLINK": False,
    }
    if (
        pkg[1] != expected_compression
        or pkg[4:] != (True, False, False, [], None, None, None)
    ):
        raise ValueError("PyInstaller PKG compression or build flags differ.")
    option_entries = [entry for entry in pkg_entries if entry[2] == "OPTION"]
    if option_entries != [("pyi-contents-directory _internal", "", "OPTION")]:
        raise ValueError("PyInstaller PKG contents-directory option differs.")

    expected_local_modules = [
        "struct",
        "pyimod01_archive",
        "pyimod02_importers",
        "pyimod03_ctypes",
        "pyimod04_pywin32",
    ]
    local_modules = [entry for entry in pkg_entries if entry[2] == "PYMODULE"]
    if local_modules != [
        (name, str(build_dir / "localpycs" / f"{name}.pyc"), "PYMODULE")
        for name in expected_local_modules
    ]:
        raise ValueError("PyInstaller PKG local bootstrap modules differ.")
    pyz_pkg_entries = [entry for entry in pkg_entries if entry[2] == "PYZ"]
    if pyz_pkg_entries != [("PYZ-00.pyz", pyz[0], "PYZ")]:
        raise ValueError("PyInstaller PKG/PYZ relationship differs.")
    source_entries = [entry for entry in pkg_entries if entry[2] == "PYSOURCE"]
    if (
        not source_entries
        or source_entries[0][0] != "pyiboot01_bootstrap"
        or Path(source_entries[0][1]).name != "pyiboot01_bootstrap.py"
        or source_entries[1:] != scripts
        or [entry[0] for entry in scripts].count("main") != 1
    ):
        raise ValueError("PyInstaller PKG bootstrap/application scripts differ from Analysis.")
    expected_pkg_entries = [
        option_entries[0],
        pyz_pkg_entries[0],
        *local_modules,
        *source_entries,
    ]
    if pkg_entries != expected_pkg_entries:
        raise ValueError("PyInstaller PKG entry order or composition differs.")

    exe = payloads["EXE-00.toc"]
    exe_types = (
        str,
        bool,
        bool,
        bool,
        list,
        type(None),
        bool,
        bool,
        bytes,
        bool,
        bool,
        type(None),
        type(None),
        type(None),
        str,
        list,
        list,
        bool,
        bool,
        int,
        list,
        str,
    )
    if (
        not isinstance(exe, tuple)
        or len(exe) != len(exe_types)
        or any(
            not isinstance(value, expected)
            for value, expected in zip(exe, exe_types, strict=True)
        )
    ):
        raise ValueError("Unexpected PyInstaller EXE TOC structure.")
    if (
        Path(exe[0]).resolve() != (build_dir / "LoLReplayTool.exe").resolve()
        or Path(exe[14]).resolve() != Path(pkg[0]).resolve()
        or exe[15] != pkg_entries
        or exe[3] is not True
    ):
        raise ValueError("PyInstaller EXE/PKG TOC relationship differs.")
    executable_entries = _typed_toc_entries(
        exe[20], label="EXE bootloader", allowed_types={"EXECUTABLE"}
    )
    if len(executable_entries) != 1 or executable_entries[0][0] != "runw.exe":
        raise ValueError("PyInstaller EXE TOC must use exactly the locked runw.exe.")
    expected_icon = str(repository_root / "assets" / "app" / "app.ico")
    if (
        exe[1:4] != (False, False, True)
        or exe[4] != [expected_icon]
        or exe[5:8] != (None, False, False)
        or not exe[8]
        or exe[9:14] != (True, False, None, None, None)
        or exe[16:19] != ([], False, False)
        or exe[19] < 0
        or pkg[3] != Path(exe[21]).name
    ):
        raise ValueError("PyInstaller EXE configuration differs from the locked build.")

    collect_entries = parse_collect_toc(paths["COLLECT-00.toc"])
    raw_collect = payloads["COLLECT-00.toc"][0]
    raw_non_executables = [entry for entry in raw_collect if entry[2] != "EXECUTABLE"]
    raw_executables = [entry for entry in raw_collect if entry[2] == "EXECUTABLE"]
    if set(raw_non_executables) != set([*binaries, *datas]):
        raise ValueError("PyInstaller Analysis binary/data set differs from COLLECT.")
    if raw_executables != [("LoLReplayTool.exe", exe[0], "EXECUTABLE")]:
        raise ValueError("PyInstaller EXE output differs from COLLECT.")
    if set(pyz_entries) != {entry for entry in pure if entry[0] != "struct"}:
        raise ValueError("PyInstaller Analysis pure set differs from PYZ.")
    struct_entries = [entry for entry in pure if entry[0] == "struct"]
    if len(struct_entries) != 1:
        raise ValueError("PyInstaller Analysis must contain one local struct module.")

    return {
        "build_dir": build_dir,
        "paths": paths,
        "analysis": analysis,
        "scripts": scripts,
        "pure": pure,
        "binaries": binaries,
        "datas": datas,
        "outside": outside,
        "pyz": pyz,
        "pyz_entries": pyz_entries,
        "pkg": pkg,
        "pkg_entries": pkg_entries,
        "exe": exe,
        "bootloader": executable_entries[0],
        "collect_entries": collect_entries,
        "runtime_policy_audit_path": runtime_policy_audit_path,
        "runtime_policy_summary": runtime_policy_summary,
    }


def _path_key(path: Path) -> str:
    try:
        return os.path.normcase(str(path.resolve()))
    except OSError:
        return os.path.normcase(str(path.absolute()))


def _distribution_source_owners(
    lock: dict[str, Any],
) -> tuple[dict[str, str], list[str]]:
    owners: dict[str, str] = {}
    errors: list[str] = []
    components = _components_by_distribution(lock)
    for component in components.values():
        distribution_name = str(component["distribution"])
        try:
            distribution = metadata.distribution(distribution_name)
        except metadata.PackageNotFoundError:
            errors.append(f"Locked distribution is not installed: {distribution_name}")
            continue
        if distribution.version != str(component["version"]):
            errors.append(
                f"Installed version differs from component lock for {distribution_name}: "
                f"{distribution.version} != {component['version']}"
            )
        component_name = str(component["component"])
        for file_entry in distribution.files or ():
            source = Path(distribution.locate_file(file_entry))
            if source.is_file():
                key = _path_key(source)
                previous = owners.get(key)
                if previous is not None and previous != component_name:
                    errors.append(
                        f"Installed file has multiple component owners: {source}"
                    )
                owners[key] = component_name
    return owners, errors


def _run_git(*arguments: str) -> str:
    repository_root = Path(__file__).resolve().parents[1]
    process = subprocess.run(
        ["git", *arguments],
        cwd=repository_root,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="strict",
    )
    if process.returncode != 0:
        raise ValueError(
            f"Git provenance command failed ({' '.join(arguments)}): "
            f"{process.stderr.strip()}"
        )
    return process.stdout


def _tracked_git_source_record(source: Path) -> dict[str, Any]:
    repository_root = Path(__file__).resolve().parents[1]
    try:
        relative = source.resolve().relative_to(repository_root.resolve()).as_posix()
    except (OSError, ValueError) as exc:
        raise ValueError(f"Source is outside the repository: {source}") from exc
    index_lines = [
        line
        for line in _run_git("ls-files", "--stage", "--", relative).splitlines()
        if line
    ]
    tree_lines = [
        line
        for line in _run_git("ls-tree", "HEAD", "--", relative).splitlines()
        if line
    ]
    if len(index_lines) != 1 or len(tree_lines) != 1:
        raise ValueError(f"Repository build source is not tracked at HEAD: {relative}")
    try:
        index_metadata, index_path = index_lines[0].split("\t", 1)
        index_mode, index_blob, stage = index_metadata.split(" ")
        tree_metadata, tree_path = tree_lines[0].split("\t", 1)
        tree_mode, object_type, tree_blob = tree_metadata.split(" ")
    except ValueError as exc:
        raise ValueError(f"Cannot parse Git blob provenance for: {relative}") from exc
    if (
        stage != "0"
        or object_type != "blob"
        or index_path.replace("\\", "/") != relative
        or tree_path.replace("\\", "/") != relative
        or index_mode != tree_mode
        or index_blob != tree_blob
    ):
        raise ValueError(f"Git index/HEAD blob differs for build source: {relative}")
    if not is_safe_regular_file(source):
        raise ValueError(f"Repository build source is missing or unsafe: {relative}")
    _run_git("diff-files", "--quiet", "--", relative)
    worktree_blob = _run_git(
        "hash-object",
        f"--path={relative}",
        relative,
    ).strip()
    if worktree_blob != tree_blob:
        raise ValueError(f"Working build source differs from HEAD blob: {relative}")
    return {
        "path": relative,
        "git_mode": tree_mode,
        "git_blob": tree_blob,
        "size": source.stat().st_size,
        "sha256": sha256_file(source),
    }


def _stdlib_source_records(
    package_manifest: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    runtime = package_manifest.get("python_native_runtime")
    stdlib = runtime.get("stdlib_python_sources") if isinstance(runtime, dict) else None
    artifacts = stdlib.get("artifacts") if isinstance(stdlib, dict) else None
    if not isinstance(artifacts, list) or not artifacts:
        raise ValueError("Verified Python stdlib source inventory is missing.")
    result: dict[str, dict[str, Any]] = {}
    for record in artifacts:
        if not isinstance(record, dict):
            raise ValueError("Verified Python stdlib source inventory is invalid.")
        relative = _safe_relative(record.get("path"))
        if relative is None or not relative.endswith(".py"):
            raise ValueError("Verified Python stdlib source path is invalid.")
        source = _stdlib_root() / Path(*PurePosixPath(relative).parts)
        if (
            not is_safe_regular_file(source)
            or source.stat().st_size != record.get("size")
            or sha256_file(source) != record.get("sha256")
        ):
            raise ValueError(f"Verified Python stdlib source differs: {relative}")
        key = _path_key(source)
        if key in result:
            raise ValueError(f"Duplicate verified Python stdlib source: {relative}")
        result[key] = record
    if len(result) != len(artifacts):
        raise ValueError("Verified Python stdlib source inventory is not bijective.")
    return result


def _stdlib_root() -> Path:
    return Path(sysconfig.get_path("stdlib"))


def _module_source_component(
    source: Path,
    source_owners: dict[str, str],
    stdlib_sources: dict[str, dict[str, Any]],
) -> tuple[str, dict[str, Any]]:
    key = _path_key(source)
    owner = source_owners.get(key)
    if owner is not None:
        if not is_safe_regular_file(source):
            raise ValueError(f"Locked wheel source is missing or unsafe: {source}")
        try:
            installed_relative = source.resolve().relative_to(
                Path(sys.prefix).resolve()
            ).as_posix()
        except (OSError, ValueError) as exc:
            raise ValueError(
                f"Locked wheel source escapes the build environment: {source}"
            ) from exc
        return owner, {
            "kind": "locked-wheel-record",
            "path": installed_relative,
            "size": source.stat().st_size,
            "sha256": sha256_file(source),
        }
    stdlib = stdlib_sources.get(key)
    if stdlib is not None:
        return "python", {
            "kind": "cpython-stdlib-lock",
            "path": stdlib["path"],
            "size": stdlib["size"],
            "sha256": stdlib["sha256"],
        }
    repository_root = Path(__file__).resolve().parents[1]
    if _is_relative_to(source, repository_root):
        relative = source.resolve().relative_to(repository_root.resolve()).as_posix()
        if relative in {"assets/app/app.ico", "assets/app/app.png"}:
            recipe = _tracked_git_source_record(repository_root / "scripts" / "make_icon.py")
            if not is_safe_regular_file(source):
                raise ValueError(f"Generated application asset is unsafe: {relative}")
            return "lol-replay-tool", {
                "kind": "declared-generated-asset",
                "path": relative,
                "size": source.stat().st_size,
                "sha256": sha256_file(source),
                "recipe": recipe,
            }
        return "lol-replay-tool", {
            "kind": "git-blob",
            **_tracked_git_source_record(source),
        }
    raise ValueError(f"Python module source has no verified owner: {source}")


def _code_tree_matches(
    actual: types.CodeType,
    expected: types.CodeType,
) -> bool:
    if actual != expected or actual.co_filename != expected.co_filename:
        return False
    actual_nested = [
        value for value in actual.co_consts if isinstance(value, types.CodeType)
    ]
    expected_nested = [
        value for value in expected.co_consts if isinstance(value, types.CodeType)
    ]
    return len(actual_nested) == len(expected_nested) and all(
        _code_tree_matches(actual_code, expected_code)
        for actual_code, expected_code in zip(
            actual_nested,
            expected_nested,
            strict=True,
        )
    )


def _code_matches_source(
    code: types.CodeType,
    source: Path,
    *,
    expected_filename: str,
) -> bool:
    if not is_safe_regular_file(source):
        return False
    source_bytes = source.read_bytes()
    expected = compile(
        source_bytes,
        expected_filename,
        "exec",
        dont_inherit=True,
        optimize=0,
    )
    return _code_tree_matches(code, expected)


def _verified_marshaled_code(
    raw_payload: bytes,
    source: Path,
    *,
    label: str,
    expected_filename: str,
    expected_code: types.CodeType | None = None,
) -> types.CodeType:
    marshaled = io.BytesIO(raw_payload)
    try:
        code = marshal.load(marshaled)
    except (EOFError, TypeError, ValueError) as exc:
        raise ValueError(f"{label} code payload is invalid: {exc}") from exc
    if not isinstance(code, types.CodeType) or marshaled.read(1):
        raise ValueError(f"{label} code payload has a non-code value or trailing bytes.")
    if expected_code is not None and code != expected_code:
        raise ValueError(f"{label} raw and decoded code objects differ.")
    if not _code_matches_source(
        code,
        source,
        expected_filename=expected_filename,
    ):
        raise ValueError(f"{label} bytecode differs from verified source.")
    return code


def _verify_carchive_chain(
    carchive: Any,
    pkg_path: Path,
    pyz_path: Path,
    expected_pkg_entries: list[tuple[str, str, str]],
) -> bytes:
    if carchive.raw_pkg_data() != pkg_path.read_bytes():
        raise ValueError("Final EXE CArchive differs from LoLReplayTool.pkg.")
    embedded_pyz = carchive.extract("PYZ.pyz")
    if not isinstance(embedded_pyz, bytes) or embedded_pyz != pyz_path.read_bytes():
        raise ValueError("Final EXE embedded PYZ differs from PYZ-00.pyz.")
    archived_entries = [
        entry for entry in expected_pkg_entries if entry[2] != "OPTION"
    ]
    expected_names = {
        "PYZ.pyz" if entry[0] == "PYZ-00.pyz" and entry[2] == "PYZ" else entry[0]
        for entry in archived_entries
    }
    if set(carchive.toc) != expected_names:
        raise ValueError("Final EXE CArchive member set differs from PKG/EXE TOC.")
    type_codes = {"PYMODULE": "m", "PYSOURCE": "s", "PYZ": "z"}
    for name, _source, entry_type in archived_entries:
        archive_name = (
            "PYZ.pyz" if name == "PYZ-00.pyz" and entry_type == "PYZ" else name
        )
        raw_record = carchive.toc.get(archive_name)
        expected_compression = 0 if entry_type == "PYZ" else 1
        if (
            not isinstance(raw_record, tuple)
            or len(raw_record) != 5
            or raw_record[3] != expected_compression
            or raw_record[4] != type_codes[entry_type]
        ):
            raise ValueError(
                f"CArchive member type differs or compression changed from PKG TOC: {name}"
            )
    return embedded_pyz


def _verify_carchive_layout(
    carchive: Any,
    final_exe: Path,
    *,
    python_library: str,
    options: list[str],
) -> dict[str, Any]:
    import zlib

    cookie_format = "!8sIIII64s"
    cookie_size = struct.calcsize(cookie_format)
    toc_entry_format = "!IIIIBc"
    toc_header_size = struct.calcsize(toc_entry_format)
    pkg_data = carchive.raw_pkg_data()
    if len(pkg_data) < cookie_size:
        raise ValueError("CArchive cookie is truncated.")
    (
        magic,
        archive_length,
        toc_offset,
        toc_length,
        python_version,
        raw_python_library,
    ) = struct.unpack(cookie_format, pkg_data[-cookie_size:])
    expected_python_version = sys.version_info.major * 100 + sys.version_info.minor
    library_bytes, separator, padding = raw_python_library.partition(b"\0")
    if (
        magic != b"MEI\014\013\012\013\016"
        or archive_length != len(pkg_data)
        or toc_offset <= 0
        or toc_length <= 0
        or toc_offset + toc_length != len(pkg_data) - cookie_size
        or python_version != expected_python_version
        or not separator
        or any(padding)
        or library_bytes.decode("utf-8", errors="strict") != python_library
    ):
        raise ValueError("CArchive cookie fields differ from the locked build.")
    if (
        carchive._start_offset != final_exe.stat().st_size - len(pkg_data)
        or carchive._end_offset != final_exe.stat().st_size
    ):
        raise ValueError("CArchive overlay boundaries differ from the final EXE.")

    independent_toc: dict[str, tuple[int, int, int, int, str]] = {}
    independent_options: list[str] = []
    option_offsets: list[int] = []
    cursor = toc_offset
    toc_end = toc_offset + toc_length
    payload_ranges: list[tuple[int, int, str]] = []
    while cursor < toc_end:
        if cursor + toc_header_size > toc_end:
            raise ValueError("CArchive TOC entry header is truncated.")
        (
            entry_length,
            entry_offset,
            data_length,
            uncompressed_length,
            compression_flag,
            raw_typecode,
        ) = struct.unpack(toc_entry_format, pkg_data[cursor : cursor + toc_header_size])
        if (
            entry_length < toc_header_size + 1
            or entry_length % 16
            or cursor + entry_length > toc_end
            or compression_flag not in {0, 1}
        ):
            raise ValueError("CArchive TOC entry layout is invalid.")
        raw_name = pkg_data[cursor + toc_header_size : cursor + entry_length]
        name_bytes, name_separator, name_padding = raw_name.partition(b"\0")
        if not name_separator or any(name_padding):
            raise ValueError("CArchive TOC member name padding is invalid.")
        name = name_bytes.decode("utf-8", errors="strict")
        typecode = raw_typecode.decode("ascii", errors="strict")
        if not name:
            raise ValueError("CArchive TOC contains an empty member name.")
        if typecode == "o":
            if (
                entry_offset > toc_offset
                or data_length != 0
                or uncompressed_length != 0
                or compression_flag != 0
            ):
                raise ValueError("CArchive option unexpectedly contains a payload.")
            independent_options.append(name)
            option_offsets.append(entry_offset)
        else:
            if (
                name in independent_toc
                or data_length <= 0
                or uncompressed_length <= 0
                or entry_offset < 0
                or entry_offset + data_length > toc_offset
            ):
                raise ValueError("CArchive TOC member range is invalid or duplicated.")
            raw_payload = pkg_data[entry_offset : entry_offset + data_length]
            try:
                if compression_flag:
                    decompressor = zlib.decompressobj()
                    unpacked = decompressor.decompress(raw_payload)
                    unpacked += decompressor.flush()
                    if (
                        not decompressor.eof
                        or decompressor.unused_data
                        or decompressor.unconsumed_tail
                    ):
                        raise ValueError(
                            f"CArchive compressed stream is not exact: {name}"
                        )
                else:
                    unpacked = raw_payload
            except zlib.error as exc:
                raise ValueError(f"CArchive member decompression failed: {name}") from exc
            if (
                len(unpacked) != uncompressed_length
                or carchive.extract(name) != unpacked
            ):
                raise ValueError(
                    f"CArchive member length or extracted bytes differ: {name}"
                )
            independent_toc[name] = (
                entry_offset,
                data_length,
                uncompressed_length,
                compression_flag,
                typecode,
            )
            payload_ranges.append(
                (entry_offset, entry_offset + data_length, name)
            )
        cursor += entry_length
    if cursor != toc_end:
        raise ValueError("CArchive TOC length is not exact.")
    ordered_ranges = sorted(payload_ranges)
    if (
        not ordered_ranges
        or ordered_ranges[0][0] != 0
        or ordered_ranges[-1][1] != toc_offset
        or any(
            previous[1] != following[0]
            for previous, following in zip(
                ordered_ranges,
                ordered_ranges[1:],
                strict=False,
            )
        )
    ):
        raise ValueError("CArchive member payload ranges are not contiguous.")
    if independent_toc != carchive.toc or independent_options != options:
        raise ValueError("CArchive independently parsed TOC/options differ.")
    payload_boundaries = {
        boundary
        for start, end, _name in ordered_ranges
        for boundary in (start, end)
    }
    if any(offset not in payload_boundaries for offset in option_offsets):
        raise ValueError("CArchive option offset is not a payload boundary.")
    return {
        "archive_start": carchive._start_offset,
        "archive_end": carchive._end_offset,
        "archive_size": archive_length,
        "toc_offset": toc_offset,
        "toc_length": toc_length,
        "python_version": python_version,
        "python_library": python_library,
        "options": independent_options,
    }


def _verify_pyz_member_set(
    pyz_reader: Any,
    pyz_entries: list[tuple[str, str, str]],
) -> None:
    expected_names = {entry[0] for entry in pyz_entries}
    if set(pyz_reader.toc) != expected_names:
        raise ValueError("Embedded PYZ module set differs from PYZ TOC.")


def _verify_pyz_layout(
    raw_pyz: bytes,
    pyz_reader: Any,
    pyz_entries: list[tuple[str, str, str]],
) -> dict[str, Any]:
    import zlib

    header_length = 17
    if (
        len(raw_pyz) <= header_length
        or raw_pyz[:4] != b"PYZ\0"
        or raw_pyz[4:8] != importlib.util.MAGIC_NUMBER
        or raw_pyz[12:header_length] != b"\0" * 5
    ):
        raise ValueError("Embedded PYZ header or Python magic differs.")
    toc_offset = struct.unpack("!i", raw_pyz[8:12])[0]
    if toc_offset < header_length or toc_offset >= len(raw_pyz):
        raise ValueError("Embedded PYZ TOC offset is outside the archive.")
    stream = io.BytesIO(raw_pyz)
    stream.seek(toc_offset)
    try:
        raw_toc = marshal.load(stream)
    except (EOFError, TypeError, ValueError) as exc:
        raise ValueError(f"Embedded PYZ TOC is invalid: {exc}") from exc
    if stream.read(1):
        raise ValueError("Embedded PYZ has trailing bytes after its TOC.")
    if not isinstance(raw_toc, list):
        raise ValueError("Embedded PYZ TOC must be a list.")

    expected_by_name = {entry[0]: entry for entry in pyz_entries}
    expected_names = [entry[0] for entry in pyz_entries]
    actual_toc: dict[str, tuple[int, int, int]] = {}
    actual_names: list[str] = []
    cursor = header_length
    for raw_entry in raw_toc:
        if (
            not isinstance(raw_entry, tuple)
            or len(raw_entry) != 2
            or not isinstance(raw_entry[0], str)
            or not isinstance(raw_entry[1], tuple)
            or len(raw_entry[1]) != 3
            or not all(isinstance(value, int) for value in raw_entry[1])
        ):
            raise ValueError("Embedded PYZ TOC contains an invalid entry.")
        name, archive_record = raw_entry
        type_code, offset, compressed_size = archive_record
        if name in actual_toc or name not in expected_by_name:
            raise ValueError("Embedded PYZ TOC has a duplicate or unknown module.")
        _expected_name, source_text, _entry_type = expected_by_name[name]
        expected_type = (
            3
            if source_text == "-"
            else 1
            if Path(source_text).name == "__init__.py"
            else 0
        )
        if type_code != expected_type or offset != cursor or compressed_size < 0:
            raise ValueError(f"Embedded PYZ record layout or type differs: {name}")
        if expected_type == 3:
            if compressed_size != 0 or pyz_reader.extract(name) is not None:
                raise ValueError(f"Embedded PYZ namespace differs: {name}")
        else:
            end = offset + compressed_size
            if compressed_size == 0 or end > toc_offset:
                raise ValueError(f"Embedded PYZ payload range differs: {name}")
            decompressor = zlib.decompressobj()
            try:
                payload = decompressor.decompress(raw_pyz[offset:end])
                payload += decompressor.flush()
            except zlib.error as exc:
                raise ValueError(
                    f"Embedded PYZ payload decompression failed: {name}"
                ) from exc
            if (
                not decompressor.eof
                or decompressor.unused_data
                or decompressor.unconsumed_tail
                or pyz_reader.extract(name, raw=True) != payload
            ):
                raise ValueError(
                    f"Embedded PYZ compressed payload is not exact: {name}"
                )
            cursor = end
        actual_names.append(name)
        actual_toc[name] = archive_record
    if (
        actual_names != expected_names
        or cursor != toc_offset
        or actual_toc != pyz_reader.toc
    ):
        raise ValueError("Embedded PYZ TOC order, member set, or payload span differs.")
    return {
        "archive_size": len(raw_pyz),
        "toc_offset": toc_offset,
        "toc_size": len(raw_pyz) - toc_offset,
        "header_sha256": hashlib.sha256(raw_pyz[:header_length]).hexdigest(),
    }


def _flatten_pe_keys(structure: Any) -> list[str]:
    return [name for group in structure.__keys__ for name in group]


def _pe_import_inventory(pe: Any) -> list[tuple[str, list[tuple[str | None, int | None]]]]:
    return [
        (
            entry.dll.decode("ascii", errors="strict"),
            [
                (
                    imported.name.decode("ascii", errors="strict")
                    if imported.name is not None
                    else None,
                    imported.ordinal,
                )
                for imported in entry.imports
            ],
        )
        for entry in pe.DIRECTORY_ENTRY_IMPORT
    ]


def _pe_resource_payloads(
    pe: Any,
    resource_section: Any,
) -> dict[tuple[int, int, int], bytes]:
    root = getattr(pe, "DIRECTORY_ENTRY_RESOURCE", None)
    if root is None:
        raise ValueError("Final EXE has no resource directory.")
    raw_start = resource_section.PointerToRawData
    raw_end = raw_start + resource_section.SizeOfRawData
    payloads: dict[tuple[int, int, int], bytes] = {}
    ranges: list[tuple[int, int]] = []
    for type_entry in root.entries:
        if type_entry.name is not None or not hasattr(type_entry, "directory"):
            raise ValueError("Final EXE resource type must use a numeric directory.")
        for identifier_entry in type_entry.directory.entries:
            if (
                identifier_entry.name is not None
                or not hasattr(identifier_entry, "directory")
            ):
                raise ValueError("Final EXE resource identifier must be numeric.")
            for language_entry in identifier_entry.directory.entries:
                if language_entry.name is not None or not hasattr(language_entry, "data"):
                    raise ValueError("Final EXE resource language entry is invalid.")
                record = language_entry.data.struct
                offset = pe.get_offset_from_rva(record.OffsetToData)
                end = offset + record.Size
                key = (type_entry.id, identifier_entry.id, language_entry.id)
                if (
                    key in payloads
                    or record.Size <= 0
                    or offset < raw_start
                    or end > raw_end
                    or any(offset < old_end and old_start < end for old_start, old_end in ranges)
                ):
                    raise ValueError("Final EXE resource payload range is invalid.")
                payload = pe.__data__[offset:end]
                if len(payload) != record.Size:
                    raise ValueError("Final EXE resource payload is truncated.")
                payloads[key] = payload
                ranges.append((offset, end))
    return payloads


def _verify_pe_resource_layout(
    pe: Any,
    resource_section: Any,
    expected_payloads: dict[tuple[int, int, int], bytes],
) -> dict[str, Any]:
    raw_start = resource_section.PointerToRawData
    raw_size = resource_section.SizeOfRawData
    virtual_size = resource_section.Misc_VirtualSize
    raw = bytes(pe.__data__[raw_start : raw_start + raw_size])
    if (
        len(raw) != raw_size
        or virtual_size <= 0
        or virtual_size > raw_size
    ):
        raise ValueError("Final EXE resource section bytes are truncated or invalid.")

    claimed = bytearray(raw_size)
    payloads: dict[tuple[int, int, int], bytes] = {}
    directory_count = 0
    data_entry_count = 0

    def claim(offset: int, size: int, *, label: str) -> None:
        end = offset + size
        if (
            offset < 0
            or size <= 0
            or end > virtual_size
            or any(claimed[offset:end])
        ):
            raise ValueError(f"Final EXE resource {label} range is invalid or overlaps.")
        claimed[offset:end] = b"\1" * size

    def parse_directory(
        offset: int,
        level: int,
        identifiers: tuple[int, ...],
    ) -> None:
        nonlocal data_entry_count, directory_count
        directory_header_size = struct.calcsize("<IIHHHH")
        if offset % 4 or offset + directory_header_size > virtual_size:
            raise ValueError("Final EXE resource directory offset is invalid.")
        (
            characteristics,
            timestamp,
            major_version,
            minor_version,
            named_count,
            identifier_count,
        ) = struct.unpack_from("<IIHHHH", raw, offset)
        directory_size = directory_header_size + identifier_count * 8
        claim(offset, directory_size, label="directory")
        if (
            (characteristics, timestamp, major_version, minor_version)
            != (0, 0, 4, 0)
            or named_count != 0
            or identifier_count <= 0
        ):
            raise ValueError("Final EXE resource directory metadata differs.")
        directory_count += 1
        previous_identifier = -1
        for index in range(identifier_count):
            entry_offset = offset + directory_header_size + index * 8
            raw_identifier, raw_target = struct.unpack_from("<II", raw, entry_offset)
            if (
                raw_identifier & 0x80000000
                or raw_identifier > 0xFFFF
                or raw_identifier <= previous_identifier
            ):
                raise ValueError(
                    "Final EXE resource identifiers must be unique sorted numeric IDs."
                )
            previous_identifier = raw_identifier
            target_offset = raw_target & 0x7FFFFFFF
            if level < 2:
                if (raw_target & 0x80000000) == 0:
                    raise ValueError("Final EXE resource tree ends before language level.")
                parse_directory(
                    target_offset,
                    level + 1,
                    (*identifiers, raw_identifier),
                )
                continue
            if raw_target & 0x80000000:
                raise ValueError("Final EXE resource tree exceeds three levels.")
            if target_offset % 4 or target_offset + 16 > virtual_size:
                raise ValueError("Final EXE resource data-entry offset is invalid.")
            claim(target_offset, 16, label="data entry")
            data_rva, data_size, code_page, reserved = struct.unpack_from(
                "<IIII",
                raw,
                target_offset,
            )
            payload_offset = data_rva - resource_section.VirtualAddress
            if (
                data_size <= 0
                or payload_offset % 4
                or code_page != 1252
                or reserved != 0
            ):
                raise ValueError("Final EXE resource data-entry metadata differs.")
            claim(payload_offset, data_size, label="payload")
            key = (*identifiers, raw_identifier)
            if len(key) != 3 or key in payloads:
                raise ValueError("Final EXE resource key is duplicated or malformed.")
            payloads[key] = raw[payload_offset : payload_offset + data_size]
            data_entry_count += 1

    parse_directory(0, 0, ())
    if payloads != expected_payloads:
        raise ValueError("Final EXE raw resource payloads differ from build inputs.")
    interior_padding_bytes = 0
    cursor = 0
    while cursor < virtual_size:
        if claimed[cursor]:
            cursor += 1
            continue
        end = cursor + 1
        while end < virtual_size and not claimed[end]:
            end += 1
        padding = raw[cursor:end]
        if len(padding) > 3 or padding != b"PADDING"[: len(padding)]:
            raise ValueError(
                "Final EXE resource section contains unexpected internal padding."
            )
        interior_padding_bytes += len(padding)
        cursor = end
    raw_alignment_padding = raw[virtual_size:]
    raw_alignment_pattern = b"PADDINGXXPADDING"
    expected_raw_alignment_padding = (
        raw_alignment_pattern
        * (
            (len(raw_alignment_padding) + len(raw_alignment_pattern) - 1)
            // len(raw_alignment_pattern)
        )
    )[: len(raw_alignment_padding)]
    if raw_alignment_padding != expected_raw_alignment_padding:
        raise ValueError(
            "Final EXE resource section raw-alignment padding differs."
        )
    return {
        "directory_count": directory_count,
        "data_entry_count": data_entry_count,
        "virtual_size": virtual_size,
        "raw_size": raw_size,
        "raw_sha256": hashlib.sha256(raw).hexdigest(),
        "verified_padding_bytes": raw_size - sum(claimed),
        "interior_padding_bytes": interior_padding_bytes,
        "raw_alignment_padding_bytes": len(raw_alignment_padding),
    }


def _expected_icon_resources(icon_path: Path) -> dict[tuple[int, int, int], bytes]:
    icon = icon_path.read_bytes()
    if len(icon) < 6:
        raise ValueError("Application ICO header is truncated.")
    reserved, icon_type, count = struct.unpack("<HHH", icon[:6])
    table_end = 6 + 16 * count
    if reserved != 0 or icon_type != 1 or count <= 0 or table_end > len(icon):
        raise ValueError("Application ICO header is invalid.")
    resources: dict[tuple[int, int, int], bytes] = {}
    group_entries: list[bytes] = []
    cursor = table_end
    for index in range(count):
        raw_entry = icon[6 + index * 16 : 22 + index * 16]
        (
            width,
            height,
            color_count,
            entry_reserved,
            planes,
            bit_count,
            image_size,
            image_offset,
        ) = struct.unpack("<BBBBHHII", raw_entry)
        image_end = image_offset + image_size
        if (
            entry_reserved != 0
            or image_size <= 0
            or image_offset != cursor
            or image_end > len(icon)
        ):
            raise ValueError("Application ICO image range is invalid.")
        resource_id = index + 1
        resources[(3, resource_id, 0)] = icon[image_offset:image_end]
        group_entries.append(
            struct.pack(
                "<BBBBHHIH",
                width,
                height,
                color_count,
                entry_reserved,
                planes,
                bit_count,
                image_size,
                resource_id,
            )
        )
        cursor = image_end
    if cursor != len(icon):
        raise ValueError("Application ICO contains trailing or unreferenced bytes.")
    resources[(14, 1, 0)] = struct.pack("<HHH", 0, 1, count) + b"".join(
        group_entries
    )
    return resources


def _verify_bootloader_pe(
    locked_bootloader: Path,
    final_exe: Path,
    *,
    manifest: bytes,
    icon_path: Path,
    carchive_start: int,
) -> dict[str, Any]:
    try:
        import pefile
    except ImportError as exc:
        raise ValueError("pefile is required to verify the final Windows EXE.") from exc

    try:
        source_pe = pefile.PE(str(locked_bootloader), fast_load=False)
        final_pe = pefile.PE(str(final_exe), fast_load=False)
    except pefile.PEFormatError as exc:
        raise ValueError(f"Invalid PyInstaller PE image: {exc}") from exc
    try:
        source_bytes = locked_bootloader.read_bytes()
        final_bytes = final_exe.read_bytes()
        source_lfanew = source_pe.DOS_HEADER.e_lfanew
        final_lfanew = final_pe.DOS_HEADER.e_lfanew
        if (
            source_lfanew != final_lfanew
            or source_bytes[: source_lfanew + 4] != final_bytes[: final_lfanew + 4]
            or source_pe.get_overlay_data_start_offset() is not None
            or final_pe.get_overlay_data_start_offset() != carchive_start
            or final_pe.generate_checksum() != final_pe.OPTIONAL_HEADER.CheckSum
        ):
            raise ValueError("Final EXE DOS stub, overlay, or checksum differs from runw.exe.")

        file_header_exceptions = {"NumberOfSections", "TimeDateStamp"}
        for field in _flatten_pe_keys(source_pe.FILE_HEADER):
            if field not in file_header_exceptions and getattr(
                source_pe.FILE_HEADER, field
            ) != getattr(final_pe.FILE_HEADER, field):
                raise ValueError(f"Final EXE file header differs from runw.exe: {field}")
        if final_pe.FILE_HEADER.NumberOfSections != source_pe.FILE_HEADER.NumberOfSections + 1:
            raise ValueError("Final EXE must add exactly one PE section.")

        optional_exceptions = {"SizeOfInitializedData", "SizeOfImage", "CheckSum"}
        for field in _flatten_pe_keys(source_pe.OPTIONAL_HEADER):
            if field not in optional_exceptions and getattr(
                source_pe.OPTIONAL_HEADER, field
            ) != getattr(final_pe.OPTIONAL_HEADER, field):
                raise ValueError(
                    f"Final EXE optional header differs from runw.exe: {field}"
                )

        source_sections = {
            section.Name.rstrip(b"\0").decode("ascii", errors="strict"): section
            for section in source_pe.sections
        }
        final_sections = {
            section.Name.rstrip(b"\0").decode("ascii", errors="strict"): section
            for section in final_pe.sections
        }
        source_names = list(source_sections)
        final_names = list(final_sections)
        if (
            source_names[-1:] != [".reloc"]
            or final_names != [*source_names[:-1], ".rsrc", ".reloc"]
        ):
            raise ValueError("Final EXE PE section set or order differs from runw.exe.")

        debug_directory_index = pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_DEBUG"]
        debug_directory = source_pe.OPTIONAL_HEADER.DATA_DIRECTORY[
            debug_directory_index
        ]
        common_header_fields = (
            "Misc_VirtualSize",
            "SizeOfRawData",
            "PointerToRelocations",
            "PointerToLinenumbers",
            "NumberOfRelocations",
            "NumberOfLinenumbers",
            "Characteristics",
        )
        section_hashes: dict[str, str] = {}
        for name in source_names:
            source_section = source_sections[name]
            final_section = final_sections[name]
            for field in common_header_fields:
                if getattr(source_section, field) != getattr(final_section, field):
                    raise ValueError(f"Final EXE section header differs for {name}: {field}")
            if name != ".reloc" and (
                source_section.VirtualAddress != final_section.VirtualAddress
                or source_section.PointerToRawData != final_section.PointerToRawData
            ):
                raise ValueError(f"Final EXE section placement differs for {name}.")
            source_data = bytearray(source_section.get_data())
            final_data = bytearray(final_section.get_data())
            if name == ".rdata":
                for pe, section, data in (
                    (source_pe, source_section, source_data),
                    (final_pe, final_section, final_data),
                ):
                    offset = (
                        pe.get_offset_from_rva(debug_directory.VirtualAddress)
                        - section.PointerToRawData
                    )
                    if offset < 0 or offset + 8 > len(data):
                        raise ValueError("PE debug directory is outside .rdata.")
                    data[offset + 4 : offset + 8] = b"\0" * 4
            if source_data != final_data:
                raise ValueError(f"Final EXE section bytes differ from runw.exe: {name}")
            section_hashes[name] = hashlib.sha256(final_data).hexdigest()

        file_alignment = final_pe.OPTIONAL_HEADER.FileAlignment
        section_alignment = final_pe.OPTIONAL_HEADER.SectionAlignment
        def align(value: int, alignment: int) -> int:
            return (value + alignment - 1) // alignment * alignment
        resource_section = final_sections[".rsrc"]
        source_reloc = source_sections[".reloc"]
        final_reloc = final_sections[".reloc"]
        if (
            resource_section.VirtualAddress != source_reloc.VirtualAddress
            or resource_section.PointerToRawData != source_reloc.PointerToRawData
            or resource_section.Characteristics != 0x40000040
            or resource_section.SizeOfRawData
            != align(resource_section.Misc_VirtualSize, file_alignment)
            or final_reloc.VirtualAddress
            != align(
                resource_section.VirtualAddress + resource_section.Misc_VirtualSize,
                section_alignment,
            )
            or final_reloc.PointerToRawData
            != resource_section.PointerToRawData + resource_section.SizeOfRawData
            or final_reloc.PointerToRawData + final_reloc.SizeOfRawData
            != carchive_start
            or final_pe.OPTIONAL_HEADER.SizeOfImage
            != align(
                final_reloc.VirtualAddress + final_reloc.Misc_VirtualSize,
                section_alignment,
            )
        ):
            raise ValueError("Final EXE resource/relocation section layout differs.")
        initialized_size = sum(
            section.SizeOfRawData
            for section in final_pe.sections
            if section.Characteristics & 0x40
        )
        if final_pe.OPTIONAL_HEADER.SizeOfInitializedData != initialized_size:
            raise ValueError("Final EXE initialized-data size differs from its sections.")

        resource_index = pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_RESOURCE"]
        reloc_index = pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_BASERELOC"]
        for index, (source_directory, final_directory) in enumerate(
            zip(
                source_pe.OPTIONAL_HEADER.DATA_DIRECTORY,
                final_pe.OPTIONAL_HEADER.DATA_DIRECTORY,
                strict=True,
            )
        ):
            if index == resource_index:
                if (
                    source_directory.VirtualAddress != 0
                    or source_directory.Size != 0
                    or final_directory.VirtualAddress != resource_section.VirtualAddress
                    or final_directory.Size != resource_section.Misc_VirtualSize
                ):
                    raise ValueError("Final EXE resource data directory differs.")
            elif index == reloc_index:
                if (
                    final_directory.VirtualAddress != final_reloc.VirtualAddress
                    or final_directory.Size != source_directory.Size
                ):
                    raise ValueError("Final EXE relocation data directory differs.")
            elif (
                source_directory.VirtualAddress != final_directory.VirtualAddress
                or source_directory.Size != final_directory.Size
            ):
                raise ValueError(f"Final EXE data directory differs: {index}")

        if _pe_import_inventory(source_pe) != _pe_import_inventory(final_pe):
            raise ValueError("Final EXE import table differs from runw.exe.")
        debug_timestamps = {
            entry.struct.TimeDateStamp for entry in final_pe.DIRECTORY_ENTRY_DEBUG
        }
        if debug_timestamps != {final_pe.FILE_HEADER.TimeDateStamp}:
            raise ValueError("Final EXE PE/debug timestamps differ.")

        actual_resources = _pe_resource_payloads(final_pe, resource_section)
        expected_resources = _expected_icon_resources(icon_path)
        expected_resources[(24, 1, 0)] = manifest
        if actual_resources != expected_resources:
            raise ValueError("Final EXE icon/manifest resources differ from build inputs.")
        resource_layout = _verify_pe_resource_layout(
            final_pe,
            resource_section,
            expected_resources,
        )
        return {
            "overlay_start": carchive_start,
            "entry_point": final_pe.OPTIONAL_HEADER.AddressOfEntryPoint,
            "image_base": final_pe.OPTIONAL_HEADER.ImageBase,
            "section_hashes": section_hashes,
            "resource_layout": resource_layout,
            "resource_sha256": hashlib.sha256(resource_section.get_data()).hexdigest(),
            "manifest_sha256": hashlib.sha256(manifest).hexdigest(),
            "icon_sha256": sha256_file(icon_path),
            "import_inventory_sha256": hashlib.sha256(
                repr(_pe_import_inventory(final_pe)).encode("utf-8")
            ).hexdigest(),
        }
    finally:
        source_pe.close()
        final_pe.close()


def _inventory_digest(records: list[dict[str, Any]]) -> str:
    serialized = json.dumps(
        sorted(records, key=lambda item: str(item.get("name", item.get("path", ""))).casefold()),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()


def _normalized_runtime_policy_summary(
    summary: dict[str, Any],
    source_owners: dict[str, str],
    python_core_sources: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    normalized = dict(summary)
    raw_records = normalized.pop("_raw_binaries", None)
    if not isinstance(raw_records, list):
        raise ValueError("PyInstaller runtime policy raw inventory is unavailable.")
    excluded_records = normalized.get("excluded_binaries")
    if not isinstance(excluded_records, list):
        raise ValueError("PyInstaller runtime policy exclusions are unavailable.")
    excluded_by_index = {
        record.get("raw_index"): record
        for record in excluded_records
        if isinstance(record, dict)
    }
    if len(excluded_by_index) != len(excluded_records):
        raise ValueError("PyInstaller runtime policy exclusion indexes are invalid.")

    repository_root = Path(__file__).resolve().parents[1]
    environment_root = Path(sys.prefix).resolve()
    public_records: list[dict[str, Any]] = []
    for expected_index, record in enumerate(raw_records):
        if not isinstance(record, dict) or record.get("raw_index") != expected_index:
            raise ValueError("PyInstaller runtime policy raw record is invalid.")
        source = Path(str(record.get("source", "")))
        excluded = excluded_by_index.get(expected_index)
        public_record = {
            "raw_index": expected_index,
            "destination": record.get("destination"),
            "type": record.get("type"),
            "size": record.get("size"),
            "sha256": record.get("sha256"),
        }
        if excluded is not None:
            public_record.update(
                {
                    "decision": "excluded",
                    "source": excluded.get("source"),
                    "source_component": excluded.get("source_boundary"),
                    "reason": excluded.get("reason"),
                }
            )
        else:
            source_key = _path_key(source)
            owner = source_owners.get(source_key)
            python_core = python_core_sources.get(source_key)
            final_path = f"_internal/{record['destination']}"
            component = _classify_toc_entry(
                {
                    "toc_name": str(record["destination"]),
                    "path": final_path,
                    "source": str(source),
                    "type": str(record["type"]),
                },
                source_owners,
                python_core_sources,
            )
            if component is None:
                raise ValueError(
                    "PyInstaller runtime policy retained source has no verified owner: "
                    f"{record['destination']}"
                )
            if owner is not None:
                try:
                    relative = source.resolve().relative_to(environment_root).as_posix()
                except (OSError, ValueError) as exc:
                    raise ValueError(
                        "Locked wheel runtime source escapes the build environment: "
                        f"{record['destination']}"
                    ) from exc
                safe_relative = _safe_relative(relative)
                if safe_relative is None:
                    raise ValueError(
                        "Locked wheel runtime source has an unsafe relative path: "
                        f"{record['destination']}"
                    )
                source_identifier = f"{owner}/{safe_relative}"
            elif python_core is not None:
                locked_path = _safe_relative(python_core.get("path"))
                if locked_path is None:
                    raise ValueError(
                        "Python runtime source lock has an unsafe path: "
                        f"{record['destination']}"
                    )
                source_identifier = f"python-runtime/{locked_path}"
            elif _is_relative_to(source, repository_root):
                relative = source.resolve().relative_to(
                    repository_root.resolve()
                ).as_posix()
                safe_relative = _safe_relative(relative)
                if safe_relative is None:
                    raise ValueError(
                        "Repository runtime source has an unsafe path: "
                        f"{record['destination']}"
                    )
                source_identifier = f"repository/{safe_relative}"
            else:
                raise ValueError(
                    "PyInstaller runtime policy retained source cannot be normalized: "
                    f"{record['destination']}"
                )
            public_record.update(
                {
                    "decision": "retained",
                    "source": source_identifier,
                    "source_component": component,
                }
            )
        if _safe_relative(public_record.get("source")) is None:
            raise ValueError(
                "PyInstaller runtime policy normalized source is unsafe: "
                f"{record['destination']}"
            )
        public_records.append(public_record)
    if len(public_records) != normalized.get("raw_binary_count"):
        raise ValueError("PyInstaller runtime policy normalized raw count differs.")
    normalized["raw_binaries"] = public_records
    normalized["normalized_raw_inventory_sha256"] = hashlib.sha256(
        json.dumps(
            public_records,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    return normalized


def _validate_pyinstaller_build(
    distribution_root: Path,
    toc_path: Path,
    lock: dict[str, Any],
    package_manifest: dict[str, Any],
    source_owners: dict[str, str],
) -> tuple[list[str], dict[str, Any] | None]:
    try:
        parsed = _parse_pyinstaller_tocs(toc_path)
        stdlib_sources = _stdlib_source_records(package_manifest)
        from PyInstaller.archive.readers import CArchiveReader, ZlibArchiveReader

        build_dir = parsed["build_dir"]
        numpy_distribution = metadata.distribution("numpy")
        hook_distribution = metadata.distribution("pyinstaller-hooks-contrib")
        expected_hook_paths = [
            (str(Path(numpy_distribution.locate_file("numpy/_pyinstaller"))), 0),
            (
                str(
                    Path(
                        hook_distribution.locate_file(
                            "_pyinstaller_hooks_contrib/stdhooks"
                        )
                    )
                ),
                -1000,
            ),
            (
                str(
                    Path(
                        hook_distribution.locate_file("_pyinstaller_hooks_contrib")
                    )
                ),
                -1000,
            ),
        ]
        if parsed["analysis"][3] != expected_hook_paths:
            raise ValueError("PyInstaller Analysis hook paths differ from locked wheels.")
        repository_root = Path(__file__).resolve().parents[1]
        expected_input_datas = {
            (
                "assets\\app\\app.ico",
                str(repository_root / "assets" / "app" / "app.ico"),
                "DATA",
            ),
            (
                "config\\champion_aliases.json",
                str(repository_root / "config" / "champion_aliases.json"),
                "DATA",
            ),
            (
                "config\\setting.sample.json",
                str(repository_root / "config" / "setting.sample.json"),
                "DATA",
            ),
        }
        if set(parsed["analysis"][11]) != expected_input_datas:
            raise ValueError("PyInstaller Analysis application data inputs differ.")
        for _name, source_text, _entry_type in parsed["analysis"][11]:
            _module_source_component(
                Path(source_text),
                source_owners,
                stdlib_sources,
            )
        paths = parsed["paths"]
        pkg_path = build_dir / "LoLReplayTool.pkg"
        pyz_path = build_dir / "PYZ-00.pyz"
        build_exe = build_dir / "LoLReplayTool.exe"
        final_exe = distribution_root / "LoLReplayTool.exe"
        for artifact, label in (
            (pkg_path, "PKG archive"),
            (pyz_path, "PYZ archive"),
            (build_exe, "build EXE"),
            (final_exe, "final EXE"),
        ):
            if not _regular_nonempty_file(artifact):
                raise ValueError(f"PyInstaller {label} is missing or unsafe: {artifact}")
        if (
            build_exe.stat().st_size != final_exe.stat().st_size
            or sha256_file(build_exe) != sha256_file(final_exe)
        ):
            raise ValueError("PyInstaller build EXE differs from final distribution EXE.")

        carchive = CArchiveReader(str(final_exe))
        expected_pkg_entries = [
            entry for entry in parsed["pkg_entries"] if entry[2] != "OPTION"
        ]
        embedded_pyz = _verify_carchive_chain(
            carchive,
            pkg_path,
            pyz_path,
            parsed["pkg_entries"],
        )

        pyinstaller_component = next(
            (
                component
                for component in lock.get("build_components", [])
                if component.get("component") == "pyinstaller"
            ),
            None,
        )
        binary_archive = (
            pyinstaller_component.get("binary_archive")
            if isinstance(pyinstaller_component, dict)
            else None
        )
        contents = binary_archive.get("contents") if isinstance(binary_archive, dict) else None
        if not isinstance(contents, list) or len(contents) != 1:
            raise ValueError("PyInstaller bootloader wheel-content lock is missing.")
        bootloader_lock = contents[0]
        bootloader_name, bootloader_source, _bootloader_type = parsed["bootloader"]
        expected_bootloader = Path(
            metadata.distribution("PyInstaller").locate_file(bootloader_lock["path"])
        ).resolve()
        actual_bootloader = Path(bootloader_source).resolve()
        if (
            bootloader_name != "runw.exe"
            or actual_bootloader != expected_bootloader
            or source_owners.get(_path_key(actual_bootloader)) != "pyinstaller"
            or not is_safe_regular_file(actual_bootloader)
            or actual_bootloader.stat().st_size != bootloader_lock.get("size")
            or sha256_file(actual_bootloader) != bootloader_lock.get("sha256")
        ):
            raise ValueError("PyInstaller runw.exe differs from the locked wheel member.")

        expected_carchive_order = [
            entry[0]
            for entry in parsed["pkg_entries"]
            if entry[2] in {"PYMODULE", "PYSOURCE"}
        ] + ["PYZ.pyz"]
        if list(carchive.toc) != expected_carchive_order:
            raise ValueError("Final EXE CArchive member order differs from the PKG build.")

        python_core_sources = _python_core_source_locks(
            lock,
            str(package_manifest["build_python_version"]),
        )
        python_library = Path(parsed["exe"][21])
        python_library_lock = python_core_sources.get(_path_key(python_library))
        if (
            python_library_lock is None
            or not is_safe_regular_file(python_library)
            or python_library.stat().st_size != python_library_lock.get("size")
            or sha256_file(python_library) != python_library_lock.get("sha256")
        ):
            raise ValueError("PyInstaller EXE Python library differs from CPython lock.")

        carchive_layout = _verify_carchive_layout(
            carchive,
            final_exe,
            python_library=python_library.name,
            options=["pyi-contents-directory _internal"],
        )
        try:
            from PyInstaller.utils.win32 import winmanifest

            expected_manifest = winmanifest.create_application_manifest(
                None,
                False,
                False,
            )
        except (ImportError, OSError, RuntimeError) as exc:
            raise ValueError(f"Cannot reconstruct the default EXE manifest: {exc}") from exc
        if parsed["exe"][8] != expected_manifest:
            raise ValueError("PyInstaller EXE manifest differs from the default build input.")
        pe_summary = _verify_bootloader_pe(
            actual_bootloader,
            final_exe,
            manifest=expected_manifest,
            icon_path=repository_root / "assets" / "app" / "app.ico",
            carchive_start=carchive._start_offset,
        )

        module_records: list[dict[str, Any]] = []
        analysis_struct = next(
            entry for entry in parsed["pure"] if entry[0] == "struct"
        )
        pyinstaller_distribution = metadata.distribution("PyInstaller")
        bootstrap_source = Path(
            pyinstaller_distribution.locate_file(
                "PyInstaller/loader/pyiboot01_bootstrap.py"
            )
        ).resolve()
        bootstrap_entry = next(
            entry
            for entry in expected_pkg_entries
            if entry[0] == "pyiboot01_bootstrap"
        )
        if Path(bootstrap_entry[1]).resolve() != bootstrap_source:
            raise ValueError("PyInstaller bootstrap script differs from the locked wheel.")
        for name, source_text, entry_type in expected_pkg_entries:
            if entry_type == "PYZ":
                continue
            raw_code = carchive.extract(name)
            if not isinstance(raw_code, bytes):
                raise ValueError(f"CArchive code payload is not bytes: {name}")
            source = Path(source_text)
            if "localpycs" in source.parts:
                if name == "struct":
                    source = Path(analysis_struct[1])
                elif name in {
                    "pyimod01_archive",
                    "pyimod02_importers",
                    "pyimod03_ctypes",
                    "pyimod04_pywin32",
                }:
                    source = Path(
                        pyinstaller_distribution.locate_file(
                            f"PyInstaller/loader/{name}.py"
                        )
                    )
                else:
                    raise ValueError(f"Unknown PyInstaller local bytecode source: {name}")
            component, provenance = _module_source_component(
                source,
                source_owners,
                stdlib_sources,
            )
            _verified_marshaled_code(
                raw_code,
                source,
                label=f"CArchive {name}",
                expected_filename=f"{name}.py",
            )
            module_records.append(
                {
                    "name": name,
                    "container": "carchive",
                    "component": component,
                    "source_path": provenance.get("path"),
                    "source_sha256": sha256_file(source),
                    "code_sha256": hashlib.sha256(raw_code).hexdigest(),
                    "source_kind": provenance["kind"],
                }
            )

        pyz_record = carchive.toc["PYZ.pyz"]
        pyz_reader = ZlibArchiveReader(
            str(final_exe),
            carchive._start_offset + pyz_record[0],
            check_pymagic=True,
        )
        _verify_pyz_member_set(pyz_reader, parsed["pyz_entries"])
        pyz_layout = _verify_pyz_layout(
            embedded_pyz,
            pyz_reader,
            parsed["pyz_entries"],
        )
        module_components: dict[str, str] = {}
        pending_namespaces: list[str] = []
        for name, source_text, _entry_type in parsed["pyz_entries"]:
            archive_record = pyz_reader.toc.get(name)
            if not isinstance(archive_record, tuple) or len(archive_record) != 3:
                raise ValueError(f"Embedded PYZ record is invalid: {name}")
            type_code, _offset, compressed_size = archive_record
            if source_text == "-":
                if type_code != 3 or compressed_size != 0 or pyz_reader.extract(name) is not None:
                    raise ValueError(f"Embedded PYZ namespace differs: {name}")
                pending_namespaces.append(name)
                continue
            expected_type = 1 if Path(source_text).name == "__init__.py" else 0
            if type_code != expected_type:
                raise ValueError(f"Embedded PYZ code type differs: {name}")
            code = pyz_reader.extract(name)
            if not isinstance(code, types.CodeType):
                raise ValueError(f"Embedded PYZ payload is not a code object: {name}")
            raw_marshaled = pyz_reader.extract(name, raw=True)
            if not isinstance(raw_marshaled, bytes):
                raise ValueError(f"Embedded PYZ raw payload is not bytes: {name}")
            source = Path(source_text)
            component, provenance = _module_source_component(
                source,
                source_owners,
                stdlib_sources,
            )
            archive_module_path = name.replace(".", "\\")
            archive_filename = (
                f"{archive_module_path}\\__init__.py"
                if type_code == 1
                else f"{archive_module_path}.py"
            )
            _verified_marshaled_code(
                raw_marshaled,
                source,
                label=f"Embedded PYZ {name}",
                expected_filename=archive_filename,
                expected_code=code,
            )
            module_components[name] = component
            module_records.append(
                {
                    "name": name,
                    "container": "pyz",
                    "component": component,
                    "source_path": provenance.get("path"),
                    "source_sha256": sha256_file(source),
                    "code_sha256": hashlib.sha256(raw_marshaled).hexdigest(),
                    "source_kind": provenance["kind"],
                }
            )
        for namespace in pending_namespaces:
            child_owners = {
                component
                for name, component in module_components.items()
                if name.startswith(f"{namespace}.")
            }
            if len(child_owners) != 1:
                raise ValueError(
                    f"Embedded PYZ namespace has no unique locked owner: {namespace}"
                )
            component = child_owners.pop()
            module_components[namespace] = component
            module_records.append(
                {
                    "name": namespace,
                    "container": "pyz-namespace",
                    "component": component,
                    "source_path": None,
                    "source_sha256": None,
                    "code_sha256": None,
                    "source_kind": "verified-namespace-children",
                }
            )

        outside_members: set[str] = set()
        outside_data_sources = {
            _path_key(Path(source)): name
            for name, source, _entry_type in parsed["datas"]
        }
        outside_namespaces = {
            "cv2.dnn": "opencv-python",
            "cv2.gapi.wip": "opencv-python",
            "cv2.gapi.wip.draw": "opencv-python",
        }
        for name, source_text, _entry_type in parsed["outside"]:
            if source_text == "-":
                if outside_namespaces.get(name) != "opencv-python":
                    raise ValueError(
                        f"Analysis outside-PYZ namespace is not explicitly owned: {name}"
                    )
                continue
            source = Path(source_text)
            component, _provenance = _module_source_component(
                source,
                source_owners,
                stdlib_sources,
            )
            if component == "python":
                source_record = stdlib_sources[_path_key(source)]
                outside_members.add(str(source_record["path"]) + "c")
            elif component != "opencv-python" or _path_key(source) not in outside_data_sources:
                raise ValueError(
                    f"Analysis outside-PYZ module is neither stdlib nor locked cv2 data: "
                    f"{name}"
                )
        with zipfile.ZipFile(build_dir / "base_library.zip") as base_archive:
            actual_outside = {
                info.filename.replace("\\", "/")
                for info in base_archive.infolist()
                if not info.is_dir()
            }
        if actual_outside != outside_members:
            raise ValueError(
                "PyInstaller Analysis outside-PYZ set differs from base_library.zip."
            )

        toc_files = {
            name: {
                "size": paths[name].stat().st_size,
                "sha256": sha256_file(paths[name]),
            }
            for name in PYINSTALLER_TOC_FILES
        }
        module_records = sorted(
            module_records,
            key=lambda item: (str(item["container"]), str(item["name"]).casefold()),
        )
        owners = Counter(str(record["component"]) for record in module_records)
        build_policy_sources = [
            _tracked_git_source_record(repository_root / "LoLReplayTool.spec"),
            _tracked_git_source_record(
                repository_root / "scripts" / "pyinstaller_runtime_policy.py"
            ),
        ]
        runtime_policy_summary = _normalized_runtime_policy_summary(
            parsed["runtime_policy_summary"],
            source_owners,
            python_core_sources,
        )
        summary = {
            "toc_files": toc_files,
            "build_executable": {
                "size": build_exe.stat().st_size,
                "sha256": sha256_file(build_exe),
            },
            "pkg_archive": {
                "size": pkg_path.stat().st_size,
                "sha256": sha256_file(pkg_path),
            },
            "pyz_archive": {
                "size": pyz_path.stat().st_size,
                "sha256": sha256_file(pyz_path),
            },
            "carchive_layout": carchive_layout,
            "pyz_layout": pyz_layout,
            "pe_bootloader": pe_summary,
            "collect_entry_count": len(parsed["collect_entries"]),
            "carchive_member_count": len(carchive.toc),
            "pyz_module_count": len(pyz_reader.toc),
            "base_library_module_count": len(outside_members),
            "embedded_module_inventory_sha256": _inventory_digest(module_records),
            "embedded_module_owners": dict(sorted(owners.items())),
            "embedded_modules": module_records,
            "bootloader": {
                "wheel_path": bootloader_lock["path"],
                "size": bootloader_lock["size"],
                "sha256": bootloader_lock["sha256"],
            },
            "python_library": {
                "filename": python_library.name,
                "size": python_library.stat().st_size,
                "sha256": sha256_file(python_library),
            },
            "build_policy_sources": build_policy_sources,
            "runtime_policy_audit": runtime_policy_summary,
            "provenance_binding": {
                "runtime_policy_audit_sha256": runtime_policy_summary[
                    "artifact"
                ]["sha256"],
                "runtime_policy_payload_sha256": runtime_policy_summary[
                    "artifact"
                ]["payload_sha256"],
                "raw_inventory_sha256": runtime_policy_summary[
                    "raw_inventory_sha256"
                ],
                "normalized_raw_inventory_sha256": runtime_policy_summary[
                    "normalized_raw_inventory_sha256"
                ],
                "analysis_toc_sha256": toc_files["Analysis-00.toc"][
                    "sha256"
                ],
                "collect_toc_sha256": toc_files["COLLECT-00.toc"]["sha256"],
                "build_provenance_sha256": package_manifest.get(
                    "build_provenance_sha256"
                ),
            },
        }
        return [], summary
    except (
        ImportError,
        KeyError,
        OSError,
        RuntimeError,
        StopIteration,
        TypeError,
        ValueError,
        zipfile.BadZipFile,
    ) as exc:
        return [f"Cannot verify complete PyInstaller build provenance: {exc}"], None


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    return True


def _python_core_source_locks(
    lock: dict[str, Any],
    python_version: str,
) -> dict[str, dict[str, Any]]:
    profiles = lock.get("python", {}).get("windows_native_runtime_profiles")
    if not isinstance(profiles, dict):
        return {}
    profile = profiles.get(python_version)
    inventory = (
        profile.get("core_native_inventory")
        if isinstance(profile, dict)
        else None
    )
    artifacts = inventory.get("artifacts") if isinstance(inventory, dict) else None
    if not isinstance(artifacts, list):
        return {}
    result: dict[str, dict[str, Any]] = {}
    base_prefix = Path(sys.base_prefix)
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            continue
        raw_path = artifact.get("path")
        safe_path = _safe_relative(raw_path)
        if safe_path is None:
            continue
        source = base_prefix.joinpath(*PurePosixPath(safe_path).parts)
        result[_path_key(source)] = artifact
    return result


def _microsoft_runtime_owner_matches(
    owner: str | None,
    final_path: str,
    source: Path,
    python_core_sources: dict[str, dict[str, Any]],
) -> bool:
    final_lower = final_path.casefold()
    if _path_key(source) in python_core_sources:
        return final_lower == f"_internal/{source.name.casefold()}"
    allowed_prefixes = {
        "qt": "_internal/pyqt6/qt6/bin/",
        "numpy": "_internal/numpy.libs/",
        "scikit-learn": "_internal/sklearn/.libs/",
    }
    prefix = allowed_prefixes.get(owner or "")
    if prefix is not None and final_lower.startswith(prefix):
        return True
    return owner == "scikit-learn" and final_lower == "_internal/vcomp140.dll"


def _classify_toc_entry(
    entry: dict[str, str],
    source_owners: dict[str, str],
    python_core_sources: dict[str, dict[str, Any]] | None = None,
) -> str | None:
    python_core_sources = python_core_sources or {}
    final_path = entry["path"]
    source = Path(entry["source"])
    final_lower = final_path.casefold()
    source_lower = source.name.casefold()
    owner = source_owners.get(_path_key(source))
    python_core = python_core_sources.get(_path_key(source))

    if re.fullmatch(
        r"(?:msvcp140(?:_[12])?|vcruntime140(?:_1)?|vcomp140)(?:-[0-9a-f]+)?\.dll",
        source_lower,
    ):
        if not _microsoft_runtime_owner_matches(
            owner,
            final_path,
            source,
            python_core_sources,
        ):
            return None
        return (
            "microsoft-vc-runtime-python"
            if python_core is not None
            else "microsoft-vc-runtime"
        )
    if source_lower == "opengl32sw.dll":
        return (
            "mesa-opengl32sw"
            if owner == "qt"
            and final_lower == "_internal/pyqt6/qt6/bin/opengl32sw.dll"
            else None
        )
    python_dependency_owners = {
        "_bz2.pyd": "python-bzip2",
        "_ctypes.pyd": "python-libffi",
        "libffi-8.dll": "python-libffi",
        "_decimal.pyd": "python-mpdecimal",
        "_sqlite3.pyd": "python-sqlite",
        "sqlite3.dll": "python-sqlite",
        "_lzma.pyd": "python-xz",
        "_zstd.pyd": "python-zstd",
        "zlib1.dll": "python-zlib",
        "_hashlib.pyd": "python-openssl",
        "_ssl.pyd": "python-openssl",
    }
    if (
        source_lower in python_dependency_owners
        and python_core is not None
        and final_lower == f"_internal/{source_lower}"
    ):
        return python_dependency_owners[source_lower]

    if re.fullmatch(
        r"_internal/cv2/opencv_videoio_ffmpeg[^/]*\.dll",
        final_lower,
    ):
        if source_owners.get(_path_key(source)) != "opencv-python":
            return None
        return "opencv-ffmpeg"

    if owner is not None:
        return owner

    if (
        source_lower in {"libcrypto-3.dll", "libssl-3.dll"}
        and python_core is not None
        and final_lower == f"_internal/{source_lower}"
    ):
        return "python-openssl"
    if (
        python_core is not None
        and final_lower == f"_internal/{source_lower}"
        and source.suffix.casefold() in {".dll", ".pyd"}
    ):
        return "python"

    repo_root = Path(__file__).resolve().parents[1]
    if _is_relative_to(source, repo_root):
        if (
            entry["toc_name"] == "base_library.zip"
            and entry["path"] == "_internal/base_library.zip"
            and entry["type"] == "DATA"
            and _is_relative_to(source, repo_root / "build")
        ):
            return "python"
        if entry["type"] == "EXECUTABLE" or _is_relative_to(
            source, repo_root / "assets"
        ) or _is_relative_to(source, repo_root / "config"):
            return "lol-replay-tool"
    return None


def _matches_pattern(path: str, pattern: str) -> bool:
    normalized_pattern = pattern.replace("\\", "/")
    candidates = {normalized_pattern}
    if "/**/" in normalized_pattern:
        candidates.add(normalized_pattern.replace("/**/", "/"))
    return any(fnmatch.fnmatchcase(path.casefold(), item.casefold()) for item in candidates)


def _artifact_patterns(lock: dict[str, Any]) -> dict[str, list[str]]:
    return {
        str(component["component"]): [
            str(pattern) for pattern in component.get("artifact_patterns", [])
        ]
        for component in _component_entries(lock)
    }


def _physical_files(distribution_root: Path) -> dict[str, Path]:
    try:
        root_metadata = distribution_root.lstat()
    except OSError as exc:
        raise ValueError(f"Cannot inspect distribution root: {exc}") from exc
    if _is_reparse_point(root_metadata) or not stat.S_ISDIR(root_metadata.st_mode):
        raise ValueError("Distribution root must be a real directory, not a link.")

    result: dict[str, Path] = {}
    pending = [distribution_root]
    while pending:
        directory = pending.pop()
        try:
            entries = list(os.scandir(directory))
        except OSError as exc:
            raise ValueError(f"Cannot scan distribution directory: {exc}") from exc
        for entry in entries:
            path = Path(entry.path)
            try:
                metadata = path.lstat()
            except OSError as exc:
                raise ValueError(f"Cannot inspect distribution path {path}: {exc}") from exc
            relative = path.relative_to(distribution_root).as_posix()
            if _safe_relative(relative) is None:
                raise ValueError(f"Unsafe path in final distribution: {relative}")
            if _is_reparse_point(metadata):
                raise ValueError(
                    f"Links and reparse points are forbidden in distribution: {relative}"
                )
            if stat.S_ISDIR(metadata.st_mode):
                pending.append(path)
                continue
            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError(
                    f"Non-regular object is forbidden in distribution: {relative}"
                )
            if relative == MANIFEST_RELATIVE_PATH:
                continue
            collision_key = relative.casefold()
            previous = result.get(collision_key)
            if previous is not None and previous != path:
                raise ValueError(
                    f"Case-insensitive distribution path collision: "
                    f"{previous.relative_to(distribution_root).as_posix()} / {relative}"
                )
            result[collision_key] = path
    return result


def _forbidden_user_runtime_errors(
    distribution_root: Path,
    physical: dict[str, Path],
) -> list[str]:
    errors: list[str] = []
    for path in physical.values():
        relative = path.relative_to(distribution_root).as_posix()
        if is_user_provided_runtime_path(relative):
            errors.append(
                "User-provided OBS/standalone FFmpeg must not be bundled: "
                f"{relative}"
            )
    return errors


def _package_license_paths(package_manifest: dict[str, Any] | None) -> set[str]:
    result: set[str] = set()
    if not package_manifest:
        return result
    packages = package_manifest.get("packages")
    if not isinstance(packages, list):
        return result
    for package in packages:
        if not isinstance(package, dict):
            continue
        values = package.get("license_files")
        if not isinstance(values, list):
            continue
        for value in values:
            relative = _safe_relative(value)
            if relative is not None:
                result.add(f"licenses/{relative}")
    return result


def _allowed_generated_files(
    package_manifest: dict[str, Any] | None,
) -> set[str]:
    result = {
        *GENERATED_ROOT_DOCUMENTS,
        "licenses/components.json",
        "licenses/python-packages.json",
        *_package_license_paths(package_manifest),
    }
    if package_manifest and isinstance(
        package_manifest.get("build_provenance_sha256"),
        str,
    ):
        result.add("licenses/build-provenance.json")
    return result


def _python_native_artifact_locks(
    lock: dict[str, Any],
    python_version: str,
) -> dict[tuple[str, str], dict[str, Any]]:
    profiles = lock.get("python", {}).get("windows_native_runtime_profiles")
    if not isinstance(profiles, dict):
        return {}
    profile = profiles.get(python_version)
    if not isinstance(profile, dict):
        return {}
    result: dict[tuple[str, str], dict[str, Any]] = {}
    for component in profile.get("components", []):
        if not isinstance(component, dict):
            continue
        component_name = str(component.get("component", ""))
        for artifact in component.get("artifacts", []):
            if not isinstance(artifact, dict):
                continue
            filename = str(artifact.get("filename", "")).casefold()
            result[(component_name, filename)] = artifact
    return result


def _validate_base_library_archive(
    archive_path: Path,
    package_manifest: dict[str, Any] | None,
) -> tuple[list[str], dict[str, Any] | None]:
    if package_manifest is None:
        return ["Python package manifest is required for base_library.zip."], None
    runtime = package_manifest.get("python_native_runtime")
    stdlib = (
        runtime.get("stdlib_python_sources")
        if isinstance(runtime, dict)
        else None
    )
    source_artifacts = stdlib.get("artifacts") if isinstance(stdlib, dict) else None
    if not isinstance(source_artifacts, list) or not source_artifacts:
        return ["Verified Python stdlib source inventory is missing."], None
    allowed_sources: dict[str, dict[str, Any]] = {}
    for item in source_artifacts:
        if not isinstance(item, dict):
            continue
        relative = _safe_relative(item.get("path"))
        size = item.get("size")
        digest = item.get("sha256")
        if (
            relative is None
            or not relative.endswith(".py")
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
            or not isinstance(digest, str)
            or SHA256_PATTERN.fullmatch(digest) is None
            or relative.casefold() in allowed_sources
        ):
            continue
        allowed_sources[relative.casefold()] = {
            "path": relative,
            "size": size,
            "sha256": digest,
        }
    if len(allowed_sources) != len(source_artifacts):
        return ["Verified Python stdlib source inventory is invalid."], None
    if not _regular_nonempty_file(archive_path):
        return ["base_library.zip is missing or unsafe."], None
    errors: list[str] = []
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    try:
        with zipfile.ZipFile(archive_path) as archive:
            bad_member = archive.testzip()
            if bad_member is not None:
                errors.append(f"base_library.zip CRC failure: {bad_member}")
            for info in archive.infolist():
                if info.is_dir():
                    errors.append(
                        f"base_library.zip contains a directory entry: {info.filename}"
                    )
                    continue
                relative = _safe_relative(info.filename)
                if relative is None or not relative.casefold().endswith(".pyc"):
                    errors.append(
                        f"base_library.zip contains an unsafe or non-pyc member: "
                        f"{info.filename}"
                    )
                    continue
                collision_key = relative.casefold()
                if collision_key in seen:
                    errors.append(
                        f"base_library.zip contains a duplicate member: {relative}"
                    )
                    continue
                seen.add(collision_key)
                source_relative = relative[:-1]
                source_record = allowed_sources.get(source_relative.casefold())
                if source_record is None:
                    errors.append(
                        f"base_library.zip member has no verified stdlib source: "
                        f"{relative}"
                    )
                    continue
                data = archive.read(info)
                if len(data) < 16:
                    errors.append(f"base_library.zip pyc header is truncated: {relative}")
                    continue
                if data[:4] != importlib.util.MAGIC_NUMBER:
                    errors.append(f"base_library.zip pyc magic differs: {relative}")
                    continue
                flags = struct.unpack("<I", data[4:8])[0]
                if flags != 1 or data[8:16] != b"\0" * 8:
                    errors.append(f"base_library.zip pyc header differs: {relative}")
                    continue
                marshaled = io.BytesIO(data[16:])
                try:
                    code = marshal.load(marshaled)
                except (EOFError, TypeError, ValueError) as exc:
                    errors.append(
                        f"base_library.zip pyc payload is invalid: {relative}: {exc}"
                    )
                    continue
                if not isinstance(code, types.CodeType):
                    errors.append(
                        f"base_library.zip pyc payload is not a code object: {relative}"
                    )
                    continue
                if marshaled.read(1):
                    errors.append(
                        f"base_library.zip pyc payload has trailing bytes: {relative}"
                    )
                    continue
                normalized_filename = code.co_filename.replace("\\", "/")
                if (
                    _safe_relative(normalized_filename) != normalized_filename
                    or normalized_filename != source_record["path"]
                ):
                    errors.append(
                        f"base_library.zip pyc filename differs from stdlib source: "
                        f"{relative}"
                    )
                    continue
                source = _stdlib_root() / Path(
                    *PurePosixPath(source_record["path"]).parts
                )
                if (
                    not is_safe_regular_file(source)
                    or source.stat().st_size != source_record["size"]
                    or sha256_file(source) != source_record["sha256"]
                ):
                    errors.append(
                        f"Verified stdlib source differs while checking base_library.zip: "
                        f"{source_record['path']}"
                    )
                    continue
                source_bytes = source.read_bytes()
                expected_code = compile(
                    source_bytes,
                    code.co_filename,
                    "exec",
                    dont_inherit=True,
                    optimize=0,
                )
                if not _code_tree_matches(code, expected_code):
                    errors.append(
                        f"base_library.zip bytecode differs from verified stdlib source: "
                        f"{relative}"
                    )
                    continue
                records.append(
                    {
                        "path": relative,
                        "size": len(data),
                        "sha256": hashlib.sha256(data).hexdigest(),
                        "source_path": source_record["path"],
                        "source_size": source_record["size"],
                        "source_sha256": source_record["sha256"],
                        "optimization": 0,
                    }
                )
    except (OSError, RuntimeError, zipfile.BadZipFile, KeyError) as exc:
        return [f"Cannot inspect base_library.zip: {exc}"], None
    if not records:
        errors.append("base_library.zip contains no verified Python modules.")
    digest = hashlib.sha256()
    for record in sorted(records, key=lambda item: str(item["path"])):
        digest.update(str(record["path"]).encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(record["size"]).encode("ascii"))
        digest.update(b"\0")
        digest.update(str(record["sha256"]).encode("ascii"))
        digest.update(b"\n")
    summary = {
        "member_count": len(records),
        "total_size": sum(int(item["size"]) for item in records),
        "inventory_sha256": digest.hexdigest(),
        "stdlib_source_inventory_sha256": stdlib.get("inventory_sha256"),
    }
    return errors, summary


def _generated_file(
    relative: str,
    allowed_generated: set[str],
) -> bool:
    return relative in allowed_generated


def _validate_locked_license_materials(
    distribution_root: Path,
    lock: dict[str, Any],
    package_manifest: dict[str, Any] | None,
) -> list[str]:
    errors: list[str] = []
    referenced = {
        *GENERATED_ROOT_DOCUMENTS,
        *_package_license_paths(package_manifest),
    }
    build_python = (
        str(package_manifest.get("build_python_version"))
        if package_manifest is not None
        else sys.version.split()[0]
    )
    seen_paths: dict[str, tuple[str, bool]] = {}
    for component in _component_entries(lock):
        component_name = str(component.get("component", "<unknown>"))
        materials = component.get("license_materials")
        if materials is None:
            exception = component.get("license_materials_exception")
            if isinstance(exception, dict):
                required_strings = ("reason", "evidence", "scope", "reviewer", "date")
                if (
                    not all(isinstance(exception.get(field), str) for field in required_strings)
                    or not exception["reason"].strip()
                    or not isinstance(exception.get("review_completed"), bool)
                    or (
                        exception["review_completed"] is True
                        and not all(
                            exception[field].strip()
                            for field in ("evidence", "scope", "reviewer", "date")
                        )
                    )
                ):
                    errors.append(
                        f"Component license material exception is incomplete: "
                        f"{component_name}"
                    )
                continue
            errors.append(
                f"Component has no locked license materials or exception: "
                f"{component_name}"
            )
            continue
        if not isinstance(materials, list) or not materials:
            errors.append(
                f"Component license material list is empty: {component_name}"
            )
            continue
        for material in materials:
            if not isinstance(material, dict):
                errors.append(
                    f"Invalid locked license material for {component_name}."
                )
                continue
            relative = _safe_relative(material.get("path"))
            if relative is None:
                errors.append(
                    f"Unsafe locked license material for {component_name}: "
                    f"{material.get('path')}"
                )
                continue
            key = relative.casefold()
            shared = material.get("shared") is True
            previous = seen_paths.get(key)
            if previous is not None and (not shared or not previous[1]):
                errors.append(
                    f"Locked license material is multiply assigned without shared "
                    f"review: {previous[0]} / {component_name}: {relative}"
                )
            seen_paths[key] = (component_name, shared)
            if relative not in referenced:
                errors.append(
                    f"Locked license material is not referenced by packaged "
                    f"inventory for {component_name}: {relative}"
                )
            target = distribution_root / Path(*PurePosixPath(relative).parts)
            if not _within(distribution_root, target) or not is_meaningful_license_file(
                target
            ):
                errors.append(
                    f"Locked license material is missing, unsafe, or placeholder for "
                    f"{component_name}: {relative}"
                )
                continue
            expected_hash = material.get("sha256")
            version_hashes = material.get("sha256_by_python_version")
            if isinstance(version_hashes, dict):
                expected_hash = version_hashes.get(build_python)
            if (
                not isinstance(expected_hash, str)
                or SHA256_PATTERN.fullmatch(expected_hash) is None
                or sha256_file(target) != expected_hash
            ):
                errors.append(
                    f"Locked license material SHA256 differs for {component_name}: "
                    f"{relative}"
                )
    return errors


def _validate_build_provenance(
    distribution_root: Path,
    lock: dict[str, Any],
    package_manifest: dict[str, Any] | None,
    *,
    release: bool,
    expected_sha256: str | None = None,
) -> list[str]:
    errors: list[str] = []
    if release and (
        not isinstance(expected_sha256, str)
        or SHA256_PATTERN.fullmatch(expected_sha256) is None
    ):
        errors.append(
            "Release validation requires an externally sealed build provenance SHA256."
        )
    if package_manifest is None:
        return errors
    expected_hash = package_manifest.get("build_provenance_sha256")
    provenance_path = distribution_root / "licenses" / "build-provenance.json"
    if expected_hash is None:
        if release:
            errors.append("Release build provenance is missing.")
        return errors
    if (
        not isinstance(expected_hash, str)
        or SHA256_PATTERN.fullmatch(expected_hash) is None
        or not _regular_nonempty_file(provenance_path)
        or sha256_file(provenance_path) != expected_hash
    ):
        errors.append("Build provenance file is missing or its SHA256 differs.")
        return errors
    if expected_sha256 is not None and (
        SHA256_PATTERN.fullmatch(expected_sha256) is None
        or expected_hash != expected_sha256
    ):
        errors.append("Build provenance differs from the externally sealed SHA256.")
        return errors
    try:
        payload = _read_json(provenance_path)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        errors.append(f"Cannot read build provenance: {exc}")
        return errors
    if payload.get("schema_version") != 1:
        errors.append("Unsupported build provenance schema.")
    if payload.get("component_lock_sha256") != sha256_file(
        distribution_root / "licenses" / "components.json"
    ):
        errors.append("Build provenance component lock SHA256 differs.")
    if payload.get("python_version") != package_manifest.get(
        "build_python_version"
    ):
        errors.append("Build provenance Python version differs.")
    policy = lock.get("release_binary_policy", {})
    if payload.get("python_implementation") != "cpython":
        errors.append("Build provenance Python implementation differs.")
    if payload.get("platform") != policy.get("platform"):
        errors.append("Build provenance platform differs from release policy.")
    if payload.get("pip_version") != policy.get("pip_version"):
        errors.append("Build provenance pip version differs from release policy.")
    if payload.get("python_native_runtime") != package_manifest.get(
        "python_native_runtime"
    ):
        errors.append("Build provenance Python runtime differs from package manifest.")
    for field in ("requirements_set_sha256", "binary_manifest_sha256"):
        value = payload.get(field)
        if not isinstance(value, str) or SHA256_PATTERN.fullmatch(value) is None:
            errors.append(f"Build provenance {field} is invalid.")
    from scripts.prepare_release_assets import (
        _verify_bootstrap_pip_environment,
        _verify_environment_file_ownership,
        capture_git_source_identity,
        flatten_exact_requirements,
        verify_recorded_install_inventory,
    )

    try:
        current_git_source = capture_git_source_identity()
    except (OSError, RuntimeError, ValueError) as exc:
        errors.append(f"Cannot verify Git source identity: {exc}")
    else:
        if payload.get("git_source") != current_git_source:
            errors.append("Build provenance Git source identity differs after build.")

    try:
        pins, expected_inputs = flatten_exact_requirements(
            RUNTIME_REQUIREMENTS.with_name("requirements-dev.txt")
        )
    except (OSError, RuntimeError) as exc:
        errors.append(f"Cannot verify build requirement provenance: {exc}")
    else:
        if payload.get("requirements_inputs") != expected_inputs:
            errors.append("Build provenance requirement inputs differ.")
        requirements_set = "".join(
            f"{item['canonical_name']}=={item['version']}\n"
            for item in sorted(pins, key=lambda item: item["canonical_name"])
        ).encode("utf-8")
        if payload.get("requirements_set_sha256") != hashlib.sha256(
            requirements_set
        ).hexdigest():
            errors.append("Build provenance requirement set differs.")
    expected_components = set(
        lock.get("release_binary_policy", {}).get("required_components", [])
    )
    if set(package_manifest.get("verified_binary_components", [])) != expected_components:
        errors.append("Package manifest verified binary component set differs.")
    records = payload.get("installed_binaries")
    if not isinstance(records, list):
        return [*errors, "Build provenance has no installed binary records."]
    try:
        observed_bootstrap = _verify_bootstrap_pip_environment(
            package_manifest.get("python_native_runtime", {})
        )
    except (OSError, RuntimeError, ValueError) as exc:
        errors.append(f"Cannot verify bootstrap pip provenance: {exc}")
        observed_bootstrap = None
        allowed_environment_files: set[str] = set()
    else:
        if payload.get("bootstrap_pip") != observed_bootstrap:
            errors.append("Build provenance bootstrap pip inventory differs.")
        try:
            allowed_environment_files = verify_recorded_install_inventory(
                "pip",
                observed_bootstrap,
            )
        except (OSError, RuntimeError, ValueError) as exc:
            errors.append(f"Cannot verify recorded bootstrap pip files: {exc}")
            allowed_environment_files = set()
    locked = {
        str(component["component"]): component
        for component in _component_entries(lock)
    }
    observed: set[str] = set()
    for record in records:
        if not isinstance(record, dict):
            errors.append("Build provenance contains an invalid binary record.")
            continue
        component_name = str(record.get("component", ""))
        component = locked.get(component_name)
        if component_name in observed or component is None:
            errors.append(
                f"Build provenance component is duplicate or unexpected: "
                f"{component_name}"
            )
            continue
        observed.add(component_name)
        archive = component.get("binary_archive")
        if not isinstance(archive, dict):
            errors.append(
                f"Build provenance component has no binary lock: {component_name}"
            )
            continue
        for field in ("filename", "sha256", "size"):
            if record.get(field) != archive.get(field):
                errors.append(
                    f"Build provenance differs for {component_name}.{field}."
                )
        if record.get("version") != component.get("version"):
            errors.append(f"Build provenance version differs for {component_name}.")
        try:
            owned = verify_recorded_install_inventory(
                str(component["distribution"]),
                record,
            )
        except (OSError, RuntimeError, metadata.PackageNotFoundError) as exc:
            errors.append(
                f"Cannot verify installed build provenance for {component_name}: "
                f"{exc}"
            )
        else:
            allowed_environment_files.update(owned)
    if observed != expected_components:
        errors.append("Build provenance component set differs from binary policy.")
    if observed_bootstrap is not None:
        try:
            _verify_environment_file_ownership(allowed_environment_files)
        except (OSError, RuntimeError, ValueError) as exc:
            errors.append(f"Build environment ownership verification failed: {exc}")
    return errors


def _validate_project_documents(distribution_root: Path) -> list[str]:
    errors: list[str] = []
    repository_root = Path(__file__).resolve().parents[1]
    for relative in PROJECT_DOCUMENTS:
        packaged = distribution_root / relative
        repository = repository_root / relative
        if not _regular_nonempty_file(packaged):
            errors.append(
                f"Required distribution document is not a non-empty regular file: "
                f"{relative}"
            )
            continue
        if not _regular_nonempty_file(repository):
            errors.append(
                f"Repository distribution document is not a non-empty regular file: "
                f"{relative}"
            )
            continue
        if sha256_file(packaged) != sha256_file(repository):
            errors.append(
                f"Packaged distribution document differs from repository: {relative}"
            )
    license_path = distribution_root / "LICENSE"
    if _regular_nonempty_file(license_path):
        try:
            license_text = license_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            errors.append(f"Cannot read packaged project license: {exc}")
        else:
            if (
                "GNU GENERAL PUBLIC LICENSE" not in license_text
                or "Version 3" not in license_text
            ):
                errors.append("Packaged LICENSE is not the GNU GPL version 3 text.")
    return errors


def _validate_runtime_download_lock(lock: dict[str, Any]) -> list[str]:
    actual_entries = lock.get("runtime_downloads")
    if not isinstance(actual_entries, list):
        return ["Component lock runtime_downloads must be a list."]
    actual: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    for entry in actual_entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("component"), str):
            errors.append("Component lock contains an invalid runtime download.")
            continue
        component = entry["component"]
        if component in actual:
            errors.append(f"Duplicate runtime download lock: {component}")
        actual[component] = entry
    if actual:
        errors.append("Runtime download component set must be empty for user-provided tools.")
    return errors


def _release_gate_errors(
    lock: dict[str, Any],
    package_manifest: dict[str, Any] | None,
) -> list[str]:
    from scripts.prepare_release_assets import release_gate_errors

    errors = []
    build_python = (
        str(package_manifest.get("build_python_version"))
        if package_manifest is not None
        else sys.version.split()[0]
    )
    release_python = str(lock["python"]["release_version"])
    if build_python != release_python:
        errors.append(
            f"Release build Python must be {release_python}; actual build used "
            f"{build_python}."
        )
    if sys.version.split()[0] != release_python:
        errors.append(
            f"Release checker must run with Python {release_python}; "
            f"running {sys.version.split()[0]}."
        )
    errors.extend(
        f"Release legal/source gate remains for {error}"
        for error in release_gate_errors(lock)
    )
    required_binary_components = set(
        lock.get("release_binary_policy", {}).get("required_components", [])
    )
    verified_binary_components = set(
        package_manifest.get("verified_binary_components", [])
        if package_manifest is not None
        else []
    )
    if verified_binary_components != required_binary_components:
        errors.append(
            "Release build did not verify the complete locked binary component set."
        )
    if package_manifest is not None:
        package_components = {
            str(package.get("component")): package
            for package in package_manifest.get("packages", [])
            if isinstance(package, dict)
        }
        for component_name in required_binary_components:
            if package_components.get(component_name, {}).get(
                "binary_install_verified"
            ) is not True:
                errors.append(
                    f"Release binary install is not verified for {component_name}."
                )
    return errors


def _write_distribution_manifest(
    distribution_root: Path,
    component_lock: dict[str, Any],
    toc_path: Path,
    toc_entries: list[dict[str, str]],
    ownership: dict[str, str],
    package_manifest: dict[str, Any],
    pyinstaller_summary: dict[str, Any],
) -> Path:
    toc_by_path = {entry["path"].casefold(): entry for entry in toc_entries}
    files = []
    for collision_key, path in sorted(
        _physical_files(distribution_root).items(),
        key=lambda item: item[0],
    ):
        relative = path.relative_to(distribution_root).as_posix()
        entry = toc_by_path.get(collision_key)
        component = (
            "license-materials"
            if relative.startswith("licenses/")
            else "lol-replay-tool"
            if relative in GENERATED_ROOT_DOCUMENTS
            else ownership[collision_key]
        )
        record: dict[str, Any] = {
            "path": relative,
            "size": path.stat().st_size,
            "sha256": sha256_file(path),
            "component": component,
        }
        if entry is not None:
            record["toc_name"] = entry["toc_name"]
            record["toc_type"] = entry["type"]
        files.append(record)

    base_library_errors, base_library_summary = _validate_base_library_archive(
        distribution_root / "_internal" / "base_library.zip",
        package_manifest,
    )
    if base_library_errors or base_library_summary is None:
        raise ValueError("Cannot inventory base_library.zip: " + " | ".join(base_library_errors))
    manifest = {
        "schema_version": 1,
        "statement": (
            "This is a technical inventory of the actual build, not the formal "
            "or controlling legal record. Included license texts and applicable "
            "law remain controlling."
        ),
        "build_python_version": package_manifest["build_python_version"],
        "release_python_version": component_lock["python"]["release_version"],
        "component_lock_sha256": sha256_file(
            distribution_root / "licenses" / "components.json"
        ),
        "build_provenance_sha256": package_manifest.get(
            "build_provenance_sha256"
        ),
        "pyinstaller_collect_toc_sha256": sha256_file(toc_path),
        "pyinstaller_build": pyinstaller_summary,
        "python_base_library": base_library_summary,
        "files": files,
    }
    manifest_path = distribution_root / MANIFEST_RELATIVE_PATH
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest_path


def _validate_runtime_policy_manifest_binding(
    pyinstaller_summary: dict[str, Any],
    package_manifest: dict[str, Any],
) -> list[str]:
    runtime_audit = pyinstaller_summary.get("runtime_policy_audit")
    binding = pyinstaller_summary.get("provenance_binding")
    if runtime_audit is None and binding is None:
        return []
    errors: list[str] = []
    if not isinstance(runtime_audit, dict) or not isinstance(binding, dict):
        return [
            "Distribution manifest runtime policy audit binding is incomplete."
        ]
    expected_runtime_keys = {
        "artifact",
        "policy",
        "pyinstaller_version",
        "raw_inventory_sha256",
        "policy_result_sha256",
        "normalized_raw_inventory_sha256",
        "raw_binary_count",
        "retained_binary_count",
        "excluded_binary_count",
        "allowed_source_boundaries",
        "excluded_binaries",
        "raw_binaries",
    }
    if set(runtime_audit) != expected_runtime_keys:
        errors.append("Distribution manifest runtime policy structure changed.")
    artifact = runtime_audit.get("artifact")
    if not isinstance(artifact, dict):
        errors.append("Distribution manifest runtime policy artifact is missing.")
        artifact = {}
    elif set(artifact) != {"filename", "size", "sha256", "payload_sha256"}:
        errors.append("Distribution manifest runtime policy artifact structure changed.")
    if artifact.get("filename") != RUNTIME_POLICY_AUDIT_FILENAME:
        errors.append("Distribution manifest runtime policy artifact name differs.")
    if not isinstance(artifact.get("size"), int) or artifact.get("size", 0) <= 0:
        errors.append("Distribution manifest runtime policy artifact size is invalid.")
    for field in ("sha256", "payload_sha256"):
        value = artifact.get(field)
        if not isinstance(value, str) or SHA256_PATTERN.fullmatch(value) is None:
            errors.append(
                f"Distribution manifest runtime policy artifact {field} is invalid."
            )
    for field in (
        "raw_inventory_sha256",
        "policy_result_sha256",
        "normalized_raw_inventory_sha256",
    ):
        value = runtime_audit.get(field)
        if not isinstance(value, str) or SHA256_PATTERN.fullmatch(value) is None:
            errors.append(
                f"Distribution manifest runtime policy {field} is invalid."
            )
    counts = [
        runtime_audit.get("raw_binary_count"),
        runtime_audit.get("retained_binary_count"),
        runtime_audit.get("excluded_binary_count"),
    ]
    if not all(isinstance(value, int) and value >= 0 for value in counts) or (
        counts[0] != counts[1] + counts[2]
    ):
        errors.append("Distribution manifest runtime policy counts are inconsistent.")
    boundaries = runtime_audit.get("allowed_source_boundaries")
    if (
        not isinstance(boundaries, list)
        or not all(isinstance(value, str) and value for value in boundaries)
        or len(boundaries) != len(set(boundaries))
    ):
        errors.append("Distribution manifest runtime policy boundaries are invalid.")
        boundaries = []
    excluded = runtime_audit.get("excluded_binaries")
    if not isinstance(excluded, list):
        errors.append("Distribution manifest runtime policy exclusions are missing.")
        excluded = []
    elif isinstance(counts[2], int) and len(excluded) != counts[2]:
        errors.append("Distribution manifest runtime policy exclusion count differs.")
    for record in excluded:
        if not isinstance(record, dict):
            errors.append("Distribution manifest runtime policy exclusion is invalid.")
            continue
        if set(record) != {
            "raw_index",
            "destination",
            "source",
            "source_boundary",
            "type",
            "size",
            "sha256",
            "reason",
        }:
            errors.append(
                "Distribution manifest runtime policy exclusion structure changed."
            )
        destination = _safe_relative(record.get("destination"))
        source = _safe_relative(record.get("source"))
        boundary = record.get("source_boundary")
        if destination is None:
            errors.append(
                "Distribution manifest runtime policy exclusion destination is unsafe."
            )
        if (
            source is None
            or not isinstance(boundary, str)
            or boundary not in boundaries
            or not source.startswith(f"{boundary}/")
        ):
            errors.append(
                "Distribution manifest runtime policy exclusion source is unsafe."
            )
        digest = record.get("sha256")
        if (
            not isinstance(record.get("size"), int)
            or record.get("size", -1) < 0
            or not isinstance(digest, str)
            or SHA256_PATTERN.fullmatch(digest) is None
            or record.get("type") != "BINARY"
            or record.get("reason")
            not in {"supported-windows-os-runtime", "redundant-root-vcomp"}
        ):
            errors.append(
                "Distribution manifest runtime policy exclusion metadata is invalid."
            )
    raw_records = runtime_audit.get("raw_binaries")
    if not isinstance(raw_records, list):
        errors.append(
            "Distribution manifest runtime policy normalized raw inventory is missing."
        )
        raw_records = []
    elif isinstance(counts[0], int) and len(raw_records) != counts[0]:
        errors.append(
            "Distribution manifest runtime policy normalized raw count differs."
        )
    seen_destinations: set[str] = set()
    excluded_indexes: set[int] = set()
    retained_indexes: set[int] = set()
    for index, record in enumerate(raw_records):
        if not isinstance(record, dict) or record.get("raw_index") != index:
            errors.append(
                "Distribution manifest runtime policy normalized raw record is invalid."
            )
            continue
        decision = record.get("decision")
        expected_record_keys = {
            "raw_index",
            "destination",
            "source",
            "source_component",
            "type",
            "size",
            "sha256",
            "decision",
        }
        if decision == "excluded":
            expected_record_keys.add("reason")
        if set(record) != expected_record_keys:
            errors.append(
                "Distribution manifest runtime policy normalized raw structure "
                "changed."
            )
        destination = _safe_relative(record.get("destination"))
        source = _safe_relative(record.get("source"))
        destination_key = destination.casefold() if destination is not None else ""
        if (
            destination is None
            or destination_key in seen_destinations
            or source is None
            or not isinstance(record.get("source_component"), str)
            or re.fullmatch(
                r"[a-z0-9][a-z0-9.-]*",
                record["source_component"],
            )
            is None
            or record.get("type") not in {"BINARY", "EXTENSION"}
            or not isinstance(record.get("size"), int)
            or record.get("size", -1) < 0
            or not isinstance(record.get("sha256"), str)
            or SHA256_PATTERN.fullmatch(record["sha256"]) is None
        ):
            errors.append(
                "Distribution manifest runtime policy normalized raw metadata is invalid."
            )
        seen_destinations.add(destination_key)
        if decision == "excluded":
            excluded_indexes.add(index)
            if record.get("reason") not in {
                "supported-windows-os-runtime",
                "redundant-root-vcomp",
            }:
                errors.append(
                    "Distribution manifest runtime policy normalized exclusion reason "
                    "is invalid."
                )
        elif decision == "retained":
            retained_indexes.add(index)
            if "reason" in record:
                errors.append(
                    "Distribution manifest runtime policy retained record has a reason."
                )
        else:
            errors.append(
                "Distribution manifest runtime policy normalized decision is invalid."
            )
    declared_excluded_indexes = {
        record.get("raw_index")
        for record in excluded
        if isinstance(record, dict)
    }
    if (
        excluded_indexes != declared_excluded_indexes
        or excluded_indexes & retained_indexes
        or excluded_indexes | retained_indexes != set(range(len(raw_records)))
    ):
        errors.append(
            "Distribution manifest runtime policy normalized decisions are not a "
            "partition."
        )
    raw_by_index = {
        record.get("raw_index"): record
        for record in raw_records
        if isinstance(record, dict)
    }
    for record in excluded:
        if not isinstance(record, dict):
            continue
        raw_record = raw_by_index.get(record.get("raw_index"))
        if raw_record is None or any(
            raw_record.get(raw_field) != record.get(excluded_field)
            for raw_field, excluded_field in (
                ("destination", "destination"),
                ("source", "source"),
                ("source_component", "source_boundary"),
                ("type", "type"),
                ("size", "size"),
                ("sha256", "sha256"),
                ("reason", "reason"),
            )
        ):
            errors.append(
                "Distribution manifest runtime policy normalized exclusion differs."
            )
    normalized_digest = hashlib.sha256(
        json.dumps(
            raw_records,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    if runtime_audit.get("normalized_raw_inventory_sha256") != normalized_digest:
        errors.append(
            "Distribution manifest runtime policy normalized raw SHA256 differs."
        )
    toc_files = pyinstaller_summary.get("toc_files")
    if not isinstance(toc_files, dict):
        toc_files = {}
    expected_binding = {
        "runtime_policy_audit_sha256": artifact.get("sha256"),
        "runtime_policy_payload_sha256": artifact.get("payload_sha256"),
        "raw_inventory_sha256": runtime_audit.get("raw_inventory_sha256"),
        "normalized_raw_inventory_sha256": runtime_audit.get(
            "normalized_raw_inventory_sha256"
        ),
        "analysis_toc_sha256": (
            toc_files.get("Analysis-00.toc", {}).get("sha256")
            if isinstance(toc_files.get("Analysis-00.toc"), dict)
            else None
        ),
        "collect_toc_sha256": (
            toc_files.get("COLLECT-00.toc", {}).get("sha256")
            if isinstance(toc_files.get("COLLECT-00.toc"), dict)
            else None
        ),
        "build_provenance_sha256": package_manifest.get(
            "build_provenance_sha256"
        ),
    }
    if binding != expected_binding:
        errors.append(
            "Distribution manifest runtime policy external provenance binding differs."
        )
    return errors


def _validate_existing_distribution_manifest(
    distribution_root: Path,
    manifest_path: Path,
    component_lock: dict[str, Any],
    package_manifest: dict[str, Any],
    *,
    toc_path: Path | None = None,
    expected_ownership: dict[str, str] | None = None,
    toc_entries: list[dict[str, str]] | None = None,
    pyinstaller_summary: dict[str, Any] | None = None,
) -> list[str]:
    try:
        payload = _read_json(manifest_path)
        physical = _physical_files(distribution_root)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        return [f"Cannot validate distribution manifest: {exc}"]
    errors: list[str] = []
    if payload.get("schema_version") != 1:
        errors.append("Unsupported distribution manifest schema.")
    if not isinstance(payload.get("statement"), str) or not payload["statement"].strip():
        errors.append("Distribution manifest statement is missing.")
    if payload.get("build_python_version") != package_manifest.get(
        "build_python_version"
    ):
        errors.append("Distribution manifest build Python version does not match.")
    if payload.get("release_python_version") != component_lock["python"].get(
        "release_version"
    ):
        errors.append("Distribution manifest release Python version does not match.")
    expected_lock_hash = sha256_file(
        distribution_root / "licenses" / "components.json"
    )
    if payload.get("component_lock_sha256") != expected_lock_hash:
        errors.append("Distribution manifest component lock SHA256 does not match.")
    if payload.get("build_provenance_sha256") != package_manifest.get(
        "build_provenance_sha256"
    ):
        errors.append("Distribution manifest build provenance SHA256 does not match.")
    base_library_errors, base_library_summary = _validate_base_library_archive(
        distribution_root / "_internal" / "base_library.zip",
        package_manifest,
    )
    errors.extend(base_library_errors)
    if payload.get("python_base_library") != base_library_summary:
        errors.append("Distribution manifest base_library inventory does not match.")
    toc_hash = payload.get("pyinstaller_collect_toc_sha256")
    if not isinstance(toc_hash, str) or SHA256_PATTERN.fullmatch(toc_hash) is None:
        errors.append("Distribution manifest TOC SHA256 is invalid.")
    elif toc_path is not None and toc_hash != sha256_file(toc_path):
        errors.append("Distribution manifest TOC SHA256 does not match.")
    recorded_pyinstaller = payload.get("pyinstaller_build")
    if not isinstance(recorded_pyinstaller, dict):
        errors.append("Distribution manifest complete PyInstaller inventory is missing.")
    else:
        errors.extend(
            _validate_runtime_policy_manifest_binding(
                recorded_pyinstaller,
                package_manifest,
            )
        )
        if pyinstaller_summary is not None and recorded_pyinstaller != pyinstaller_summary:
            errors.append("Distribution manifest PyInstaller inventory does not match.")

    records = payload.get("files")
    if not isinstance(records, list):
        return [*errors, "Distribution manifest does not contain a file list."]

    recorded: dict[str, str] = {}
    valid_components = _component_names(component_lock)
    toc_by_path = {
        entry["path"].casefold(): entry for entry in (toc_entries or [])
    }
    allowed_generated = _allowed_generated_files(package_manifest)
    for record in records:
        if not isinstance(record, dict):
            errors.append("Distribution manifest contains an invalid file entry.")
            continue
        relative = _safe_relative(record.get("path"))
        if relative is None:
            errors.append(f"Unsafe path in distribution manifest: {record.get('path')}")
            continue
        key = relative.casefold()
        previous = recorded.get(key)
        if previous is not None:
            errors.append(f"Duplicate path in distribution manifest: {relative}")
            continue
        recorded[key] = relative
        path = physical.get(key)
        if path is None:
            errors.append(f"Manifest file is missing from distribution: {relative}")
            continue
        if path.relative_to(distribution_root).as_posix() != relative:
            errors.append(f"Manifest path casing differs from distribution: {relative}")
        if path.stat().st_size != record.get("size"):
            errors.append(f"Manifest size differs for: {relative}")
        record_hash = record.get("sha256")
        if (
            not isinstance(record_hash, str)
            or SHA256_PATTERN.fullmatch(record_hash) is None
            or sha256_file(path) != record_hash
        ):
            errors.append(f"Manifest SHA256 differs for: {relative}")
        component = record.get("component")
        if component not in valid_components:
            errors.append(f"Manifest component is invalid for: {relative}")
        expected_component = None
        toc_entry = toc_by_path.get(key)
        if relative.startswith("licenses/") or relative in GENERATED_ROOT_DOCUMENTS:
            expected_component = (
                "license-materials"
                if relative.startswith("licenses/")
                else "lol-replay-tool"
            )
        elif expected_ownership is not None:
            expected_component = expected_ownership.get(key)
        if expected_component is not None and component != expected_component:
            errors.append(f"Manifest component ownership differs for: {relative}")
        if toc_entry is None:
            if toc_entries is not None and relative not in allowed_generated:
                errors.append(f"Manifest file has no matching TOC entry: {relative}")
            if toc_entries is not None and (
                "toc_name" in record or "toc_type" in record
            ):
                errors.append(
                    f"Generated manifest file unexpectedly records TOC metadata: "
                    f"{relative}"
                )
        elif (
            record.get("toc_name") != toc_entry["toc_name"]
            or record.get("toc_type") != toc_entry["type"]
        ):
            errors.append(f"Manifest TOC metadata differs for: {relative}")

    for key, path in physical.items():
        if key not in recorded:
            errors.append(
                "Distribution file is missing from manifest: "
                + path.relative_to(distribution_root).as_posix()
            )
    return errors


def validate_distribution(
    distribution_root: Path,
    *,
    toc_path: Path | None = None,
    write_manifest: bool = False,
    release: bool = False,
    build_provenance_sha256: str | None = None,
) -> list[str]:
    errors = [
        f"Required distribution document is missing or unsafe: {relative_path}"
        for relative_path in validate_distribution_documents(distribution_root)
    ]
    errors.extend(_validate_project_documents(distribution_root))
    try:
        physical = _physical_files(distribution_root)
    except (OSError, ValueError) as exc:
        errors.append(str(exc))
        return errors
    errors.extend(_forbidden_user_runtime_errors(distribution_root, physical))
    lock_path = distribution_root / "licenses" / "components.json"
    package_manifest_path = distribution_root / "licenses" / "python-packages.json"
    if not _regular_nonempty_file(lock_path):
        errors.append("Packaged component lock is not a non-empty regular file.")
        return errors
    try:
        lock = _read_json(lock_path)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        errors.append(f"Cannot read packaged component lock: {exc}")
        return errors

    if lock.get("schema_version") != 1:
        errors.append("Unsupported packaged component lock schema.")
    errors.extend(_validate_runtime_download_lock(lock))
    try:
        if sha256_file(lock_path) != sha256_file(COMPONENTS_FILE):
            errors.append("Packaged component lock differs from repository lock.")
    except OSError as exc:
        errors.append(f"Cannot compare component lock: {exc}")

    package_manifest: dict[str, Any] | None = None
    if _regular_nonempty_file(package_manifest_path):
        errors.extend(validate_package_manifest(package_manifest_path, lock))
        try:
            package_manifest = _read_json(package_manifest_path)
        except (OSError, json.JSONDecodeError, ValueError):
            pass
    else:
        errors.append(
            "Python package license manifest is missing, empty, or not a regular file."
        )

    base_library_errors, _base_library_summary = _validate_base_library_archive(
        distribution_root / "_internal" / "base_library.zip",
        package_manifest,
    )
    errors.extend(base_library_errors)

    try:
        pins = parse_requirement_pins(RUNTIME_REQUIREMENTS)
        locked_runtime = {
            canonicalize_distribution_name(str(component["distribution"])): str(
                component["version"]
            )
            for component in lock.get("runtime_components", [])
            if component.get("distribution")
        }
        if {name: version for name, (_spelling, version) in pins.items()} != locked_runtime:
            errors.append("Runtime requirements and packaged component lock differ.")
    except RuntimeError as exc:
        errors.append(str(exc))

    errors.extend(
        _validate_locked_license_materials(
            distribution_root,
            lock,
            package_manifest,
        )
    )
    errors.extend(
        _validate_build_provenance(
            distribution_root,
            lock,
            package_manifest,
            release=release,
            expected_sha256=build_provenance_sha256,
        )
    )

    allowed_generated = _allowed_generated_files(package_manifest)
    for path in physical.values():
        relative = path.relative_to(distribution_root).as_posix()
        if relative.startswith("licenses/") and relative not in allowed_generated:
            errors.append(f"Unreferenced file in license directory: {relative}")

    if release:
        errors.extend(_release_gate_errors(lock, package_manifest))
        if toc_path is None:
            errors.append(
                "Release validation requires the exact PyInstaller COLLECT TOC."
            )

    if toc_path is None:
        manifest_path = distribution_root / MANIFEST_RELATIVE_PATH
        if _regular_nonempty_file(manifest_path) and package_manifest is not None:
            errors.extend(
                _validate_existing_distribution_manifest(
                    distribution_root,
                    manifest_path,
                    lock,
                    package_manifest,
                )
            )
        else:
            errors.append(
                "Distribution manifest is missing, empty, or not a regular file."
            )
        return errors

    try:
        toc_entries = parse_collect_toc(toc_path)
        source_owners, ownership_errors = _distribution_source_owners(lock)
        errors.extend(ownership_errors)
        patterns = _artifact_patterns(lock)
        native_artifact_locks = _python_native_artifact_locks(
            lock,
            str(package_manifest.get("build_python_version"))
            if package_manifest is not None
            else sys.version.split()[0],
        )
        python_core_sources = _python_core_source_locks(
            lock,
            str(package_manifest.get("build_python_version"))
            if package_manifest is not None
            else sys.version.split()[0],
        )
    except (OSError, ValueError) as exc:
        errors.append(str(exc))
        return errors

    pyinstaller_summary: dict[str, Any] | None = None
    if package_manifest is not None:
        pyinstaller_errors, pyinstaller_summary = _validate_pyinstaller_build(
            distribution_root,
            toc_path,
            lock,
            package_manifest,
            source_owners,
        )
        errors.extend(pyinstaller_errors)

    toc_by_path = {entry["path"].casefold(): entry for entry in toc_entries}
    for key, entry in toc_by_path.items():
        source = Path(entry["source"])
        if not is_safe_regular_file(source):
            errors.append(
                f"TOC source is missing, linked, or not a regular file: "
                f"{entry['source']}"
            )
        path = physical.get(key)
        if path is None:
            errors.append(f"TOC file is missing from final distribution: {entry['path']}")
        elif path.relative_to(distribution_root).as_posix() != entry["path"]:
            errors.append(f"TOC path casing differs from final distribution: {entry['path']}")
        elif is_safe_regular_file(source) and (
            source.stat().st_size != path.stat().st_size
            or sha256_file(source) != sha256_file(path)
        ):
            errors.append(
                f"TOC source differs from final distribution: {entry['path']}"
            )

    for key, path in physical.items():
        relative = path.relative_to(distribution_root).as_posix()
        if not _generated_file(relative, allowed_generated) and key not in toc_by_path:
            errors.append(f"Final distribution file is missing from TOC: {relative}")

    ownership: dict[str, str] = {}
    for key, entry in toc_by_path.items():
        if key not in physical:
            continue
        packaged_path = physical[key]
        component = _classify_toc_entry(
            entry,
            source_owners,
            python_core_sources,
        )
        if component is None:
            owner = source_owners.get(_path_key(Path(entry["source"])))
            errors.append(
                f"Unclassified packaged file: {entry['path']} "
                f"(TOC type {entry['type']}, source {entry['source']}, "
                f"owner {owner or 'none'})"
            )
            continue
        ownership[key] = component
        if PurePosixPath(entry["path"]).suffix.casefold() in NATIVE_SUFFIXES:
            component_patterns = patterns.get(component, [])
            if not any(
                _matches_pattern(entry["path"], pattern)
                for pattern in component_patterns
            ):
                errors.append(
                    f"Native artifact is not allowed by component lock: "
                    f"{entry['path']} ({component})"
                )
            core_lock = python_core_sources.get(_path_key(Path(entry["source"])))
            if core_lock is not None and (
                packaged_path.stat().st_size != core_lock.get("size")
                or sha256_file(packaged_path) != core_lock.get("sha256")
                or Path(entry["source"]).stat().st_size != core_lock.get("size")
                or sha256_file(Path(entry["source"])) != core_lock.get("sha256")
            ):
                errors.append(
                    f"Python core native artifact differs from official inventory: "
                    f"{entry['path']}"
                )
            if component.startswith("python-"):
                artifact_lock = native_artifact_locks.get(
                    (component, PurePosixPath(entry["path"]).name.casefold())
                )
                if artifact_lock is None:
                    errors.append(
                        f"Python native artifact has no verified runtime hash: "
                        f"{entry['path']} ({component})"
                    )
                elif (
                    packaged_path.stat().st_size != artifact_lock.get("size")
                    or sha256_file(packaged_path) != artifact_lock.get("sha256")
                ):
                    errors.append(
                        f"Python native artifact differs from verified runtime: "
                        f"{entry['path']} ({component})"
                    )

    manifest_path = distribution_root / MANIFEST_RELATIVE_PATH
    if (
        not write_manifest
        and _regular_nonempty_file(manifest_path)
        and package_manifest is not None
    ):
        errors.extend(
            _validate_existing_distribution_manifest(
                distribution_root,
                manifest_path,
                lock,
                package_manifest,
                toc_path=toc_path,
                expected_ownership=ownership,
                toc_entries=toc_entries,
                pyinstaller_summary=pyinstaller_summary,
            )
        )
    elif not write_manifest:
        errors.append(
            "Distribution manifest is missing, empty, or not a regular file."
        )

    if (
        write_manifest
        and not errors
        and package_manifest is not None
        and pyinstaller_summary is not None
    ):
        _write_distribution_manifest(
            distribution_root,
            lock,
            toc_path,
            toc_entries,
            ownership,
            package_manifest,
            pyinstaller_summary,
        )
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("distribution_root", type=Path)
    parser.add_argument("--toc", type=Path)
    parser.add_argument("--write-manifest", action="store_true")
    parser.add_argument("--release", action="store_true")
    parser.add_argument("--build-provenance-sha256")
    args = parser.parse_args()
    if args.write_manifest and args.toc is None:
        parser.error("--write-manifest requires --toc")
    if args.release and args.build_provenance_sha256 is None:
        parser.error("--release requires --build-provenance-sha256")
    errors = validate_distribution(
        args.distribution_root,
        toc_path=args.toc,
        write_manifest=args.write_manifest,
        release=args.release,
        build_provenance_sha256=args.build_provenance_sha256,
    )
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1
    if not args.release:
        try:
            from scripts.prepare_release_assets import release_gate_errors

            lock = _read_json(args.distribution_root / "licenses" / "components.json")
            release_version = str(lock["python"]["release_version"])
            if sys.version.split()[0] != release_version:
                print(
                    f"WARNING: local build uses Python {sys.version.split()[0]}; "
                    f"release builds are locked to {release_version}."
                )
            open_gates = release_gate_errors(lock)
            if open_gates:
                print(
                    f"WARNING: {len(open_gates)} release legal/source gates remain, "
                    "including incomplete bundled native source coverage, "
                    "unverified build provenance, and legal evidence."
                )
        except (OSError, json.JSONDecodeError, KeyError, ValueError):
            pass
    print(f"License compliance check passed: {args.distribution_root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
