"""Validate and inventory the files in a packaged application."""

from __future__ import annotations

import argparse
import ast
import fnmatch
import hashlib
import json
import os
import re
import sys
from importlib import metadata
from pathlib import Path, PurePosixPath
from typing import Any

from scripts.collect_licenses import (
    COMPONENTS_FILE,
    RUNTIME_REQUIREMENTS,
    canonicalize_distribution_name,
    parse_requirement_pins,
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
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        return None
    return relative.as_posix()


def _within(root: Path, candidate: Path) -> bool:
    try:
        candidate.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    return True


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

    packages = payload.get("packages")
    if not isinstance(packages, list):
        return ["License manifest does not contain a package list."]

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
    errors: list[str] = []
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

        license_files = package.get("license_files")
        if not isinstance(license_files, list) or not license_files:
            errors.append(f"No license file was collected for package: {package_name}")
            continue
        substantive = False
        for value in license_files:
            relative_path = _safe_relative(value)
            if relative_path is None:
                errors.append(f"Unsafe license path for {package_name}: {value}")
                continue
            collision_key = relative_path.casefold()
            previous = seen_paths.get(collision_key)
            if previous is not None and previous != relative_path:
                errors.append(
                    f"Case-insensitive license path collision: {previous} / {relative_path}"
                )
            seen_paths[collision_key] = relative_path
            target = manifest_path.parent / Path(*PurePosixPath(relative_path).parts)
            if not _within(manifest_path.parent, target) or not target.is_file():
                errors.append(
                    f"Collected license file is missing for {package_name}: {value}"
                )
                continue
            filename = target.name.casefold()
            if not filename.startswith("authors"):
                substantive = True
        if not substantive:
            errors.append(
                f"AUTHORS-only material is not a license for package: {package_name}"
            )

        expected_component = expected.get(canonical)
        if expected_component is not None:
            expected_version = str(
                expected_component.get("release_version")
                or expected_component.get("version")
            )
            if str(package.get("version")) != expected_version and canonical != "python":
                errors.append(
                    f"Package version differs from component lock for {package_name}: "
                    f"{package.get('version')} != {expected_version}"
                )
            if package.get("expected_license") != expected_component.get("license"):
                errors.append(
                    f"Expected license differs from component lock for {package_name}."
                )

    required = (
        set(expected)
        if expected
        else {canonicalize_distribution_name(name) for name in REQUIRED_PACKAGE_LICENSES}
    )
    for canonical in sorted(required - set(by_name)):
        errors.append(f"Required package is missing from license manifest: {canonical}")

    if lock is not None:
        actual_lock_hash = sha256_file(manifest_path.parent / "components.json")
        if payload.get("component_lock_sha256") != actual_lock_hash:
            errors.append("License manifest component lock SHA256 does not match.")
        try:
            expected_requirements_hash = sha256_file(RUNTIME_REQUIREMENTS)
        except OSError as exc:
            errors.append(f"Cannot hash runtime requirements: {exc}")
        else:
            if payload.get("requirements_sha256") != expected_requirements_hash:
                errors.append("License manifest requirements SHA256 does not match.")
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


def _classify_toc_entry(
    entry: dict[str, str],
    source_owners: dict[str, str],
) -> str | None:
    final_path = entry["path"]
    source = Path(entry["source"])
    final_lower = final_path.casefold()
    source_lower = source.name.casefold()

    if re.fullmatch(
        r"_internal/cv2/opencv_videoio_ffmpeg[^/]*\.dll",
        final_lower,
    ):
        if source_owners.get(_path_key(source)) != "opencv-python":
            return None
        return "opencv-ffmpeg"

    owner = source_owners.get(_path_key(source))
    if owner is not None:
        return owner

    if source_lower in {"libcrypto-3.dll", "libssl-3.dll"} and _is_relative_to(
        source, Path(sys.base_prefix)
    ):
        return "python-openssl"
    if (
        source_lower.startswith("libffi-")
        or source_lower in {"sqlite3.dll", "vcomp140.dll"}
    ) and (
        _is_relative_to(source, Path(sys.base_prefix))
        or _is_relative_to(source, Path(os.environ.get("SystemRoot", r"C:\Windows")))
    ):
        return "python-runtime-support"
    if _is_relative_to(source, Path(sys.base_prefix)):
        return "python"

    repo_root = Path(__file__).resolve().parents[1]
    if _is_relative_to(source, repo_root):
        if entry["toc_name"] == "base_library.zip":
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
    result: dict[str, Path] = {}
    for path in distribution_root.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(distribution_root).as_posix()
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


def _generated_file(relative: str) -> bool:
    return relative in GENERATED_ROOT_DOCUMENTS or relative.startswith("licenses/")


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
        "pyinstaller_collect_toc_sha256": sha256_file(toc_path),
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
) -> list[str]:
    try:
        payload = _read_json(manifest_path)
        physical = _physical_files(distribution_root)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        return [f"Cannot validate distribution manifest: {exc}"]
    records = payload.get("files")
    if not isinstance(records, list):
        return ["Distribution manifest does not contain a file list."]

    errors: list[str] = []
    recorded: dict[str, str] = {}
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
        if sha256_file(path) != record.get("sha256"):
            errors.append(f"Manifest SHA256 differs for: {relative}")
        if not isinstance(record.get("component"), str) or not record["component"]:
            errors.append(f"Manifest component is missing for: {relative}")

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
        f"Required distribution document is missing: {relative_path}"
        for relative_path in validate_distribution_documents(distribution_root)
    ]
    lock_path = distribution_root / "licenses" / "components.json"
    package_manifest_path = distribution_root / "licenses" / "python-packages.json"
    try:
        lock = _read_json(lock_path)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        errors.append(f"Cannot read packaged component lock: {exc}")
        return errors

    if lock.get("schema_version") != 1:
        errors.append("Unsupported packaged component lock schema.")
    try:
        if sha256_file(lock_path) != sha256_file(COMPONENTS_FILE):
            errors.append("Packaged component lock differs from repository lock.")
    except OSError as exc:
        errors.append(f"Cannot compare component lock: {exc}")

    package_manifest: dict[str, Any] | None = None
    if package_manifest_path.is_file():
        errors.extend(validate_package_manifest(package_manifest_path, lock))
        try:
            package_manifest = _read_json(package_manifest_path)
        except (OSError, json.JSONDecodeError, ValueError):
            pass
    else:
        errors.append("Python package license manifest is missing.")

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

    if release:
        errors.extend(_release_gate_errors(lock, package_manifest))

    if toc_path is None:
        manifest_path = distribution_root / MANIFEST_RELATIVE_PATH
        if manifest_path.is_file():
            errors.extend(
                _validate_existing_distribution_manifest(
                    distribution_root,
                    manifest_path,
                )
            )
        else:
            errors.append("Distribution manifest is missing.")
        return errors

    try:
        toc_entries = parse_collect_toc(toc_path)
        physical = _physical_files(distribution_root)
        source_owners, ownership_errors = _distribution_source_owners(lock)
        errors.extend(ownership_errors)
        patterns = _artifact_patterns(lock)
    except (OSError, ValueError) as exc:
        errors.append(str(exc))
        return errors

    toc_by_path = {entry["path"].casefold(): entry for entry in toc_entries}
    for key, entry in toc_by_path.items():
        path = physical.get(key)
        if path is None:
            errors.append(f"TOC file is missing from final distribution: {entry['path']}")
        elif path.relative_to(distribution_root).as_posix() != entry["path"]:
            errors.append(f"TOC path casing differs from final distribution: {entry['path']}")

    for key, path in physical.items():
        relative = path.relative_to(distribution_root).as_posix()
        if not _generated_file(relative) and key not in toc_by_path:
            errors.append(f"Final distribution file is missing from TOC: {relative}")

    ownership: dict[str, str] = {}
    for key, entry in toc_by_path.items():
        if key not in physical:
            continue
        component = _classify_toc_entry(entry, source_owners)
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
