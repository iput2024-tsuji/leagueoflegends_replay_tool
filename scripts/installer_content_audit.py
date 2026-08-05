"""Verify that a finished installer contains exactly the validated distribution."""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import stat
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from scripts.check_license_compliance import (
    MANIFEST_RELATIVE_PATH,
    validate_distribution,
)
from scripts.external_runtime_policy import is_user_provided_runtime_path
from src.license_info import REQUIRED_DISTRIBUTION_DOCUMENTS

FILE_ATTRIBUTE_REPARSE_POINT = 0x400
INVALID_WINDOWS_CHARS = re.compile(r'[<>:"|?*\x00-\x1f]')
WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}
REQUIRED_INSTALLER_PATHS = (
    *REQUIRED_DISTRIBUTION_DOCUMENTS,
    MANIFEST_RELATIVE_PATH,
)
INNO_AUDIT_GUARD = re.compile(r"not\s+IsContentAuditMode(?:\(\))?\Z", re.IGNORECASE)
INNO_AUDIT_MODE_FUNCTION = re.compile(
    r"function\s+IsContentAuditMode\s*:\s*Boolean\s*;\s*"
    r"begin\s*"
    r"Result\s*:=\s*ExpandConstant\(\s*'\{param:contentaudit\|\}'\s*\)\s*"
    r"=\s*'1'\s*;\s*"
    r"end\s*;",
    re.IGNORECASE,
)
INNO_INITIALIZE_WIZARD_GUARD = re.compile(
    r"procedure\s+InitializeWizard\s*;\s*"
    r"(?:var\b.*?)?"
    r"begin\s*"
    r"if\s+IsContentAuditMode(?:\(\))?\s+then\s*"
    r"Exit\s*;",
    re.IGNORECASE | re.DOTALL,
)
INNO_CODE_DECLARATION = re.compile(
    r"\b(?:function|procedure)\s+([A-Za-z_][A-Za-z0-9_]*)\b",
    re.IGNORECASE,
)
INNO_ALLOWED_CODE_DECLARATIONS = (
    "curuninstallstepchanged",
    "deletemanagedrecordings",
    "deletemanageduserdata",
    "initializeuninstall",
    "initializewizard",
    "iscontentauditmode",
    "showuninstalloptions",
)
INNO_SIDE_EFFECT_SECTIONS = frozenset(
    {
        "dirs",
        "icons",
        "ini",
        "installdelete",
        "registry",
        "run",
        "uninstalldelete",
        "uninstallrun",
    }
)
INNO_ALLOWED_SECTIONS = frozenset(
    {
        "code",
        "components",
        "custommessages",
        "dirs",
        "files",
        "icons",
        "ini",
        "installdelete",
        "languages",
        "langoptions",
        "messages",
        "registry",
        "run",
        "setup",
        "tasks",
        "types",
        "uninstalldelete",
        "uninstallrun",
    }
)
INNO_PREPROCESSOR_ALLOWLIST = (
    re.compile(r'#ifndef\s+AppVersion\Z', re.IGNORECASE),
    re.compile(
        r'#define\s+AppVersion\s+"\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?"\Z',
        re.IGNORECASE,
    ),
    re.compile(r'#endif\Z', re.IGNORECASE),
    re.compile(r'#define\s+AppName\s+"LoL Replay Tool"\Z'),
    re.compile(r'#define\s+AppExeName\s+"LoLReplayTool\.exe"\Z'),
    re.compile(
        r'#define\s+AppPublisher\s+"LoL Replay Tool Contributors"\Z'
    ),
    re.compile(
        r'#define\s+AppURL\s+"https://github\.com/'
        r'iput2024-tsuji/leagueoflegends_replay_tool"\Z'
    ),
)
INNO_INLINE_PREPROCESSOR_ALLOWLIST = (
    ("setup", "AppName={#AppName}"),
    ("setup", "AppVersion={#AppVersion}"),
    ("setup", "AppPublisher={#AppPublisher}"),
    ("setup", "AppPublisherURL={#AppURL}"),
    ("setup", "AppSupportURL={#AppURL}/issues"),
    ("setup", "AppUpdatesURL={#AppURL}/releases"),
    ("setup", "DefaultGroupName={#AppName}"),
    ("setup", "OutputBaseFilename=LoLReplayTool-Setup-{#AppVersion}"),
    ("setup", r"UninstallDisplayIcon={app}\{#AppExeName}"),
    ("setup", "CloseApplicationsFilter={#AppExeName}"),
    (
        "icons",
        'Name: "{autoprograms}\\{#AppName}"; '
        'Filename: "{app}\\{#AppExeName}"; WorkingDir: "{app}"; '
        "Check: not IsContentAuditMode",
    ),
    (
        "icons",
        'Name: "{autodesktop}\\{#AppName}"; '
        'Filename: "{app}\\{#AppExeName}"; WorkingDir: "{app}"; '
        "Tasks: desktopicon; Check: not IsContentAuditMode",
    ),
    (
        "run",
        'Filename: "{app}\\{#AppExeName}"; '
        'Description: "{#AppName} を起動"; '
        "Flags: nowait postinstall skipifsilent; Check: not IsContentAuditMode",
    ),
    ("code", "OptionsForm.Caption := '{#AppName} アンインストール';"),
)
INNO_CONDITIONAL_ENTRY_SECTIONS = frozenset(
    {"components", "languages", "tasks", "types"}
)
INNO_FORBIDDEN_FILE_FLAGS = frozenset(
    {
        "deleteafterinstall",
        "download",
        "external",
        "gacinstall",
        "gacomcache",
        "regserver",
        "regtypelib",
        "restartreplace",
        "sharedfile",
    }
)
INNO_FORBIDDEN_FILE_FIELDS = frozenset(
    {"afterinstall", "beforeinstall", "check", "fontinstall", "permissions"}
)
INNO_UNSAFE_DESTINATION_CHARACTER = re.compile(r'[<>:"/\\|?*{}\x00-\x1f]')
INNO_WINDOWS_RESERVED_NAMES = frozenset(
    {
        "aux",
        "clock$",
        "con",
        "nul",
        "prn",
        *(f"com{number}" for number in range(1, 10)),
        *(f"lpt{number}" for number in range(1, 10)),
    }
)
INNO_REQUIRED_SETUP_VALUES = {
    "appid": "{{B8D87E69-41F7-4B28-978D-2F8FA5AF4BE2}",
    "changesassociations": "no",
    "changesenvironment": "no",
    "createuninstallregkey": "not IsContentAuditMode",
    "privilegesrequired": "lowest",
    "uninstallable": "not IsContentAuditMode",
}


