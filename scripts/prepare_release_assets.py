"""Create verified, immutable release source and license assets."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import urllib.request
import zipfile
from collections.abc import Callable, Iterable
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO

from scripts.collect_licenses import COMPONENTS_FILE

MAX_GITHUB_ASSET_SIZE = 2_000_000_000
TARGET_SOURCE_PART_SIZE = 1_500_000_000
COPY_BUFFER_SIZE = 1024 * 1024
VERSION_PATTERN = re.compile(r"\d+\.\d+\.\d+(?:\.\d+)?")
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
COMMIT_PATTERN = re.compile(r"[0-9a-f]{40,64}")
WINDOWS_DRIVE_PATTERN = re.compile(r"^[A-Za-z]:")
WINDOWS_INVALID_CHARS = re.compile(r'[<>:"|?*\x00-\x1f]')
WINDOWS_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{number}" for number in range(1, 10)}
    | {f"LPT{number}" for number in range(1, 10)}
)
LICENSE_ROOT_FILES = (
    "LICENSE",
    "QT_RELINKING.md",
    "SOURCE_OFFER.md",
    "THIRD_PARTY_NOTICES.md",
    "VERSION",
)


class ReleaseAssetError(RuntimeError):
    """Release inputs are incomplete or cannot be verified."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(COPY_BUFFER_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_path_component(part: str, *, label: str) -> None:
    if (
        not part
        or part in {".", ".."}
        or part.endswith((" ", "."))
        or WINDOWS_INVALID_CHARS.search(part)
        or part.split(".", 1)[0].upper() in WINDOWS_RESERVED_NAMES
    ):
        raise ReleaseAssetError(f"Unsafe {label}: {part!r}")


