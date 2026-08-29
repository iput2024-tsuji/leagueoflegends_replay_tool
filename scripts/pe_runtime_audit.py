"""Create a deterministic PE import inventory for Windows distribution audits."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

import pefile

from scripts.pyinstaller_runtime_policy import classify_microsoft_runtime_name


class AuditError(ValueError):
    """An input cannot be safely audited."""


_ICU_DLL_NAME = re.compile(r"icu.*\.dll\Z", re.IGNORECASE)
_EXPECTED_QT_SYSTEM_ICU_IMPORT = {
    "name": "icuuc.dll",
    "pe": "_internal/PyQt6/Qt6/bin/Qt6Core.dll",
    "import_type": "normal",
}


def _runtime_kind(name: str) -> str | None:
    return classify_microsoft_runtime_name(name)


def _is_icu_dll_name(name: str) -> bool:
    return bool(_ICU_DLL_NAME.fullmatch(name))


def _normalized_import(item: dict[str, str]) -> tuple[str, str, str]:
    return (
        item["pe"].casefold(),
        item["name"].casefold(),
        item["import_type"],
    )


def _decode_name(value: Any) -> str:
    if not isinstance(value, (bytes, bytearray)):
        raise AuditError(f"invalid import name: {value!r}")
    try:
        name = bytes(value).decode("ascii")
    except UnicodeDecodeError as exc:
        raise AuditError("non-ASCII import name") from exc
    if not name or "\x00" in name or "/" in name or "\\" in name:
        raise AuditError(f"invalid import name: {name!r}")
    return name


def _imports(pe: Any) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    for directory, kind in (
        ("DIRECTORY_ENTRY_IMPORT", "normal"),
        ("DIRECTORY_ENTRY_DELAY_IMPORT", "delay"),
    ):
        for entry in getattr(pe, directory, []) or []:
            result.append(
                {"name": _decode_name(getattr(entry, "dll", None)), "type": kind}
            )
    return sorted(result, key=lambda item: (item["name"].casefold(), item["type"]))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_inventory(
    root: str | Path,
    *,
    enforce_external: bool = False,
    require_qt_system_icu: bool = False,
) -> dict[str, Any]:
    """Audit a PE tree; the Qt ICU requirement targets the final onedir layout."""

    root_path = Path(root)
    if not root_path.is_dir():
        raise AuditError(f"root is not a directory: {root_path}")
    candidates = sorted(
        (path for path in root_path.rglob("*") if path.is_file() and path.suffix.lower() in {".exe", ".dll", ".pyd"}),
        key=lambda path: (
            path.relative_to(root_path).as_posix().casefold(),
            path.relative_to(root_path).as_posix(),
        ),
    )
    files: list[dict[str, Any]] = []
    reverse: dict[str, list[dict[str, str]]] = {}
    app_local: list[str] = []
    app_local_icu: list[str] = []
    hashed_imports: list[dict[str, str]] = []
    unknown_imports: list[dict[str, str]] = []
    icu_imports: list[dict[str, str]] = []
    for path in candidates:
        relative = path.relative_to(root_path).as_posix()
        runtime = _runtime_kind(path.name)
        if runtime:
            app_local.append(relative)
        if _is_icu_dll_name(path.name):
            app_local_icu.append(relative)
        pe = None
        try:
            pe = pefile.PE(str(path), fast_load=False)
            imports = _imports(pe)
        except (OSError, pefile.PEFormatError, AuditError, ValueError) as exc:
            raise AuditError(f"failed to parse {relative}: {exc}") from exc
        finally:
            close = getattr(pe, "close", None)
            if callable(close):
                close()
        files.append({"path": relative, "size": path.stat().st_size, "sha256": _sha256(path), "imports": imports})
        for item in imports:
            ref = {"pe": relative, "import_type": item["type"]}
            if _is_icu_dll_name(item["name"]):
                icu_imports.append({"name": item["name"], **ref})
            kind = _runtime_kind(item["name"])
            if not kind:
                continue
            reverse.setdefault(item["name"].lower(), []).append(ref)
            if kind == "hashed":
                hashed_imports.append({"name": item["name"], **ref})
            elif kind == "unknown":
                unknown_imports.append({"name": item["name"], **ref})
    for refs in reverse.values():
        refs.sort(key=lambda item: (item["pe"].casefold(), item["import_type"]))
    reverse = {name: reverse[name] for name in sorted(reverse, key=str.casefold)}
    app_local.sort(key=str.casefold)
    app_local_icu.sort(key=str.casefold)
    hashed_imports.sort(key=lambda item: (item["name"].casefold(), item["pe"].casefold(), item["import_type"]))
    unknown_imports.sort(key=lambda item: (item["name"].casefold(), item["pe"].casefold(), item["import_type"]))
    icu_imports.sort(
        key=lambda item: (
            item["name"].casefold(),
            item["pe"].casefold(),
            item["import_type"],
        )
    )
    inventory = {
        "schema_version": 2,
        "tool": {
            "name": "pe_runtime_audit",
            "pefile_version": pefile.__version__,
        },
        "files": files,
        "runtime_reverse": reverse,
        "summary": {
            "pe_files": len(files),
            "import_count": sum(len(item["imports"]) for item in files),
            "runtime_import_count": sum(len(refs) for refs in reverse.values()),
            "app_local_runtime_files": app_local,
            "hashed_imports": hashed_imports,
            "unknown_runtime_imports": unknown_imports,
            "app_local_icu_files": app_local_icu,
            "icu_imports": icu_imports,
        },
    }
    if require_qt_system_icu:
        enforce_external = True
    if enforce_external:
        failures = []
        if app_local:
            failures.append(f"app-local Runtime files: {', '.join(app_local)}")
        if hashed_imports:
            failures.append(
                "hashed Runtime imports: "
                + ", ".join(
                    f"{item['pe']} -> {item['name']} ({item['import_type']})"
                    for item in hashed_imports
                )
            )
        if unknown_imports:
            failures.append(
                "unknown Runtime imports: "
                + ", ".join(
                    f"{item['pe']} -> {item['name']} ({item['import_type']})"
                    for item in unknown_imports
                )
            )
        if app_local_icu:
            failures.append(f"app-local ICU files: {', '.join(app_local_icu)}")
        expected_icu = _normalized_import(_EXPECTED_QT_SYSTEM_ICU_IMPORT)
        actual_icu = [_normalized_import(item) for item in icu_imports]
        if require_qt_system_icu and actual_icu != [expected_icu]:
            actual_text = ", ".join(
                f"{item['pe']} -> {item['name']} ({item['import_type']})"
                for item in icu_imports
            ) or "none"
            failures.append(
                "Qt system ICU import graph differs; expected "
                f"{_EXPECTED_QT_SYSTEM_ICU_IMPORT['pe']} -> "
                f"{_EXPECTED_QT_SYSTEM_ICU_IMPORT['name']} "
                f"({_EXPECTED_QT_SYSTEM_ICU_IMPORT['import_type']}); actual "
                f"{actual_text}"
            )
        elif any(_normalized_import(item) != expected_icu for item in icu_imports):
            failures.append(
                "unexpected ICU imports: "
                + ", ".join(
                    f"{item['pe']} -> {item['name']} ({item['import_type']})"
                    for item in icu_imports
                    if _normalized_import(item) != expected_icu
                )
            )
        if failures:
            raise AuditError(
                "external runtime enforcement failed; " + "; ".join(failures)
            )
    return inventory


def _json_bytes(inventory: dict[str, Any]) -> str:
    return json.dumps(inventory, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--enforce-external",
        action="store_true",
        help="reject app-local Runtime/ICU DLLs and unexpected imports",
    )
    parser.add_argument(
        "--require-qt-system-icu",
        action="store_true",
        help=(
            "require the fixed Qt6Core -> icuuc.dll graph in the final "
            "PyInstaller onedir layout (implies --enforce-external)"
        ),
    )
    args = parser.parse_args(argv)
    try:
        output = _json_bytes(
            build_inventory(
                args.root,
                enforce_external=args.enforce_external,
                require_qt_system_icu=args.require_qt_system_icu,
            )
        )
        if args.output:
            args.output.write_text(output, encoding="utf-8", newline="\n")
        else:
            sys.stdout.write(output)
    except (AuditError, OSError) as exc:
        print(f"pe-runtime-audit: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
