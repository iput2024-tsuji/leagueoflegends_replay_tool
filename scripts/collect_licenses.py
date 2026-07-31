"""Collect license files for the exact Python build environment."""

from __future__ import annotations

import argparse
import base64
import csv
import email.parser
import hashlib
import importlib
import json
import re
import shutil
import stat
import sys
import zipfile
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


def verified_wheel_record_inventory(
    wheel_path: Path,
    *,
    distribution: str,
    version: str,
) -> dict[str, Any]:
    """Verify a wheel's RECORD and return an exact member inventory."""
    if not is_safe_regular_file(wheel_path):
        raise RuntimeError(f"Wheel is missing or unsafe: {wheel_path}")
    try:
        with zipfile.ZipFile(wheel_path) as archive:
            members: dict[str, zipfile.ZipInfo] = {}
            seen: set[str] = set()
            for info in archive.infolist():
                relative = safe_relative_path(info.filename).as_posix()
                if relative != info.filename.replace("\\", "/"):
                    raise RuntimeError(f"Wheel member is not normalized: {info.filename}")
                folded = relative.casefold()
                if folded in seen:
                    raise RuntimeError(f"Wheel member collides by case: {relative}")
                seen.add(folded)
                if info.flag_bits & 0x1:
                    raise RuntimeError(f"Encrypted wheel member is forbidden: {relative}")
                mode = (info.external_attr >> 16) & 0o170000
                if mode == stat.S_IFLNK:
                    raise RuntimeError(f"Wheel symlink is forbidden: {relative}")
                members[relative] = info

            metadata_names = [
                name
                for name, info in members.items()
                if not info.is_dir()
                and len(PurePosixPath(name).parts) == 2
                and PurePosixPath(name).name == "METADATA"
                and PurePosixPath(name).parent.name.casefold().endswith(".dist-info")
            ]
            matching_metadata = []
            for metadata_name in metadata_names:
                message = email.parser.BytesParser().parsebytes(
                    archive.read(members[metadata_name])
                )
                if (
                    canonicalize_distribution_name(message.get("Name", ""))
                    == canonicalize_distribution_name(distribution)
                    and message.get("Version", "") == version
                ):
                    matching_metadata.append((metadata_name, message))
            if len(matching_metadata) != 1:
                raise RuntimeError("Wheel has no unique matching root METADATA.")
            metadata_name, message = matching_metadata[0]
            dist_info = PurePosixPath(metadata_name).parent
            wheel_name = (dist_info / "WHEEL").as_posix()
            wheel_info = members.get(wheel_name)
            if wheel_info is None or wheel_info.is_dir():
                raise RuntimeError("Wheel matching WHEEL file is missing.")
            record_name = (dist_info / "RECORD").as_posix()
            record_info = members.get(record_name)
            if record_info is None or record_info.is_dir():
                raise RuntimeError("Wheel RECORD is missing.")
            try:
                rows = list(
                    csv.reader(
                        archive.read(record_info)
                        .decode("utf-8", errors="strict")
                        .splitlines()
                    )
                )
            except (UnicodeError, csv.Error) as exc:
                raise RuntimeError(f"Wheel RECORD is invalid: {exc}") from exc
            recorded: set[str] = set()
            for row in rows:
                if len(row) != 3:
                    raise RuntimeError("Wheel RECORD row is invalid.")
                relative = safe_relative_path(row[0]).as_posix()
                folded = relative.casefold()
                if folded in recorded:
                    raise RuntimeError(f"Wheel RECORD path is duplicated: {relative}")
                recorded.add(folded)
                info = members.get(relative)
                if info is None or info.is_dir():
                    raise RuntimeError(f"Wheel RECORD member is missing: {relative}")
                if relative == record_name:
                    if row[1] or row[2]:
                        raise RuntimeError("Wheel RECORD self-entry must be unhashed.")
                    continue
                data = archive.read(info)
                encoded = base64.urlsafe_b64encode(
                    hashlib.sha256(data).digest()
                ).rstrip(b"=").decode("ascii")
                if row[1] != f"sha256={encoded}" or row[2] != str(len(data)):
                    raise RuntimeError(f"Wheel RECORD digest or size differs: {relative}")
            actual_files = {
                name.casefold() for name, info in members.items() if not info.is_dir()
            }
            if recorded != actual_files:
                raise RuntimeError("Wheel RECORD file set differs from archive.")
            artifacts = []
            for relative, info in members.items():
                if info.is_dir():
                    continue
                data = archive.read(info)
                artifacts.append(
                    {
                        "path": relative,
                        "size": len(data),
                        "sha256": hashlib.sha256(data).hexdigest(),
                    }
                )
    except (OSError, RuntimeError, zipfile.BadZipFile, KeyError) as exc:
        raise RuntimeError(f"Cannot verify wheel {wheel_path.name}: {exc}") from exc
    return {
        "distribution": distribution,
        "version": version,
        "record_path": record_name,
        "artifacts": sorted(artifacts, key=lambda item: str(item["path"]).casefold()),
    }


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