class InstallerContentAuditError(RuntimeError):
    """The installer payload could not be inspected safely."""


@dataclass(frozen=True)
class FileRecord:
    path: str
    size: int
    sha256: str


@dataclass(frozen=True)
class TreeInventory:
    files: dict[str, FileRecord]
    directories: dict[str, str]
    forbidden_runtimes: tuple[str, ...]


def _split_inno_fields(line: str) -> list[str]:
    fields: list[str] = []
    start = 0
    in_quotes = False
    index = 0
    while index < len(line):
        character = line[index]
        if character == '"':
            if in_quotes and index + 1 < len(line) and line[index + 1] == '"':
                index += 2
                continue
            in_quotes = not in_quotes
        elif character == ";" and not in_quotes:
            fields.append(line[start:index].strip())
            start = index + 1
        index += 1
    if in_quotes:
        raise ValueError("unterminated quoted value")
    fields.append(line[start:].strip())
    return fields


def _inno_code_section(source: str) -> str:
    section = ""
    code_lines: list[str] = []
    for raw_line in source.splitlines():
        section_match = re.fullmatch(r"\s*\[([^]]+)]\s*", raw_line)
        if section_match:
            section = section_match.group(1).strip().casefold()
            continue
        if section == "code":
            code_lines.append(raw_line)
    return "\n".join(code_lines)


