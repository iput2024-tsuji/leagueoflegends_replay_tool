"""Create verified, immutable release source and license assets."""

from __future__ import annotations

import argparse
import base64
import csv
import email.parser
import hashlib
import importlib.util
import io
import json
import marshal
import os
import platform
import re
import shutil
import stat
import struct
import subprocess
import sys
import sysconfig
import tempfile
import types
import urllib.parse
import urllib.request
import zipfile
from collections.abc import Callable, Iterable
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO

from scripts.binary_install_policy import (
    BinaryInstallPolicyError,
    expected_install_archive,
    external_vc_runtime_policy,
)
from scripts.collect_licenses import (
    COMPONENTS_FILE,
    canonicalize_distribution_name,
    probe_python_native_runtime,
    verified_wheel_record_inventory,
)
from scripts.external_runtime_policy import is_user_provided_runtime_path
from scripts.inno_setup_provenance import (
    INNO_COMPONENT,
    INNO_VERSION,
    InnoSetupProvenanceError,
    validate_build_provenance as validate_inno_build_provenance,
    validate_component_lock as validate_inno_component_lock,
)

MAX_GITHUB_ASSET_SIZE = 2_000_000_000
TARGET_SOURCE_PART_SIZE = 1_500_000_000
COPY_BUFFER_SIZE = 1024 * 1024
VERSION_PATTERN = re.compile(r"\d+\.\d+\.\d+(?:\.\d+)?")
PYTHON_PACKAGE_VERSION_PATTERN = re.compile(r"\d+(?:\.\d+){1,3}")
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
PYTHON_RUNTIME_SOURCE = "official_binary_archive"
REQUIRED_RELEASE_BINARY_COMPONENTS = frozenset(
    {
        "aiohappyeyeballs",
        "aiohttp",
        "aiosignal",
        "altgraph",
        "attrs",
        "certifi",
        "charset-normalizer",
        "colorama",
        "ffmpeg-python",
        "frozenlist",
        "future",
        "idna",
        "iniconfig",
        "joblib",
        "multidict",
        "numpy",
        "obsws-python",
        "opencv-python",
        "packaging",
        "pandas",
        "pefile",
        "pillow",
        "pluggy",
        "propcache",
        "pygments",
        "pyinstaller",
        "pyinstaller-hooks-contrib",
        "pyqt6",
        "pyqt6-sip",
        "pytest",
        "pytest-asyncio",
        "pytest-qt",
        "pywin32-ctypes",
        "python-dateutil",
        "python-mpv",
        "qt",
        "requests",
        "ruff",
        "scikit-learn",
        "scipy",
        "setuptools-vendored-runtime",
        "six",
        "threadpoolctl",
        "typing-extensions",
        "tzdata",
        "urllib3",
        "websocket-client",
        "yarl",
    }
)


class ReleaseAssetError(RuntimeError):
    """Release inputs are incomplete or cannot be verified."""


def _git_output(*arguments: str, binary: bool = False) -> bytes | str:
    repository_root = Path(__file__).resolve().parents[1]
    process = subprocess.run(
        ["git", *arguments],
        cwd=repository_root,
        check=False,
        capture_output=True,
        text=not binary,
        encoding=None if binary else "utf-8",
        errors=None if binary else "strict",
    )
    if process.returncode != 0:
        stderr = (
            process.stderr.decode("utf-8", errors="replace")
            if binary
            else process.stderr
        )
        raise ReleaseAssetError(
            f"Git source provenance command failed ({' '.join(arguments)}): "
            f"{stderr.strip()}"
        )
    return process.stdout


