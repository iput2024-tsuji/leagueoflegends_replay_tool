"""Apply the reproducible Windows runtime policy to PyInstaller binaries."""

from __future__ import annotations

import re
from collections.abc import Iterable

PyInstallerBinary = tuple[str, str, str]

_WINDOWS_OS_RUNTIME_NAME = re.compile(
    r"api-ms-win-(?:core|crt)-[a-z0-9-]+\.dll\Z",
    re.IGNORECASE,
)
MICROSOFT_RUNTIME_NAMES = frozenset(
    {
        "msvcp140.dll",
        "msvcp140_1.dll",
        "msvcp140_2.dll",
        "vcruntime140.dll",
        "vcruntime140_1.dll",
        "vcomp140.dll",
        "concrt140.dll",
    }
)
_HASHED_MICROSOFT_RUNTIME_NAME = re.compile(
    r"(?:msvcp140(?:_[12])?|vcruntime140(?:_1)?|vcomp140|concrt140)"
    r"-[0-9a-f]+\.dll\Z",
    re.IGNORECASE,
)
_MICROSOFT_RUNTIME_PREFIXES = ("msvcp", "vcruntime", "vcomp", "concrt")
_UNUSED_QT_RUNTIME_NAMES = {
    "pyqt6/qt6/bin/opengl32sw.dll",
    "pyqt6/qt6/bin/qt6pdf.dll",
    "pyqt6/qt6/plugins/imageformats/qpdf.dll",
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


def classify_microsoft_runtime_name(toc_name: str) -> str | None:
    """Classify an app-local Microsoft Runtime basename, including nested paths."""

    normalized = toc_name.replace("\\", "/")
    basename = normalized.rsplit("/", 1)[-1]
    lowered = basename.casefold()
    if lowered in MICROSOFT_RUNTIME_NAMES:
        return "generic"
    if _HASHED_MICROSOFT_RUNTIME_NAME.fullmatch(basename) is not None:
        return "hashed"
    if lowered.endswith(".dll") and lowered.startswith(
        _MICROSOFT_RUNTIME_PREFIXES
    ):
        return "unknown"
    return None


def apply_windows_runtime_policy(
    binaries: Iterable[PyInstallerBinary],
) -> list[PyInstallerBinary]:
    """Remove known external runtimes and reject unknown Runtime names."""

    retained: list[PyInstallerBinary] = []
    seen_destinations: set[str] = set()
    for entry in binaries:
        if (
            not isinstance(entry, tuple)
            or len(entry) != 3
            or not all(isinstance(value, str) for value in entry)
        ):
            raise RuntimeError("PyInstaller emitted an invalid binary TOC entry.")
        toc_name, _source, entry_type = entry
        if entry_type not in {"BINARY", "EXTENSION"}:
            raise RuntimeError(
                f"PyInstaller emitted an unexpected binary type: {entry_type}"
            )
        normalized_name = toc_name.replace("\\", "/")
        runtime_kind = classify_microsoft_runtime_name(normalized_name)
        if runtime_kind == "unknown":
            raise RuntimeError(
                f"PyInstaller emitted an unknown Microsoft Runtime: {toc_name}"
            )
        if (
            is_windows_os_runtime_name(toc_name)
            or runtime_kind is not None
            or normalized_name.casefold() in _UNUSED_QT_RUNTIME_NAMES
        ):
            continue
        destination_key = normalized_name.casefold()
        if destination_key in seen_destinations:
            raise RuntimeError(
                f"PyInstaller emitted a duplicate binary destination: {toc_name}"
            )
        seen_destinations.add(destination_key)
        retained.append(entry)
    return retained