def _mask_pascal_strings(source: str) -> str:
    """Mask literals and reject comments so code tokens have one interpretation."""
    masked: list[str] = []
    index = 0
    state = "code"
    while index < len(source):
        character = source[index]
        following = source[index + 1] if index + 1 < len(source) else ""
        if state == "code":
            if character == "'":
                state = "string"
                masked.append(" ")
            elif character == "/" and following == "/":
                raise ValueError("Pascal comments are not allowed")
            elif character == "{":
                raise ValueError("Pascal comments are not allowed")
            elif character == "(" and following == "*":
                raise ValueError("Pascal comments are not allowed")
            else:
                masked.append(character)
        else:
            if character in "\r\n":
                raise ValueError("newline in Pascal string")
            masked.append(" ")
            if character == "'":
                if following == "'":
                    masked.append(" ")
                    index += 1
                else:
                    state = "code"
        index += 1
    if state == "string":
        raise ValueError("unterminated Pascal string")
    return "".join(masked)


def _validate_inno_code(source: str) -> list[str]:
    code = _inno_code_section(source)
    try:
        structural_code = _mask_pascal_strings(code)
    except ValueError as exc:
        return [f"Cannot parse Inno [Code]: {exc}"]

    errors: list[str] = []
    declaration_matches = list(INNO_CODE_DECLARATION.finditer(structural_code))
    declarations = sorted(match.group(1).casefold() for match in declaration_matches)
    if declarations != sorted(INNO_ALLOWED_CODE_DECLARATIONS):
        errors.append(
            "Inno [Code] declarations differ from the audit-safe allowlist: "
            + ", ".join(declarations)
        )
    audit_mode_declarations = [
        match
        for match in declaration_matches
        if match.group(1).casefold() == "iscontentauditmode"
    ]
    if (
        len(audit_mode_declarations) != 1
        or INNO_AUDIT_MODE_FUNCTION.match(
            code,
            audit_mode_declarations[0].start(),
        )
        is None
    ):
        errors.append(
            "Inno IsContentAuditMode must be defined exactly once with the "
            "fixed /CONTENTAUDIT parameter semantics."
        )
    initialize_wizard_declarations = [
        match
        for match in declaration_matches
        if match.group(1).casefold() == "initializewizard"
    ]
    if (
        len(initialize_wizard_declarations) != 1
        or INNO_INITIALIZE_WIZARD_GUARD.match(
            structural_code,
            initialize_wizard_declarations[0].start(),
        )
        is None
    ):
        errors.append(
            "Inno InitializeWizard must exit before side effects in content-audit mode."
        )
    return errors


def _validate_inno_preprocessor(lines: list[tuple[int, str]]) -> list[str]:
    if len(lines) != len(INNO_PREPROCESSOR_ALLOWLIST):
        return [
            "Inno preprocessor directives differ from the audit-safe allowlist."
        ]
    errors: list[str] = []
    for (line_number, line), expected in zip(
        lines,
        INNO_PREPROCESSOR_ALLOWLIST,
        strict=True,
    ):
        if expected.fullmatch(line) is None:
            errors.append(
                "Inno preprocessor directive is not audit-safe at line "
                f"{line_number}: {line}"
            )
    return errors


def _validate_inline_inno_preprocessor(
    lines: list[tuple[str, int, str]],
) -> list[str]:
    actual = [(section, line) for section, _, line in lines]
    if actual == list(INNO_INLINE_PREPROCESSOR_ALLOWLIST):
        return []
    details = ", ".join(
        f"[{section}] line {line_number}: {line}"
        for section, line_number, line in lines
    )
    return [
        "Inno inline preprocessor expansions differ from the audit-safe "
        f"allowlist: {details or '<none>'}"
    ]


