"""Apply and attest the reproducible Windows runtime policy for PyInstaller."""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import stat
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from importlib import metadata
from pathlib import Path, PurePosixPath
from typing import Any

PyInstallerBinary = tuple[str, str, str]
SourceBoundary = tuple[str, Path]

RUNTIME_POLICY_AUDIT_FILENAME = "windows-runtime-policy-audit.json"
RUNTIME_POLICY_SCHEMA_VERSION = 1
RUNTIME_POLICY_NAME = "windows-runtime-exclusion-v1"

ANALYSIS_GUTS_FIELDS = (
    "inputs",
    "pathex",
    "hiddenimports",
    "hookspath",
    "hooksconfig",
    "excludes",
    "custom_runtime_hooks",
    "noarchive",
    "module_collection_mode",
    "optimize",
    "_input_binaries",
    "_input_datas",
    "_python_version",
    "scripts",
    "pure",
    "binaries",
    "zipfiles",
    "zipped_data",
    "datas",
    "_modules_outside_pyz",
)
ANALYSIS_BINARY_INDEX = ANALYSIS_GUTS_FIELDS.index("binaries")
ANALYSIS_DATA_INDEX = ANALYSIS_GUTS_FIELDS.index("datas")

_WINDOWS_OS_RUNTIME_NAME = re.compile(
    r"api-ms-win-(?:core|crt)-[a-z0-9-]+\.dll\Z",
    re.IGNORECASE,
)
_SCIKIT_LEARN_VCOMP_NAME = "sklearn/.libs/vcomp140.dll"
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_ALLOWED_AUDIT_KEYS = {
    "schema_version",
    "policy",
    "pyinstaller_version",
    "analysis_contract",
    "allowed_source_boundaries",
    "raw_binaries",
    "retained_raw_indexes",
    "excluded_binaries",
    "raw_inventory_sha256",
    "policy_result_sha256",
    "payload_sha256",
}


def is_windows_os_runtime_name(toc_name: str) -> bool:
    """Return whether a root TOC entry is supplied by supported Windows."""

    normalized = toc_name.replace("\\", "/")
    if "/" in normalized:
        return False
    return (
        normalized.casefold() == "ucrtbase.dll"
        or _WINDOWS_OS_RUNTIME_NAME.fullmatch(normalized) is not None
    )


def is_root_vcomp_name(toc_name: str) -> bool:
    """Return whether a TOC entry is PyInstaller's redundant root VCOMP copy."""

    normalized = toc_name.replace("\\", "/")
    return "/" not in normalized and normalized.casefold() == "vcomp140.dll"


def _canonical_json_sha256(value: object) -> str:
    serialized = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_reparse_point(file_stat: os.stat_result) -> bool:
    return bool(
        stat.S_ISLNK(file_stat.st_mode)
        or (
            getattr(file_stat, "st_file_attributes", 0)
            & _FILE_ATTRIBUTE_REPARSE_POINT
        )
    )


def _verified_source(source_text: str) -> tuple[Path, int, str]:
    source = Path(source_text)
    if not source.is_absolute():
        raise RuntimeError(
            f"PyInstaller binary source must be absolute: {source_text}"
        )
    try:
        source_stat = source.lstat()
        resolved = source.resolve(strict=True)
    except OSError as exc:
        raise RuntimeError(
            f"PyInstaller binary source cannot be resolved: {source_text}: {exc}"
        ) from exc
    if _is_reparse_point(source_stat) or not stat.S_ISREG(source_stat.st_mode):
        raise RuntimeError(
            f"PyInstaller binary source must be a regular non-reparse file: "
            f"{source_text}"
        )
    resolved_stat = resolved.stat()
    if not stat.S_ISREG(resolved_stat.st_mode):
        raise RuntimeError(
            f"Resolved PyInstaller binary source is not a regular file: {source_text}"
        )
    digest = _sha256_file(resolved)
    final_stat = resolved.stat()
    stable_fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
    if any(
        getattr(resolved_stat, field, None) != getattr(final_stat, field, None)
        for field in stable_fields
    ):
        raise RuntimeError(
            f"PyInstaller binary source changed while hashing: {source_text}"
        )
    return resolved, final_stat.st_size, digest