def capture_git_source_identity() -> dict[str, Any]:
    """Capture a clean, complete HEAD/index identity for release sources."""
    commit = str(_git_output("rev-parse", "HEAD")).strip().casefold()
    tree = str(_git_output("rev-parse", "HEAD^{tree}")).strip().casefold()
    if COMMIT_PATTERN.fullmatch(commit) is None or re.fullmatch(
        r"[0-9a-f]{40,64}", tree
    ) is None:
        raise ReleaseAssetError("Git HEAD commit or tree identity is invalid.")
    status = str(
        _git_output("status", "--porcelain=v1", "--untracked-files=all")
    )
    if status:
        raise ReleaseAssetError(
            "Release source tree must be clean before and after build: "
            + status.splitlines()[0]
        )
    index_raw = _git_output("ls-files", "--stage", "-z", binary=True)
    tree_raw = _git_output("ls-tree", "-r", "-z", "HEAD", binary=True)
    if not isinstance(index_raw, bytes) or not isinstance(tree_raw, bytes):
        raise ReleaseAssetError("Git tracked source inventory output is invalid.")

    def parse_index(raw: bytes) -> dict[bytes, tuple[bytes, bytes]]:
        result: dict[bytes, tuple[bytes, bytes]] = {}
        for entry in raw.split(b"\0"):
            if not entry:
                continue
            try:
                metadata_raw, path = entry.split(b"\t", 1)
                mode, object_id, stage = metadata_raw.split(b" ")
            except ValueError as exc:
                raise ReleaseAssetError("Cannot parse Git index inventory.") from exc
            if stage != b"0" or not path or path in result:
                raise ReleaseAssetError(
                    "Git index contains a duplicate or staged conflict."
                )
            result[path] = (mode, object_id)
        return result

    def parse_tree(raw: bytes) -> dict[bytes, tuple[bytes, bytes]]:
        result: dict[bytes, tuple[bytes, bytes]] = {}
        for entry in raw.split(b"\0"):
            if not entry:
                continue
            try:
                metadata_raw, path = entry.split(b"\t", 1)
                mode, object_type, object_id = metadata_raw.split(b" ")
            except ValueError as exc:
                raise ReleaseAssetError("Cannot parse Git HEAD tree inventory.") from exc
            if object_type != b"blob" or not path or path in result:
                raise ReleaseAssetError(
                    "Git HEAD contains an unsupported tracked object."
                )
            result[path] = (mode, object_id)
        return result

    index = parse_index(index_raw)
    head_tree = parse_tree(tree_raw)
    if index != head_tree or not index:
        raise ReleaseAssetError("Git index tracked-file inventory differs from HEAD.")
    inventory = [
        {
            "path": path.decode("utf-8", errors="strict"),
            "mode": values[0].decode("ascii"),
            "blob": values[1].decode("ascii"),
        }
        for path, values in sorted(index.items())
    ]
    serialized = json.dumps(
        inventory,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return {
        "commit": commit,
        "tree": tree,
        "tracked_file_count": len(inventory),
        "tracked_inventory_sha256": hashlib.sha256(serialized).hexdigest(),
    }


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
    result.extend(lock.get("installer_components", []))
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


def _runtime_download_policy_errors(lock: dict[str, Any]) -> list[str]:
    runtime_downloads = lock.get("runtime_downloads")
    if isinstance(runtime_downloads, list) and not runtime_downloads:
        return []
    return [
        "runtime_downloads must be an empty list: this project does not download "
        "or redistribute user-provided OBS/standalone FFmpeg. Remove every "
        "runtime_downloads entry and keep external tools outside Release assets."
    ]


def _assert_runtime_downloads_disabled(lock: dict[str, Any]) -> None:
    errors = _runtime_download_policy_errors(lock)
    if errors:
        raise ReleaseAssetError("Release runtime policy violation: " + " | ".join(errors))


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


def _license_materials_exception_reviewed(component: dict[str, Any]) -> bool:
    exception = component.get("license_materials_exception")
    return bool(
        isinstance(exception, dict)
        and isinstance(exception.get("reason"), str)
        and exception["reason"].strip()
        and _completed_review(component, "license_materials_exception")
    )


def _has_native_artifacts(component: dict[str, Any]) -> bool:
    return any(
        re.search(r"\.(?:dll|pyd|exe)(?:\*|$)", str(pattern), re.IGNORECASE)
        for pattern in component.get("artifact_patterns", [])
    )


def _binary_archive_required(component: dict[str, Any]) -> bool:
    return bool(component.get("distribution"))


def _release_binary_policy_errors(lock: dict[str, Any]) -> list[str]:
    policy = lock.get("release_binary_policy")
    if not isinstance(policy, dict):
        return ["release_binary_policy: required binary set is missing"]
    errors: list[str] = []
    raw_required = policy.get("required_components")
    if not isinstance(raw_required, list) or not all(
        isinstance(item, str) and item for item in raw_required
    ):
        errors.append("release_binary_policy: required_components is invalid")
        required: set[str] = set()
    else:
        required = set(raw_required)
        if len(required) != len(raw_required):
            errors.append("release_binary_policy: required_components contains duplicates")
    if required != REQUIRED_RELEASE_BINARY_COMPONENTS:
        errors.append(
            "release_binary_policy: required component set differs from the "
            "audited native/build set"
        )
    inferred = {
        str(component.get("component"))
        for component in _component_entries(lock)
        if _binary_archive_required(component)
    }
    if inferred != required:
        errors.append(
            "release_binary_policy: required component set differs from "
            "component artifact metadata"
        )
    release_python = str(lock.get("python", {}).get("release_version", ""))
    expected_abi = "cp" + "".join(release_python.split(".")[:2])
    expected_fields = {
        "python_implementation": "CPython",
        "python_version": release_python,
        "abi": expected_abi,
        "platform": "win_amd64",
    }
    for field, expected in expected_fields.items():
        if policy.get(field) != expected:
            errors.append(
                f"release_binary_policy: {field} must be {expected!r}"
            )
    pip_version = policy.get("pip_version")
    if not isinstance(pip_version, str) or PYTHON_PACKAGE_VERSION_PATTERN.fullmatch(
        pip_version
    ) is None:
        errors.append("release_binary_policy: pip_version must be an exact version")
    for component in _component_entries(lock):
        if component.get("component") in required:
            errors.extend(_wheel_compatibility_errors(component, policy))
    try:
        external_policy = external_vc_runtime_policy(lock)
    except BinaryInstallPolicyError as exc:
        errors.append(f"external_vc_runtime_policy: {exc}")
    else:
        if external_policy is not None and not set(
            external_policy["required_components"]
        ).issubset(required):
            errors.append(
                "external_vc_runtime_policy: component set is outside the "
                "release binary policy"
            )
    return errors


def _wheel_compatibility_errors(
    component: dict[str, Any],
    policy: dict[str, Any],
) -> list[str]:
    component_name = str(component.get("component", "<unknown>"))
    archive = component.get("binary_archive")
    if not isinstance(archive, dict):
        return []
    filename = archive.get("filename")
    if not isinstance(filename, str) or not filename.casefold().endswith(".whl"):
        return []
    wheel_fields = filename[:-4].rsplit("-", 3)
    if len(wheel_fields) != 4:
        return [f"{component_name}: binary wheel filename tags are invalid"]
    _prefix, python_tag, abi_tag, platform_tag = wheel_fields
    python_tags = set(python_tag.casefold().split("."))
    abi_tags = set(abi_tag.casefold().split("."))
    platform_tags = set(platform_tag.casefold().split("."))
    expected_abi = str(policy.get("abi", "")).casefold()
    expected_platform = str(policy.get("platform", "")).casefold()
    errors: list[str] = []
    if "py3" in python_tags and "none" in abi_tags and "any" in platform_tags:
        return errors
    if expected_platform not in platform_tags:
        errors.append(
            f"{component_name}: binary wheel platform does not include "
            f"{expected_platform}"
        )
    if component_name in {"pyinstaller", "qt", "ruff"}:
        if "py3" not in python_tags or "none" not in abi_tags:
            errors.append(
                f"{component_name}: wheel tags must be py3-none for the locked "
                "release archive"
            )
        return errors
    if "abi3" in abi_tags:
        expected_match = re.fullmatch(r"cp(\d)(\d+)", expected_abi)
        compatible = False
        if expected_match is not None:
            expected_major = int(expected_match.group(1))
            expected_minor = int(expected_match.group(2))
            for tag in python_tags:
                match = re.fullmatch(r"cp(\d)(\d+)", tag)
                if (
                    match is not None
                    and int(match.group(1)) == expected_major
                    and int(match.group(2)) <= expected_minor
                ):
                    compatible = True
                    break
        if not compatible:
            errors.append(
                f"{component_name}: abi3 wheel Python tag is incompatible with "
                f"{expected_abi}"
            )
    elif expected_abi not in python_tags or expected_abi not in abi_tags:
        errors.append(
            f"{component_name}: binary wheel ABI tags do not match {expected_abi}"
        )
    return errors


def _locked_archive_errors(
    component: dict[str, Any],
    field: str,
    *,
    wheel_required: bool = False,
) -> list[str]:
    component_name = str(component.get("component", "<unknown>"))
    archive = component.get(field)
    if not isinstance(archive, dict):
        return [f"{component_name}: {field} metadata is missing"]
    errors: list[str] = []
    try:
        filename = _safe_filename(archive.get("filename"))
    except ReleaseAssetError:
        errors.append(f"{component_name}: {field}.filename is unsafe or missing")
        filename = ""
    if wheel_required and not filename.casefold().endswith(".whl"):
        errors.append(f"{component_name}: {field} must identify a wheel file")
    url = archive.get("url")
    if not isinstance(url, str) or not url.startswith("https://"):
        errors.append(f"{component_name}: {field}.url must use HTTPS")
    digest = archive.get("sha256")
    if not isinstance(digest, str) or SHA256_PATTERN.fullmatch(digest.casefold()) is None:
        errors.append(f"{component_name}: {field}.sha256 is invalid")
    size = archive.get("size")
    if (
        not isinstance(size, int)
        or isinstance(size, bool)
        or size <= 0
        or size >= MAX_GITHUB_ASSET_SIZE
    ):
        errors.append(f"{component_name}: {field}.size must be below 2 GiB")
    contents = archive.get("contents")
    if contents is not None:
        if not isinstance(contents, list) or not contents:
            errors.append(f"{component_name}: {field}.contents must be a non-empty list")
        else:
            seen_contents: set[str] = set()
            for content in contents:
                if not isinstance(content, dict):
                    errors.append(f"{component_name}: {field}.contents entry is invalid")
                    continue
                try:
                    content_path = _safe_archive_member(
                        content.get("path"),
                        label=f"{field} content for {component_name}",
                    ).as_posix()
                except ReleaseAssetError:
                    errors.append(f"{component_name}: {field}.contents path is unsafe")
                    continue
                if content_path.casefold() in seen_contents:
                    errors.append(
                        f"{component_name}: {field}.contents path is duplicated: "
                        f"{content_path}"
                    )
                seen_contents.add(content_path.casefold())
                content_size = content.get("size")
                content_hash = content.get("sha256")
                if (
                    not isinstance(content_size, int)
                    or isinstance(content_size, bool)
                    or content_size <= 0
                    or not isinstance(content_hash, str)
                    or SHA256_PATTERN.fullmatch(content_hash) is None
                ):
                    errors.append(
                        f"{component_name}: {field}.contents lock is invalid for "
                        f"{content_path}"
                    )
    return errors


def _license_material_lock_errors(
    component: dict[str, Any],
    *,
    release_python_version: str,
) -> list[str]:
    component_name = str(component.get("component", "<unknown>"))
    materials = component.get("license_materials")
    if materials is None:
        if _license_materials_exception_reviewed(component):
            return []
        return [
            f"{component_name}: exact license materials or a completed reviewed "
            "exception are missing"
        ]
    if not isinstance(materials, list) or not materials:
        return [f"{component_name}: license_materials must be a non-empty list"]
    errors: list[str] = []
    seen: set[str] = set()
    for material in materials:
        if not isinstance(material, dict):
            errors.append(f"{component_name}: invalid license_materials entry")
            continue
        try:
            path = _safe_archive_member(
                material.get("path"),
                label=f"license material for {component_name}",
            ).as_posix()
        except ReleaseAssetError:
            errors.append(f"{component_name}: unsafe license material path")
            continue
        if path.casefold() in seen:
            errors.append(f"{component_name}: duplicate license material path: {path}")
        seen.add(path.casefold())
        digest = material.get("sha256")
        version_digests = material.get("sha256_by_python_version")
        digest_is_valid = bool(
            isinstance(digest, str)
            and SHA256_PATTERN.fullmatch(digest.casefold()) is not None
        )
        version_digests_are_valid = bool(
            isinstance(version_digests, dict)
            and version_digests
            and release_python_version in version_digests
            and all(
                isinstance(version, str)
                and version.strip()
                and isinstance(version_digest, str)
                and SHA256_PATTERN.fullmatch(version_digest.casefold()) is not None
                for version, version_digest in version_digests.items()
            )
        )
        if digest is not None and version_digests is not None:
            errors.append(
                f"{component_name}: license material SHA256 is ambiguous for {path}"
            )
        if not digest_is_valid and not version_digests_are_valid:
            errors.append(
                f"{component_name}: invalid license material SHA256 for {path}"
            )
    return errors


def _python_native_profile_errors(lock: dict[str, Any]) -> list[str]:
    python_lock = lock.get("python")
    if not isinstance(python_lock, dict):
        return ["python: component lock is missing"]
    release_version = str(python_lock.get("release_version", ""))
    profiles = python_lock.get("windows_native_runtime_profiles")
    if not isinstance(profiles, dict) or not isinstance(
        profiles.get(release_version),
        dict,
    ):
        return [
            f"python: verified Windows native runtime profile is missing for "
            f"{release_version}"
        ]
    profile = profiles[release_version]
    errors: list[str] = []
    if profile.get("provenance_verified") is not True:
        errors.append("python: release native runtime provenance is not verified")
    if profile.get("runtime_source") != PYTHON_RUNTIME_SOURCE:
        errors.append(
            "python: release native runtime source must be "
            f"{PYTHON_RUNTIME_SOURCE}"
        )
    for profile_field, label in (
        ("official_binary_archive", "windows_binary_archive"),
        ("official_actions_archive", "windows_actions_archive"),
        ("official_installer", "windows_installer"),
    ):
        errors.extend(
            _locked_archive_errors(
                {
                    "component": "python",
                    label: profile.get(profile_field),
                },
                label,
            )
        )
    actions_archive = profile.get("official_actions_archive")
    if (
        not isinstance(actions_archive, dict)
        or not isinstance(actions_archive.get("url"), str)
        or "github.com/actions/python-versions/releases/download/" not in actions_archive["url"]
    ):
        errors.append("python: official Actions archive provenance is invalid")
    binary_archive = profile.get("official_binary_archive")
    expected_binary_filename = f"python-{release_version}-amd64.zip"
    expected_binary_url = (
        f"https://www.python.org/ftp/python/{release_version}/"
        f"{expected_binary_filename}"
    )
    if (
        not isinstance(binary_archive, dict)
        or binary_archive.get("filename") != expected_binary_filename
        or binary_archive.get("url") != expected_binary_url
    ):
        errors.append("python: official binary archive provenance is invalid")
    installer = profile.get("official_installer")
    if (
        not isinstance(installer, dict)
        or not isinstance(installer.get("url"), str)
        or not installer["url"].startswith("https://www.python.org/ftp/python/")
    ):
        errors.append("python: official installer provenance is invalid")

    core_inventory = profile.get("core_native_inventory")
    core_artifacts = (
        core_inventory.get("artifacts")
        if isinstance(core_inventory, dict)
        else None
    )
    core_records: list[dict[str, Any]] = []
    core_paths: set[str] = set()
    if not isinstance(core_artifacts, list) or not core_artifacts:
        errors.append("python: core native inventory is missing")
    else:
        for artifact in core_artifacts:
            if not isinstance(artifact, dict):
                errors.append("python: invalid core native artifact")
                continue
            raw_path = artifact.get("path")
            try:
                relative = _safe_archive_member(
                    raw_path,
                    label="Python core native inventory",
                )
            except ReleaseAssetError:
                errors.append("python: unsafe core native artifact path")
                continue
            path = relative.as_posix()
            if not (
                len(relative.parts) == 1
                and relative.suffix.casefold() in {".dll", ".exe"}
                or len(relative.parts) == 2
                and relative.parts[0] == "DLLs"
                and relative.suffix.casefold() in {".dll", ".pyd"}
            ):
                errors.append(f"python: invalid core native artifact path: {path}")
            if path.casefold() in core_paths:
                errors.append(f"python: duplicate core native artifact path: {path}")
            core_paths.add(path.casefold())
            size = artifact.get("size")
            digest = artifact.get("sha256")
            if (
                not isinstance(size, int)
                or isinstance(size, bool)
                or size <= 0
                or not isinstance(digest, str)
                or SHA256_PATTERN.fullmatch(digest) is None
            ):
                errors.append(f"python: invalid core native artifact lock: {path}")
                continue
            core_records.append({"path": path, "size": size, "sha256": digest})
    inventory_digest = hashlib.sha256()
    for record in sorted(core_records, key=lambda item: str(item["path"])):
        inventory_digest.update(str(record["path"]).encode("utf-8"))
        inventory_digest.update(b"\0")
        inventory_digest.update(str(record["size"]).encode("ascii"))
        inventory_digest.update(b"\0")
        inventory_digest.update(str(record["sha256"]).encode("ascii"))
        inventory_digest.update(b"\n")
    if isinstance(core_inventory, dict) and (
        core_inventory.get("file_count") != len(core_records)
        or core_inventory.get("total_size")
        != sum(int(item["size"]) for item in core_records)
        or core_inventory.get("inventory_sha256") != inventory_digest.hexdigest()
    ):
        errors.append("python: core native inventory summary differs")
    stdlib_inventory = profile.get("stdlib_python_sources")
    if not isinstance(stdlib_inventory, dict) or (
        not isinstance(stdlib_inventory.get("file_count"), int)
        or stdlib_inventory.get("file_count", 0) <= 0
        or not isinstance(stdlib_inventory.get("total_size"), int)
        or stdlib_inventory.get("total_size", 0) <= 0
        or not isinstance(stdlib_inventory.get("inventory_sha256"), str)
        or SHA256_PATTERN.fullmatch(
            str(stdlib_inventory.get("inventory_sha256", ""))
        )
        is None
        or not isinstance(stdlib_inventory.get("excluded_prefixes"), list)
    ):
        errors.append("python: stdlib Python source inventory is invalid")
    launchers = profile.get("venv_launchers")
    if not isinstance(launchers, list) or {
        item.get("kind") for item in launchers if isinstance(item, dict)
    } != {"console", "windows"} or any(
        not isinstance(item, dict)
        or not isinstance(item.get("size"), int)
        or item.get("size", 0) <= 0
        or not isinstance(item.get("sha256"), str)
        or SHA256_PATTERN.fullmatch(str(item.get("sha256", ""))) is None
        for item in launchers if isinstance(launchers, list)
    ):
        errors.append("python: venv launcher inventory is invalid")
    locked_native = {
        str(component["component"]): component
        for component in lock.get("runtime_components", [])
        if component.get("python_native_runtime_profile") is True
    }
    raw_components = profile.get("components")
    if not isinstance(raw_components, list):
        return [*errors, "python: release native runtime component list is missing"]
    observed: set[str] = set()
    for component in raw_components:
        if not isinstance(component, dict):
            errors.append("python: invalid release native runtime component")
            continue
        component_name = str(component.get("component", ""))
        if component_name in observed or component_name not in locked_native:
            errors.append(
                f"python: duplicate or unexpected native runtime component: "
                f"{component_name}"
            )
            continue
        observed.add(component_name)
        if component.get("version") != locked_native[component_name].get("version"):
            errors.append(
                f"python: native runtime version differs for {component_name}"
            )
        probes = component.get("probes", [])
        artifacts = component.get("artifacts", [])
        if not isinstance(probes, list) or not isinstance(artifacts, list) or (
            not probes and not artifacts
        ):
            errors.append(
                f"python: native runtime evidence is missing for {component_name}"
            )
        for probe in probes if isinstance(probes, list) else []:
            if not isinstance(probe, dict) or not all(
                isinstance(probe.get(field), str) and probe[field]
                for field in ("module", "attribute", "expected")
            ):
                errors.append(
                    f"python: invalid native runtime probe for {component_name}"
                )
        for artifact in artifacts if isinstance(artifacts, list) else []:
            if not isinstance(artifact, dict):
                errors.append(
                    f"python: invalid native runtime artifact for {component_name}"
                )
                continue
            try:
                filename = _safe_filename(artifact.get("filename"))
            except ReleaseAssetError:
                filename = ""
            if (
                not filename
                or not isinstance(artifact.get("size"), int)
                or artifact["size"] <= 0
                or not isinstance(artifact.get("sha256"), str)
                or SHA256_PATTERN.fullmatch(artifact["sha256"]) is None
            ):
                errors.append(
                    f"python: invalid native runtime artifact lock for "
                    f"{component_name}"
                )
            else:
                core_match = next(
                    (
                        item
                        for item in core_records
                        if PurePosixPath(str(item["path"])).name.casefold()
                        == filename.casefold()
                    ),
                    None,
                )
                if core_match is None or any(
                    artifact.get(field) != core_match.get(field)
                    for field in ("size", "sha256")
                ):
                    errors.append(
                        f"python: dependency artifact is outside core inventory for "
                        f"{component_name}: {filename}"
                    )
    if observed != set(locked_native):
        errors.append(
            "python: release native runtime profile component set differs from lock"
        )
    return errors


def release_gate_errors(lock: dict[str, Any]) -> list[str]:
    errors = _runtime_download_policy_errors(lock)
    from scripts.prepare_opencv_wheel import (
        OpenCVWheelError,
        source_build_policy as opencv_source_build_policy,
    )

    try:
        opencv_policy = opencv_source_build_policy(lock)
    except OpenCVWheelError as exc:
        errors.append(f"opencv-python-source-build: {exc}")
        opencv_policy = None
    if opencv_policy is not None:
        if opencv_policy["expected_byte_identical"] is None:
            errors.append(
                "opencv-python-source-build: byte reproducibility is not fixed"
            )
        if opencv_policy["expected_semantic_manifest_sha256"] is None:
            errors.append(
                "opencv-python-source-build: semantic manifest SHA256 is not fixed"
            )
    try:
        validate_inno_component_lock(lock)
    except InnoSetupProvenanceError as exc:
        errors.append(f"inno-setup: {exc}")
    if not _completed_review(lock, "historical_remediation"):
        errors.append(
            "v0.5.2-historical-remediation: review evidence is incomplete"
        )
    errors.extend(_release_binary_policy_errors(lock))
    errors.extend(_python_native_profile_errors(lock))
    for component in _component_entries(lock):
        if component.get("release_legal_review_required"):
            errors.append(
                f"{component['component']}: {component.get('release_gate_reason', 'expert legal review is required')}"
            )
    source_required_components = [
        lock["python"],
        *lock.get("runtime_components", []),
        *[component for component in lock.get("build_components", []) if component.get("packaged_in_distribution")],
        *lock.get("installer_components", []),
    ]
    for component in source_required_components:
        archives = component.get("source_archives")
        exception_reviewed = _source_exception_reviewed(component)
        if not archives and not exception_reviewed:
            errors.append(f"{component['component']}: no verified exact source archive is locked")
        if (
            component.get("source_status") != "verified_corresponding_source"
            and not exception_reviewed
        ):
            errors.append(
                f"{component['component']}: source_status is not "
                "verified_corresponding_source"
            )
        license_expression = str(component.get("license", "")).casefold()
        if (
            "bundled component licenses" in license_expression
            and component.get("vendored_source_coverage_verified") is not True
            and component.get("native_source_coverage_verified") is not True
        ):
            errors.append(
                f"{component['component']}: source coverage for wheel-vendored native components is not verified"
            )
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
    for component in _component_entries(lock):
        errors.extend(
            _license_material_lock_errors(
                component,
                release_python_version=str(lock["python"]["release_version"]),
            )
        )
        if _binary_archive_required(component):
            errors.extend(
                _locked_archive_errors(
                    component,
                    "binary_archive",
                    wheel_required=True,
                )
            )
        elif component.get("binary_archive") is not None:
            errors.extend(
                _locked_archive_errors(
                    component,
                    "binary_archive",
                    wheel_required=True,
                )
            )
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
            *lock.get("installer_components", []),
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


def binary_archive_records(lock: dict[str, Any]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    records_by_filename: dict[str, dict[str, Any]] = {}
    policy_errors = _release_binary_policy_errors(lock)
    if policy_errors:
        raise ReleaseAssetError(" | ".join(policy_errors))
    seen_components: set[str] = set()
    source_build = lock.get("opencv_source_build_policy")
    source_build_component = (
        source_build.get("component") if isinstance(source_build, dict) else None
    )
    for component in _component_entries(lock):
        component_name = str(component.get("component"))
        if component_name in seen_components:
            raise ReleaseAssetError(
                f"Duplicate component in binary archive lock: {component_name}"
            )
        seen_components.add(component_name)
        if component_name == source_build_component:
            continue
        archive = component.get("binary_archive")
        if (
            archive is None
            and component_name not in REQUIRED_RELEASE_BINARY_COMPONENTS
        ):
            continue
        archive_errors = _locked_archive_errors(
            component,
            "binary_archive",
            wheel_required=True,
        )
        if archive_errors:
            raise ReleaseAssetError(" | ".join(archive_errors))
        assert isinstance(archive, dict)
        filename = _safe_filename(archive["filename"])
        record = {
            "component": component_name,
            "distribution": str(component["distribution"]),
            "version": str(component["version"]),
            "filename": filename,
            "url": str(archive["url"]),
            "sha256": str(archive["sha256"]).casefold(),
            "size": int(archive["size"]),
        }
        if "contents" in archive:
            record["contents"] = archive["contents"]
        collision_key = filename.casefold()
        previous = records_by_filename.get(collision_key)
        if previous is not None:
            raise ReleaseAssetError(
                "Duplicate locked binary archive filename: "
                f"{previous['component']} / {record['component']}: {filename}"
            )
        records_by_filename[collision_key] = record
        records.append(record)
    missing_components = REQUIRED_RELEASE_BINARY_COMPONENTS - seen_components
    if missing_components:
        raise ReleaseAssetError(
            "Required binary components are missing from lock: "
            + ", ".join(sorted(missing_components))
        )
    if not records:
        raise ReleaseAssetError("Component lock contains no binary archives.")
    return sorted(records, key=lambda item: str(item["filename"]).casefold())


def _verify_wheel_metadata(
    path: Path,
    record: dict[str, Any],
) -> list[dict[str, Any]]:
    _require_regular_file(path, label="Locked wheel")
    try:
        with zipfile.ZipFile(path) as archive:
            members = _validated_zip_members(
                archive,
                label=f"locked wheel {path.name}",
            )
            metadata_members = [
                (info, PurePosixPath(member))
                for member, info in members.items()
                if PurePosixPath(member).name == "METADATA"
                and len(PurePosixPath(member).parts) == 2
                and PurePosixPath(member).parent.name.casefold().endswith(
                    ".dist-info"
                )
            ]
            matching_metadata = []
            for info, metadata_path in metadata_members:
                metadata_bytes = archive.read(info)
                message = email.parser.BytesParser().parsebytes(metadata_bytes)
                if (
                    canonicalize_distribution_name(message.get("Name", ""))
                    == canonicalize_distribution_name(str(record["distribution"]))
                    and message.get("Version", "") == str(record["version"])
                ):
                    matching_metadata.append(
                        (info, metadata_path, metadata_bytes, message)
                    )
            if len(matching_metadata) != 1:
                raise ReleaseAssetError(
                    f"Locked wheel must contain exactly one matching root METADATA: "
                    f"{path.name}"
                )
            _metadata_info, metadata_path, metadata_bytes, message = matching_metadata[0]
            dist_info = metadata_path.parent
            wheel_name = (dist_info / "WHEEL").as_posix()
            wheel_info = members.get(wheel_name)
            if wheel_info is None or wheel_info.is_dir():
                raise ReleaseAssetError(
                    f"Locked wheel has no matching dist-info WHEEL: {path.name}"
                )
            record_name = (dist_info / "RECORD").as_posix()
            record_info = members.get(record_name)
            if record_info is None:
                raise ReleaseAssetError(
                    f"Locked wheel has no dist-info RECORD: {path.name}"
                )
            try:
                record_rows = list(
                    csv.reader(
                        archive.read(record_info).decode("utf-8", errors="strict").splitlines()
                    )
                )
            except (UnicodeError, csv.Error) as exc:
                raise ReleaseAssetError(
                    f"Locked wheel RECORD is invalid for {path.name}: {exc}"
                ) from exc
            recorded_members: set[str] = set()
            observed_contents: list[dict[str, Any]] = []
            for row in record_rows:
                if len(row) != 3:
                    raise ReleaseAssetError(
                        f"Locked wheel RECORD row is invalid for {path.name}"
                    )
                recorded_path = _safe_archive_member(
                    row[0].replace("\\", "/"),
                    label=f"RECORD in locked wheel {path.name}",
                ).as_posix()
                collision_key = recorded_path.casefold()
                if collision_key in recorded_members:
                    raise ReleaseAssetError(
                        f"Locked wheel RECORD contains a duplicate path: {recorded_path}"
                    )
                recorded_members.add(collision_key)
                member_info = members.get(recorded_path)
                if member_info is None or member_info.is_dir():
                    raise ReleaseAssetError(
                        f"Locked wheel RECORD references a missing file: {recorded_path}"
                    )
                size, digest = _hash_zip_member(archive, member_info)
                observed_contents.append(
                    {
                        "path": recorded_path,
                        "size": size,
                        "sha256": digest,
                    }
                )
                if recorded_path == record_name:
                    if row[1] or row[2]:
                        raise ReleaseAssetError(
                            f"Locked wheel RECORD self-entry must be unhashed: {path.name}"
                        )
                    continue
                encoded_digest = base64.urlsafe_b64encode(
                    bytes.fromhex(digest)
                ).rstrip(b"=").decode("ascii")
                if row[1] != f"sha256={encoded_digest}" or row[2] != str(size):
                    raise ReleaseAssetError(
                        f"Locked wheel RECORD digest or size differs: {recorded_path}"
                    )
            actual_members = {
                member.casefold()
                for member, info in members.items()
                if not info.is_dir()
            }
            if recorded_members != actual_members:
                raise ReleaseAssetError(
                    f"Locked wheel RECORD file set differs for {path.name}"
                )
            for content in record.get("contents", []):
                if not isinstance(content, dict):
                    raise ReleaseAssetError(
                        f"Locked wheel content lock is invalid for {path.name}"
                    )
                content_path = _safe_archive_member(
                    content.get("path"),
                    label=f"content lock in wheel {path.name}",
                ).as_posix()
                content_info = members.get(content_path)
                if content_info is None or content_info.is_dir():
                    raise ReleaseAssetError(
                        f"Locked wheel content is missing: {content_path}"
                    )
                size, digest = _hash_zip_member(archive, content_info)
                if size != content.get("size") or digest != content.get("sha256"):
                    raise ReleaseAssetError(
                        f"Locked wheel content differs: {content_path}"
                    )
    except (OSError, RuntimeError, zipfile.BadZipFile, KeyError) as exc:
        raise ReleaseAssetError(f"Cannot inspect locked wheel {path.name}: {exc}") from exc
    observed_name = message.get("Name", "")
    observed_version = message.get("Version", "")
    if canonicalize_distribution_name(observed_name) != canonicalize_distribution_name(
        str(record["distribution"])
    ):
        raise ReleaseAssetError(
            f"Locked wheel METADATA name differs for {path.name}: {observed_name}"
        )
    if observed_version != str(record["version"]):
        raise ReleaseAssetError(
            f"Locked wheel METADATA version differs for {path.name}: "
            f"{observed_version}"
        )
    return sorted(observed_contents, key=lambda item: str(item["path"]).casefold())


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


def create_verified_binary_manifest(
    components_file: Path,
    cache_dir: Path,
    output_manifest: Path,
    *,
    opener: Callable[..., BinaryIO] = urllib.request.urlopen,
) -> dict[str, Any]:
    lock = _load_lock(components_file)
    records = binary_archive_records(lock)
    fetched = fetch_verified_sources(records, cache_dir, opener=opener)
    verified_records = []
    for record, path in fetched:
        verified_record = dict(record)
        verified_record["verified_contents"] = _verify_wheel_metadata(path, record)
        verified_records.append(verified_record)
    if output_manifest.parent.resolve() != cache_dir.resolve():
        raise ReleaseAssetError(
            "Verified binary manifest must be written directly inside its cache."
        )
    _reject_link_target(output_manifest, label="Verified binary manifest")
    payload = {
        "schema_version": 1,
        "component_lock_sha256": sha256_file(components_file),
        "release_python_version": str(lock["python"]["release_version"]),
        "archives": verified_records,
    }
    output_manifest.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    verify_binary_manifest(output_manifest, components_file=components_file)
    return payload


def verify_binary_manifest(
    manifest_path: Path,
    *,
    components_file: Path = COMPONENTS_FILE,
) -> dict[str, Any]:
    _require_regular_file(manifest_path, label="Verified binary manifest")
    lock = _load_lock(components_file)
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReleaseAssetError(f"Cannot read verified binary manifest: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ReleaseAssetError("Unsupported verified binary manifest schema.")
    if payload.get("component_lock_sha256") != sha256_file(components_file):
        raise ReleaseAssetError("Verified binary manifest component lock hash differs.")
    if payload.get("release_python_version") != lock["python"].get(
        "release_version"
    ):
        raise ReleaseAssetError("Verified binary manifest Python version differs.")
    expected = binary_archive_records(lock)
    actual = payload.get("archives")
    if not isinstance(actual, list) or len(actual) != len(expected):
        raise ReleaseAssetError("Verified binary manifest records differ from lock.")
    for record, manifest_record in zip(expected, actual, strict=True):
        if not isinstance(manifest_record, dict):
            raise ReleaseAssetError("Verified binary manifest record is invalid.")
        static_record = dict(manifest_record)
        recorded_contents = static_record.pop("verified_contents", None)
        if static_record != record or not isinstance(recorded_contents, list):
            raise ReleaseAssetError("Verified binary manifest records differ from lock.")
        target = manifest_path.parent / str(record["filename"])
        _require_regular_file(target, label="Verified binary cache entry")
        if target.stat().st_size != record["size"]:
            raise ReleaseAssetError(
                f"Verified binary size mismatch for {target.name}."
            )
        if sha256_file(target) != record["sha256"]:
            raise ReleaseAssetError(
                f"Verified binary SHA256 mismatch for {target.name}."
            )
        if _verify_wheel_metadata(target, record) != recorded_contents:
            raise ReleaseAssetError(
                f"Verified binary wheel inventory differs for {target.name}."
            )
    return payload


def flatten_exact_requirements(
    requirements_file: Path,
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    root = requirements_file.resolve().parent
    pins: list[dict[str, str]] = []
    inputs: list[dict[str, str]] = []
    seen_names: dict[str, str] = {}
    visited: set[Path] = set()

    def visit(path: Path, active: set[Path]) -> None:
        resolved = path.resolve()
        try:
            relative = resolved.relative_to(root).as_posix()
        except ValueError as exc:
            raise ReleaseAssetError(
                f"Requirements include escapes its root: {path}"
            ) from exc
        if resolved in active:
            raise ReleaseAssetError(f"Requirements include cycle: {relative}")
        if resolved in visited:
            return
        _require_regular_file(resolved, label="Requirements input")
        active.add(resolved)
        visited.add(resolved)
        inputs.append({"path": relative, "sha256": sha256_file(resolved)})
        for line_number, raw_line in enumerate(
            resolved.read_text(encoding="utf-8").splitlines(),
            start=1,
        ):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            include = re.fullmatch(r"(?:-r|--requirement)\s+([^\s]+)", line)
            if include is not None:
                included = resolved.parent / include.group(1)
                visit(included, active)
                continue
            if line.startswith("-"):
                raise ReleaseAssetError(
                    f"Unsupported requirements option ({relative}:{line_number}): "
                    f"{line}"
                )
            match = re.fullmatch(
                r"([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s;@]+)",
                line,
            )
            if match is None:
                raise ReleaseAssetError(
                    f"Release requirement must be an exact unmarked == pin "
                    f"({relative}:{line_number}): {line}"
                )
            name, version = match.groups()
            canonical = canonicalize_distribution_name(name)
            previous = seen_names.get(canonical)
            if previous is not None:
                raise ReleaseAssetError(
                    f"Duplicate release requirement: {previous} / {name}"
                )
            seen_names[canonical] = name
            pins.append(
                {
                    "name": name,
                    "canonical_name": canonical,
                    "version": version,
                }
            )
        active.remove(resolved)

    visit(requirements_file, set())
    return pins, sorted(inputs, key=lambda item: item["path"].casefold())


def _prepare_external_vc_runtime_wheels(
    *,
    components_file: Path,
    lock: dict[str, Any],
    cache_dir: Path,
    binary_payload: dict[str, Any],
    opener: Callable[..., BinaryIO],
) -> dict[str, Any] | None:
    try:
        policy = external_vc_runtime_policy(lock)
    except BinaryInstallPolicyError as exc:
        raise ReleaseAssetError(str(exc)) from exc
    if policy is None:
        return None
    from scripts import prepare_external_vc_runtime_wheels as external_wheels

    output_dir = cache_dir / "external-vc-runtime-wheels"
    if os.path.lexists(output_dir):
        raise ReleaseAssetError(
            f"External VC++ Runtime wheel output already exists: {output_dir}"
        )
    tool_dir = cache_dir / "external-vc-runtime-tools"
    fetch_verified_sources(policy["tool_artifacts"], tool_dir, opener=opener)
    binary_by_component = {
        str(item["component"]): item for item in binary_payload["archives"]
    }
    try:
        with tempfile.TemporaryDirectory(
            prefix=".external-vc-runtime-input-",
            dir=cache_dir,
        ) as temporary:
            input_dir = Path(temporary)
            for component_name in policy["required_components"]:
                binary = binary_by_component.get(component_name)
                if binary is None:
                    raise ReleaseAssetError(
                        "External VC++ Runtime component has no verified source "
                        f"wheel: {component_name}"
                    )
                source = cache_dir / str(binary["filename"])
                _require_regular_file(source, label="External Runtime source wheel")
                shutil.copyfile(source, input_dir / source.name)
            external_wheels.run(
                input_dir,
                output_dir,
                components_file,
                tool_dir,
            )
        provenance = external_wheels.validate_output_directory(
            output_dir,
            components_file,
        )
    except (
        BinaryInstallPolicyError,
        external_wheels.WheelError,
        OSError,
        subprocess.CalledProcessError,
        zipfile.BadZipFile,
    ) as exc:
        raise ReleaseAssetError(
            f"External VC++ Runtime wheel preparation failed: {exc}"
        ) from exc
    provenance_path = output_dir / external_wheels.PROVENANCE_NAME
    return {
        "directory": str(output_dir.resolve()),
        "provenance": str(provenance_path.resolve()),
        "provenance_sha256": sha256_file(provenance_path),
        "payload": provenance,
    }


def _prepare_opencv_source_wheel(
    *,
    components_file: Path,
    lock: dict[str, Any],
    cache_dir: Path,
    opener: Callable[..., BinaryIO],
    prepared_directory: Path | None = None,
    expected_provenance_sha256: str | None = None,
) -> dict[str, Any] | None:
    from scripts import prepare_opencv_wheel as opencv_wheel

    try:
        policy = opencv_wheel.source_build_policy(lock)
    except opencv_wheel.OpenCVWheelError as exc:
        raise ReleaseAssetError(str(exc)) from exc
    if (prepared_directory is None) != (expected_provenance_sha256 is None):
        raise ReleaseAssetError("OpenCV artifact directory and provenance SHA256 must be supplied together.")
    if policy is None:
        if prepared_directory is not None:
            raise ReleaseAssetError("OpenCV artifact supplied without a source-build policy.")
        return None
    output_dir = cache_dir / "opencv-source-built-wheel"
    # MSBuild FileTracker is not long-path aware; keep the private build root short.
    work_dir = cache_dir / "w"
    if any(os.path.lexists(path) for path in (output_dir, work_dir)):
        raise ReleaseAssetError("OpenCV source-build path already exists.")
    try:
        if prepared_directory is not None:
            _require_directory(prepared_directory, label="OpenCV workflow artifact")
            provenance_path = prepared_directory / opencv_wheel.PROVENANCE_NAME
            _require_regular_file(provenance_path, label="OpenCV artifact provenance")
            if (
                re.fullmatch(r"[0-9a-f]{64}", str(expected_provenance_sha256)) is None
                or sha256_file(provenance_path) != expected_provenance_sha256
            ):
                raise ReleaseAssetError("OpenCV workflow artifact provenance SHA256 differs.")
            # The caller binds this digest to the producer job in the same run.
            # Re-audit the actual wheel before and after copying into our cache.
            opencv_wheel.validate_output_directory(prepared_directory, components_file)
            output_dir.mkdir(parents=True)
            for name in (str(policy["output_filename"]), opencv_wheel.PROVENANCE_NAME):
                shutil.copyfile(prepared_directory / name, output_dir / name)
            if sha256_file(output_dir / opencv_wheel.PROVENANCE_NAME) != expected_provenance_sha256:
                raise ReleaseAssetError("OpenCV workflow artifact changed during import.")
            provenance = opencv_wheel.validate_output_directory(output_dir, components_file)
        else:
            cache_dir.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix="i-", dir=cache_dir) as temporary:
                input_dir = Path(temporary)
                fetch_verified_sources(
                    [*policy["source_artifacts"], *policy["build_artifacts"]],
                    input_dir,
                    opener=opener,
                )
                provenance = opencv_wheel.run(
                    input_dir,
                    output_dir,
                    components_file,
                    work_dir,
                )
    except (
        opencv_wheel.OpenCVWheelError,
        OSError,
        subprocess.CalledProcessError,
        zipfile.BadZipFile,
    ) as exc:
        raise ReleaseAssetError(f"OpenCV source build failed: {exc}") from exc
    provenance_path = output_dir / opencv_wheel.PROVENANCE_NAME
    return {
        "directory": str(output_dir.resolve()),
        "provenance": str(provenance_path.resolve()),
        "provenance_sha256": sha256_file(provenance_path),
        "payload": provenance,
    }


def _resolve_external_vc_runtime_plan(
    *,
    components_file: Path,
    lock: dict[str, Any],
    plan: dict[str, Any],
    plan_path: Path,
) -> tuple[Path | None, dict[str, Any] | None, dict[str, Any] | None]:
    try:
        policy = external_vc_runtime_policy(lock)
    except BinaryInstallPolicyError as exc:
        raise ReleaseAssetError(str(exc)) from exc
    if plan.get("components_file") != str(components_file.resolve()):
        raise ReleaseAssetError("Binary install plan component lock path differs.")
    raw = plan.get("external_vc_runtime_wheels")
    if policy is None:
        if raw is not None:
            raise ReleaseAssetError(
                "Binary install plan has unexpected external Runtime wheels."
            )
        return None, None, None
    if not isinstance(raw, dict) or set(raw) != {
        "directory",
        "provenance",
        "provenance_sha256",
    }:
        raise ReleaseAssetError(
            "Binary install plan external Runtime provenance is invalid."
        )
    directory = Path(str(raw["directory"]))
    provenance_path = Path(str(raw["provenance"]))
    if (
        not _is_within_directory(plan_path.parent, directory)
        or provenance_path.parent.resolve() != directory.resolve()
    ):
        raise ReleaseAssetError(
            "External Runtime wheel directory escapes the install plan directory."
        )
    _require_regular_file(
        provenance_path,
        label="External Runtime wheel provenance",
    )
    if raw["provenance_sha256"] != sha256_file(provenance_path):
        raise ReleaseAssetError(
            "External Runtime wheel provenance SHA256 differs from the plan."
        )
    from scripts import prepare_external_vc_runtime_wheels as external_wheels

    try:
        payload = external_wheels.validate_output_directory(
            directory,
            components_file,
        )
    except (BinaryInstallPolicyError, external_wheels.WheelError, OSError) as exc:
        raise ReleaseAssetError(
            f"External Runtime wheel validation failed: {exc}"
        ) from exc
    return directory, payload, raw


def _resolve_opencv_source_build_plan(
    *,
    components_file: Path,
    lock: dict[str, Any],
    plan: dict[str, Any],
    plan_path: Path,
) -> tuple[Path | None, dict[str, Any] | None, dict[str, Any] | None]:
    from scripts import prepare_opencv_wheel as opencv_wheel

    try:
        policy = opencv_wheel.source_build_policy(lock)
    except opencv_wheel.OpenCVWheelError as exc:
        raise ReleaseAssetError(str(exc)) from exc
    raw = plan.get("opencv_source_build")
    if policy is None:
        if raw is not None:
            raise ReleaseAssetError("Binary install plan has an unexpected OpenCV build.")
        return None, None, None
    if not isinstance(raw, dict) or set(raw) != {
        "directory",
        "provenance",
        "provenance_sha256",
    }:
        raise ReleaseAssetError("Binary install plan OpenCV provenance is invalid.")
    directory = Path(str(raw["directory"]))
    provenance_path = Path(str(raw["provenance"]))
    if (
        not _is_within_directory(plan_path.parent, directory)
        or provenance_path.parent.resolve() != directory.resolve()
    ):
        raise ReleaseAssetError("OpenCV source build escapes the install plan directory.")
    _require_regular_file(provenance_path, label="OpenCV source-build provenance")
    if raw["provenance_sha256"] != sha256_file(provenance_path):
        raise ReleaseAssetError("OpenCV source-build provenance SHA256 differs.")
    try:
        payload = opencv_wheel.validate_output_directory(directory, components_file)
    except (opencv_wheel.OpenCVWheelError, OSError) as exc:
        raise ReleaseAssetError(f"OpenCV source-build validation failed: {exc}") from exc
    return directory, payload, raw


def _binary_install_requirements(
    *,
    lock: dict[str, Any],
    pins: list[dict[str, str]],
    binary_by_name: dict[str, dict[str, Any]],
    binary_cache_dir: Path,
    external_dir: Path | None,
    opencv_dir: Path | None,
    opencv_payload: dict[str, Any] | None,
    opencv_plan: dict[str, Any] | None,
) -> list[tuple[dict[str, Any], Path]]:
    components = {
        canonicalize_distribution_name(str(item["distribution"])): item
        for item in _component_entries(lock)
        if item.get("distribution")
    }
    result: list[tuple[dict[str, Any], Path]] = []
    for pin in pins:
        binary = binary_by_name.get(pin["canonical_name"])
        component = components.get(pin["canonical_name"])
        if component is None:
            raise ReleaseAssetError(
                f"Release requirement has no locked wheel: {pin['name']}"
            )
        source_built_opencv = (
            component.get("component") == "opencv-python"
            and lock.get("opencv_source_build_policy") is not None
        )
        if binary is None and not source_built_opencv:
            raise ReleaseAssetError(
                f"Release requirement has no locked wheel: {pin['name']}"
            )
        if source_built_opencv:
            if opencv_dir is None or opencv_payload is None or opencv_plan is None:
                raise ReleaseAssetError("Verified OpenCV source-built wheel is missing.")
            wheel = opencv_payload.get("wheel")
            if not isinstance(wheel, dict):
                raise ReleaseAssetError("OpenCV source-build wheel record is missing.")
            archive = {
                key: wheel.get(key) for key in ("filename", "sha256", "size")
            }
            if (
                wheel.get("distribution") != component.get("distribution")
                or wheel.get("version") != component.get("version")
                or not isinstance(archive["filename"], str)
                or not isinstance(archive["sha256"], str)
                or SHA256_PATTERN.fullmatch(archive["sha256"]) is None
                or not isinstance(archive["size"], int)
                or isinstance(archive["size"], bool)
                or archive["size"] <= 0
            ):
                raise ReleaseAssetError("OpenCV source-build wheel record differs.")
            source_kind = "source-built-wheel"
            path = opencv_dir / archive["filename"]
        else:
            try:
                source_kind, archive = expected_install_archive(lock, component)
            except BinaryInstallPolicyError as exc:
                raise ReleaseAssetError(str(exc)) from exc
            path = binary_cache_dir / str(archive["filename"])
        if source_kind == "external-vc-runtime-wheel":
            if external_dir is None:
                raise ReleaseAssetError(
                    f"External Runtime wheel is missing: {pin['name']}"
                )
            path = external_dir / str(archive["filename"])
        record: dict[str, Any] = {
            **pin,
            "source": source_kind,
            "component": component["component"],
            "distribution": component["distribution"],
            **archive,
        }
        if source_kind == "external-vc-runtime-wheel":
            assert binary is not None
            record["upstream_archive"] = {
                key: binary[key]
                for key in ("filename", "url", "sha256", "size")
            }
        elif source_kind == "source-built-wheel":
            record["source_build_provenance_sha256"] = opencv_plan[
                "provenance_sha256"
            ]
        result.append((record, path))
    return result


def prepare_binary_install(
    *,
    components_file: Path,
    requirements_file: Path,
    cache_dir: Path,
    output_requirements: Path,
    output_plan: Path,
    opener: Callable[..., BinaryIO] = urllib.request.urlopen,
    opencv_wheel_directory: Path | None = None,
    opencv_provenance_sha256: str | None = None,
) -> dict[str, Any]:
    lock = _load_lock(components_file)
    if not _is_within_directory(output_plan.parent, cache_dir):
        raise ReleaseAssetError(
            "Binary cache must remain inside the install plan directory."
        )
    pins, requirement_inputs = flatten_exact_requirements(requirements_file)
    pins_by_name = {item["canonical_name"]: item for item in pins}
    locked_distributions: dict[str, dict[str, Any]] = {}
    for component in _component_entries(lock):
        distribution = component.get("distribution")
        if not distribution:
            continue
        canonical = canonicalize_distribution_name(str(distribution))
        if canonical in locked_distributions:
            raise ReleaseAssetError(
                f"Duplicate component distribution: {distribution}"
            )
        locked_distributions[canonical] = component
        pin = pins_by_name.get(canonical)
        if pin is None:
            raise ReleaseAssetError(
                f"Locked distribution is absent from release requirements: "
                f"{distribution}"
            )
        if pin["version"] != str(component.get("version")):
            raise ReleaseAssetError(
                f"Release requirements version differs from component lock for "
                f"{distribution}."
            )

    binary_manifest_path = cache_dir / "verified-binaries.json"
    binary_payload = create_verified_binary_manifest(
        components_file,
        cache_dir,
        binary_manifest_path,
        opener=opener,
    )
    binary_by_name = {
        canonicalize_distribution_name(str(record["distribution"])): record
        for record in binary_payload["archives"]
    }
    external = _prepare_external_vc_runtime_wheels(
        components_file=components_file,
        lock=lock,
        cache_dir=cache_dir,
        binary_payload=binary_payload,
        opener=opener,
    )
    external_dir = Path(external["directory"]) if external is not None else None
    # The required external-Runtime preparation above loads pe_runtime_audit
    # from the hash-verified pefile wheel into this interpreter. The OpenCV
    # artifact audit below reuses that locked parser before application pip install.
    opencv = _prepare_opencv_source_wheel(
        components_file=components_file,
        lock=lock,
        cache_dir=cache_dir,
        opener=opener,
        prepared_directory=opencv_wheel_directory,
        expected_provenance_sha256=opencv_provenance_sha256,
    )
    opencv_dir = Path(opencv["directory"]) if opencv is not None else None
    install_requirements = _binary_install_requirements(
        lock=lock,
        pins=pins,
        binary_by_name=binary_by_name,
        binary_cache_dir=cache_dir,
        external_dir=external_dir,
        opencv_dir=opencv_dir,
        opencv_payload=opencv["payload"] if opencv is not None else None,
        opencv_plan=opencv,
    )
    lines = [
        "# Generated from exact repository pins and verified locked wheels.",
        "# Do not edit or reuse outside this release run.",
    ]
    plan_requirements = []
    for requirement, wheel_path in install_requirements:
        _require_regular_file(wheel_path, label="Verified install wheel")
        wheel_record = {
            "distribution": requirement["name"],
            "version": requirement["version"],
        }
        _verify_wheel_metadata(wheel_path, wheel_record)
        wheel_url = wheel_path.resolve().as_uri()
        lines.append(
            f"{requirement['name']} @ {wheel_url}#sha256={requirement['sha256']}"
        )
        plan_requirements.append(requirement)

    for target, label in (
        (output_requirements, "Generated release requirements"),
        (output_plan, "Binary install plan"),
    ):
        target.parent.mkdir(parents=True, exist_ok=True)
        _require_directory(target.parent, label=f"{label} directory")
        _reject_link_target(target, label=label)
    output_requirements.write_text("\n".join(lines) + "\n", encoding="utf-8")
    requirements_set = "".join(
        f"{item['canonical_name']}=={item['version']}\n"
        for item in sorted(pins, key=lambda item: item["canonical_name"])
    ).encode("utf-8")
    plan = {
        "schema_version": 1,
        "components_file": str(components_file.resolve()),
        "component_lock_sha256": sha256_file(components_file),
        "requirements_file": str(requirements_file.resolve()),
        "requirements_inputs": requirement_inputs,
        "requirements_set_sha256": hashlib.sha256(requirements_set).hexdigest(),
        "generated_requirements": str(output_requirements.resolve()),
        "generated_requirements_sha256": sha256_file(output_requirements),
        "release_python_version": str(lock["python"]["release_version"]),
        "release_binary_policy": lock["release_binary_policy"],
        "binary_manifest": str(binary_manifest_path.resolve()),
        "binary_manifest_sha256": sha256_file(binary_manifest_path),
        "external_vc_runtime_wheels": (
            {
                key: external[key]
                for key in ("directory", "provenance", "provenance_sha256")
            }
            if external is not None
            else None
        ),
        "opencv_source_build": (
            {
                key: opencv[key]
                for key in ("directory", "provenance", "provenance_sha256")
            }
            if opencv is not None
            else None
        ),
        "requirements": plan_requirements,
    }
    output_plan.write_text(
        json.dumps(plan, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return plan


def _installed_distribution_digest(distribution_name: str) -> str:
    from importlib import metadata

    distribution = metadata.distribution(distribution_name)
    records = []
    environment_root = Path(sys.prefix)
    for file_entry in distribution.files or ():
        relative = str(file_entry).replace("\\", "/")
        if relative.casefold().endswith(".pyc") or "/__pycache__/" in relative.casefold():
            continue
        path = Path(distribution.locate_file(file_entry))
        if not _is_within_directory(environment_root, path):
            raise ReleaseAssetError(
                f"Installed distribution file escapes Python environment: {path}"
            )
        _require_regular_file(path, label="Installed distribution file")
        records.append(
            {
                "path": relative,
                "size": path.stat().st_size,
                "sha256": sha256_file(path),
            }
        )
    if not records:
        raise ReleaseAssetError(
            f"Installed distribution has no verifiable files: {distribution_name}"
        )
    serialized = json.dumps(
        sorted(records, key=lambda item: item["path"].casefold()),
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()


def _installed_wheel_target(
    member: str,
    *,
    site_packages: Path,
    environment_root: Path,
) -> Path:
    relative = _safe_archive_member(member, label="wheel installation member")
    if len(relative.parts) >= 3 and relative.parts[0].casefold().endswith(".data"):
        scheme = relative.parts[1].casefold()
        remainder = relative.parts[2:]
        if scheme in {"purelib", "platlib"}:
            target = site_packages.joinpath(*remainder)
        elif scheme == "scripts":
            target = Path(sysconfig.get_path("scripts")).joinpath(*remainder)
        elif scheme == "data":
            target = environment_root.joinpath(*remainder)
        else:
            raise ReleaseAssetError(f"Unsupported wheel installation scheme: {scheme}")
    else:
        target = site_packages.joinpath(*relative.parts)
    if not _is_within_directory(environment_root, target):
        raise ReleaseAssetError(f"Wheel installation target escapes environment: {target}")
    return target.resolve()


def _generated_pyc_matches_source(pyc_path: Path, source_path: Path) -> bool:
    try:
        data = pyc_path.read_bytes()
        source_bytes = source_path.read_bytes()
    except OSError:
        return False
    if len(data) < 16 or data[:4] != importlib.util.MAGIC_NUMBER:
        return False
    flags = struct.unpack("<I", data[4:8])[0]
    if flags not in {0, 1, 3}:
        return False
    stream = io.BytesIO(data[16:])
    try:
        code = marshal.load(stream)
    except (EOFError, TypeError, ValueError):
        return False
    if not isinstance(code, types.CodeType) or stream.read(1):
        return False
    return any(
        code
        == compile(
            source_bytes,
            code.co_filename,
            "exec",
            dont_inherit=True,
            optimize=optimization,
        )
        for optimization in (0, 1, 2)
    )


def _generated_pyc_source(pyc_path: Path) -> Path | None:
    """Return the source for a PEP 3147 cache path, including dotted filenames."""
    cache_tag = sys.implementation.cache_tag
    if cache_tag is None or pyc_path.parent.name.casefold() != "__pycache__":
        return None
    match = re.fullmatch(
        rf"(?P<stem>.+)\.{re.escape(cache_tag)}(?:\.opt-[12])?\.pyc",
        pyc_path.name,
        flags=re.IGNORECASE,
    )
    if match is None:
        return None
    return (pyc_path.parent.parent / f"{match.group('stem')}.py").resolve()


def _verify_installed_distribution_from_wheel(
    distribution_name: str,
    wheel_path: Path,
) -> dict[str, Any]:
    from importlib import metadata

    installed = metadata.distribution(distribution_name)
    wheel_inventory = verified_wheel_record_inventory(
        wheel_path,
        distribution=distribution_name,
        version=installed.version,
    )
    environment_root = Path(sys.prefix).resolve()
    site_packages = Path(installed.locate_file("")).resolve()
    if not _is_within_directory(environment_root, site_packages):
        raise ReleaseAssetError(
            f"Installed distribution root escapes environment: {distribution_name}"
        )
    expected_by_target: dict[str, dict[str, Any]] = {}
    expected_by_wheel_path: dict[str, dict[str, Any]] = {}
    record_target: Path | None = None
    for artifact in wheel_inventory["artifacts"]:
        target = _installed_wheel_target(
            str(artifact["path"]),
            site_packages=site_packages,
            environment_root=environment_root,
        )
        key = os.path.normcase(str(target))
        if key in expected_by_target:
            raise ReleaseAssetError(
                f"Wheel members collide after installation: {distribution_name}"
            )
        expected_by_target[key] = {**artifact, "target": target}
        expected_by_wheel_path[str(artifact["path"]).casefold()] = {
            **artifact,
            "target": target,
        }
        if artifact["path"] == wheel_inventory["record_path"]:
            record_target = target
    if record_target is None:
        raise ReleaseAssetError(f"Installed RECORD is missing: {distribution_name}")
    _require_regular_file(record_target, label="Installed RECORD")

    try:
        installed_rows = list(
            csv.reader(
                record_target.read_text(encoding="utf-8", errors="strict").splitlines()
            )
        )
    except (OSError, UnicodeError, csv.Error) as exc:
        raise ReleaseAssetError(
            f"Installed RECORD is invalid for {distribution_name}: {exc}"
        ) from exc
    actual_by_target: dict[str, dict[str, Any]] = {}
    recorded_metadata_targets: set[str] = set()
    locked_record_targets: set[str] = set()
    expected_record_kinds: dict[str, set[str]] = {}
    record_self_spellings: set[str] = set()
    missing_record_targets: set[str] = set()
    relocated_record_targets: set[str] = set()
    for row in installed_rows:
        if len(row) != 3 or not row[0]:
            raise ReleaseAssetError(
                f"Installed RECORD row is invalid for {distribution_name}."
            )
        raw_relative = row[0].replace("/", os.sep).replace("\\", os.sep)
        candidate = (site_packages / raw_relative).resolve()
        if not _is_within_directory(environment_root, candidate):
            raise ReleaseAssetError(
                f"Installed RECORD escapes environment for {distribution_name}: {row[0]}"
            )
        key = os.path.normcase(str(candidate))
        recorded_metadata_targets.add(key)
        if not os.path.lexists(candidate):
            if key in missing_record_targets:
                raise ReleaseAssetError(
                    f"Installed RECORD path is duplicated for {distribution_name}: {row[0]}"
                )
            missing_record_targets.add(key)
            try:
                wheel_member = _safe_archive_member(
                    row[0],
                    label=f"installed RECORD for {distribution_name}",
                ).as_posix()
            except ReleaseAssetError:
                wheel_member = ""
            expected_origin = expected_by_wheel_path.get(wheel_member.casefold())
            member_parts = PurePosixPath(wheel_member).parts
            is_relocated_script = (
                expected_origin is not None
                and len(member_parts) >= 3
                and member_parts[0].casefold().endswith(".data")
                and member_parts[1].casefold() == "scripts"
                and candidate != expected_origin["target"]
            )
            if not is_relocated_script:
                raise ReleaseAssetError(
                    f"Installed distribution file must be a regular file: {candidate}"
                )
            encoded = base64.urlsafe_b64encode(
                bytes.fromhex(str(expected_origin["sha256"]))
            ).rstrip(b"=").decode("ascii")
            if (
                row[1] != f"sha256={encoded}"
                or row[2] != str(expected_origin["size"])
            ):
                raise ReleaseAssetError(
                    f"Relocated script RECORD differs from locked wheel: "
                    f"{distribution_name}: {row[0]}"
                )
            locked_record_targets.add(
                os.path.normcase(str(expected_origin["target"]))
            )
            relocated_record_targets.add(key)
            continue
        _require_regular_file(candidate, label="Installed distribution file")
        if candidate == record_target:
            if row[1] or row[2]:
                raise ReleaseAssetError(
                    f"Installed RECORD self-entry must be unhashed: {distribution_name}"
                )
            spelling = row[0].casefold()
            if spelling in record_self_spellings or len(record_self_spellings) >= 2:
                raise ReleaseAssetError(
                    f"Installed RECORD self-entry is duplicated: {distribution_name}"
                )
            record_self_spellings.add(spelling)
        elif key in expected_by_target:
            expected = expected_by_target[key]
            actual_size = candidate.stat().st_size
            actual_sha256 = sha256_file(candidate)
            if (
                actual_size != expected["size"]
                or actual_sha256 != expected["sha256"]
            ):
                raise ReleaseAssetError(
                    f"Installed file differs from locked wheel for {distribution_name}: "
                    f"{expected['path']}"
                )
            encoded = base64.urlsafe_b64encode(
                bytes.fromhex(actual_sha256)
            ).rstrip(b"=").decode("ascii")
            if row[1] == f"sha256={encoded}" and row[2] == str(actual_size):
                record_kind = "locked"
                locked_record_targets.add(key)
            elif not row[1] and not row[2]:
                record_kind = "pip-generated"
            else:
                raise ReleaseAssetError(
                    f"Installed RECORD digest differs for {distribution_name}: {row[0]}"
                )
            kinds = expected_record_kinds.setdefault(key, set())
            if record_kind in kinds or len(kinds) >= 2:
                raise ReleaseAssetError(
                    f"Installed RECORD path is duplicated for {distribution_name}: {row[0]}"
                )
            kinds.add(record_kind)
            if key in actual_by_target:
                continue
        elif candidate.suffix.casefold() == ".pyc" and "__pycache__" in candidate.parts:
            if key in actual_by_target:
                raise ReleaseAssetError(
                    f"Installed RECORD path is duplicated for {distribution_name}: {row[0]}"
                )
            if bool(row[1]) != bool(row[2]):
                raise ReleaseAssetError(
                    f"Generated pyc RECORD row is incomplete: {distribution_name}: "
                    f"{row[0]}"
                )
            if row[1]:
                encoded = base64.urlsafe_b64encode(
                    bytes.fromhex(sha256_file(candidate))
                ).rstrip(b"=").decode("ascii")
                if (
                    row[1] != f"sha256={encoded}"
                    or row[2] != str(candidate.stat().st_size)
                ):
                    raise ReleaseAssetError(
                        f"Generated pyc RECORD digest differs: "
                        f"{distribution_name}: {row[0]}"
                    )
        else:
            if key in actual_by_target:
                raise ReleaseAssetError(
                    f"Installed RECORD path is duplicated for {distribution_name}: {row[0]}"
                )
            encoded = base64.urlsafe_b64encode(
                bytes.fromhex(sha256_file(candidate))
            ).rstrip(b"=").decode("ascii")
            if row[1] != f"sha256={encoded}" or row[2] != str(candidate.stat().st_size):
                raise ReleaseAssetError(
                    f"Installed RECORD digest differs for {distribution_name}: {row[0]}"
                )
        actual_by_target[key] = {
            "path": candidate.relative_to(environment_root).as_posix(),
            "size": candidate.stat().st_size,
            "sha256": sha256_file(candidate),
        }

    unlocked_targets = sorted(
        str(expected_by_target[key]["path"])
        for key in actual_by_target
        if key in expected_by_target
        and Path(str(expected_by_target[key]["target"])) != record_target
        and key not in locked_record_targets
    )
    if unlocked_targets:
        raise ReleaseAssetError(
            f"Installed RECORD has no locked-wheel digest for {distribution_name}: "
            + ", ".join(unlocked_targets[:5])
        )

    metadata_files = {
        os.path.normcase(str(Path(installed.locate_file(item)).resolve()))
        for item in installed.files or ()
    }
    if metadata_files | relocated_record_targets != recorded_metadata_targets:
        raise ReleaseAssetError(
            f"Installed metadata file set differs from RECORD: {distribution_name}"
        )

    generated: list[dict[str, Any]] = []
    exact_records: list[dict[str, Any]] = []
    expected_source_targets = {
        key: record
        for key, record in expected_by_target.items()
        if str(record["path"]).casefold().endswith(".py")
    }
    entry_point_names = {
        entry.name.casefold()
        for entry in installed.entry_points
        if entry.group in {"console_scripts", "gui_scripts"}
    }
    if canonicalize_distribution_name(distribution_name) == "pip":
        entry_point_names.update(
            {
                "pip",
                f"pip{sys.version_info.major}",
                f"pip{sys.version_info.major}.{sys.version_info.minor}",
            }
        )
    scripts_root = Path(sysconfig.get_path("scripts")).resolve()
    for key, actual in actual_by_target.items():
        expected = expected_by_target.get(key)
        target = environment_root / str(actual["path"])
        if expected is not None:
            if target == record_target:
                generated.append({**actual, "reason": "rewritten-record"})
            elif any(actual[field] != expected[field] for field in ("size", "sha256")):
                raise ReleaseAssetError(
                    f"Installed file differs from locked wheel for {distribution_name}: "
                    f"{expected['path']}"
                )
            else:
                exact_records.append(
                    {
                        **actual,
                        "wheel_path": expected["path"],
                    }
                )
            continue
        name = target.name
        if name in {"INSTALLER", "REQUESTED", "direct_url.json"} and target.parent == record_target.parent:
            if name == "INSTALLER" and target.read_bytes() != b"pip\n":
                raise ReleaseAssetError(
                    f"Installed INSTALLER marker differs: {distribution_name}"
                )
            if name == "REQUESTED" and target.read_bytes() != b"":
                raise ReleaseAssetError(
                    f"Installed REQUESTED marker differs: {distribution_name}"
                )
            generated.append({**actual, "reason": name.casefold()})
            continue
        if name.casefold().endswith(".pyc") and "__pycache__" in target.parts:
            source = _generated_pyc_source(target)
            if source is None:
                source = Path()
            source_key = os.path.normcase(str(source))
            if source_key not in expected_source_targets or not _generated_pyc_matches_source(
                target,
                source,
            ):
                raise ReleaseAssetError(
                    f"Generated bytecode differs from locked wheel source: {target}"
                )
            generated.append({**actual, "reason": "verified-pyc"})
            continue
        if target.parent == scripts_root:
            stem = target.stem.casefold().removesuffix("-script")
            if stem in entry_point_names and target.suffix.casefold() in {
                ".exe",
                ".py",
                ".cmd",
            }:
                generated.append({**actual, "reason": "entry-point-wrapper"})
                continue
        raise ReleaseAssetError(
            f"Installed file is not derived from locked wheel: {distribution_name}: "
            f"{actual['path']}"
        )
    missing = [
        record["path"]
        for key, record in expected_by_target.items()
        if key not in actual_by_target
    ]
    if missing:
        raise ReleaseAssetError(
            f"Installed locked wheel files are missing for {distribution_name}: "
            + ", ".join(sorted(missing)[:5])
        )
    records = sorted(
        [*exact_records, *generated],
        key=lambda item: str(item["path"]).casefold(),
    )
    serialized = json.dumps(
        records,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return {
        "inventory_sha256": hashlib.sha256(serialized).hexdigest(),
        "files": records,
    }


def _verify_bootstrap_pip_environment(
    python_native_runtime: dict[str, Any],
) -> dict[str, Any]:
    from importlib import metadata

    ensurepip_wheel = python_native_runtime.get("ensurepip_wheel")
    if not isinstance(ensurepip_wheel, dict):
        raise ReleaseAssetError("Verified ensurepip wheel inventory is missing.")
    relative = ensurepip_wheel.get("relative_path")
    if not isinstance(relative, str):
        raise ReleaseAssetError("Verified ensurepip wheel path is missing.")
    wheel_path = Path(sys.base_prefix).joinpath(*PurePosixPath(relative).parts)
    _require_regular_file(wheel_path, label="Bootstrap pip wheel")
    if (
        not _is_within_directory(Path(sys.base_prefix), wheel_path)
        or wheel_path.stat().st_size != ensurepip_wheel.get("size")
        or sha256_file(wheel_path) != ensurepip_wheel.get("sha256")
    ):
        raise ReleaseAssetError("Bootstrap pip wheel differs from verified runtime.")
    inventory = _verify_installed_distribution_from_wheel("pip", wheel_path)
    if metadata.version("pip") != ensurepip_wheel.get("version"):
        raise ReleaseAssetError("Installed pip version differs from ensurepip wheel.")
    return {
        "filename": ensurepip_wheel.get("filename"),
        "version": ensurepip_wheel.get("version"),
        "size": ensurepip_wheel.get("size"),
        "sha256": ensurepip_wheel.get("sha256"),
        **inventory,
    }


def verify_python_runtime(components_file: Path) -> dict[str, Any]:
    """Verify the base interpreter before it is allowed to create a release venv."""
    if Path(sys.prefix).resolve() != Path(sys.base_prefix).resolve():
        raise ReleaseAssetError(
            "Release base Python verification must run outside a virtual environment."
        )
    try:
        return probe_python_native_runtime(_load_lock(components_file))
    except (KeyError, OSError, RuntimeError, ValueError) as exc:
        raise ReleaseAssetError(f"Release Python runtime verification failed: {exc}") from exc


def verify_bootstrap_pip(components_file: Path) -> dict[str, Any]:
    """Verify ensurepip and its installed pip before any release dependency install."""
    if Path(sys.prefix).resolve() == Path(sys.base_prefix).resolve():
        raise ReleaseAssetError(
            "Bootstrap pip verification must run inside the new release virtual "
            "environment."
        )
    try:
        runtime = probe_python_native_runtime(_load_lock(components_file))
        return _verify_bootstrap_pip_environment(runtime)
    except (KeyError, OSError, RuntimeError, ValueError) as exc:
        raise ReleaseAssetError(f"Bootstrap pip verification failed: {exc}") from exc


def _verify_environment_file_ownership(
    allowed_environment_files: set[str],
) -> None:
    from importlib import metadata

    site_packages = Path(metadata.distribution("pip").locate_file("")).resolve()
    for candidate in site_packages.rglob("*"):
        if candidate.is_dir():
            continue
        _require_regular_file(candidate, label="Installed environment file")
        if os.path.normcase(str(candidate.resolve())) not in allowed_environment_files:
            raise ReleaseAssetError(
                f"Installed environment contains an unowned file: "
                f"{candidate.relative_to(site_packages).as_posix()}"
            )


def verify_recorded_install_inventory(
    distribution_name: str,
    record: dict[str, Any],
) -> set[str]:
    """Re-hash a recorded install inventory and return its owned absolute paths."""
    from importlib import metadata

    files = record.get("installed_files", record.get("files"))
    expected_digest = record.get(
        "installed_files_sha256",
        record.get("inventory_sha256"),
    )
    if not isinstance(files, list) or not files:
        raise ReleaseAssetError(
            f"Recorded installed-file inventory is missing: {distribution_name}"
        )
    if not isinstance(expected_digest, str) or SHA256_PATTERN.fullmatch(
        expected_digest
    ) is None:
        raise ReleaseAssetError(
            f"Recorded installed-file inventory digest is invalid: {distribution_name}"
        )
    environment_root = Path(sys.prefix).resolve()
    normalized: list[dict[str, Any]] = []
    owned: set[str] = set()
    for item in files:
        if not isinstance(item, dict):
            raise ReleaseAssetError(
                f"Recorded installed-file entry is invalid: {distribution_name}"
            )
        relative = item.get("path")
        size = item.get("size")
        digest = item.get("sha256")
        provenance_fields = {
            key for key in ("wheel_path", "reason") if key in item
        }
        if (
            not isinstance(relative, str)
            or not relative
            or PurePosixPath(relative).is_absolute()
            or PurePosixPath(relative).as_posix() != relative
            or any(part in {"", ".", ".."} for part in PurePosixPath(relative).parts)
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
            or not isinstance(digest, str)
            or SHA256_PATTERN.fullmatch(digest) is None
            or provenance_fields not in ({"wheel_path"}, {"reason"})
            or set(item) != {"path", "size", "sha256", *provenance_fields}
        ):
            raise ReleaseAssetError(
                f"Recorded installed-file entry is invalid: "
                f"{distribution_name}: {relative}"
            )
        if "wheel_path" in item:
            wheel_path = item["wheel_path"]
            if (
                not isinstance(wheel_path, str)
                or PurePosixPath(wheel_path).is_absolute()
                or PurePosixPath(wheel_path).as_posix() != wheel_path
                or any(
                    part in {"", ".", ".."}
                    for part in PurePosixPath(wheel_path).parts
                )
            ):
                raise ReleaseAssetError(
                    f"Recorded wheel member path is invalid: "
                    f"{distribution_name}: {wheel_path}"
                )
        else:
            reason = item["reason"]
            if reason not in {
                "rewritten-record",
                "installer",
                "requested",
                "direct_url.json",
                "verified-pyc",
                "entry-point-wrapper",
            }:
                raise ReleaseAssetError(
                    f"Recorded generated-file reason is invalid: "
                    f"{distribution_name}: {reason}"
                )
        target = environment_root.joinpath(*PurePosixPath(relative).parts).resolve()
        key = os.path.normcase(str(target))
        if not _is_within_directory(environment_root, target) or key in owned:
            raise ReleaseAssetError(
                f"Recorded installed-file path is unsafe or duplicated: "
                f"{distribution_name}: {relative}"
            )
        _require_regular_file(target, label="Recorded installed file")
        if target.stat().st_size != size or sha256_file(target) != digest:
            raise ReleaseAssetError(
                f"Recorded installed file differs: {distribution_name}: {relative}"
            )
        owned.add(key)
        normalized.append(dict(item))
    if normalized != sorted(
        normalized,
        key=lambda item: str(item["path"]).casefold(),
    ):
        raise ReleaseAssetError(
            f"Recorded installed-file inventory is not sorted: {distribution_name}"
        )
    serialized = json.dumps(
        normalized,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    if hashlib.sha256(serialized).hexdigest() != expected_digest:
        raise ReleaseAssetError(
            f"Recorded installed-file inventory digest differs: {distribution_name}"
        )
    try:
        installed = metadata.distribution(distribution_name)
    except metadata.PackageNotFoundError as exc:
        raise ReleaseAssetError(
            f"Recorded installed distribution is missing: {distribution_name}"
        ) from exc
    metadata_files = {
        os.path.normcase(str(Path(installed.locate_file(item)).resolve()))
        for item in installed.files or ()
    }
    if metadata_files != owned:
        raise ReleaseAssetError(
            f"Installed metadata file set differs from recorded inventory: "
            f"{distribution_name}"
        )
    return owned


def _is_within_directory(root: Path, candidate: Path) -> bool:
    try:
        candidate.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    return True


def attest_binary_install(
    *,
    components_file: Path,
    plan_path: Path,
    pip_report_path: Path,
    output_provenance: Path,
) -> dict[str, Any]:
    _require_regular_file(plan_path, label="Binary install plan")
    _require_regular_file(pip_report_path, label="pip install report")
    lock = _load_lock(components_file)
    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        report = json.loads(pip_report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReleaseAssetError(f"Cannot read install attestation input: {exc}") from exc
    if not isinstance(plan, dict) or plan.get("schema_version") != 1:
        raise ReleaseAssetError("Unsupported binary install plan schema.")
    if plan.get("component_lock_sha256") != sha256_file(components_file):
        raise ReleaseAssetError("Binary install plan component lock hash differs.")
    binary_manifest_path = Path(str(plan.get("binary_manifest", "")))
    if not _is_within_directory(plan_path.parent, binary_manifest_path):
        raise ReleaseAssetError("Binary manifest escapes the install plan directory.")
    binary_payload = verify_binary_manifest(
        binary_manifest_path,
        components_file=components_file,
    )
    if plan.get("binary_manifest_sha256") != sha256_file(binary_manifest_path):
        raise ReleaseAssetError("Binary install plan manifest hash differs.")
    policy = lock["release_binary_policy"]
    if plan.get("release_binary_policy") != policy:
        raise ReleaseAssetError("Binary install plan release policy differs from lock.")
    requirements_file = Path(str(plan.get("requirements_file", "")))
    live_pins, live_inputs = flatten_exact_requirements(requirements_file)
    if plan.get("requirements_inputs") != live_inputs:
        raise ReleaseAssetError("Binary install plan requirement inputs differ.")
    requirements_set = "".join(
        f"{item['canonical_name']}=={item['version']}\n"
        for item in sorted(live_pins, key=lambda item: item["canonical_name"])
    ).encode("utf-8")
    if plan.get("requirements_set_sha256") != hashlib.sha256(
        requirements_set
    ).hexdigest():
        raise ReleaseAssetError("Binary install plan requirement set hash differs.")
    generated_requirements = Path(
        str(plan.get("generated_requirements", ""))
    )
    if generated_requirements.parent.resolve() != plan_path.parent.resolve():
        raise ReleaseAssetError(
            "Generated release requirements must remain beside the install plan."
        )
    _require_regular_file(
        generated_requirements,
        label="Generated release requirements",
    )
    if plan.get("generated_requirements_sha256") != sha256_file(
        generated_requirements
    ):
        raise ReleaseAssetError("Generated release requirements hash differs.")
    if sys.implementation.name != "cpython":
        raise ReleaseAssetError("Release install must use CPython.")
    actual_python = sys.version.split()[0]
    if actual_python != policy["python_version"]:
        raise ReleaseAssetError(
            f"Release install Python differs: {actual_python} != "
            f"{policy['python_version']}"
        )
    if os.name != "nt" or platform.machine().casefold() not in {
        "amd64",
        "x86_64",
    }:
        raise ReleaseAssetError("Release install must use Windows amd64.")
    python_native_runtime = probe_python_native_runtime(lock)
    bootstrap_pip = _verify_bootstrap_pip_environment(python_native_runtime)
    allowed_environment_files = {
        os.path.normcase(str((Path(sys.prefix) / item["path"]).resolve()))
        for item in bootstrap_pip["files"]
    }
    if not isinstance(report, dict) or str(report.get("version")) != "1":
        raise ReleaseAssetError("Unsupported pip install report schema.")
    raw_install = report.get("install")
    if not isinstance(raw_install, list):
        raise ReleaseAssetError("pip install report has no install list.")
    expected_requirements = plan.get("requirements")
    if not isinstance(expected_requirements, list):
        raise ReleaseAssetError("Binary install plan has no requirements list.")
    binary_by_name = {
        canonicalize_distribution_name(str(record["distribution"])): record
        for record in binary_payload["archives"]
    }
    external_dir, external_payload, external_plan = (
        _resolve_external_vc_runtime_plan(
            components_file=components_file,
            lock=lock,
            plan=plan,
            plan_path=plan_path,
        )
    )
    opencv_dir, opencv_payload, opencv_plan = _resolve_opencv_source_build_plan(
        components_file=components_file,
        lock=lock,
        plan=plan,
        plan_path=plan_path,
    )
    expected_install_requirements = _binary_install_requirements(
        lock=lock,
        pins=live_pins,
        binary_by_name=binary_by_name,
        binary_cache_dir=binary_manifest_path.parent,
        external_dir=external_dir,
        opencv_dir=opencv_dir,
        opencv_payload=opencv_payload,
        opencv_plan=opencv_plan,
    )
    expected_plan_requirements = [
        requirement for requirement, _path in expected_install_requirements
    ]
    if expected_requirements != expected_plan_requirements:
        raise ReleaseAssetError("Binary install plan requirements differ from lock.")
    expected_by_name = {
        str(item["canonical_name"]): item for item in expected_requirements
    }
    expected_paths = {
        str(requirement["canonical_name"]): path
        for requirement, path in expected_install_requirements
    }
    reported_by_name: dict[str, dict[str, Any]] = {}
    for item in raw_install:
        if not isinstance(item, dict) or not isinstance(item.get("metadata"), dict):
            raise ReleaseAssetError("pip install report contains an invalid item.")
        metadata_record = item["metadata"]
        canonical = canonicalize_distribution_name(str(metadata_record.get("name", "")))
        if not canonical or canonical in reported_by_name:
            raise ReleaseAssetError(
                f"pip install report contains a duplicate or unnamed package: {canonical}"
            )
        if item.get("requested") is not True or item.get("is_yanked") is not False:
            raise ReleaseAssetError(
                f"pip install report package was not directly requested or is yanked: "
                f"{canonical}"
            )
        reported_by_name[canonical] = item
    if set(reported_by_name) != set(expected_by_name):
        raise ReleaseAssetError("pip install report package set differs from plan.")

    installed_binaries = []
    from importlib import metadata

    for canonical, expected in expected_by_name.items():
        report_item = reported_by_name[canonical]
        report_metadata = report_item["metadata"]
        if str(report_metadata.get("version")) != str(expected["version"]):
            raise ReleaseAssetError(
                f"pip install report version differs for {canonical}."
            )
        try:
            installed = metadata.distribution(str(expected["name"]))
        except metadata.PackageNotFoundError as exc:
            raise ReleaseAssetError(
                f"Installed distribution is missing: {expected['name']}"
            ) from exc
        if installed.version != str(expected["version"]):
            raise ReleaseAssetError(
                f"Installed distribution version differs for {canonical}."
            )
        download_info = report_item.get("download_info")
        if not isinstance(download_info, dict) or report_item.get("is_direct") is not True:
            raise ReleaseAssetError(
                f"Locked wheel was not installed as a direct URL: {canonical}"
            )
        parsed_url = urllib.parse.urlsplit(str(download_info.get("url", "")))
        if parsed_url.scheme != "file":
            raise ReleaseAssetError(
                f"Locked wheel report URL is not a local file: {canonical}"
            )
        reported_path = Path(
            urllib.request.url2pathname(urllib.parse.unquote(parsed_url.path))
        )
        expected_path = expected_paths[canonical]
        if reported_path.resolve() != expected_path.resolve():
            raise ReleaseAssetError(
                f"Locked wheel report path differs for {canonical}."
            )
        archive_info = download_info.get("archive_info")
        hashes = archive_info.get("hashes") if isinstance(archive_info, dict) else None
        if not isinstance(hashes, dict) or hashes.get("sha256") != expected["sha256"]:
            raise ReleaseAssetError(
                f"Locked wheel report SHA256 differs for {canonical}."
            )
        direct_url_text = installed.read_text("direct_url.json")
        try:
            direct_url = json.loads(direct_url_text or "")
        except json.JSONDecodeError as exc:
            raise ReleaseAssetError(
                f"Installed direct_url.json is invalid for {canonical}."
            ) from exc
        direct_archive = direct_url.get("archive_info") if isinstance(direct_url, dict) else None
        direct_hashes = (
            direct_archive.get("hashes") if isinstance(direct_archive, dict) else None
        )
        if (
            direct_url.get("url") != expected_path.resolve().as_uri()
            or not isinstance(direct_hashes, dict)
            or direct_hashes.get("sha256") != expected["sha256"]
        ):
            raise ReleaseAssetError(
                f"Installed direct_url provenance differs for {canonical}."
            )
        installed_inventory = _verify_installed_distribution_from_wheel(
            str(expected["name"]),
            expected_path,
        )
        allowed_environment_files.update(
            os.path.normcase(str((Path(sys.prefix) / item["path"]).resolve()))
            for item in installed_inventory["files"]
        )
        installed_binaries.append(
            {
                "component": expected["component"],
                "distribution": expected["distribution"],
                "version": expected["version"],
                "source": expected["source"],
                "filename": expected["filename"],
                "size": expected["size"],
                "sha256": expected["sha256"],
                **(
                    {"upstream_archive": expected["upstream_archive"]}
                    if "upstream_archive" in expected
                    else {}
                ),
                **(
                    {
                        "source_build_provenance_sha256": expected[
                            "source_build_provenance_sha256"
                        ]
                    }
                    if "source_build_provenance_sha256" in expected
                    else {}
                ),
                "installed_files_sha256": installed_inventory["inventory_sha256"],
                "installed_files": installed_inventory["files"],
            }
        )
    if {item["component"] for item in installed_binaries} != set(
        policy["required_components"]
    ):
        raise ReleaseAssetError("Installed locked binary component set differs.")
    _verify_environment_file_ownership(allowed_environment_files)
    git_source = capture_git_source_identity()

    provenance = {
        "schema_version": 1,
        "component_lock_sha256": sha256_file(components_file),
        "requirements_inputs": plan["requirements_inputs"],
        "requirements_set_sha256": plan["requirements_set_sha256"],
        "binary_manifest_sha256": plan["binary_manifest_sha256"],
        "external_vc_runtime_wheels": (
            {
                "provenance_sha256": external_plan["provenance_sha256"],
                "provenance": external_payload,
            }
            if external_plan is not None and external_payload is not None
            else None
        ),
        "opencv_source_build": (
            {
                "provenance_sha256": opencv_plan["provenance_sha256"],
                "provenance": opencv_payload,
            }
            if opencv_plan is not None and opencv_payload is not None
            else None
        ),
        "python_implementation": sys.implementation.name,
        "python_version": actual_python,
        "platform": "win_amd64",
        "pip_version": str(report.get("pip_version", "")),
        "bootstrap_pip": bootstrap_pip,
        "git_source": git_source,
        "installed_binaries": sorted(
            installed_binaries,
            key=lambda item: str(item["component"]).casefold(),
        ),
        "python_native_runtime": python_native_runtime,
    }
    if provenance["pip_version"] != policy.get("pip_version"):
        raise ReleaseAssetError("pip install report version differs from release policy.")
    output_provenance.parent.mkdir(parents=True, exist_ok=True)
    _require_directory(
        output_provenance.parent,
        label="Build provenance directory",
    )
    _reject_link_target(output_provenance, label="Build provenance")
    output_provenance.write_text(
        json.dumps(provenance, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return provenance


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


def _release_python_runtime_record(lock: dict[str, Any]) -> dict[str, Any]:
    profile_errors = _python_native_profile_errors(lock)
    if profile_errors:
        raise ReleaseAssetError(" | ".join(profile_errors))
    python_lock = lock["python"]
    version = str(python_lock["release_version"])
    profile = python_lock["windows_native_runtime_profiles"][version]
    runtime_source = profile["runtime_source"]
    if runtime_source != PYTHON_RUNTIME_SOURCE:
        raise ReleaseAssetError("Release Python runtime source is not uniquely locked.")
    archive = profile[runtime_source]
    return {
        "component": "python",
        "version": version,
        "filename": _safe_filename(archive["filename"]),
        "url": str(archive["url"]),
        "sha256": str(archive["sha256"]).casefold(),
        "size": int(archive["size"]),
    }


def _validated_python_runtime_members(
    archive: zipfile.ZipFile,
) -> dict[str, zipfile.ZipInfo]:
    label = "locked Python runtime archive"
    for info in archive.infolist():
        if "\\" in info.filename:
            raise ReleaseAssetError(
                f"Backslash path is forbidden in {label}: {info.filename}"
            )
    members = _validated_zip_members(archive, label=label)
    files = {
        name.casefold()
        for name, info in members.items()
        if not info.is_dir()
    }
    for name in members:
        parts = PurePosixPath(name).parts
        for index in range(1, len(parts)):
            parent = PurePosixPath(*parts[:index]).as_posix().casefold()
            if parent in files:
                raise ReleaseAssetError(
                    f"File/directory collision in {label}: {name}"
                )
    python_executable = members.get("python.exe")
    if python_executable is None or python_executable.is_dir():
        raise ReleaseAssetError(
            "Locked Python runtime archive does not contain root python.exe."
        )
    return members


def prepare_python_runtime(
    *,
    components_file: Path,
    cache_dir: Path,
    output_dir: Path,
    opener: Callable[..., BinaryIO] = urllib.request.urlopen,
) -> Path:
    """Fetch and transactionally extract the locked release Python runtime."""
    components_file = components_file.absolute()
    raw_cache_dir = cache_dir.absolute()
    raw_output_dir = output_dir.absolute()
    _safe_path_component(
        raw_output_dir.name,
        label="Python runtime output directory name",
    )
    if os.path.lexists(raw_output_dir):
        raise ReleaseAssetError(
            f"Python runtime output must not already exist: {raw_output_dir}"
        )
    if os.path.lexists(raw_cache_dir) and _path_is_link_or_reparse(raw_cache_dir):
        raise ReleaseAssetError(
            f"Python runtime cache must not be a link: {raw_cache_dir}"
        )
    cache_dir = raw_cache_dir.resolve(strict=False)
    output_dir = raw_output_dir.parent.resolve(strict=False) / raw_output_dir.name
    if (
        cache_dir == output_dir
        or cache_dir.is_relative_to(output_dir)
        or output_dir.is_relative_to(cache_dir)
    ):
        raise ReleaseAssetError(
            "Python runtime cache and output directories must not overlap."
        )
    try:
        lock = _load_lock(components_file)
        record = _release_python_runtime_record(lock)
        fetched = fetch_verified_sources([record], cache_dir, opener=opener)
        archive_path = fetched[0][1]

        output_parent = output_dir.parent
        output_parent.mkdir(parents=True, exist_ok=True)
        _require_directory(output_parent, label="Python runtime output parent")
        if os.path.lexists(output_dir):
            raise ReleaseAssetError(
                f"Python runtime output appeared during preparation: {output_dir}"
            )
    except ReleaseAssetError:
        raise
    except (OSError, RuntimeError) as exc:
        raise ReleaseAssetError(
            f"Cannot prepare locked Python runtime archive: {exc}"
        ) from exc

    try:
        with tempfile.TemporaryDirectory(
            prefix=f".{output_dir.name}-",
            dir=output_parent,
        ) as transaction_root_text:
            transaction_root = Path(transaction_root_text)
            staging = transaction_root / "runtime"
            staging.mkdir()
            with zipfile.ZipFile(archive_path) as archive:
                members = _validated_python_runtime_members(archive)
                for name, info in members.items():
                    relative = PurePosixPath(name)
                    target = staging.joinpath(*relative.parts)
                    if info.is_dir():
                        target.mkdir(parents=True, exist_ok=True)
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(info) as source, target.open("xb") as output:
                        shutil.copyfileobj(source, output, COPY_BUFFER_SIZE)
            python_executable = staging / "python.exe"
            _require_regular_file(
                python_executable,
                label="Extracted release Python executable",
            )
            if python_executable.stat().st_size <= 0:
                raise ReleaseAssetError(
                    "Extracted release Python executable is empty."
                )
            if os.path.lexists(output_dir):
                raise ReleaseAssetError(
                    f"Python runtime output appeared during extraction: {output_dir}"
                )
            staging.replace(output_dir)
    except ReleaseAssetError:
        raise
    except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
        raise ReleaseAssetError(
            f"Cannot extract locked Python runtime archive: {exc}"
        ) from exc
    return output_dir / "python.exe"


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


def validate_application_source(
    source_zip: Path,
    version: str,
    source_commit: str | None = None,
) -> None:
    _require_regular_file(source_zip, label="Application source archive")
    if source_zip.stat().st_size >= MAX_GITHUB_ASSET_SIZE:
        raise ReleaseAssetError("Application source archive exceeds GitHub asset limit.")
    try:
        with zipfile.ZipFile(source_zip) as archive:
            names = _validated_zip_members(
                archive,
                label="application source archive",
            )
            forbidden_runtime = next(
                (
                    name
                    for name, info in names.items()
                    if not info.is_dir() and is_user_provided_runtime_path(name)
                ),
                None,
            )
            if forbidden_runtime is not None:
                raise ReleaseAssetError(
                    "User-provided OBS/standalone FFmpeg must not be included "
                    f"in the application source archive: {forbidden_runtime}"
                )
            folded_names = {name.casefold(): name for name in names}
            for required in ("LICENSE", "VERSION"):
                if required.casefold() not in folded_names:
                    raise ReleaseAssetError(f"Application source archive is missing {required}.")
            archived_version = archive.read(names[folded_names["version"]]).decode("utf-8").strip()
            if archived_version != version:
                raise ReleaseAssetError(f"Application source VERSION mismatch: {archived_version} != {version}")
            if source_commit is not None:
                source_commit = source_commit.casefold()
                if COMMIT_PATTERN.fullmatch(source_commit) is None:
                    raise ReleaseAssetError("Application source commit is invalid.")
                resolved_commit = str(
                    _git_output("rev-parse", f"{source_commit}^{{commit}}")
                ).strip().casefold()
                if resolved_commit != source_commit:
                    raise ReleaseAssetError(
                        "Application source commit does not resolve exactly."
                    )
                tree_raw = _git_output(
                    "ls-tree",
                    "-r",
                    "-z",
                    source_commit,
                    binary=True,
                )
                if not isinstance(tree_raw, bytes):
                    raise ReleaseAssetError("Application source Git tree is invalid.")
                expected_blobs: dict[str, tuple[str, str]] = {}
                for raw_entry in tree_raw.split(b"\0"):
                    if not raw_entry:
                        continue
                    try:
                        metadata_raw, raw_path = raw_entry.split(b"\t", 1)
                        mode, object_type, object_id = metadata_raw.split(b" ")
                        relative = raw_path.decode("utf-8", errors="strict")
                    except (UnicodeError, ValueError) as exc:
                        raise ReleaseAssetError(
                            "Cannot parse application source Git tree."
                        ) from exc
                    if object_type != b"blob" or _safe_archive_member(
                        relative,
                        label="application source Git tree",
                    ).as_posix() != relative:
                        raise ReleaseAssetError(
                            f"Application source Git tree entry is unsupported: {relative}"
                        )
                    expected_blobs[relative] = (
                        mode.decode("ascii"),
                        object_id.decode("ascii"),
                    )
                archived_files = {
                    name: info for name, info in names.items() if not info.is_dir()
                }
                if set(archived_files) != set(expected_blobs):
                    raise ReleaseAssetError(
                        "Application source archive file set differs from its Git commit."
                    )
                for relative, (mode, object_id) in expected_blobs.items():
                    archived_mode = (
                        archived_files[relative].external_attr >> 16
                    ) & 0xFFFF
                    if f"{archived_mode:o}" != mode:
                        raise ReleaseAssetError(
                            f"Application source mode differs from Git tree: {relative}"
                        )
                    blob = _git_output("cat-file", "blob", object_id, binary=True)
                    if not isinstance(blob, bytes) or archive.read(
                        archived_files[relative]
                    ) != blob:
                        raise ReleaseAssetError(
                            f"Application source archive differs from Git blob: {relative}"
                        )
    except (OSError, RuntimeError, zipfile.BadZipFile, UnicodeDecodeError) as exc:
        raise ReleaseAssetError(f"Cannot validate application source archive: {exc}") from exc


def create_source_parts(
    version: str,
    sources: list[tuple[dict[str, Any], Path]],
    output_dir: Path,
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


def verify_source_part(path: Path) -> list[dict[str, Any]]:
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
            return records
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


def verify_license_archive(
    path: Path,
    expected_build_provenance_sha256: str | None = None,
) -> None:
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
            if expected_build_provenance_sha256 is not None:
                provenance = members.get("licenses/build-provenance.json")
                if (
                    SHA256_PATTERN.fullmatch(expected_build_provenance_sha256)
                    is None
                    or provenance is None
                    or _hash_zip_member(archive, provenance)[1]
                    != expected_build_provenance_sha256
                ):
                    raise ReleaseAssetError(
                        "License archive build provenance differs from the sealed SHA256."
                    )
    except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
        if isinstance(exc, ReleaseAssetError):
            raise
        raise ReleaseAssetError(f"Cannot verify license archive {path}: {exc}") from exc


def _inno_identity_from_license_archive(
    path: Path,
    lock: dict[str, Any],
) -> str:
    """Validate the sealed Inno provenance carried by the license asset."""

    try:
        with zipfile.ZipFile(path) as archive:
            members = _validated_zip_members(
                archive,
                label=f"license materials archive {path.name}",
            )
            provenance_info = members.get("licenses/build-provenance.json")
            if provenance_info is None:
                raise ReleaseAssetError(
                    f"{path.name} is missing licenses/build-provenance.json."
                )
            build_provenance = _load_zip_json(
                archive,
                provenance_info,
                label=f"build provenance in {path.name}",
            )
        return validate_inno_build_provenance(build_provenance, lock)
    except InnoSetupProvenanceError as exc:
        raise ReleaseAssetError(
            f"Inno Setup build provenance is invalid: {exc}"
        ) from exc


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


def validate_installer_audit_receipt(
    receipt_path: Path,
    *,
    installer: Path,
    distribution_root: Path,
) -> str:
    """Bind Release creation to the exact installer and dist that were audited."""

    _require_regular_file(receipt_path, label="Installer content audit receipt")
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReleaseAssetError(
            f"Cannot read installer content audit receipt: {exc}"
        ) from exc
    if not isinstance(receipt, dict) or set(receipt) != {
        "schema_version",
        "installer",
        "distribution",
        "inno_script",
    }:
        raise ReleaseAssetError("Installer content audit receipt fields differ.")
    if receipt.get("schema_version") != 1:
        raise ReleaseAssetError("Unsupported installer content audit receipt schema.")

    _require_regular_file(installer, label="Installer")
    expected_installer = {
        "filename": installer.name,
        "size": installer.stat().st_size,
        "sha256": sha256_file(installer),
    }
    inno_script = Path(__file__).resolve().parents[1] / "installer" / "LoLReplayTool.iss"
    _require_regular_file(inno_script, label="Inno Setup script")
    expected_inno = {
        "filename": inno_script.name,
        "size": inno_script.stat().st_size,
        "sha256": sha256_file(inno_script),
    }
    from scripts.installer_content_audit import (
        InstallerContentAuditError,
        tree_inventory_identity,
    )

    try:
        expected_distribution = tree_inventory_identity(distribution_root)
    except (InstallerContentAuditError, OSError) as exc:
        raise ReleaseAssetError(
            f"Cannot revalidate audited distribution inventory: {exc}"
        ) from exc
    if receipt.get("installer") != expected_installer:
        raise ReleaseAssetError(
            "Installer bytes differ from the completed content audit."
        )
    if receipt.get("distribution") != expected_distribution:
        raise ReleaseAssetError(
            "Distribution bytes differ from the completed installer content audit."
        )
    if receipt.get("inno_script") != expected_inno:
        raise ReleaseAssetError(
            "Inno Setup script differs from the completed content audit."
        )
    return sha256_file(receipt_path)


def verify_release_asset_payload(
    payload: dict[str, Any],
    *,
    asset_dir: Path | None = None,
    components_file: Path | None = None,
) -> None:
    components_file = components_file or COMPONENTS_FILE
    lock = _load_lock(components_file)
    _assert_runtime_downloads_disabled(lock)
    version = payload.get("version")
    if not isinstance(version, str) or not VERSION_PATTERN.fullmatch(version):
        raise ReleaseAssetError("Release asset list contains an invalid version.")
    source_commit = payload.get("source_commit")
    if not isinstance(source_commit, str) or not COMMIT_PATTERN.fullmatch(source_commit):
        raise ReleaseAssetError("Release asset list contains an invalid source commit.")
    build_provenance_sha256 = payload.get("build_provenance_sha256")
    if (
        not isinstance(build_provenance_sha256, str)
        or SHA256_PATTERN.fullmatch(build_provenance_sha256) is None
    ):
        raise ReleaseAssetError(
            "Release asset list contains an invalid build provenance SHA256."
        )
    installer_build = payload.get("installer_build")
    if not isinstance(installer_build, dict) or set(installer_build) != {
        "component",
        "version",
        "inno_setup_provenance_sha256",
        "installer_sha256",
        "content_audit_receipt_sha256",
    }:
        raise ReleaseAssetError(
            "Release asset list contains invalid installer build provenance."
        )
    if (
        installer_build.get("component") != INNO_COMPONENT
        or installer_build.get("version") != INNO_VERSION
        or not isinstance(installer_build.get("inno_setup_provenance_sha256"), str)
        or SHA256_PATTERN.fullmatch(
            installer_build["inno_setup_provenance_sha256"]
        )
        is None
        or not isinstance(installer_build.get("installer_sha256"), str)
        or SHA256_PATTERN.fullmatch(installer_build["installer_sha256"]) is None
        or not isinstance(
            installer_build.get("content_audit_receipt_sha256"),
            str,
        )
        or SHA256_PATTERN.fullmatch(
            installer_build["content_audit_receipt_sha256"]
        )
        is None
    ):
        raise ReleaseAssetError(
            "Release asset list contains invalid Inno Setup or installer identity."
        )
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

    expected_installer = f"LoLReplayTool-Setup-{version}.exe"
    expected_source = f"LoLReplayTool-source-{version}.zip"
    expected_license = f"LoLReplayTool-license-materials-{version}.zip"
    source_pattern = re.compile(
        rf"LoLReplayTool-third-party-sources-{re.escape(version)}-(\d{{2}})\.zip"
    )
    exact_required = {
        expected_installer,
        expected_source,
        expected_license,
        "SHA256SUMS.txt",
    }
    for required_name in sorted(exact_required):
        matches = [asset for asset in assets if asset.name == required_name]
        if len(matches) != 1:
            raise ReleaseAssetError(
                f"Release assets must contain {required_name} exactly once."
            )

    source_parts_by_number: dict[int, Path] = {}
    unexpected_assets: list[str] = []
    for asset in assets:
        if asset.name in exact_required:
            continue
        match = source_pattern.fullmatch(asset.name)
        if match is None:
            unexpected_assets.append(asset.name)
            continue
        part_number = int(match.group(1))
        if part_number == 0 or part_number in source_parts_by_number:
            raise ReleaseAssetError(
                f"Invalid or duplicate third-party source part number: {asset.name}"
            )
        source_parts_by_number[part_number] = asset
    if unexpected_assets:
        raise ReleaseAssetError(
            "Unexpected Release assets: " + ", ".join(sorted(unexpected_assets))
        )
    if not source_parts_by_number:
        raise ReleaseAssetError("Release assets contain no third-party source archive.")
    expected_part_numbers = list(range(1, len(source_parts_by_number) + 1))
    if sorted(source_parts_by_number) != expected_part_numbers:
        raise ReleaseAssetError(
            "Third-party source asset part numbers must be contiguous from 01."
        )

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

    installers = [asset for asset in assets if asset.name == expected_installer]
    if sha256_file(installers[0]) != installer_build["installer_sha256"]:
        raise ReleaseAssetError(
            "Installer hash differs from its recorded Inno Setup provenance."
        )

    application_sources = [asset for asset in assets if asset.name == expected_source]
    validate_application_source(application_sources[0], version, source_commit)

    observed_source_records: list[dict[str, Any]] = []
    for source_part in source_parts_by_number.values():
        observed_source_records.extend(verify_source_part(source_part))
    expected_source_records = source_archive_records(lock)
    def source_sort_key(item: dict[str, Any]) -> str:
        return str(item.get("filename", "")).casefold()
    if sorted(observed_source_records, key=source_sort_key) != sorted(
        expected_source_records,
        key=source_sort_key,
    ):
        raise ReleaseAssetError(
            "Third-party source asset inventory differs from the checkout component lock."
        )

    license_archives = [asset for asset in assets if asset.name == expected_license]
    verify_license_archive(
        license_archives[0],
        expected_build_provenance_sha256=build_provenance_sha256,
    )
    inno_identity = _inno_identity_from_license_archive(license_archives[0], lock)
    if inno_identity != installer_build["inno_setup_provenance_sha256"]:
        raise ReleaseAssetError(
            "Installer and sealed Inno Setup provenance identities differ."
        )


def verify_release_asset_list(
    path: Path,
    *,
    asset_dir: Path | None = None,
    components_file: Path | None = None,
) -> dict[str, Any]:
    payload = _load_asset_list(path)
    verify_release_asset_payload(
        payload,
        asset_dir=asset_dir,
        components_file=components_file,
    )
    return payload


def create_release_assets(
    *,
    version: str,
    installer: Path,
    installer_audit_receipt: Path,
    application_source: Path,
    distribution_root: Path,
    output_dir: Path,
    source_commit: str,
    components_file: Path = COMPONENTS_FILE,
    cache_dir: Path | None = None,
    enforce_release_gates: bool = True,
    opener: Callable[..., BinaryIO] = urllib.request.urlopen,
    build_provenance_sha256: str | None = None,
) -> dict[str, Any]:
    if not VERSION_PATTERN.fullmatch(version):
        raise ReleaseAssetError(f"Invalid release version: {version}")
    source_commit = source_commit.casefold()
    if not COMMIT_PATTERN.fullmatch(source_commit):
        raise ReleaseAssetError(f"Invalid release source commit: {source_commit}")
    _require_regular_file(installer, label="Installer")
    installer_audit_receipt_sha256 = validate_installer_audit_receipt(
        installer_audit_receipt,
        installer=installer,
        distribution_root=distribution_root,
    )
    _require_regular_file(application_source, label="Application source")
    expected_installer_name = f"LoLReplayTool-Setup-{version}.exe"
    if installer.name != expected_installer_name:
        raise ReleaseAssetError(f"Installer asset must be named {expected_installer_name}.")
    expected_source_name = f"LoLReplayTool-source-{version}.zip"
    if application_source.name != expected_source_name:
        raise ReleaseAssetError(f"Application source asset must be named {expected_source_name}.")
    validate_application_source(application_source, version, source_commit)

    build_provenance_path = (
        distribution_root / "licenses" / "build-provenance.json"
    )
    _require_regular_file(build_provenance_path, label="Build provenance")
    try:
        build_provenance = json.loads(
            build_provenance_path.read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise ReleaseAssetError(f"Cannot read build provenance: {exc}") from exc
    git_source = (
        build_provenance.get("git_source")
        if isinstance(build_provenance, dict)
        else None
    )
    if not isinstance(git_source, dict) or git_source.get("commit") != source_commit:
        raise ReleaseAssetError(
            "Application source commit differs from sealed build provenance."
        )
    actual_build_provenance_sha256 = sha256_file(build_provenance_path)
    if (
        not isinstance(build_provenance_sha256, str)
        or SHA256_PATTERN.fullmatch(build_provenance_sha256) is None
        or actual_build_provenance_sha256 != build_provenance_sha256
    ):
        raise ReleaseAssetError(
            "Release assets require the externally sealed build provenance SHA256."
        )

    lock = _load_lock(components_file)
    _assert_runtime_downloads_disabled(lock)
    try:
        inno_identity = validate_inno_build_provenance(build_provenance, lock)
    except InnoSetupProvenanceError as exc:
        raise ReleaseAssetError(
            f"Release assets require verified Inno Setup provenance: {exc}"
        ) from exc
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
        "build_provenance_sha256": actual_build_provenance_sha256,
        "installer_build": {
            "component": INNO_COMPONENT,
            "version": INNO_VERSION,
            "inno_setup_provenance_sha256": inno_identity,
            "installer_sha256": sha256_file(installer),
            "content_audit_receipt_sha256": installer_audit_receipt_sha256,
        },
        "assets": [str(path.resolve()) for path in assets],
        "sha256sums": str(checksum_path.resolve()),
    }
    verify_release_asset_payload(payload, components_file=components_file)
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

    binaries = commands.add_parser(
        "fetch-binaries",
        help="Fetch locked wheels and create a verified cache manifest.",
    )
    binaries.add_argument("--components", type=Path, default=COMPONENTS_FILE)
    binaries.add_argument("--cache-dir", required=True, type=Path)
    binaries.add_argument("--output-manifest", required=True, type=Path)

    verify_binaries = commands.add_parser(
        "verify-binaries",
        help="Re-verify a locked wheel cache manifest.",
    )
    verify_binaries.add_argument("--components", type=Path, default=COMPONENTS_FILE)
    verify_binaries.add_argument("--manifest", required=True, type=Path)

    verify_runtime = commands.add_parser(
        "verify-python-runtime",
        help="Verify the locked base Python runtime before creating a venv.",
    )
    verify_runtime.add_argument("--components", type=Path, default=COMPONENTS_FILE)

    prepare_runtime = commands.add_parser(
        "prepare-python-runtime",
        help="Fetch and safely extract the locked release Python runtime.",
    )
    prepare_runtime.add_argument("--components", type=Path, default=COMPONENTS_FILE)
    prepare_runtime.add_argument("--cache-dir", required=True, type=Path)
    prepare_runtime.add_argument("--output-dir", required=True, type=Path)

    verify_bootstrap = commands.add_parser(
        "verify-bootstrap-pip",
        help="Verify ensurepip and the new venv's pip before installing packages.",
    )
    verify_bootstrap.add_argument("--components", type=Path, default=COMPONENTS_FILE)

    prepare_install = commands.add_parser(
        "prepare-binary-install",
        help="Create one requirements file bound to verified locked wheels.",
    )
    prepare_install.add_argument("--components", type=Path, default=COMPONENTS_FILE)
    prepare_install.add_argument("--requirements", required=True, type=Path)
    prepare_install.add_argument("--cache-dir", required=True, type=Path)
    prepare_install.add_argument("--output-requirements", required=True, type=Path)
    prepare_install.add_argument("--output-plan", required=True, type=Path)
    prepare_install.add_argument("--opencv-wheel-directory", type=Path)
    prepare_install.add_argument("--opencv-provenance-sha256")

    build_opencv = commands.add_parser(
        "build-opencv-wheel",
        help="Build and verify the locked OpenCV wheel twice for this workflow run.",
    )
    build_opencv.add_argument("--components", type=Path, default=COMPONENTS_FILE)
    build_opencv.add_argument("--cache-dir", required=True, type=Path)

    attest_install = commands.add_parser(
        "attest-binary-install",
        help="Verify pip report and installed files, then write normalized provenance.",
    )
    attest_install.add_argument("--components", type=Path, default=COMPONENTS_FILE)
    attest_install.add_argument("--plan", required=True, type=Path)
    attest_install.add_argument("--pip-report", required=True, type=Path)
    attest_install.add_argument("--output-provenance", required=True, type=Path)

    create = commands.add_parser("create", help="Create and verify release assets.")
    create.add_argument("--version", required=True)
    create.add_argument("--source-commit", required=True)
    create.add_argument("--installer", required=True, type=Path)
    create.add_argument("--installer-audit-receipt", required=True, type=Path)
    create.add_argument("--application-source", required=True, type=Path)
    create.add_argument("--distribution-root", required=True, type=Path)
    create.add_argument("--output-dir", required=True, type=Path)
    create.add_argument("--components", type=Path, default=COMPONENTS_FILE)
    create.add_argument("--cache-dir", type=Path)
    create.add_argument("--build-provenance-sha256", required=True)
    create.add_argument(
        "--allow-open-legal-gates",
        action="store_true",
        help="Test/audit only; release workflow must never pass this option.",
    )

    verify = commands.add_parser("verify", help="Re-verify an immutable asset set.")
    verify.add_argument("--asset-list", required=True, type=Path)
    verify.add_argument("--components", required=True, type=Path)
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
        if args.command == "fetch-binaries":
            create_verified_binary_manifest(
                args.components,
                args.cache_dir,
                args.output_manifest,
            )
            print(f"Verified binary manifest created: {args.output_manifest}")
            return 0
        if args.command == "verify-binaries":
            verify_binary_manifest(
                args.manifest,
                components_file=args.components,
            )
            print(f"Verified binary manifest passed: {args.manifest}")
            return 0
        if args.command == "verify-python-runtime":
            runtime = verify_python_runtime(args.components)
            print(
                "Verified release Python runtime: "
                f"{runtime['python_version']} ({runtime['active_python_executable']['kind']})"
            )
            return 0
        if args.command == "prepare-python-runtime":
            python_executable = prepare_python_runtime(
                components_file=args.components,
                cache_dir=args.cache_dir,
                output_dir=args.output_dir,
            )
            print(f"Verified release Python runtime prepared: {python_executable}")
            return 0
        if args.command == "verify-bootstrap-pip":
            bootstrap = verify_bootstrap_pip(args.components)
            print(
                "Verified bootstrap pip: "
                f"{bootstrap['version']} ({bootstrap['inventory_sha256']})"
            )
            return 0
        if args.command == "prepare-binary-install":
            prepare_binary_install(
                components_file=args.components,
                requirements_file=args.requirements,
                cache_dir=args.cache_dir,
                output_requirements=args.output_requirements,
                output_plan=args.output_plan,
                opencv_wheel_directory=args.opencv_wheel_directory,
                opencv_provenance_sha256=args.opencv_provenance_sha256,
            )
            print(f"Binary install plan created: {args.output_plan}")
            return 0
        if args.command == "build-opencv-wheel":
            result = _prepare_opencv_source_wheel(
                components_file=args.components,
                lock=_load_lock(args.components),
                cache_dir=args.cache_dir,
                opener=urllib.request.urlopen,
            )
            if result is None:
                raise ReleaseAssetError("OpenCV source-build policy is required.")
            print(json.dumps({key: result[key] for key in (
                "directory", "provenance", "provenance_sha256"
            )}))
            return 0
        if args.command == "attest-binary-install":
            attest_binary_install(
                components_file=args.components,
                plan_path=args.plan,
                pip_report_path=args.pip_report,
                output_provenance=args.output_provenance,
            )
            print(f"Build provenance created: {args.output_provenance}")
            return 0
        if args.command == "verify":
            verify_release_asset_list(
                args.asset_list,
                asset_dir=args.asset_dir,
                components_file=args.components,
            )
            print(f"Release asset list verified: {args.asset_list}")
            return 0
        payload = create_release_assets(
            version=args.version,
            source_commit=args.source_commit,
            installer=args.installer,
            installer_audit_receipt=args.installer_audit_receipt,
            application_source=args.application_source,
            distribution_root=args.distribution_root,
            output_dir=args.output_dir,
            components_file=args.components,
            cache_dir=args.cache_dir,
            enforce_release_gates=not args.allow_open_legal_gates,
            build_provenance_sha256=args.build_provenance_sha256,
        )
    except ReleaseAssetError as exc:
        print(f"ERROR: {exc}")
        return 1
    print(f"Release asset list created: {payload['asset_list']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