def is_meaningful_license_file(path: Path) -> bool:
    if not is_safe_regular_file(path) or path.stat().st_size < 64:
        return False
    try:
        normalized = " ".join(
            path.read_text(encoding="utf-8", errors="strict")
            .casefold()
            .split()
        )
    except (OSError, UnicodeError):
        return False
    return normalized not in {
        "license",
        "licence",
        "placeholder",
        "test license",
        "todo",
    }


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
    binary_archive: dict[str, Any] | None = None,
    verified_binary_components: set[str] | None = None,
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
        if not is_meaningful_license_file(source):
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
    result: dict[str, object] = {
        "component": component or canonicalize_distribution_name(canonical_name),
        "name": canonical_name,
        "version": distribution.version,
        "expected_license": expected_license or "UNKNOWN",
        "observed_license": observed_license,
        "homepage": distribution.metadata.get("Home-page") or "",
        "license_files": sorted(set(copied_files), key=str.casefold),
        "license_file_sha256": {
            relative: sha256_file(destination_root.parent / relative)
            for relative in sorted(set(copied_files), key=str.casefold)
        },
        "substantive_license_files": sorted(
            set(substantive_files),
            key=str.casefold,
        ),
    }
    if binary_archive is not None:
        result["binary_archive"] = {
            key: binary_archive[key]
            for key in ("filename", "sha256", "size")
        }
        result["binary_install_verified"] = bool(
            component and component in (verified_binary_components or set())
        )
    return result


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
            if is_meaningful_license_file(path)
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
            if is_meaningful_license_file(path)
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
        "license_file_sha256": {
            license_target.relative_to(destination_root.parent).as_posix(): sha256_file(
                license_target
            ),
            notices_target.relative_to(destination_root.parent).as_posix(): sha256_file(
                notices_target
            ),
        },
        "substantive_license_files": [
            license_target.relative_to(destination_root.parent).as_posix()
        ],
    }


