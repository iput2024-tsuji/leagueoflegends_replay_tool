"""Validate and inventory the files in a packaged application."""

from __future__ import annotations

import argparse
import ast
import fnmatch
import hashlib
import json
import os
import re
import stat
import sys
import zipfile
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
    if lock is not None:
        expected = _components_by_distribution(lock)
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
            if not relative_path.startswith("python-packages/"):
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


def _is_trusted_windows_system_runtime_source(source: Path) -> bool:
    system_root = os.environ.get("SystemRoot")
    if not system_root:
        return False
    try:
        return source.resolve().parent == (Path(system_root) / "System32").resolve()
    except OSError:
        return False


def _microsoft_runtime_owner_matches(
    owner: str | None,
    final_path: str,
    source: Path,
    python_core_sources: dict[str, dict[str, Any]],
) -> bool:
    final_lower = final_path.casefold()
    if _path_key(source) in python_core_sources:
        return final_lower == f"_internal/{source.name.casefold()}"
    if _is_trusted_windows_system_runtime_source(source):
        return (
            owner is None
            and source.name.casefold() == "vcomp140.dll"
            and final_lower == "_internal/vcomp140.dll"
        )
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
    allowed_sources = {
        str(item.get("path", "")).casefold()
        for item in source_artifacts
        if isinstance(item, dict)
        and isinstance(item.get("path"), str)
        and isinstance(item.get("size"), int)
        and isinstance(item.get("sha256"), str)
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
                source_path = (relative[:-1]).casefold()
                if source_path not in allowed_sources:
                    errors.append(
                        f"base_library.zip member has no verified stdlib source: "
                        f"{relative}"
                    )
                data = archive.read(info)
                records.append(
                    {
                        "path": relative,
                        "size": len(data),
                        "sha256": hashlib.sha256(data).hexdigest(),
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
) -> list[str]:
    if package_manifest is None:
        return []
    expected_hash = package_manifest.get("build_provenance_sha256")
    provenance_path = distribution_root / "licenses" / "build-provenance.json"
    if expected_hash is None:
        return ["Release build provenance is missing."] if release else []
    if (
        not isinstance(expected_hash, str)
        or SHA256_PATTERN.fullmatch(expected_hash) is None
        or not _regular_nonempty_file(provenance_path)
        or sha256_file(provenance_path) != expected_hash
    ):
        return ["Build provenance file is missing or its SHA256 differs."]
    try:
        payload = _read_json(provenance_path)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        return [f"Cannot read build provenance: {exc}"]
    errors: list[str] = []
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
        _installed_distribution_digest,
        flatten_exact_requirements,
    )

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
            actual_digest = _installed_distribution_digest(
                str(component["distribution"])
            )
        except (OSError, RuntimeError, metadata.PackageNotFoundError) as exc:
            errors.append(
                f"Cannot verify installed build provenance for {component_name}: "
                f"{exc}"
            )
        else:
            if record.get("installed_files_sha256") != actual_digest:
                errors.append(
                    f"Installed files differ from build provenance for "
                    f"{component_name}."
                )
    if observed != expected_components:
        errors.append("Build provenance component set differs from binary policy.")
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
    from scripts import setup_env

    expected = {
        "obs-studio": {
            "version": setup_env.OBS_PACKAGE.version,
            "archive_url": setup_env.OBS_PACKAGE.url,
            "archive_sha256": setup_env.OBS_PACKAGE.sha256,
            "fallback_urls": list(setup_env.OBS_PACKAGE.fallback_urls),
        },
        "gyan-ffmpeg": {
            "version": setup_env.FFMPEG_PACKAGE.version,
            "archive_url": setup_env.FFMPEG_PACKAGE.url,
            "archive_sha256": setup_env.FFMPEG_PACKAGE.sha256,
            "fallback_urls": list(setup_env.FFMPEG_PACKAGE.fallback_urls),
        },
    }
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
    if set(actual) != set(expected):
        errors.append("Runtime download component set differs from setup_env constants.")
    for component, fields in expected.items():
        entry = actual.get(component)
        if entry is None:
            continue
        for field, value in fields.items():
            if entry.get(field, [] if field == "fallback_urls" else None) != value:
                errors.append(
                    f"Runtime download lock differs from setup_env for "
                    f"{component}.{field}."
                )
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
        "python_base_library": base_library_summary,
        "files": files,
    }
    manifest_path = distribution_root / MANIFEST_RELATIVE_PATH
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest_path


def _validate_existing_distribution_manifest(
    distribution_root: Path,
    manifest_path: Path,
    component_lock: dict[str, Any],
    package_manifest: dict[str, Any],
    *,
    toc_path: Path | None = None,
    expected_ownership: dict[str, str] | None = None,
    toc_entries: list[dict[str, str]] | None = None,
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
            errors.append(
                f"Unclassified packaged file: {entry['path']} "
                f"(TOC type {entry['type']})"
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
            )
        )
    elif not write_manifest:
        errors.append(
            "Distribution manifest is missing, empty, or not a regular file."
        )

    if write_manifest and not errors and package_manifest is not None:
        _write_distribution_manifest(
            distribution_root,
            lock,
            toc_path,
            toc_entries,
            ownership,
            package_manifest,
        )
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("distribution_root", type=Path)
    parser.add_argument("--toc", type=Path)
    parser.add_argument("--write-manifest", action="store_true")
    parser.add_argument("--release", action="store_true")
    args = parser.parse_args()
    if args.write_manifest and args.toc is None:
        parser.error("--write-manifest requires --toc")
    errors = validate_distribution(
        args.distribution_root,
        toc_path=args.toc,
        write_manifest=args.write_manifest,
        release=args.release,
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
                    "including incomplete runtime source coverage, unverified "
                    "PyQt6-Qt6 wheel build provenance, and runtime downloads."
                )
        except (OSError, json.JSONDecodeError, KeyError, ValueError):
            pass
    print(f"License compliance check passed: {args.distribution_root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
