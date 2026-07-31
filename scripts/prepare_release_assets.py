"""Create verified, immutable release source and license assets."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
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


def _safe_filename(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ReleaseAssetError("Source archive filename is missing.")
    normalized = value.replace("\\", "/")
    path = PurePosixPath(normalized)
    if (
        path.is_absolute()
        or len(path.parts) != 1
        or path.name in {".", ".."}
        or re.search(r'[<>:"|?*\x00-\x1f]', path.name)
    ):
        raise ReleaseAssetError(f"Unsafe source archive filename: {value}")
    return path.name


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


def release_gate_errors(lock: dict[str, Any]) -> list[str]:
    errors = []
    for component in [
        *_component_entries(lock),
        *lock.get("runtime_downloads", []),
    ]:
        if component.get("release_legal_review_required"):
            errors.append(
                f"{component['component']}: "
                f"{component.get('release_gate_reason', 'expert legal review is required')}"
            )
    source_required_components = [
        *lock.get("runtime_components", []),
        *[
            component
            for component in lock.get("build_components", [])
            if component.get("packaged_in_distribution")
        ],
    ]
    for component in source_required_components:
        archives = component.get("source_archives")
        exception_reviewed = (
            component.get("source_archive_exception_reviewed") is True
            and bool(component.get("source_archive_exception_reason"))
        )
        if not archives and not exception_reviewed:
            errors.append(
                f"{component['component']}: no verified exact source archive is locked"
            )
        license_expression = str(component.get("license", "")).casefold()
        if (
            "bundled component licenses" in license_expression
            and component.get("vendored_source_coverage_verified") is not True
        ):
            errors.append(
                f"{component['component']}: source coverage for wheel-vendored "
                "native components is not verified"
            )
    return errors


def source_archive_records(lock: dict[str, Any]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    filenames: dict[str, str] = {}
    source_required_ids = {
        id(component)
        for component in [
            *lock.get("runtime_components", []),
            *[
                item
                for item in lock.get("build_components", [])
                if item.get("packaged_in_distribution")
            ],
        ]
    }
    for component in _component_entries(lock):
        archives = component.get("source_archives", [])
        if id(component) in source_required_ids and not archives:
            exception_reviewed = (
                component.get("source_archive_exception_reviewed") is True
                and bool(component.get("source_archive_exception_reason"))
            )
            if not exception_reviewed:
                raise ReleaseAssetError(
                    f"Runtime component has no verified exact source archive: "
                    f"{component['component']}"
                )
        for source in archives:
            if not isinstance(source, dict):
                raise ReleaseAssetError(
                    f"Invalid source archive for {component['component']}."
                )
            filename = _safe_filename(source.get("filename"))
            collision_key = filename.casefold()
            previous = filenames.get(collision_key)
            if previous is not None:
                raise ReleaseAssetError(
                    f"Duplicate source archive filename: {previous} / {filename}"
                )
            filenames[collision_key] = filename
            url = str(source.get("url", ""))
            sha256 = str(source.get("sha256", "")).casefold()
            size = source.get("size")
            if not url.startswith("https://"):
                raise ReleaseAssetError(f"Source URL must use HTTPS: {url}")
            if not SHA256_PATTERN.fullmatch(sha256):
                raise ReleaseAssetError(f"Invalid source SHA256 for {filename}.")
            if size is not None and (not isinstance(size, int) or size <= 0):
                raise ReleaseAssetError(f"Invalid source size for {filename}.")
            records.append(
                {
                    "component": str(component["component"]),
                    "version": str(component.get("version") or component.get("release_version")),
                    "license": str(component["license"]),
                    "source_status": str(
                        component.get("source_status", "declared_component_source")
                    ),
                    "filename": filename,
                    "url": url,
                    "sha256": sha256,
                    "size": size,
                }
            )
    if not records:
        raise ReleaseAssetError("Component lock contains no source archives.")
    return records


def _download(
    url: str,
    target: Path,
    opener: Callable[..., BinaryIO] = urllib.request.urlopen,
) -> None:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "LoLReplayTool-release-compliance/1"},
    )
    with opener(request, timeout=120) as response, target.open("wb") as output:
        shutil.copyfileobj(response, output, COPY_BUFFER_SIZE)


def fetch_verified_sources(
    records: Iterable[dict[str, Any]],
    cache_dir: Path,
    *,
    opener: Callable[..., BinaryIO] = urllib.request.urlopen,
) -> list[tuple[dict[str, Any], Path]]:
    cache_dir.mkdir(parents=True, exist_ok=True)
    result = []
    for record in records:
        target = cache_dir / str(record["filename"])
        if not target.is_file():
            partial = target.with_suffix(target.suffix + ".partial")
            if partial.exists():
                partial.unlink()
            try:
                _download(str(record["url"]), partial, opener)
                partial.replace(target)
            finally:
                if partial.exists():
                    partial.unlink()
        expected_size = record.get("size")
        if expected_size is not None and target.stat().st_size != expected_size:
            raise ReleaseAssetError(
                f"Source size mismatch for {target.name}: "
                f"{target.stat().st_size} != {expected_size}"
            )
        actual_hash = sha256_file(target)
        if actual_hash != record["sha256"]:
            raise ReleaseAssetError(
                f"Source SHA256 mismatch for {target.name}: {actual_hash}"
            )
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
            raise ReleaseAssetError(
                f"Source archive exceeds GitHub asset limit: {item[1].name}"
            )
        if current and current_size + size > target_size:
            parts.append(current)
            current = []
            current_size = 0
        current.append(item)
        current_size += size
    if current:
        parts.append(current)
    return parts


def _zip_symlink(info: zipfile.ZipInfo) -> bool:
    mode = (info.external_attr >> 16) & 0xFFFF
    return stat.S_ISLNK(mode)


def validate_application_source(source_zip: Path, version: str) -> None:
    try:
        with zipfile.ZipFile(source_zip) as archive:
            names: dict[str, str] = {}
            for info in archive.infolist():
                normalized = info.filename.replace("\\", "/")
                relative = PurePosixPath(normalized)
                if (
                    relative.is_absolute()
                    or any(part in {"", ".", ".."} for part in relative.parts)
                    or _zip_symlink(info)
                ):
                    raise ReleaseAssetError(
                        f"Unsafe entry in application source archive: {info.filename}"
                    )
                collision_key = relative.as_posix().casefold()
                previous = names.get(collision_key)
                if previous is not None and previous != relative.as_posix():
                    raise ReleaseAssetError(
                        f"Case-insensitive source archive collision: "
                        f"{previous} / {relative.as_posix()}"
                    )
                names[collision_key] = relative.as_posix()
            for required in ("LICENSE", "VERSION"):
                if required.casefold() not in names:
                    raise ReleaseAssetError(
                        f"Application source archive is missing {required}."
                    )
            archived_version = archive.read(names["version"]).decode("utf-8").strip()
            if archived_version != version:
                raise ReleaseAssetError(
                    f"Application source VERSION mismatch: "
                    f"{archived_version} != {version}"
                )
    except (OSError, zipfile.BadZipFile, UnicodeDecodeError) as exc:
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
        path = output_dir / (
            f"LoLReplayTool-third-party-sources-{version}-{part_number:02d}.zip"
        )
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
                    "release_legal_review_required": item[
                        "release_legal_review_required"
                    ],
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
        output_paths.append(path)
    return output_paths


def create_license_archive(
    version: str,
    distribution_root: Path,
    output_dir: Path,
) -> Path:
    path = output_dir / f"LoLReplayTool-license-materials-{version}.zip"
    files: list[tuple[Path, str]] = []
    for filename in LICENSE_ROOT_FILES:
        source = distribution_root / filename
        if not source.is_file():
            raise ReleaseAssetError(f"License material is missing: {source}")
        files.append((source, filename))
    licenses_dir = distribution_root / "licenses"
    if not licenses_dir.is_dir():
        raise ReleaseAssetError("Packaged licenses directory is missing.")
    files.extend(
        (source, source.relative_to(distribution_root).as_posix())
        for source in licenses_dir.rglob("*")
        if source.is_file()
    )
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for source, relative in sorted(files, key=lambda item: item[1].casefold()):
            archive.write(source, relative)
    return path


def create_release_assets(
    *,
    version: str,
    installer: Path,
    application_source: Path,
    distribution_root: Path,
    output_dir: Path,
    components_file: Path = COMPONENTS_FILE,
    cache_dir: Path | None = None,
    enforce_release_gates: bool = True,
    opener: Callable[..., BinaryIO] = urllib.request.urlopen,
) -> dict[str, Any]:
    if not VERSION_PATTERN.fullmatch(version):
        raise ReleaseAssetError(f"Invalid release version: {version}")
    if not installer.is_file():
        raise ReleaseAssetError(f"Installer is missing: {installer}")
    if not application_source.is_file():
        raise ReleaseAssetError(f"Application source is missing: {application_source}")
    expected_source_name = f"LoLReplayTool-source-{version}.zip"
    if application_source.name != expected_source_name:
        raise ReleaseAssetError(
            f"Application source asset must be named {expected_source_name}."
        )
    validate_application_source(application_source, version)

    lock = _load_lock(components_file)
    gates = release_gate_errors(lock)
    if enforce_release_gates and gates:
        raise ReleaseAssetError("Release legal gates remain: " + " | ".join(gates))

    output_dir.mkdir(parents=True, exist_ok=True)
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
    checksum_path.write_text(
        "".join(f"{sha256_file(path)}  {path.name}\n" for path in assets),
        encoding="ascii",
    )
    assets.append(checksum_path)
    payload = {
        "schema_version": 1,
        "version": version,
        "assets": [str(path.resolve()) for path in assets],
        "sha256sums": str(checksum_path.resolve()),
    }
    asset_list_path = output_dir / "release-assets.json"
    asset_list_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    payload["asset_list"] = str(asset_list_path.resolve())
    return payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", required=True)
    parser.add_argument("--installer", required=True, type=Path)
    parser.add_argument("--application-source", required=True, type=Path)
    parser.add_argument("--distribution-root", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--components", type=Path, default=COMPONENTS_FILE)
    parser.add_argument("--cache-dir", type=Path)
    parser.add_argument(
        "--allow-open-legal-gates",
        action="store_true",
        help="Test/audit only; release workflow must never pass this option.",
    )
    args = parser.parse_args()
    try:
        payload = create_release_assets(
            version=args.version,
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