def probe_python_native_runtime(component_lock: dict[str, Any]) -> dict[str, Any]:
    python_lock = component_lock["python"]
    version = sys.version.split()[0]
    profiles = python_lock.get("windows_native_runtime_profiles")
    if not isinstance(profiles, dict) or not isinstance(profiles.get(version), dict):
        raise RuntimeError(
            f"No verified Windows native runtime profile is locked for Python {version}."
        )
    profile = profiles[version]
    if profile.get("provenance_verified") is not True:
        raise RuntimeError(
            f"Python {version} native runtime profile is not verified."
        )
    runtime_source = profile.get("runtime_source")
    if runtime_source != "official_binary_archive":
        raise RuntimeError(
            f"Python {version} native runtime source is not the locked official "
            "binary archive."
        )
    raw_components = profile.get("components")
    if not isinstance(raw_components, list):
        raise RuntimeError("Python native runtime profile has no component list.")
    expected_components = {
        str(component["component"])
        for component in component_lock.get("runtime_components", [])
        if component.get("python_native_runtime_profile") is True
    }
    observed_components: set[str] = set()
    base_prefix = Path(sys.base_prefix)
    core_lock = profile.get("core_native_inventory")
    if not isinstance(core_lock, dict) or not isinstance(
        core_lock.get("artifacts"), list
    ):
        raise RuntimeError("Python core native runtime inventory is missing.")
    core_artifacts: list[dict[str, Any]] = []
    seen_core_paths: set[str] = set()
    for artifact in core_lock["artifacts"]:
        if not isinstance(artifact, dict):
            raise RuntimeError("Python core native runtime inventory is invalid.")
        raw_path = artifact.get("path")
        if not isinstance(raw_path, str) or not raw_path:
            raise RuntimeError("Python core native runtime path is missing.")
        relative = PurePosixPath(raw_path.replace("\\", "/"))
        if (
            relative.is_absolute()
            or relative.as_posix() != raw_path
            or any(part in {"", ".", ".."} for part in relative.parts)
            or not (
                len(relative.parts) == 1
                and relative.suffix.casefold() in {".dll", ".exe"}
                or len(relative.parts) == 2
                and relative.parts[0] == "DLLs"
                and relative.suffix.casefold() in {".dll", ".pyd"}
            )
        ):
            raise RuntimeError(f"Unsafe Python core native path: {raw_path}")
        collision_key = relative.as_posix().casefold()
        if collision_key in seen_core_paths:
            raise RuntimeError(f"Duplicate Python core native path: {raw_path}")
        seen_core_paths.add(collision_key)
        expected_size = artifact.get("size")
        expected_hash = artifact.get("sha256")
        if (
            not isinstance(expected_size, int)
            or isinstance(expected_size, bool)
            or expected_size <= 0
            or not isinstance(expected_hash, str)
            or re.fullmatch(r"[0-9a-f]{64}", expected_hash) is None
        ):
            raise RuntimeError(f"Invalid Python core native lock: {raw_path}")
        source = base_prefix.joinpath(*relative.parts)
        if not is_safe_regular_file(source):
            raise RuntimeError(f"Python core native runtime file is missing: {raw_path}")
        actual_hash = sha256_file(source)
        if source.stat().st_size != expected_size or actual_hash != expected_hash:
            raise RuntimeError(f"Python core native runtime differs: {raw_path}")
        core_artifacts.append(
            {"path": relative.as_posix(), "size": expected_size, "sha256": actual_hash}
        )

    def inventory_digest(records: list[dict[str, Any]]) -> str:
        digest = hashlib.sha256()
        for record in sorted(records, key=lambda item: str(item["path"])):
            digest.update(str(record["path"]).encode("utf-8"))
            digest.update(b"\0")
            digest.update(str(record["size"]).encode("ascii"))
            digest.update(b"\0")
            digest.update(str(record["sha256"]).encode("ascii"))
            digest.update(b"\n")
        return digest.hexdigest()

    if (
        core_lock.get("file_count") != len(core_artifacts)
        or core_lock.get("total_size")
        != sum(int(item["size"]) for item in core_artifacts)
        or core_lock.get("inventory_sha256") != inventory_digest(core_artifacts)
    ):
        raise RuntimeError("Python core native runtime inventory summary differs.")

    launcher_lock = profile.get("venv_launchers")
    if not isinstance(launcher_lock, list) or not launcher_lock:
        raise RuntimeError("Python venv launcher inventory is missing.")
    executable = Path(sys.executable)
    if not is_safe_regular_file(executable):
        raise RuntimeError("Active Python executable is missing or unsafe.")
    executable_record = {
        "size": executable.stat().st_size,
        "sha256": sha256_file(executable),
    }
    if Path(sys.prefix).resolve() != base_prefix.resolve():
        matching_launchers = [
            item
            for item in launcher_lock
            if isinstance(item, dict)
            and item.get("kind") == "console"
            and item.get("size") == executable_record["size"]
            and item.get("sha256") == executable_record["sha256"]
        ]
        if len(matching_launchers) != 1:
            raise RuntimeError("Active Python venv launcher differs from official lock.")
        executable_record["kind"] = "console-venv-launcher"
    else:
        python_executable = next(
            (
                item
                for item in core_artifacts
                if item["path"].casefold() == "python.exe"
            ),
            None,
        )
        if python_executable is None or any(
            executable_record[field] != python_executable[field]
            for field in ("size", "sha256")
        ):
            raise RuntimeError("Active base Python executable differs from official lock.")
        executable_record["kind"] = "base-python-executable"

    ensurepip_lock = profile.get("ensurepip_wheel")
    if not isinstance(ensurepip_lock, dict):
        raise RuntimeError("Python ensurepip wheel lock is missing.")
    ensurepip_relative = ensurepip_lock.get("relative_path")
    ensurepip_filename = ensurepip_lock.get("filename")
    ensurepip_distribution = ensurepip_lock.get("distribution")
    ensurepip_version = ensurepip_lock.get("version")
    ensurepip_size = ensurepip_lock.get("size")
    ensurepip_sha256 = ensurepip_lock.get("sha256")
    if (
        not isinstance(ensurepip_relative, str)
        or safe_relative_path(ensurepip_relative).as_posix() != ensurepip_relative
        or not ensurepip_relative.startswith("Lib/ensurepip/_bundled/")
        or PurePosixPath(ensurepip_relative).name != ensurepip_filename
        or ensurepip_distribution != "pip"
        or not isinstance(ensurepip_version, str)
        or not isinstance(ensurepip_size, int)
        or isinstance(ensurepip_size, bool)
        or ensurepip_size <= 0
        or not isinstance(ensurepip_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", ensurepip_sha256) is None
    ):
        raise RuntimeError("Python ensurepip wheel lock is invalid.")
    ensurepip_path = base_prefix.joinpath(*PurePosixPath(ensurepip_relative).parts)
    if (
        not is_safe_regular_file(ensurepip_path)
        or ensurepip_path.stat().st_size != ensurepip_size
        or sha256_file(ensurepip_path) != ensurepip_sha256
    ):
        raise RuntimeError("Python ensurepip wheel differs from official lock.")
    ensurepip_inventory = verified_wheel_record_inventory(
        ensurepip_path,
        distribution=ensurepip_distribution,
        version=ensurepip_version,
    )

    stdlib_lock = profile.get("stdlib_python_sources")
    if not isinstance(stdlib_lock, dict):
        raise RuntimeError("Python stdlib source inventory is missing.")
    excluded_prefixes = stdlib_lock.get("excluded_prefixes")
    if not isinstance(excluded_prefixes, list) or not all(
        isinstance(value, str)
        and value
        and value.endswith("/")
        and PurePosixPath(value[:-1]).as_posix() == value[:-1]
        and ".." not in PurePosixPath(value[:-1]).parts
        for value in excluded_prefixes
    ):
        raise RuntimeError("Python stdlib source exclusions are invalid.")
    folded_exclusions = tuple(value.casefold() for value in excluded_prefixes)
    stdlib_root = base_prefix / "Lib"
    if not stdlib_root.is_dir():
        raise RuntimeError("Python stdlib root is missing.")
    stdlib_artifacts: list[dict[str, Any]] = []
    for source in stdlib_root.rglob("*.py"):
        relative = source.relative_to(stdlib_root).as_posix()
        if relative.casefold().startswith(folded_exclusions):
            continue
        if not is_safe_regular_file(source):
            raise RuntimeError(f"Python stdlib source is unsafe: {relative}")
        stdlib_artifacts.append(
            {
                "path": relative,
                "size": source.stat().st_size,
                "sha256": sha256_file(source),
            }
        )
    if (
        stdlib_lock.get("file_count") != len(stdlib_artifacts)
        or stdlib_lock.get("total_size")
        != sum(int(item["size"]) for item in stdlib_artifacts)
        or stdlib_lock.get("inventory_sha256")
        != inventory_digest(stdlib_artifacts)
    ):
        raise RuntimeError("Python stdlib source inventory differs from official lock.")

    result_components = []
    for component in raw_components:
        if not isinstance(component, dict):
            raise RuntimeError("Python native runtime profile has an invalid component.")
        component_name = str(component.get("component", ""))
        if not component_name or component_name in observed_components:
            raise RuntimeError(
                f"Duplicate or unnamed Python native component: {component_name}"
            )
        observed_components.add(component_name)
        probes = []
        for probe in component.get("probes", []):
            if not isinstance(probe, dict):
                raise RuntimeError(f"Invalid runtime probe for {component_name}.")
            module_name = str(probe.get("module", ""))
            attribute = str(probe.get("attribute", ""))
            expected = probe.get("expected")
            if not module_name or not attribute or not isinstance(expected, str):
                raise RuntimeError(f"Incomplete runtime probe for {component_name}.")
            module = importlib.import_module(module_name)
            actual = str(getattr(module, attribute))
            if actual != expected:
                raise RuntimeError(
                    f"Python native runtime probe differs for {component_name} "
                    f"({module_name}.{attribute}): {actual} != {expected}"
                )
            probes.append(
                {
                    "module": module_name,
                    "attribute": attribute,
                    "value": actual,
                }
            )
        artifacts = []
        for artifact in component.get("artifacts", []):
            if not isinstance(artifact, dict):
                raise RuntimeError(f"Invalid runtime artifact for {component_name}.")
            filename = str(artifact.get("filename", ""))
            if safe_component_name(filename) != filename or Path(filename).name != filename:
                raise RuntimeError(
                    f"Unsafe Python native runtime artifact name: {filename}"
                )
            expected_hash = str(artifact.get("sha256", ""))
            expected_size = artifact.get("size")
            if not re.fullmatch(r"[0-9a-f]{64}", expected_hash) or not isinstance(
                expected_size, int
            ):
                raise RuntimeError(
                    f"Invalid Python native artifact lock for {component_name}: {filename}"
                )
            candidates = [base_prefix / "DLLs" / filename, base_prefix / filename]
            source = next((path for path in candidates if is_safe_regular_file(path)), None)
            if source is None:
                raise RuntimeError(
                    f"Python native runtime artifact is missing: {filename}"
                )
            actual_hash = sha256_file(source)
            if source.stat().st_size != expected_size or actual_hash != expected_hash:
                raise RuntimeError(
                    f"Python native runtime artifact differs for {component_name}: "
                    f"{filename}"
                )
            artifacts.append(
                {
                    "filename": filename,
                    "size": expected_size,
                    "sha256": actual_hash,
                }
            )
        if not probes and not artifacts:
            raise RuntimeError(
                f"Python native component has no probe or artifact: {component_name}"
            )
        result_components.append(
            {
                "component": component_name,
                "version": str(component.get("version", "")),
                "probes": probes,
                "artifacts": artifacts,
            }
        )
    if observed_components != expected_components:
        raise RuntimeError(
            "Python native runtime profile component set differs from component lock."
        )
    return {
        "python_version": version,
        "runtime_source": runtime_source,
        "official_binary_archive": profile.get("official_binary_archive"),
        "official_actions_archive": profile.get("official_actions_archive"),
        "official_installer": profile.get("official_installer"),
        "active_python_executable": executable_record,
        "ensurepip_wheel": {
            **ensurepip_lock,
            "record_path": ensurepip_inventory["record_path"],
            "artifacts": ensurepip_inventory["artifacts"],
        },
        "core_native_inventory": {
            "file_count": len(core_artifacts),
            "total_size": sum(int(item["size"]) for item in core_artifacts),
            "inventory_sha256": inventory_digest(core_artifacts),
            "artifacts": core_artifacts,
        },
        "stdlib_python_sources": {
            "excluded_prefixes": excluded_prefixes,
            "file_count": len(stdlib_artifacts),
            "total_size": sum(int(item["size"]) for item in stdlib_artifacts),
            "inventory_sha256": inventory_digest(stdlib_artifacts),
            "artifacts": sorted(
                stdlib_artifacts,
                key=lambda item: str(item["path"]),
            ),
        },
        "components": result_components,
    }


def _validated_build_provenance(
    provenance_path: Path | None,
    component_lock: dict[str, Any],
    components_file: Path,
    python_native_runtime: dict[str, Any],
    expected_sha256: str | None = None,
) -> tuple[dict[str, Any] | None, set[str]]:
    if provenance_path is None:
        return None, set()
    if not is_safe_regular_file(provenance_path) or provenance_path.stat().st_size == 0:
        raise RuntimeError(f"Build provenance is missing or unsafe: {provenance_path}")
    if expected_sha256 is not None and (
        re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None
        or sha256_file(provenance_path) != expected_sha256
    ):
        raise RuntimeError("Build provenance differs from the externally sealed SHA256.")
    try:
        payload = json.loads(provenance_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Cannot read build provenance: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise RuntimeError("Unsupported build provenance schema.")
    if payload.get("component_lock_sha256") != sha256_file(components_file):
        raise RuntimeError("Build provenance component lock hash differs.")
    if payload.get("python_version") != sys.version.split()[0]:
        raise RuntimeError("Build provenance Python version differs.")
    policy = component_lock.get("release_binary_policy", {})
    if payload.get("python_implementation") != "cpython":
        raise RuntimeError("Build provenance Python implementation differs.")
    if payload.get("platform") != policy.get("platform"):
        raise RuntimeError("Build provenance platform differs from release policy.")
    if payload.get("pip_version") != policy.get("pip_version"):
        raise RuntimeError("Build provenance pip version differs from release policy.")
    if payload.get("python_native_runtime") != python_native_runtime:
        raise RuntimeError("Build provenance Python runtime differs from verified runtime.")
    for field in ("requirements_set_sha256", "binary_manifest_sha256"):
        if not isinstance(payload.get(field), str) or re.fullmatch(
            r"[0-9a-f]{64}",
            payload[field],
        ) is None:
            raise RuntimeError(f"Build provenance {field} is invalid.")
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
        raise RuntimeError(f"Cannot verify Git source identity: {exc}") from exc
    if payload.get("git_source") != current_git_source:
        raise RuntimeError("Build provenance Git source identity differs after build.")

    build_requirements = REPO_ROOT / "requirements-dev.txt"
    pins, expected_inputs = flatten_exact_requirements(build_requirements)
    if payload.get("requirements_inputs") != expected_inputs:
        raise RuntimeError("Build provenance requirement inputs differ.")
    requirements_set = "".join(
        f"{item['canonical_name']}=={item['version']}\n"
        for item in sorted(pins, key=lambda item: item["canonical_name"])
    ).encode("utf-8")
    if payload.get("requirements_set_sha256") != hashlib.sha256(
        requirements_set
    ).hexdigest():
        raise RuntimeError("Build provenance requirement set differs.")
    expected_components = set(
        component_lock.get("release_binary_policy", {}).get(
            "required_components",
            [],
        )
    )
    records = payload.get("installed_binaries")
    if not isinstance(records, list):
        raise RuntimeError("Build provenance has no installed binary list.")
    try:
        observed_bootstrap = _verify_bootstrap_pip_environment(
            python_native_runtime
        )
    except (OSError, RuntimeError, ValueError) as exc:
        raise RuntimeError(f"Cannot verify bootstrap pip provenance: {exc}") from exc
    if payload.get("bootstrap_pip") != observed_bootstrap:
        raise RuntimeError("Build provenance bootstrap pip inventory differs.")
    try:
        allowed_environment_files = verify_recorded_install_inventory(
            "pip",
            observed_bootstrap,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        raise RuntimeError(f"Cannot verify recorded bootstrap pip files: {exc}") from exc
    observed: set[str] = set()
    locked = {
        str(component["component"]): component
        for component in [
            *component_lock.get("runtime_components", []),
            *component_lock.get("build_components", []),
        ]
    }
    for record in records:
        if not isinstance(record, dict):
            raise RuntimeError("Build provenance contains an invalid binary record.")
        component_name = str(record.get("component", ""))
        if component_name in observed or component_name not in expected_components:
            raise RuntimeError(
                f"Build provenance contains a duplicate or unexpected component: "
                f"{component_name}"
            )
        observed.add(component_name)
        component = locked.get(component_name)
        archive = component.get("binary_archive") if component else None
        if not isinstance(component, dict) or not isinstance(archive, dict):
            raise RuntimeError(
                f"Build provenance component is not locked: {component_name}"
            )
        for field in ("filename", "sha256", "size"):
            if record.get(field) != archive.get(field):
                raise RuntimeError(
                    f"Build provenance differs from binary lock for "
                    f"{component_name}.{field}."
                )
        if record.get("version") != component.get("version"):
            raise RuntimeError(
                f"Build provenance version differs for {component_name}."
            )
        try:
            owned = verify_recorded_install_inventory(
                str(component["distribution"]),
                record,
            )
        except (OSError, RuntimeError, metadata.PackageNotFoundError, ValueError) as exc:
            raise RuntimeError(
                f"Cannot verify installed files for {component_name}: {exc}"
            ) from exc
        allowed_environment_files.update(owned)
    if observed != expected_components:
        raise RuntimeError("Build provenance binary component set differs from policy.")
    try:
        _verify_environment_file_ownership(allowed_environment_files)
    except (OSError, RuntimeError, ValueError) as exc:
        raise RuntimeError(f"Build environment ownership verification failed: {exc}") from exc
    return payload, observed


def collect_licenses(
    destination: Path,
    requirements_file: Path = RUNTIME_REQUIREMENTS,
    components_file: Path = COMPONENTS_FILE,
    build_provenance: Path | None = None,
    build_provenance_sha256: str | None = None,
) -> Path:
    component_lock = _load_component_lock(components_file)
    runtime_components = validate_runtime_lock(requirements_file, component_lock)
    locked = _locked_distributions(component_lock)
    python_native_runtime = probe_python_native_runtime(component_lock)
    _provenance, verified_binary_components = _validated_build_provenance(
        build_provenance,
        component_lock,
        components_file,
        python_native_runtime,
        build_provenance_sha256,
    )

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

    provenance_target = destination / "build-provenance.json"
    provenance_hash: str | None = None
    if build_provenance is not None:
        _reserve_target(provenance_target, seen_targets)
        shutil.copy2(build_provenance, provenance_target)
        provenance_hash = sha256_file(provenance_target)
        if (
            build_provenance_sha256 is not None
            and provenance_hash != build_provenance_sha256
        ):
            raise RuntimeError(
                "Packaged build provenance differs from the externally sealed SHA256."
            )

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
                binary_archive=component.get("binary_archive"),
                verified_binary_components=verified_binary_components,
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
                binary_archive=locked_component.get("binary_archive"),
                verified_binary_components=verified_binary_components,
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
        "python_native_runtime": python_native_runtime,
        "build_provenance_sha256": provenance_hash,
        "verified_binary_components": sorted(verified_binary_components),
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
    parser.add_argument("--build-provenance", type=Path)
    parser.add_argument("--build-provenance-sha256")
    args = parser.parse_args()
    try:
        manifest_path = collect_licenses(
            args.destination,
            args.requirements,
            args.components,
            args.build_provenance,
            args.build_provenance_sha256,
        )
    except (metadata.PackageNotFoundError, RuntimeError) as exc:
        print(f"ERROR: {exc}")
        return 1
    print(f"License manifest created: {manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