def _safe_filename(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ReleaseAssetError("Source archive filename is missing.")
    normalized = value.replace("\\", "/")
    path = PurePosixPath(normalized)
    if path.is_absolute() or len(path.parts) != 1 or path.name in {".", ".."}:
        raise ReleaseAssetError(f"Unsafe source archive filename: {value}")
    _safe_path_component(path.name, label="source archive filename")
    return path.name


def _safe_archive_member(value: object, *, label: str) -> PurePosixPath:
    if not isinstance(value, str) or not value:
        raise ReleaseAssetError(f"Unsafe entry in {label}: {value!r}")
    normalized = value.replace("\\", "/")
    if normalized.endswith("/"):
        normalized = normalized[:-1]
    if not normalized or normalized.startswith("/") or WINDOWS_DRIVE_PATTERN.match(normalized):
        raise ReleaseAssetError(f"Unsafe entry in {label}: {value}")
    parts = normalized.split("/")
    for part in parts:
        _safe_path_component(part, label=f"entry in {label}")
    return PurePosixPath(*parts)


def _path_is_link_or_reparse(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ReleaseAssetError(f"Cannot inspect release input {path}: {exc}") from exc
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(metadata.st_mode) or bool(getattr(metadata, "st_file_attributes", 0) & reparse_flag)


def _require_regular_file(path: Path, *, label: str) -> None:
    if _path_is_link_or_reparse(path) or not path.is_file():
        raise ReleaseAssetError(f"{label} must be a regular file: {path}")


def _require_directory(path: Path, *, label: str) -> None:
    if _path_is_link_or_reparse(path) or not path.is_dir():
        raise ReleaseAssetError(f"{label} must be a regular directory: {path}")


def _reject_link_target(path: Path, *, label: str) -> None:
    if os.path.lexists(path) and _path_is_link_or_reparse(path):
        raise ReleaseAssetError(f"{label} target must not be a link: {path}")


def _load_lock(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReleaseAssetError(f"Cannot read component lock: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ReleaseAssetError("Unsupported component lock schema.")
    return payload


def _component_entries(lock: dict[str, Any]) -> list[dict[str, Any]]:
    result = [lock["python"]]
    result.extend(lock.get("runtime_components", []))
    result.extend(lock.get("build_components", []))
    return result


def _completed_review(component: dict[str, Any], field: str) -> bool:
    review = component.get(field)
    return bool(
        isinstance(review, dict)
        and review.get("review_completed") is True
        and all(
            isinstance(review.get(required), str) and review[required].strip()
            for required in ("evidence", "scope", "reviewer", "date")
        )
    )


def _source_exception_reviewed(component: dict[str, Any]) -> bool:
    legacy_exception = bool(
        component.get("source_archive_exception_reviewed") is True
        and isinstance(component.get("source_archive_exception_reason"), str)
        and component["source_archive_exception_reason"].strip()
        and _completed_review(component, "source_archive_exception_review")
    )
    structured_exception = component.get("source_exception")
    return legacy_exception or bool(
        component.get("corresponding_source_required") is False
        and isinstance(structured_exception, dict)
        and isinstance(structured_exception.get("kind"), str)
        and structured_exception["kind"].strip()
        and _completed_review(component, "source_exception")
    )


def release_gate_errors(lock: dict[str, Any]) -> list[str]:
    errors = []
    for component in [
        *_component_entries(lock),
        *lock.get("runtime_downloads", []),
    ]:
        if component.get("release_legal_review_required"):
            errors.append(
                f"{component['component']}: {component.get('release_gate_reason', 'expert legal review is required')}"
            )
    source_required_components = [
        *lock.get("runtime_components", []),
        *[component for component in lock.get("build_components", []) if component.get("packaged_in_distribution")],
    ]
    for component in source_required_components:
        archives = component.get("source_archives")
        exception_reviewed = _source_exception_reviewed(component)
        if not archives and not exception_reviewed:
            errors.append(f"{component['component']}: no verified exact source archive is locked")
        license_expression = str(component.get("license", "")).casefold()
        if (
            "bundled component licenses" in license_expression
            and component.get("vendored_source_coverage_verified") is not True
            and component.get("native_source_coverage_verified") is not True
        ):
            errors.append(
                f"{component['component']}: source coverage for wheel-vendored native components is not verified"
            )
        if (
            component.get("source_status") is not None
            and component.get("source_status") != "verified_corresponding_source"
        ):
            errors.append(f"{component['component']}: source_status is not verified_corresponding_source")
        for provenance_field in (
            "wheel_build_provenance_verified",
            "build_provenance_verified",
            "native_source_coverage_verified",
        ):
            if component.get(provenance_field) is not None and component.get(provenance_field) is not True:
                errors.append(f"{component['component']}: {provenance_field} is not verified")
        qt_notices_verified = component.get(
            "qt_plugin_third_party_notices_verified",
            component.get("qt_third_party_notices_verified"),
        )
        if component.get("component") == "qt" and qt_notices_verified is not True:
            errors.append("qt: Qt third-party notices are not verified")
    for component in lock.get("runtime_downloads", []):
        if not _completed_review(component, "legal_review"):
            errors.append(f"{component['component']}: runtime download legal review evidence is incomplete")
    return errors


def assert_release_gates_closed(components_file: Path = COMPONENTS_FILE) -> None:
    gates = release_gate_errors(_load_lock(components_file))
    if gates:
        raise ReleaseAssetError("Release legal gates remain: " + " | ".join(gates))


def source_archive_records(lock: dict[str, Any]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    records_by_filename: dict[str, dict[str, Any]] = {}
    source_required_ids = {
        id(component)
        for component in [
            *lock.get("runtime_components", []),
            *[item for item in lock.get("build_components", []) if item.get("packaged_in_distribution")],
        ]
    }
    for component in _component_entries(lock):
        archives = component.get("source_archives", [])
        if id(component) in source_required_ids and not archives:
            exception_reviewed = _source_exception_reviewed(component)
            if not exception_reviewed:
                raise ReleaseAssetError(
                    f"Runtime component has no verified exact source archive: {component['component']}"
                )
        for source in archives:
            if not isinstance(source, dict):
                raise ReleaseAssetError(f"Invalid source archive for {component['component']}.")
            filename = _safe_filename(source.get("filename"))
            collision_key = filename.casefold()
            url = str(source.get("url", ""))
            sha256 = str(source.get("sha256", "")).casefold()
            size = source.get("size")
            if not url.startswith("https://"):
                raise ReleaseAssetError(f"Source URL must use HTTPS: {url}")
            if not SHA256_PATTERN.fullmatch(sha256):
                raise ReleaseAssetError(f"Invalid source SHA256 for {filename}.")
            if not isinstance(size, int) or isinstance(size, bool) or size <= 0 or size >= MAX_GITHUB_ASSET_SIZE:
                raise ReleaseAssetError(f"Source archive requires a declared size below 2 GiB: {filename}.")
            component_reference = {
                "component": str(component["component"]),
                "version": str(component.get("version") or component.get("release_version")),
                "license": str(component["license"]),
                "source_status": str(component.get("source_status", "declared_component_source")),
            }
            previous = records_by_filename.get(collision_key)
            if previous is not None:
                if (url, sha256, size) != (
                    previous["url"],
                    previous["sha256"],
                    previous["size"],
                ):
                    raise ReleaseAssetError(
                        "Duplicate source archive filename has conflicting metadata: "
                        f"{previous['filename']} / {filename}"
                    )
                previous["component_references"].append(component_reference)
                continue
            record = {
                **component_reference,
                "component_references": [component_reference],
                "filename": filename,
                "url": url,
                "sha256": sha256,
                "size": size,
            }
            records_by_filename[collision_key] = record
            records.append(record)
    if not records:
        raise ReleaseAssetError("Component lock contains no source archives.")
    return records


def _download(
    url: str,
    target: Path,
    expected_size: int,
    opener: Callable[..., BinaryIO] = urllib.request.urlopen,
) -> None:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "LoLReplayTool-release-compliance/1"},
    )
    total = 0
    with opener(request, timeout=120) as response, target.open("xb") as output:
        while True:
            chunk = response.read(COPY_BUFFER_SIZE)
            if not chunk:
                break
            total += len(chunk)
            if total > expected_size:
                raise ReleaseAssetError(
                    f"Downloaded source exceeds declared size for {target.name}: more than {expected_size} bytes"
                )
            if total >= MAX_GITHUB_ASSET_SIZE:
                raise ReleaseAssetError(f"Downloaded source exceeds GitHub asset limit: {target.name}")
            output.write(chunk)
    if total != expected_size:
        raise ReleaseAssetError(f"Downloaded source size mismatch for {target.name}: {total} != {expected_size}")


def fetch_verified_sources(
    records: Iterable[dict[str, Any]],
    cache_dir: Path,
    *,
    opener: Callable[..., BinaryIO] = urllib.request.urlopen,
) -> list[tuple[dict[str, Any], Path]]:
    cache_dir.mkdir(parents=True, exist_ok=True)
    _require_directory(cache_dir, label="Source cache directory")
    result = []
    for record in records:
        target = cache_dir / str(record["filename"])
        expected_size = record["size"]
        if os.path.lexists(target) and _path_is_link_or_reparse(target):
            raise ReleaseAssetError(f"Source cache entry must not be a link: {target}")
        if not target.is_file():
            partial = target.with_suffix(target.suffix + ".partial")
            if os.path.lexists(partial):
                if _path_is_link_or_reparse(partial):
                    raise ReleaseAssetError(f"Partial source cache entry must not be a link: {partial}")
                partial.unlink()
            try:
                _download(str(record["url"]), partial, expected_size, opener)
                partial.replace(target)
            finally:
                if os.path.lexists(partial):
                    partial.unlink()
        _require_regular_file(target, label="Source cache entry")
        if target.stat().st_size != expected_size:
            raise ReleaseAssetError(
                f"Source size mismatch for {target.name}: {target.stat().st_size} != {expected_size}"
            )
        actual_hash = sha256_file(target)
        if actual_hash != record["sha256"]:
            raise ReleaseAssetError(f"Source SHA256 mismatch for {target.name}: {actual_hash}")
        copied_record = dict(record)
        copied_record["size"] = target.stat().st_size
        result.append((copied_record, target))
    return result


def partition_sources(
    sources: Iterable[tuple[dict[str, Any], Path]],
    target_size: int = TARGET_SOURCE_PART_SIZE,
) -> list[list[tuple[dict[str, Any], Path]]]:
    if target_size <= 0 or target_size >= MAX_GITHUB_ASSET_SIZE:
        raise ReleaseAssetError("Invalid source archive part target size.")
    parts: list[list[tuple[dict[str, Any], Path]]] = []
    current: list[tuple[dict[str, Any], Path]] = []
    current_size = 0
    for item in sources:
        size = item[1].stat().st_size
        if size >= MAX_GITHUB_ASSET_SIZE:
            raise ReleaseAssetError(f"Source archive exceeds GitHub asset limit: {item[1].name}")
        if current and current_size + size > target_size:
            parts.append(current)
            current = []
            current_size = 0
        current.append(item)
        current_size += size
    if current:
        parts.append(current)
    return parts


def _zip_special_entry(info: zipfile.ZipInfo) -> bool:
    mode = (info.external_attr >> 16) & 0xFFFF
    file_type = stat.S_IFMT(mode)
    windows_attributes = info.external_attr & 0xFFFF
    return bool(file_type not in {0, stat.S_IFREG, stat.S_IFDIR} or windows_attributes & 0x400)


def _validated_zip_members(
    archive: zipfile.ZipFile,
    *,
    label: str,
) -> dict[str, zipfile.ZipInfo]:
    bad_member = archive.testzip()
    if bad_member is not None:
        raise ReleaseAssetError(f"CRC failure in {label}: {bad_member}")
    members: dict[str, zipfile.ZipInfo] = {}
    original_names: dict[str, str] = {}
    for info in archive.infolist():
        relative = _safe_archive_member(info.filename, label=label)
        if _zip_special_entry(info):
            raise ReleaseAssetError(f"Unsafe special entry in {label}: {info.filename}")
        name = relative.as_posix()
        collision_key = name.casefold()
        previous = original_names.get(collision_key)
        if previous is not None:
            raise ReleaseAssetError(f"Duplicate or case-insensitive collision in {label}: {previous} / {name}")
        original_names[collision_key] = name
        members[name] = info
    return members


def _hash_zip_member(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with archive.open(info) as source:
        for chunk in iter(lambda: source.read(COPY_BUFFER_SIZE), b""):
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


def validate_application_source(source_zip: Path, version: str) -> None:
    _require_regular_file(source_zip, label="Application source archive")
    if source_zip.stat().st_size >= MAX_GITHUB_ASSET_SIZE:
        raise ReleaseAssetError("Application source archive exceeds GitHub asset limit.")
    try:
        with zipfile.ZipFile(source_zip) as archive:
            names = _validated_zip_members(
                archive,
                label="application source archive",
            )
            folded_names = {name.casefold(): name for name in names}
            for required in ("LICENSE", "VERSION"):
                if required.casefold() not in folded_names:
                    raise ReleaseAssetError(f"Application source archive is missing {required}.")
            archived_version = archive.read(names[folded_names["version"]]).decode("utf-8").strip()
            if archived_version != version:
                raise ReleaseAssetError(f"Application source VERSION mismatch: {archived_version} != {version}")
    except (OSError, RuntimeError, zipfile.BadZipFile, UnicodeDecodeError) as exc:
        raise ReleaseAssetError(f"Cannot validate application source archive: {exc}") from exc


def create_source_parts(
    version: str,
    sources: list[tuple[dict[str, Any], Path]],
    output_dir: Path,
    runtime_downloads: list[dict[str, Any]],
) -> list[Path]:
    parts = partition_sources(sources)
    output_paths = []
    for part_number, part in enumerate(parts, start=1):
        path = output_dir / (f"LoLReplayTool-third-party-sources-{version}-{part_number:02d}.zip")
        _reject_link_target(path, label="Third-party source archive")
        index = {
            "schema_version": 1,
            "statement": (
                "Component source inventory. Entries marked as upstream references "
                "are not asserted to be exact build provenance."
            ),
            "sources": [record for record, _source_path in part],
            "runtime_downloads_not_bundled": [
                {
                    "component": item["component"],
                    "version": item["version"],
                    "bundled_in_installer": item["bundled_in_installer"],
                    "release_legal_review_required": item["release_legal_review_required"],
                }
                for item in runtime_downloads
            ],
        }
        with zipfile.ZipFile(
            path,
            "w",
            compression=zipfile.ZIP_STORED,
            allowZip64=True,
        ) as archive:
            archive.writestr(
                "SOURCE_INDEX.json",
                json.dumps(index, ensure_ascii=False, indent=2) + "\n",
            )
            for record, source_path in part:
                archive.write(source_path, f"sources/{record['filename']}")
        if path.stat().st_size >= MAX_GITHUB_ASSET_SIZE:
            path.unlink()
            raise ReleaseAssetError(f"Generated source part exceeds 2 GiB: {path.name}")
        verify_source_part(path)
        output_paths.append(path)
    return output_paths


def _load_zip_json(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    *,
    label: str,
) -> dict[str, Any]:
    try:
        payload = json.loads(archive.read(info).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RuntimeError) as exc:
        raise ReleaseAssetError(f"Invalid {label}: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ReleaseAssetError(f"Unsupported {label} schema.")
    return payload


def verify_source_part(path: Path) -> None:
    _require_regular_file(path, label="Third-party source archive")
    try:
        with zipfile.ZipFile(path) as archive:
            members = _validated_zip_members(
                archive,
                label=f"third-party source archive {path.name}",
            )
            index_info = members.get("SOURCE_INDEX.json")
            if index_info is None:
                raise ReleaseAssetError(f"{path.name} is missing SOURCE_INDEX.json.")
            index = _load_zip_json(
                archive,
                index_info,
                label=f"SOURCE_INDEX.json in {path.name}",
            )
            records = index.get("sources")
            if not isinstance(records, list) or not records:
                raise ReleaseAssetError(f"{path.name} has no indexed sources.")
            expected = {"SOURCE_INDEX.json"}
            seen: set[str] = set()
            for record in records:
                if not isinstance(record, dict):
                    raise ReleaseAssetError(f"Invalid source index record in {path.name}.")
                filename = _safe_filename(record.get("filename"))
                member_name = f"sources/{filename}"
                if member_name.casefold() in seen:
                    raise ReleaseAssetError(f"Duplicate source index record: {member_name}")
                seen.add(member_name.casefold())
                expected.add(member_name)
                info = members.get(member_name)
                if info is None:
                    raise ReleaseAssetError(f"Indexed source is missing: {member_name}")
                expected_size = record.get("size")
                expected_hash = str(record.get("sha256", "")).casefold()
                if (
                    not isinstance(expected_size, int)
                    or isinstance(expected_size, bool)
                    or expected_size <= 0
                    or not SHA256_PATTERN.fullmatch(expected_hash)
                ):
                    raise ReleaseAssetError(f"Invalid size or SHA256 in source index: {member_name}")
                actual_size, actual_hash = _hash_zip_member(archive, info)
                if actual_size != expected_size or actual_hash != expected_hash:
                    raise ReleaseAssetError(
                        f"Source index mismatch for {member_name}: size={actual_size}, sha256={actual_hash}"
                    )
            if set(members) != expected:
                unexpected = sorted(set(members) ^ expected)
                raise ReleaseAssetError(f"Source archive/index member mismatch in {path.name}: {unexpected}")
    except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
        if isinstance(exc, ReleaseAssetError):
            raise
        raise ReleaseAssetError(f"Cannot verify source archive {path}: {exc}") from exc


def _license_input_files(distribution_root: Path) -> list[tuple[Path, str]]:
    _require_directory(distribution_root, label="Distribution root")
    files: list[tuple[Path, str]] = []
    for filename in LICENSE_ROOT_FILES:
        source = distribution_root / filename
        _require_regular_file(source, label="License material")
        files.append((source, filename))
    licenses_dir = distribution_root / "licenses"
    _require_directory(licenses_dir, label="Packaged licenses directory")
    for current_root, directory_names, filenames in os.walk(
        licenses_dir,
        topdown=True,
        followlinks=False,
    ):
        root_path = Path(current_root)
        for directory_name in directory_names:
            _safe_path_component(directory_name, label="license directory name")
            directory = root_path / directory_name
            if _path_is_link_or_reparse(directory):
                raise ReleaseAssetError(f"Packaged licenses directory contains a link: {directory}")
        for filename in filenames:
            _safe_path_component(filename, label="license filename")
            source = root_path / filename
            _require_regular_file(source, label="License material")
            relative = source.relative_to(distribution_root).as_posix()
            _safe_archive_member(relative, label="license materials archive")
            files.append((source, relative))
    folded: dict[str, str] = {}
    for _source, relative in files:
        previous = folded.get(relative.casefold())
        if previous is not None:
            raise ReleaseAssetError(f"Case-insensitive license material collision: {previous} / {relative}")
        folded[relative.casefold()] = relative
    return sorted(files, key=lambda item: item[1].casefold())


def create_license_archive(
    version: str,
    distribution_root: Path,
    output_dir: Path,
) -> Path:
    path = output_dir / f"LoLReplayTool-license-materials-{version}.zip"
    _reject_link_target(path, label="License materials archive")
    files = _license_input_files(distribution_root)
    index = {
        "schema_version": 1,
        "files": [
            {
                "path": relative,
                "size": source.stat().st_size,
                "sha256": sha256_file(source),
            }
            for source, relative in files
        ],
    }
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "LICENSE_INDEX.json",
            json.dumps(index, ensure_ascii=False, indent=2) + "\n",
        )
        for source, relative in files:
            archive.write(source, relative)
    if path.stat().st_size >= MAX_GITHUB_ASSET_SIZE:
        path.unlink()
        raise ReleaseAssetError("Generated license archive exceeds 2 GiB.")
    verify_license_archive(path)
    return path


def verify_license_archive(path: Path) -> None:
    _require_regular_file(path, label="License materials archive")
    try:
        with zipfile.ZipFile(path) as archive:
            members = _validated_zip_members(
                archive,
                label=f"license materials archive {path.name}",
            )
            index_info = members.get("LICENSE_INDEX.json")
            if index_info is None:
                raise ReleaseAssetError(f"{path.name} is missing LICENSE_INDEX.json.")
            index = _load_zip_json(
                archive,
                index_info,
                label=f"LICENSE_INDEX.json in {path.name}",
            )
            records = index.get("files")
            if not isinstance(records, list) or not records:
                raise ReleaseAssetError(f"{path.name} has no indexed license files.")
            expected = {"LICENSE_INDEX.json"}
            seen: set[str] = set()
            for record in records:
                if not isinstance(record, dict):
                    raise ReleaseAssetError(f"Invalid license index record in {path.name}.")
                member = _safe_archive_member(
                    record.get("path"),
                    label=f"LICENSE_INDEX.json in {path.name}",
                ).as_posix()
                if member.casefold() in seen:
                    raise ReleaseAssetError(f"Duplicate license index record: {member}")
                seen.add(member.casefold())
                expected.add(member)
                info = members.get(member)
                if info is None:
                    raise ReleaseAssetError(f"Indexed license file is missing: {member}")
                expected_size = record.get("size")
                expected_hash = str(record.get("sha256", "")).casefold()
                if (
                    not isinstance(expected_size, int)
                    or isinstance(expected_size, bool)
                    or expected_size <= 0
                    or not SHA256_PATTERN.fullmatch(expected_hash)
                ):
                    raise ReleaseAssetError(f"Invalid size or SHA256 in license index: {member}")
                actual_size, actual_hash = _hash_zip_member(archive, info)
                if actual_size != expected_size or actual_hash != expected_hash:
                    raise ReleaseAssetError(
                        f"License index mismatch for {member}: size={actual_size}, sha256={actual_hash}"
                    )
            if set(members) != expected:
                unexpected = sorted(set(members) ^ expected)
                raise ReleaseAssetError(f"License archive/index member mismatch in {path.name}: {unexpected}")
    except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
        if isinstance(exc, ReleaseAssetError):
            raise
        raise ReleaseAssetError(f"Cannot verify license archive {path}: {exc}") from exc


def parse_sha256sums(path: Path) -> dict[str, str]:
    _require_regular_file(path, label="SHA256SUMS")
    try:
        lines = path.read_text(encoding="ascii").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise ReleaseAssetError(f"Cannot read SHA256SUMS: {exc}") from exc
    if not lines:
        raise ReleaseAssetError("SHA256SUMS is empty.")
    records: dict[str, str] = {}
    original_names: dict[str, str] = {}
    for line_number, line in enumerate(lines, start=1):
        match = re.fullmatch(r"([0-9a-f]{64})  (.+)", line)
        if match is None:
            raise ReleaseAssetError(f"Invalid SHA256SUMS line {line_number}.")
        digest, raw_name = match.groups()
        name = _safe_filename(raw_name)
        key = name.casefold()
        if key in original_names:
            raise ReleaseAssetError(f"Duplicate SHA256SUMS filename: {original_names[key]} / {name}")
        original_names[key] = name
        records[name] = digest
    return records


def _load_asset_list(path: Path) -> dict[str, Any]:
    _require_regular_file(path, label="Release asset list")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReleaseAssetError(f"Cannot read release asset list: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ReleaseAssetError("Unsupported release asset list schema.")
    return payload


def verify_release_asset_payload(
    payload: dict[str, Any],
    *,
    asset_dir: Path | None = None,
) -> None:
    version = payload.get("version")
    if not isinstance(version, str) or not VERSION_PATTERN.fullmatch(version):
        raise ReleaseAssetError("Release asset list contains an invalid version.")
    source_commit = payload.get("source_commit")
    if not isinstance(source_commit, str) or not COMMIT_PATTERN.fullmatch(source_commit):
        raise ReleaseAssetError("Release asset list contains an invalid source commit.")
    raw_assets = payload.get("assets")
    if not isinstance(raw_assets, list) or not raw_assets:
        raise ReleaseAssetError("Release asset list is empty.")
    assets: list[Path] = []
    asset_names: dict[str, str] = {}
    for raw_asset in raw_assets:
        if not isinstance(raw_asset, str) or not raw_asset:
            raise ReleaseAssetError("Release asset path is invalid.")
        asset = Path(raw_asset)
        if asset_dir is not None:
            asset = asset_dir / asset.name
        _require_regular_file(asset, label="Release asset")
        name = _safe_filename(asset.name)
        key = name.casefold()
        if key in asset_names:
            raise ReleaseAssetError(f"Duplicate release asset filename: {asset_names[key]} / {name}")
        if asset.stat().st_size >= MAX_GITHUB_ASSET_SIZE:
            raise ReleaseAssetError(f"Release asset exceeds 2 GiB: {name}")
        asset_names[key] = name
        assets.append(asset)

    checksum_raw = payload.get("sha256sums")
    if not isinstance(checksum_raw, str):
        raise ReleaseAssetError("Release asset list has no SHA256SUMS path.")
    checksum_path = Path(checksum_raw)
    if asset_dir is not None:
        checksum_path = asset_dir / checksum_path.name
    checksum_matches = [
        asset for asset in assets if asset.resolve() == checksum_path.resolve() and asset.name == "SHA256SUMS.txt"
    ]
    if len(checksum_matches) != 1:
        raise ReleaseAssetError("SHA256SUMS.txt must appear exactly once in the release asset list.")

    checksum_records = parse_sha256sums(checksum_path)
    expected_names = {asset.name for asset in assets if asset != checksum_matches[0]}
    if set(checksum_records) != expected_names:
        differences = sorted(set(checksum_records) ^ expected_names)
        raise ReleaseAssetError(f"SHA256SUMS/release asset set mismatch: {differences}")
    for asset in assets:
        if asset == checksum_matches[0]:
            continue
        actual_hash = sha256_file(asset)
        if checksum_records[asset.name] != actual_hash:
            raise ReleaseAssetError(f"SHA256SUMS mismatch for {asset.name}: {actual_hash}")

    expected_source = f"LoLReplayTool-source-{version}.zip"
    application_sources = [asset for asset in assets if asset.name == expected_source]
    if len(application_sources) != 1:
        raise ReleaseAssetError(f"Release assets must contain {expected_source}.")
    validate_application_source(application_sources[0], version)

    source_pattern = re.compile(rf"LoLReplayTool-third-party-sources-{re.escape(version)}-\d{{2}}\.zip")
    source_parts = [asset for asset in assets if source_pattern.fullmatch(asset.name)]
    if not source_parts:
        raise ReleaseAssetError("Release assets contain no third-party source archive.")
    for source_part in source_parts:
        verify_source_part(source_part)

    expected_license = f"LoLReplayTool-license-materials-{version}.zip"
    license_archives = [asset for asset in assets if asset.name == expected_license]
    if len(license_archives) != 1:
        raise ReleaseAssetError(f"Release assets must contain {expected_license}.")
    verify_license_archive(license_archives[0])


def verify_release_asset_list(
    path: Path,
    *,
    asset_dir: Path | None = None,
) -> dict[str, Any]:
    payload = _load_asset_list(path)
    verify_release_asset_payload(payload, asset_dir=asset_dir)
    return payload


def create_release_assets(
    *,
    version: str,
    installer: Path,
    application_source: Path,
    distribution_root: Path,
    output_dir: Path,
    source_commit: str,
    components_file: Path = COMPONENTS_FILE,
    cache_dir: Path | None = None,
    enforce_release_gates: bool = True,
    opener: Callable[..., BinaryIO] = urllib.request.urlopen,
) -> dict[str, Any]:
    if not VERSION_PATTERN.fullmatch(version):
        raise ReleaseAssetError(f"Invalid release version: {version}")
    source_commit = source_commit.casefold()
    if not COMMIT_PATTERN.fullmatch(source_commit):
        raise ReleaseAssetError(f"Invalid release source commit: {source_commit}")
    _require_regular_file(installer, label="Installer")
    _require_regular_file(application_source, label="Application source")
    expected_installer_name = f"LoLReplayTool-Setup-{version}.exe"
    if installer.name != expected_installer_name:
        raise ReleaseAssetError(f"Installer asset must be named {expected_installer_name}.")
    expected_source_name = f"LoLReplayTool-source-{version}.zip"
    if application_source.name != expected_source_name:
        raise ReleaseAssetError(f"Application source asset must be named {expected_source_name}.")
    validate_application_source(application_source, version)

    lock = _load_lock(components_file)
    gates = release_gate_errors(lock)
    if enforce_release_gates and gates:
        raise ReleaseAssetError("Release legal gates remain: " + " | ".join(gates))

    output_dir.mkdir(parents=True, exist_ok=True)
    _require_directory(output_dir, label="Release output directory")
    source_cache = cache_dir or (output_dir / ".source-cache")
    records = source_archive_records(lock)
    sources = fetch_verified_sources(records, source_cache, opener=opener)
    source_parts = create_source_parts(
        version,
        sources,
        output_dir,
        lock.get("runtime_downloads", []),
    )
    license_archive = create_license_archive(version, distribution_root, output_dir)
    assets = [installer, application_source, *source_parts, license_archive]
    checksum_path = output_dir / "SHA256SUMS.txt"
    _reject_link_target(checksum_path, label="SHA256SUMS")
    checksum_path.write_text(
        "".join(f"{sha256_file(path)}  {path.name}\n" for path in assets),
        encoding="ascii",
    )
    assets.append(checksum_path)
    payload = {
        "schema_version": 1,
        "version": version,
        "source_commit": source_commit,
        "assets": [str(path.resolve()) for path in assets],
        "sha256sums": str(checksum_path.resolve()),
    }
    verify_release_asset_payload(payload)
    asset_list_path = output_dir / "release-assets.json"
    _reject_link_target(asset_list_path, label="Release asset list")
    asset_list_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    payload["asset_list"] = str(asset_list_path.resolve())
    return payload


def _create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)

    gates = commands.add_parser(
        "check-gates",
        help="Fail unless every centralized release gate is closed.",
    )
    gates.add_argument("--components", type=Path, default=COMPONENTS_FILE)

    create = commands.add_parser("create", help="Create and verify release assets.")
    create.add_argument("--version", required=True)
    create.add_argument("--source-commit", required=True)
    create.add_argument("--installer", required=True, type=Path)
    create.add_argument("--application-source", required=True, type=Path)
    create.add_argument("--distribution-root", required=True, type=Path)
    create.add_argument("--output-dir", required=True, type=Path)
    create.add_argument("--components", type=Path, default=COMPONENTS_FILE)
    create.add_argument("--cache-dir", type=Path)
    create.add_argument(
        "--allow-open-legal-gates",
        action="store_true",
        help="Test/audit only; release workflow must never pass this option.",
    )

    verify = commands.add_parser("verify", help="Re-verify an immutable asset set.")
    verify.add_argument("--asset-list", required=True, type=Path)
    verify.add_argument(
        "--asset-dir",
        type=Path,
        help="Relocate every listed asset by filename into this directory.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _create_parser().parse_args(argv)
    try:
        if args.command == "check-gates":
            assert_release_gates_closed(args.components)
            print("All centralized release gates are closed.")
            return 0
        if args.command == "verify":
            verify_release_asset_list(args.asset_list, asset_dir=args.asset_dir)
            print(f"Release asset list verified: {args.asset_list}")
            return 0
        payload = create_release_assets(
            version=args.version,
            source_commit=args.source_commit,
            installer=args.installer,
            application_source=args.application_source,
            distribution_root=args.distribution_root,
            output_dir=args.output_dir,
            components_file=args.components,
            cache_dir=args.cache_dir,
            enforce_release_gates=not args.allow_open_legal_gates,
        )
    except ReleaseAssetError as exc:
        print(f"ERROR: {exc}")
        return 1
    print(f"Release asset list created: {payload['asset_list']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
