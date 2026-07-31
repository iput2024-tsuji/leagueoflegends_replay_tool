"""Collect license files for the exact Python build environment."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import stat
import sys
from importlib import metadata
from pathlib import Path, PurePosixPath
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_REQUIREMENTS = REPO_ROOT / "requirements.txt"
COMPONENTS_FILE = REPO_ROOT / "compliance" / "components.json"
PROJECT_DOCUMENTS = (
    "LICENSE",
    "THIRD_PARTY_NOTICES.md",
    "SOURCE_OFFER.md",
    "QT_RELINKING.md",
    "VERSION",
)
LICENSE_FILE_PREFIXES = ("license", "copying", "notice", "copyright")
INVALID_WINDOWS_CHARS = re.compile(r'[<>:"|?*\x00-\x1f]')
WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}
FILE_ATTRIBUTE_REPARSE_POINT = 0x400


def canonicalize_distribution_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).casefold()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_requirement_pins(requirements_file: Path) -> dict[str, tuple[str, str]]:
    """Return canonical name -> (spelling, exact version) for direct pins."""
    result: dict[str, tuple[str, str]] = {}
    for line_number, raw_line in enumerate(
        requirements_file.read_text(encoding="utf-8").splitlines(),
        start=1,
    ):
        line = raw_line.strip()
        if not line or line.startswith(("#", "-", "--")):
            continue
        match = re.fullmatch(r"([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s;]+)", line)
        if not match:
            raise RuntimeError(
                f"Runtime requirement must be an exact == pin "
                f"({requirements_file}:{line_number}): {line}"
            )
        name, version = match.groups()
        canonical = canonicalize_distribution_name(name)
        if canonical in result:
            raise RuntimeError(f"Duplicate runtime requirement: {name}")
        result[canonical] = (name, version)
    return result


def parse_requirement_names(requirements_file: Path) -> list[str]:
    return [name for name, _version in parse_requirement_pins(requirements_file).values()]


def is_license_file(relative_path: Path) -> bool:
    name = relative_path.name.casefold()
    if name.startswith("authors"):
        return False
    if any(name.startswith(prefix) for prefix in LICENSE_FILE_PREFIXES):
        return True
    parts = [part.casefold() for part in relative_path.parts]
    if "license_files" in parts:
        return relative_path.suffix.casefold() not in {".py", ".pyc", ".pyd"}
    if "licenses" in parts and any(part.endswith(".dist-info") for part in parts):
        return relative_path.suffix.casefold() not in {".py", ".pyc", ".pyd"}
    return False


def is_substantive_license_file(relative_path: Path) -> bool:
    """Return whether a file can serve as the package's actual license text."""
    name = relative_path.name.casefold()
    return name.startswith(("license", "copying"))