def _parse_inno_entry(line: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for field in _split_inno_fields(line):
        if not field or ":" not in field:
            raise ValueError(f"invalid field: {field or '<empty>'}")
        name, value = field.split(":", 1)
        key = name.strip().casefold()
        if not key or key in result:
            raise ValueError(f"duplicate or empty field: {name}")
        result[key] = value.strip()
    return result


def _unquote_inno_value(value: str) -> str:
    stripped = value.strip()
    if len(stripped) >= 2 and stripped[0] == '"' and stripped[-1] == '"':
        return stripped[1:-1].replace('""', '"')
    return stripped


def _inno_audit_guarded(entry: dict[str, str]) -> bool:
    value = entry.get("check")
    return bool(value and INNO_AUDIT_GUARD.fullmatch(_unquote_inno_value(value)))


def _is_safe_inno_destination_component(value: str) -> bool:
    if not value or value in {".", ".."} or value.endswith((" ", ".")):
        return False
    if INNO_UNSAFE_DESTINATION_CHARACTER.search(value):
        return False
    return value.split(".", 1)[0].casefold() not in INNO_WINDOWS_RESERVED_NAMES


def _validate_inno_file_entry(entry: dict[str, str], line_number: int) -> list[str]:
    errors: list[str] = []
    source = _unquote_inno_value(entry.get("source", "")).replace("/", "\\")
    source_folded = source.casefold()
    source_prefix = "..\\dist\\lolreplaytool\\"
    if not source_folded.startswith(source_prefix):
        errors.append(
            f"Inno [Files] source must be under validated dist at line {line_number}."
        )
    else:
        remainder = source[len(source_prefix) :]
        if not remainder or any(part in {"", ".", ".."} for part in remainder.split("\\")):
            errors.append(f"Inno [Files] source is unsafe at line {line_number}.")

    destination = _unquote_inno_value(entry.get("destdir", "")).replace("/", "\\")
    destination_folded = destination.casefold()
    if not (
        destination_folded == "{app}"
        or destination_folded.startswith("{app}\\")
    ):
        errors.append(
            f"Inno [Files] destination must stay under {{app}} at line {line_number}."
        )
    elif destination_folded.startswith("{app}\\"):
        destination_parts = destination[len("{app}\\") :].split("\\")
        if not all(
            _is_safe_inno_destination_component(part) for part in destination_parts
        ):
            errors.append(f"Inno [Files] destination is unsafe at line {line_number}.")

    if "destname" in entry:
        destination_name = _unquote_inno_value(entry["destname"])
        if not _is_safe_inno_destination_component(destination_name):
            errors.append(f"Inno [Files] DestName is unsafe at line {line_number}.")

    flags = {
        flag.casefold()
        for flag in _unquote_inno_value(entry.get("flags", "")).split()
    }
    forbidden_flags = sorted(flags & INNO_FORBIDDEN_FILE_FLAGS)
    if forbidden_flags:
        errors.append(
            f"Inno [Files] has audit-unsafe flags at line {line_number}: "
            + ", ".join(forbidden_flags)
        )
    forbidden_fields = sorted(entry.keys() & INNO_FORBIDDEN_FILE_FIELDS)
    if forbidden_fields:
        errors.append(
            f"Inno [Files] has audit-unsafe fields at line {line_number}: "
            + ", ".join(forbidden_fields)
        )
    return errors


def validate_inno_audit_guards(script_path: Path) -> list[str]:
    """Reject installer-script changes that can escape the audit app directory."""
    try:
        source = script_path.read_text(encoding="utf-8")
    except OSError as exc:
        return [f"Cannot read Inno Setup script: {exc}"]

    errors = _validate_inno_code(source)
    section = ""
    section_counts: dict[str, int] = {}
    setup_values: dict[str, list[tuple[int, str]]] = {}
    section_entries: dict[str, int] = {}
    preprocessor_lines: list[tuple[int, str]] = []
    inline_preprocessor_lines: list[tuple[str, int, str]] = []
    for line_number, raw_line in enumerate(source.splitlines(), start=1):
        line = raw_line.strip()
        if "{#" in line:
            inline_preprocessor_lines.append((section, line_number, line))
        if not line or line.startswith(";"):
            continue
        section_match = re.fullmatch(r"\[([^]]+)]", line)
        if section_match:
            section = section_match.group(1).strip().casefold()
            section_counts[section] = section_counts.get(section, 0) + 1
            if section not in INNO_ALLOWED_SECTIONS:
                errors.append(
                    f"Unknown Inno section cannot be audited at line {line_number}: "
                    f"[{section}]"
                )
            continue
        if line.startswith("#"):
            if section:
                errors.append(
                    f"Inno preprocessor directive must precede sections at line {line_number}."
                )
            preprocessor_lines.append((line_number, line))
            continue
        if not section:
            errors.append(f"Inno content appears outside a section at line {line_number}.")
            continue
        if section != "code" and re.search(r"\{code:", line, re.IGNORECASE):
            errors.append(
                f"Inno code constant cannot be audited at line {line_number}."
            )
        if section == "setup":
            if "=" not in line:
                errors.append(f"Invalid Inno [Setup] directive at line {line_number}.")
                continue
            name, value = line.split("=", 1)
            setup_values.setdefault(name.strip().casefold(), []).append(
                (line_number, value.strip())
            )
            continue
        if (
            section == "files"
            or section in INNO_SIDE_EFFECT_SECTIONS
            or section in INNO_CONDITIONAL_ENTRY_SECTIONS
        ):
            section_entries[section] = section_entries.get(section, 0) + 1
            try:
                entry = _parse_inno_entry(line)
            except ValueError as exc:
                errors.append(
                    f"Cannot parse Inno [{section}] entry at line {line_number}: {exc}"
                )
                continue
            if section == "files":
                errors.extend(_validate_inno_file_entry(entry, line_number))
            elif section in INNO_SIDE_EFFECT_SECTIONS and not _inno_audit_guarded(
                entry
            ):
                errors.append(
                    f"Inno [{section}] entry lacks explicit audit guard at line "
                    f"{line_number}."
                )
            elif section in INNO_CONDITIONAL_ENTRY_SECTIONS and "check" in entry:
                errors.append(
                    f"Inno [{section}] Check callback cannot be audited at line "
                    f"{line_number}."
                )

    for name, expected in INNO_REQUIRED_SETUP_VALUES.items():
        values = setup_values.get(name, [])
        if len(values) != 1:
            errors.append(f"Inno [Setup] must define {name} exactly once.")
            continue
        line_number, actual = values[0]
        if actual.casefold() != expected.casefold():
            errors.append(
                f"Inno [Setup] {name} is not audit-safe at line {line_number}."
            )
    errors.extend(_validate_inno_preprocessor(preprocessor_lines))
    errors.extend(_validate_inline_inno_preprocessor(inline_preprocessor_lines))
    if section_counts.get("code", 0) != 1:
        errors.append("Inno [Code] must be defined exactly once.")
    if section_entries.get("files", 0) == 0:
        errors.append("Inno [Files] must contain the validated application payload.")
    return errors


def _is_reparse_point(metadata: os.stat_result) -> bool:
    return bool(
        stat.S_ISLNK(metadata.st_mode)
        or (
            getattr(metadata, "st_file_attributes", 0)
            & FILE_ATTRIBUTE_REPARSE_POINT
        )
    )


def _safe_relative(value: str) -> str | None:
    normalized = value.replace("\\", "/")
    relative = PurePosixPath(normalized)
    if (
        not normalized
        or relative.is_absolute()
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


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_real_directory(path: Path, *, label: str) -> Path:
    try:
        metadata = path.lstat()
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise InstallerContentAuditError(f"Cannot resolve {label}: {path}: {exc}") from exc
    if _is_reparse_point(metadata) or not stat.S_ISDIR(metadata.st_mode):
        raise InstallerContentAuditError(
            f"{label} must be a real directory, not a link: {path}"
        )
    return resolved


def _paths_overlap(first: Path, second: Path) -> bool:
    try:
        first.relative_to(second)
        return True
    except ValueError:
        pass
    try:
        second.relative_to(first)
        return True
    except ValueError:
        return False


def inventory_tree(root: Path) -> TreeInventory:
    """Return a fail-closed, Windows-path-safe inventory without following links."""
    canonical_root = _canonical_real_directory(root, label="inventory root")
    files: dict[str, FileRecord] = {}
    directories: dict[str, str] = {}
    forbidden: set[str] = set()
    pending = [canonical_root]

    while pending:
        directory = pending.pop()
        try:
            entries = sorted(os.scandir(directory), key=lambda entry: entry.name.casefold())
        except OSError as exc:
            raise InstallerContentAuditError(
                f"Cannot scan installer content directory: {directory}: {exc}"
            ) from exc
        for entry in entries:
            path = Path(entry.path)
            try:
                metadata = path.lstat()
            except OSError as exc:
                raise InstallerContentAuditError(
                    f"Cannot inspect installer content path: {path}: {exc}"
                ) from exc
            relative = path.relative_to(canonical_root).as_posix()
            normalized = _safe_relative(relative)
            if normalized is None:
                raise InstallerContentAuditError(
                    f"Unsafe path in installer content: {relative}"
                )
            if _is_reparse_point(metadata):
                raise InstallerContentAuditError(
                    f"Links and reparse points are forbidden in installer content: "
                    f"{normalized}"
                )
            key = normalized.casefold()
            if stat.S_ISDIR(metadata.st_mode):
                previous = directories.get(key)
                if previous is not None and previous != normalized:
                    raise InstallerContentAuditError(
                        "Case-insensitive installer directory collision: "
                        f"{previous} / {normalized}"
                    )
                if key in files:
                    raise InstallerContentAuditError(
                        f"File and directory collide in installer content: {normalized}"
                    )
                directories[key] = normalized
                if is_user_provided_runtime_path(normalized, is_directory=True):
                    forbidden.add(normalized)
                pending.append(path)
                continue
            if not stat.S_ISREG(metadata.st_mode):
                raise InstallerContentAuditError(
                    f"Non-regular object is forbidden in installer content: {normalized}"
                )
            previous_file = files.get(key)
            if previous_file is not None and previous_file.path != normalized:
                raise InstallerContentAuditError(
                    "Case-insensitive installer file collision: "
                    f"{previous_file.path} / {normalized}"
                )
            if key in directories:
                raise InstallerContentAuditError(
                    f"File and directory collide in installer content: {normalized}"
                )
            try:
                file_sha256 = _sha256_file(path)
            except OSError as exc:
                raise InstallerContentAuditError(
                    f"Cannot hash installer content file: {normalized}: {exc}"
                ) from exc
            files[key] = FileRecord(
                path=normalized,
                size=metadata.st_size,
                sha256=file_sha256,
            )
            if is_user_provided_runtime_path(normalized):
                forbidden.add(normalized)

    return TreeInventory(
        files=files,
        directories=directories,
        forbidden_runtimes=tuple(
            sorted(forbidden, key=lambda value: (value.casefold(), value))
        ),
    )


def _required_path_errors(root: Path, *, label: str) -> list[str]:
    errors: list[str] = []
    for relative in REQUIRED_INSTALLER_PATHS:
        path = root / Path(*PurePosixPath(relative).parts)
        try:
            metadata = path.lstat()
        except OSError:
            errors.append(f"{label} required file is missing: {relative}")
            continue
        if (
            _is_reparse_point(metadata)
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size == 0
        ):
            errors.append(f"{label} required file is empty or unsafe: {relative}")
    return errors


def _comparison_errors(expected: TreeInventory, actual: TreeInventory) -> list[str]:
    errors: list[str] = []
    for key, expected_record in expected.files.items():
        actual_record = actual.files.get(key)
        if actual_record is None:
            errors.append(
                f"Installer payload is missing distribution file: {expected_record.path}"
            )
            continue
        if actual_record.path != expected_record.path:
            errors.append(
                "Installer payload path casing differs: "
                f"{expected_record.path} / {actual_record.path}"
            )
        if (
            actual_record.size != expected_record.size
            or actual_record.sha256 != expected_record.sha256
        ):
            errors.append(
                f"Installer payload content SHA256 differs: {expected_record.path}"
            )
    for key, actual_record in actual.files.items():
        if key not in expected.files:
            errors.append(
                f"Installer payload contains an extra file: {actual_record.path}"
            )

    for key, expected_path in expected.directories.items():
        actual_path = actual.directories.get(key)
        if actual_path is None:
            errors.append(
                f"Installer payload is missing distribution directory: {expected_path}"
            )
        elif actual_path != expected_path:
            errors.append(
                "Installer payload directory casing differs: "
                f"{expected_path} / {actual_path}"
            )
    for key, actual_path in actual.directories.items():
        if key not in expected.directories:
            errors.append(
                f"Installer payload contains an extra directory: {actual_path}"
            )
    return errors


def audit_installer_payload(
    distribution_root: Path,
    installed_root: Path,
) -> list[str]:
    """Validate both trees and compare every path, size, and SHA256 bidirectionally."""
    try:
        expected_root = _canonical_real_directory(
            distribution_root,
            label="validated distribution root",
        )
        actual_root = _canonical_real_directory(
            installed_root,
            label="installer payload root",
        )
    except InstallerContentAuditError as exc:
        return [str(exc)]
    if _paths_overlap(expected_root, actual_root):
        return ["Validated distribution root and installer payload root must be disjoint."]

    errors = [
        *_required_path_errors(expected_root, label="Validated distribution"),
        *_required_path_errors(actual_root, label="Installer payload"),
    ]
    try:
        expected = inventory_tree(expected_root)
        actual = inventory_tree(actual_root)
    except InstallerContentAuditError as exc:
        return [*errors, str(exc)]

    errors.extend(
        f"Validated distribution contains a forbidden user-provided runtime: {path}"
        for path in expected.forbidden_runtimes
    )
    errors.extend(
        f"Installer payload contains a forbidden user-provided runtime: {path}"
        for path in actual.forbidden_runtimes
    )
    try:
        errors.extend(
            f"Validated distribution compliance: {error}"
            for error in validate_distribution(expected_root)
        )
    except (OSError, RuntimeError, ValueError) as exc:
        errors.append(f"Cannot validate distribution compliance: {exc}")
    try:
        errors.extend(
            f"Installer payload compliance: {error}"
            for error in validate_distribution(actual_root)
        )
    except (OSError, RuntimeError, ValueError) as exc:
        errors.append(f"Cannot validate installer payload compliance: {exc}")

    errors.extend(_comparison_errors(expected, actual))
    manifest_key = MANIFEST_RELATIVE_PATH.casefold()
    expected_manifest = expected.files.get(manifest_key)
    actual_manifest = actual.files.get(manifest_key)
    if (
        expected_manifest is not None
        and actual_manifest is not None
        and expected_manifest.sha256 != actual_manifest.sha256
    ):
        errors.append("Installer distribution-manifest.json SHA256 differs.")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Verify that a safely installed Setup EXE payload exactly matches "
            "the validated PyInstaller distribution."
        )
    )
    parser.add_argument("--distribution-root", type=Path)
    parser.add_argument("--installed-root", type=Path)
    parser.add_argument("--inno-script", required=True, type=Path)
    parser.add_argument("--validate-inno-only", action="store_true")
    args = parser.parse_args(argv)

    if args.validate_inno_only:
        if args.distribution_root is not None or args.installed_root is not None:
            parser.error(
                "--validate-inno-only cannot be combined with payload roots"
            )
        errors = validate_inno_audit_guards(args.inno_script)
    else:
        if args.distribution_root is None or args.installed_root is None:
            parser.error(
                "--distribution-root and --installed-root are required for payload audit"
            )
        errors = [
            *validate_inno_audit_guards(args.inno_script),
            *audit_installer_payload(args.distribution_root, args.installed_root),
        ]
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    if args.validate_inno_only:
        print(f"Inno audit structure validation passed: {args.inno_script}")
    else:
        print(
            "Installer content audit passed: "
            f"{args.installed_root} matches {args.distribution_root}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