def _normalized_destination(value: str) -> str:
    normalized = value.replace("\\", "/")
    relative = PurePosixPath(normalized)
    if (
        not normalized
        or relative.is_absolute()
        or re.match(r"^[A-Za-z]:", normalized)
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise RuntimeError(f"PyInstaller emitted an unsafe binary destination: {value}")
    canonical = relative.as_posix()
    if normalized != canonical:
        raise RuntimeError(f"PyInstaller emitted an unsafe binary destination: {value}")
    return canonical


def _validated_binary_entry(
    entry: object,
    *,
    index: int,
) -> tuple[PyInstallerBinary, dict[str, Any], Path]:
    if (
        not isinstance(entry, tuple)
        or len(entry) != 3
        or not all(isinstance(value, str) for value in entry)
    ):
        raise RuntimeError("PyInstaller emitted an invalid binary TOC entry.")
    toc_name, source_text, entry_type = entry
    if entry_type not in {"BINARY", "EXTENSION"}:
        raise RuntimeError(
            f"PyInstaller emitted an unexpected binary type: {entry_type}"
        )
    destination = _normalized_destination(toc_name)
    resolved_source, size, digest = _verified_source(source_text)
    return (
        entry,
        {
            "raw_index": index,
            "destination": destination,
            "source": source_text,
            "type": entry_type,
            "size": size,
            "sha256": digest,
        },
        resolved_source,
    )


def _resolved_boundary(name: str, path: Path) -> SourceBoundary:
    if not name or "/" in name or "\\" in name:
        raise RuntimeError(f"Invalid Windows runtime source boundary name: {name}")
    if not path.is_absolute():
        raise RuntimeError(
            f"Windows runtime source boundary must be absolute: {path}"
        )
    return name, path.resolve(strict=False)


def allowed_windows_runtime_source_boundaries(
    environment: Mapping[str, str] | None = None,
) -> list[SourceBoundary]:
    """Return the only host locations from which root runtimes may be excluded."""

    env = os.environ if environment is None else environment
    candidates: list[SourceBoundary] = []
    windows_root = env.get("SystemRoot") or env.get("WINDIR")
    if windows_root:
        candidates.append(
            _resolved_boundary("windows-system32", Path(windows_root) / "System32")
        )
    sdk_roots = []
    for key in ("UniversalCRTSdkDir", "WindowsSdkDir"):
        value = env.get(key)
        if value:
            sdk_roots.append(Path(value) / "Redist")
    for sdk_root in sdk_roots:
        candidates.append(_resolved_boundary("windows-sdk-ucrt-redist", sdk_root))

    result: list[SourceBoundary] = []
    seen: set[tuple[str, str]] = set()
    for name, path in candidates:
        key = (name, os.path.normcase(str(path)).casefold())
        if key in seen:
            continue
        seen.add(key)
        result.append((name, path))
    return result


def _relative_to_boundary(
    source: Path,
    boundaries: Sequence[SourceBoundary],
) -> tuple[str, str] | None:
    for name, boundary in boundaries:
        try:
            relative = source.relative_to(boundary)
        except ValueError:
            continue
        if not relative.parts:
            continue
        return name, PurePosixPath(*relative.parts).as_posix()
    return None


def _exclusion_decision(
    record: Mapping[str, Any],
    resolved_source: Path,
    boundaries: Sequence[SourceBoundary],
) -> dict[str, Any] | None:
    destination = str(record["destination"])
    if not (
        is_windows_os_runtime_name(destination) or is_root_vcomp_name(destination)
    ):
        return None
    boundary_match = _relative_to_boundary(resolved_source, boundaries)
    if boundary_match is None:
        raise RuntimeError(
            "Refusing to exclude a root Windows runtime from an unapproved source: "
            f"{destination} <- {record['source']}"
        )
    boundary_name, relative_source = boundary_match
    if is_root_vcomp_name(destination) and boundary_name != "windows-system32":
        raise RuntimeError(
            "Refusing to exclude root VCOMP140.DLL outside Windows System32: "
            f"{record['source']}"
        )
    reason = (
        "supported-windows-os-runtime"
        if is_windows_os_runtime_name(destination)
        else "redundant-root-vcomp"
    )
    return {
        **record,
        "reason": reason,
        "source_boundary": boundary_name,
        "source_relative_path": relative_source,
    }


def _boundary_payload(boundaries: Sequence[SourceBoundary]) -> list[dict[str, str]]:
    return [
        {"name": name, "path": str(path)}
        for name, path in boundaries
    ]


def _analysis_contract_payload() -> dict[str, Any]:
    return {
        "class": "PyInstaller.building.build_main.Analysis",
        "guts_fields": list(ANALYSIS_GUTS_FIELDS),
        "binary_index": ANALYSIS_BINARY_INDEX,
        "data_index": ANALYSIS_DATA_INDEX,
        "toc_filename": "Analysis-00.toc",
    }


def _write_audit(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary.write(encoded)
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_name = temporary.name
        os.replace(temporary_name, path)
        temporary_name = None
    finally:
        if temporary_name is not None:
            try:
                Path(temporary_name).unlink()
            except FileNotFoundError:
                pass


def apply_windows_runtime_policy(
    binaries: Iterable[PyInstallerBinary],
    *,
    audit_path: Path | None = None,
    source_boundaries: Sequence[SourceBoundary] | None = None,
) -> list[PyInstallerBinary]:
    """Remove approved host runtimes and optionally persist the raw inventory."""

    boundaries = [
        _resolved_boundary(name, Path(path))
        for name, path in (
            allowed_windows_runtime_source_boundaries()
            if source_boundaries is None
            else source_boundaries
        )
    ]
    retained: list[PyInstallerBinary] = []
    retained_indexes: list[int] = []
    excluded: list[dict[str, Any]] = []
    raw_records: list[dict[str, Any]] = []
    scikit_learn_vcomp_count = 0
    seen_destinations: set[str] = set()

    for index, raw_entry in enumerate(binaries):
        entry, record, resolved_source = _validated_binary_entry(
            raw_entry,
            index=index,
        )
        destination_key = str(record["destination"]).casefold()
        if destination_key in seen_destinations:
            raise RuntimeError(
                "PyInstaller emitted a duplicate binary destination: "
                f"{record['destination']}"
            )
        seen_destinations.add(destination_key)
        raw_records.append(record)

        decision = _exclusion_decision(record, resolved_source, boundaries)
        if decision is not None:
            excluded.append(decision)
            continue

        if destination_key == _SCIKIT_LEARN_VCOMP_NAME:
            if record["type"] != "BINARY":
                raise RuntimeError(
                    "The locked scikit-learn VCOMP entry is not a binary."
                )
            scikit_learn_vcomp_count += 1
        retained.append(entry)
        retained_indexes.append(index)

    if scikit_learn_vcomp_count != 1:
        raise RuntimeError(
            "PyInstaller must collect exactly one locked scikit-learn vcomp140.dll."
        )

    if audit_path is not None:
        policy_result = [raw_records[index] for index in retained_indexes]
        payload: dict[str, Any] = {
            "schema_version": RUNTIME_POLICY_SCHEMA_VERSION,
            "policy": RUNTIME_POLICY_NAME,
            "pyinstaller_version": metadata.version("PyInstaller"),
            "analysis_contract": _analysis_contract_payload(),
            "allowed_source_boundaries": _boundary_payload(boundaries),
            "raw_binaries": raw_records,
            "retained_raw_indexes": retained_indexes,
            "excluded_binaries": excluded,
            "raw_inventory_sha256": _canonical_json_sha256(raw_records),
            "policy_result_sha256": _canonical_json_sha256(policy_result),
        }
        payload["payload_sha256"] = _canonical_json_sha256(payload)
        _write_audit(audit_path, payload)
    return retained


def _validate_analysis_private_contract(analysis: object) -> Path:
    analysis_type = type(analysis)
    qualified_name = f"{analysis_type.__module__}.{analysis_type.__name__}"
    if qualified_name != "PyInstaller.building.build_main.Analysis":
        raise RuntimeError(
            f"Unexpected PyInstaller Analysis implementation: {qualified_name}"
        )
    guts = getattr(analysis, "_GUTS", None)
    if not isinstance(guts, tuple) or any(
        not isinstance(item, tuple)
        or len(item) != 2
        or not isinstance(item[0], str)
        for item in guts
    ):
        raise RuntimeError("PyInstaller Analysis private _GUTS structure changed.")
    fields = tuple(item[0] for item in guts)
    if fields != ANALYSIS_GUTS_FIELDS:
        raise RuntimeError("PyInstaller Analysis private _GUTS fields changed.")
    tocfilename = getattr(analysis, "tocfilename", None)
    if not isinstance(tocfilename, str):
        raise RuntimeError("PyInstaller Analysis TOC filename is missing.")
    toc_path = Path(tocfilename)
    if toc_path.name != "Analysis-00.toc" or not toc_path.is_absolute():
        raise RuntimeError(
            f"Unexpected PyInstaller Analysis TOC path: {tocfilename}"
        )
    if not callable(getattr(analysis, "_save_guts", None)):
        raise RuntimeError("PyInstaller Analysis private _save_guts API changed.")
    if not isinstance(getattr(analysis, "binaries", None), list):
        raise RuntimeError("PyInstaller Analysis binaries structure changed.")
    if not isinstance(getattr(analysis, "datas", None), list):
        raise RuntimeError("PyInstaller Analysis datas structure changed.")
    return toc_path


def apply_windows_runtime_policy_to_analysis(analysis: Any) -> Path:
    """Apply the policy and localize the pinned Analysis._save_guts dependency."""

    toc_path = _validate_analysis_private_contract(analysis)
    audit_path = toc_path.with_name(RUNTIME_POLICY_AUDIT_FILENAME)
    filtered = apply_windows_runtime_policy(
        analysis.binaries,
        audit_path=audit_path,
    )
    analysis.binaries = filtered
    analysis._save_guts()

    try:
        saved = ast.literal_eval(toc_path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, ValueError) as exc:
        raise RuntimeError(
            f"Cannot re-read the saved PyInstaller Analysis TOC: {exc}"
        ) from exc
    if not isinstance(saved, tuple) or len(saved) != len(ANALYSIS_GUTS_FIELDS):
        raise RuntimeError("Saved PyInstaller Analysis TOC structure changed.")
    if saved[ANALYSIS_BINARY_INDEX] != filtered:
        raise RuntimeError(
            "Saved PyInstaller Analysis binaries differ from the policy output."
        )
    if saved[ANALYSIS_DATA_INDEX] != analysis.datas:
        raise RuntimeError(
            "Saved PyInstaller Analysis datas differ from the Analysis object."
        )
    return audit_path


def _read_audit(path: Path) -> dict[str, Any]:
    try:
        file_stat = path.lstat()
        if _is_reparse_point(file_stat) or not stat.S_ISREG(file_stat.st_mode):
            raise ValueError("audit is not a regular non-reparse file")
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"Cannot read PyInstaller runtime policy audit: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("PyInstaller runtime policy audit root must be an object.")
    return payload


def _validated_audit_record(value: object, *, expected_index: int) -> dict[str, Any]:
    required = {
        "raw_index",
        "destination",
        "source",
        "type",
        "size",
        "sha256",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise ValueError("PyInstaller runtime policy raw record structure changed.")
    if value.get("raw_index") != expected_index:
        raise ValueError("PyInstaller runtime policy raw indexes are not contiguous.")
    destination = value.get("destination")
    source = value.get("source")
    entry_type = value.get("type")
    if not isinstance(destination, str) or _normalized_destination(destination) != destination:
        raise ValueError("PyInstaller runtime policy destination is invalid.")
    if not isinstance(source, str) or entry_type not in {"BINARY", "EXTENSION"}:
        raise ValueError("PyInstaller runtime policy source or type is invalid.")
    try:
        _resolved, size, digest = _verified_source(source)
    except RuntimeError as exc:
        raise ValueError(str(exc)) from exc
    if value.get("size") != size or value.get("sha256") != digest:
        raise ValueError(
            f"PyInstaller runtime policy source changed after audit: {destination}"
        )
    return dict(value)


def validate_windows_runtime_policy_audit(
    audit_path: Path,
    filtered_binaries: Sequence[PyInstallerBinary],
) -> dict[str, Any]:
    """Verify the raw/policy inventory and return a public-path-safe summary."""

    payload = _read_audit(audit_path)
    if set(payload) != _ALLOWED_AUDIT_KEYS:
        raise ValueError("PyInstaller runtime policy audit structure changed.")
    if payload.get("schema_version") != RUNTIME_POLICY_SCHEMA_VERSION:
        raise ValueError("Unsupported PyInstaller runtime policy audit schema.")
    if payload.get("policy") != RUNTIME_POLICY_NAME:
        raise ValueError("Unexpected PyInstaller runtime policy identifier.")
    if payload.get("pyinstaller_version") != metadata.version("PyInstaller"):
        raise ValueError("PyInstaller runtime policy audit version differs.")
    if payload.get("analysis_contract") != _analysis_contract_payload():
        raise ValueError("PyInstaller runtime policy Analysis contract differs.")
    expected_payload_hash = payload.get("payload_sha256")
    unsigned_payload = dict(payload)
    unsigned_payload.pop("payload_sha256", None)
    if (
        not isinstance(expected_payload_hash, str)
        or _SHA256_PATTERN.fullmatch(expected_payload_hash) is None
        or _canonical_json_sha256(unsigned_payload) != expected_payload_hash
    ):
        raise ValueError("PyInstaller runtime policy canonical payload SHA256 differs.")

    boundaries = allowed_windows_runtime_source_boundaries()
    if payload.get("allowed_source_boundaries") != _boundary_payload(boundaries):
        raise ValueError("PyInstaller runtime policy source boundaries differ.")
    raw_values = payload.get("raw_binaries")
    if not isinstance(raw_values, list):
        raise ValueError("PyInstaller runtime policy raw inventory is missing.")
    raw_records = [
        _validated_audit_record(value, expected_index=index)
        for index, value in enumerate(raw_values)
    ]
    seen_destinations: set[str] = set()
    resolved_sources: list[Path] = []
    for record in raw_records:
        destination_key = str(record["destination"]).casefold()
        if destination_key in seen_destinations:
            raise ValueError(
                "PyInstaller runtime policy raw inventory has a duplicate destination: "
                f"{record['destination']}"
            )
        seen_destinations.add(destination_key)
        resolved_source, _size, _digest = _verified_source(str(record["source"]))
        resolved_sources.append(resolved_source)
    if payload.get("raw_inventory_sha256") != _canonical_json_sha256(raw_records):
        raise ValueError("PyInstaller runtime policy raw inventory SHA256 differs.")

    retained_indexes = payload.get("retained_raw_indexes")
    excluded_values = payload.get("excluded_binaries")
    if (
        not isinstance(retained_indexes, list)
        or not all(isinstance(index, int) for index in retained_indexes)
        or retained_indexes != sorted(set(retained_indexes))
        or not isinstance(excluded_values, list)
    ):
        raise ValueError("PyInstaller runtime policy decision sets are invalid.")
    expected_excluded: list[dict[str, Any]] = []
    expected_retained: list[int] = []
    for index, (record, source) in enumerate(
        zip(raw_records, resolved_sources, strict=True)
    ):
        try:
            decision = _exclusion_decision(record, source, boundaries)
        except RuntimeError as exc:
            raise ValueError(str(exc)) from exc
        if decision is None:
            expected_retained.append(index)
        else:
            expected_excluded.append(decision)
    if retained_indexes != expected_retained or excluded_values != expected_excluded:
        raise ValueError("PyInstaller runtime policy retained/excluded partition differs.")
    if set(retained_indexes) & {
        int(record["raw_index"]) for record in expected_excluded
    } or len(retained_indexes) + len(expected_excluded) != len(raw_records):
        raise ValueError("PyInstaller runtime policy decision sets are not a partition.")

    retained_records = [raw_records[index] for index in retained_indexes]
    retained_tuples = [
        (
            str(record["destination"]),
            str(record["source"]),
            str(record["type"]),
        )
        for record in retained_records
    ]
    normalized_filtered = [
        (_normalized_destination(name), source, entry_type)
        for name, source, entry_type in filtered_binaries
    ]
    if retained_tuples != normalized_filtered:
        raise ValueError(
            "PyInstaller runtime policy output differs from saved Analysis binaries."
        )
    if payload.get("policy_result_sha256") != _canonical_json_sha256(retained_records):
        raise ValueError("PyInstaller runtime policy result SHA256 differs.")
    wheel_vcomp = [
        record
        for record in retained_records
        if str(record["destination"]).casefold() == _SCIKIT_LEARN_VCOMP_NAME
        and record["type"] == "BINARY"
    ]
    if len(wheel_vcomp) != 1:
        raise ValueError(
            "PyInstaller runtime policy must retain exactly one locked "
            "scikit-learn vcomp140.dll."
        )

    safe_excluded = [
        {
            "raw_index": record["raw_index"],
            "destination": record["destination"],
            "source": (
                f"{record['source_boundary']}/"
                f"{record['source_relative_path']}"
            ),
            "type": record["type"],
            "size": record["size"],
            "sha256": record["sha256"],
            "reason": record["reason"],
            "source_boundary": record["source_boundary"],
        }
        for record in expected_excluded
    ]
    return {
        "artifact": {
            "filename": RUNTIME_POLICY_AUDIT_FILENAME,
            "size": audit_path.stat().st_size,
            "sha256": _sha256_file(audit_path),
            "payload_sha256": expected_payload_hash,
        },
        "policy": RUNTIME_POLICY_NAME,
        "pyinstaller_version": payload["pyinstaller_version"],
        "raw_inventory_sha256": payload["raw_inventory_sha256"],
        "policy_result_sha256": payload["policy_result_sha256"],
        "raw_binary_count": len(raw_records),
        "retained_binary_count": len(retained_records),
        "excluded_binary_count": len(expected_excluded),
        "allowed_source_boundaries": [name for name, _path in boundaries],
        "excluded_binaries": safe_excluded,
        "_raw_binaries": raw_records,
    }
