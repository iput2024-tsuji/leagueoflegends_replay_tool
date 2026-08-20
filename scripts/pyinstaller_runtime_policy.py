"""Apply the reproducible Windows runtime policy to PyInstaller binaries."""

from __future__ import annotations

import re
from collections.abc import Iterable

PyInstallerBinary = tuple[str, str, str]

_WINDOWS_OS_RUNTIME_NAME = re.compile(
    r"api-ms-win-(?:core|crt)-[a-z0-9-]+\.dll\Z",
    re.IGNORECASE,
)
_SCIKIT_LEARN_VCOMP_NAME = "sklearn/.libs/vcomp140.dll"
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


def is_root_vcomp_name(toc_name: str) -> bool:
    """Return whether a TOC entry is PyInstaller's redundant root VCOMP copy."""

    normalized = toc_name.replace("\\", "/")
    return "/" not in normalized and normalized.casefold() == "vcomp140.dll"


def apply_windows_runtime_policy(
    binaries: Iterable[PyInstallerBinary],
) -> list[PyInstallerBinary]:
    """Remove host OS runtimes and unused Qt runtime artifacts."""

    retained: list[PyInstallerBinary] = []
    scikit_learn_vcomp_count = 0
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
        if (
            is_windows_os_runtime_name(toc_name)
            or is_root_vcomp_name(toc_name)
            or normalized_name.casefold() in _UNUSED_QT_RUNTIME_NAMES
        ):
            continue
        if normalized_name.casefold() == _SCIKIT_LEARN_VCOMP_NAME:
            if entry_type != "BINARY":
                raise RuntimeError(
                    "The locked scikit-learn VCOMP entry is not a binary."
                )
            scikit_learn_vcomp_count += 1
        destination_key = normalized_name.casefold()
        if destination_key in seen_destinations:
            raise RuntimeError(
                f"PyInstaller emitted a duplicate binary destination: {toc_name}"
            )
        seen_destinations.add(destination_key)
        retained.append(entry)
    if scikit_learn_vcomp_count != 1:
        raise RuntimeError(
            "PyInstaller must collect exactly one locked scikit-learn vcomp140.dll."
        )
    return retained