def is_safe_regular_file(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return False
    return (
        stat.S_ISREG(metadata.st_mode)
        and not stat.S_ISLNK(metadata.st_mode)
        and not (
            getattr(metadata, "st_file_attributes", 0)
            & FILE_ATTRIBUTE_REPARSE_POINT
        )
    )


def safe_component_name(value: str) -> str:
    result = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-") or "unknown"
    return "unknown" if result in {".", ".."} else result


def safe_relative_path(value: str | Path) -> Path:
    raw = str(value).replace("\\", "/")
    relative = PurePosixPath(raw)
    if (
        not raw
        or relative.is_absolute()
        or any(
            part in {"", ".", ".."}
            or part.endswith((" ", "."))
            or part.split(".", 1)[0].upper() in WINDOWS_RESERVED_NAMES
            for part in relative.parts
        )
        or any(INVALID_WINDOWS_CHARS.search(part) for part in relative.parts)
    ):
        raise RuntimeError(f"Unsafe distribution metadata path: {value}")
    return Path(*relative.parts)


def _reserve_target(target: Path, seen_targets: dict[str, Path]) -> None:
    collision_key = target.as_posix().casefold()
    previous = seen_targets.get(collision_key)
    if previous is not None and previous != target:
        raise RuntimeError(f"Case-insensitive license path collision: {previous} / {target}")
    seen_targets[collision_key] = target


def _load_component_lock(path: Path = COMPONENTS_FILE) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Cannot read component lock {path}: {exc}") from exc
    if payload.get("schema_version") != 1:
        raise RuntimeError("Unsupported component lock schema.")
    return payload


def _locked_distributions(
    component_lock: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for section in ("runtime_components", "build_components"):
        for component in component_lock.get(section, []):
            distribution_name = component.get("distribution")
            if not distribution_name:
                continue
            canonical = canonicalize_distribution_name(str(distribution_name))
            if canonical in result:
                raise RuntimeError(f"Duplicate locked distribution: {distribution_name}")
            result[canonical] = component
    return result


def validate_runtime_lock(
    requirements_file: Path,
    component_lock: dict[str, Any],
) -> list[dict[str, Any]]:
    pins = parse_requirement_pins(requirements_file)
    locked = {
        canonicalize_distribution_name(str(component["distribution"])): component
        for component in component_lock.get("runtime_components", [])
        if component.get("distribution")
    }
    missing_from_lock = sorted(set(pins) - set(locked))
    missing_from_requirements = sorted(set(locked) - set(pins))
    if missing_from_lock or missing_from_requirements:
        details = []
        if missing_from_lock:
            details.append(f"not locked: {', '.join(missing_from_lock)}")
        if missing_from_requirements:
            details.append(f"not pinned: {', '.join(missing_from_requirements)}")
        raise RuntimeError("requirements/component lock mismatch: " + "; ".join(details))

    components = []
    for canonical, (requirement_name, version) in pins.items():
        component = locked[canonical]
        if str(component.get("version")) != version:
            raise RuntimeError(
                f"Version mismatch for {requirement_name}: "
                f"requirements={version}, lock={component.get('version')}"
            )
        components.append(component)
    return components


def collect_distribution_licenses(
    distribution_name: str,
    destination_root: Path,
    *,
    expected_version: str | None = None,
    expected_license: str | None = None,
    component: str | None = None,
    seen_targets: dict[str, Path] | None = None,
) -> dict[str, object]:
    distribution = metadata.distribution(distribution_name)
    canonical_name = distribution.metadata.get("Name") or distribution_name
    if expected_version is not None and distribution.version != expected_version:
        raise RuntimeError(
            f"Installed version mismatch for {canonical_name}: "
            f"installed={distribution.version}, lock={expected_version}"
        )
    package_dir = destination_root / safe_component_name(canonical_name)
    copied_files: list[str] = []
    substantive_files: list[str] = []
    seen = seen_targets if seen_targets is not None else {}

    for metadata_path in distribution.files or ():
        raw_relative = Path(str(metadata_path))
        if not is_license_file(raw_relative):
            continue
        relative = safe_relative_path(str(metadata_path))
        source = Path(distribution.locate_file(metadata_path))
        if not is_safe_regular_file(source) or source.stat().st_size == 0:
            continue
        target = package_dir / relative
        _reserve_target(target, seen)
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists() or target.read_bytes() != source.read_bytes():
            shutil.copy2(source, target)
        relative_target = target.relative_to(destination_root.parent).as_posix()
        copied_files.append(relative_target)
        if is_substantive_license_file(relative):
            substantive_files.append(relative_target)

    if not substantive_files:
        raise RuntimeError(
            f"No non-empty LICENSE or COPYING file was found for {canonical_name}; "
            "NOTICE, COPYRIGHT, and AUTHORS alone are not sufficient."
        )

    observed_license = (
        distribution.metadata.get("License-Expression")
        or distribution.metadata.get("License")
        or "UNKNOWN"
    )
    return {
        "component": component or canonicalize_distribution_name(canonical_name),
        "name": canonical_name,
        "version": distribution.version,
        "expected_license": expected_license or "UNKNOWN",
        "observed_license": observed_license,
        "homepage": distribution.metadata.get("Home-page") or "",
        "license_files": sorted(set(copied_files), key=str.casefold),
        "substantive_license_files": sorted(
            set(substantive_files),
            key=str.casefold,
        ),
    }


def collect_python_runtime_license(
    destination_root: Path,
    *,
    expected_license: str = "PSF-2.0",
    seen_targets: dict[str, Path] | None = None,
) -> dict[str, object]:
    license_candidates = [
        Path(sys.base_prefix) / "LICENSE.txt",
        Path(sys.prefix) / "LICENSE.txt",
    ]
    license_source = next(
        (
            path
            for path in license_candidates
            if is_safe_regular_file(path) and path.stat().st_size > 0
        ),
        None,
    )
    if license_source is None:
        raise RuntimeError("Python runtime LICENSE.txt was not found.")

    notices_candidates = [
        Path(sys.base_prefix) / "Doc" / "html" / "license.html",
        Path(sys.prefix) / "Doc" / "html" / "license.html",
    ]
    notices_source = next(
        (
            path
            for path in notices_candidates
            if is_safe_regular_file(path) and path.stat().st_size > 0
        ),
        None,
    )
    if notices_source is None:
        raise RuntimeError("Python runtime third-party license page was not found.")

    package_dir = destination_root / "Python"
    package_dir.mkdir(parents=True, exist_ok=True)
    license_target = package_dir / "LICENSE.txt"
    notices_target = package_dir / "third-party-licenses.html"
    seen = seen_targets if seen_targets is not None else {}
    _reserve_target(license_target, seen)
    _reserve_target(notices_target, seen)
    shutil.copy2(license_source, license_target)
    shutil.copy2(notices_source, notices_target)
    return {
        "component": "python",
        "name": "Python",
        "version": sys.version.split()[0],
        "expected_license": expected_license,
        "observed_license": "PSF-2.0",
        "homepage": "https://www.python.org/",
        "license_files": [
            license_target.relative_to(destination_root.parent).as_posix(),
            notices_target.relative_to(destination_root.parent).as_posix(),
        ],
        "substantive_license_files": [
            license_target.relative_to(destination_root.parent).as_posix()
        ],
    }


def collect_licenses(
    destination: Path,
    requirements_file: Path = RUNTIME_REQUIREMENTS,
    components_file: Path = COMPONENTS_FILE,
) -> Path:
    component_lock = _load_component_lock(components_file)
    runtime_components = validate_runtime_lock(requirements_file, component_lock)
    locked = _locked_distributions(component_lock)

    destination.mkdir(parents=True, exist_ok=True)
    seen_targets: dict[str, Path] = {}
    for document in PROJECT_DOCUMENTS:
        source = REPO_ROOT / document
        if not is_safe_regular_file(source) or source.stat().st_size == 0:
            raise RuntimeError(f"Required project document is missing: {source}")
        target = destination.parent / document
        _reserve_target(target, seen_targets)
        shutil.copy2(source, target)

    components_target = destination / "components.json"
    _reserve_target(components_target, seen_targets)
    if not is_safe_regular_file(components_file) or components_file.stat().st_size == 0:
        raise RuntimeError(f"Component lock is not a non-empty regular file: {components_file}")
    shutil.copy2(components_file, components_target)

    package_root = destination / "python-packages"
    package_root.mkdir(parents=True, exist_ok=True)
    python_lock = component_lock["python"]
    manifest = [
        collect_python_runtime_license(
            package_root,
            expected_license=str(python_lock["license"]),
            seen_targets=seen_targets,
        )
    ]
    for component in runtime_components:
        manifest.append(
            collect_distribution_licenses(
                str(component["distribution"]),
                package_root,
                expected_version=str(component["version"]),
                expected_license=str(component["license"]),
                component=str(component["component"]),
                seen_targets=seen_targets,
            )
        )
    for build_component in component_lock.get("build_components", []):
        distribution_name = str(build_component["distribution"])
        locked_component = locked[canonicalize_distribution_name(distribution_name)]
        manifest.append(
            collect_distribution_licenses(
                distribution_name,
                package_root,
                expected_version=str(locked_component["version"]),
                expected_license=str(locked_component["license"]),
                component=str(locked_component["component"]),
                seen_targets=seen_targets,
            )
        )

    manifest_path = destination / "python-packages.json"
    payload = {
        "schema_version": 1,
        "statement": (
            "Technical inventory of the actual build environment; license texts "
            "and applicable law remain controlling."
        ),
        "generated_from": requirements_file.name,
        "requirements_sha256": sha256_file(requirements_file),
        "component_lock_sha256": sha256_file(components_file),
        "build_python_version": sys.version.split()[0],
        "release_python_version": str(python_lock["release_version"]),
        "packages": manifest,
    }
    manifest_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest_path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--destination", required=True, type=Path)
    parser.add_argument("--requirements", type=Path, default=RUNTIME_REQUIREMENTS)
    parser.add_argument("--components", type=Path, default=COMPONENTS_FILE)
    args = parser.parse_args()
    try:
        manifest_path = collect_licenses(
            args.destination,
            args.requirements,
            args.components,
        )
    except (metadata.PackageNotFoundError, RuntimeError) as exc:
        print(f"ERROR: {exc}")
        return 1
    print(f"License manifest created: {manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
