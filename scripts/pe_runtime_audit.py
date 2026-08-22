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

GENERIC_RUNTIME_NAMES = frozenset(
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
_RUNTIME_PREFIXES = ("msvcp", "vcruntime", "vcomp", "concrt")
_HASHED_RUNTIME = re.compile(
    r"^(?:msvcp140(?:_[12])?|vcruntime140(?:_1)?|vcomp140|concrt140)-[0-9a-f]+\.dll$",
    re.IGNORECASE,
)


class AuditError(ValueError):
    """An input cannot be safely audited."""


def _runtime_kind(name: str) -> str | None:
    lowered = name.lower()
    if lowered in GENERIC_RUNTIME_NAMES:
        return "generic"
    if _HASHED_RUNTIME.fullmatch(name):
        return "hashed"
    if lowered.endswith(".dll") and lowered.startswith(_RUNTIME_PREFIXES):
        return "unknown"
    return None


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


def build_inventory(root: str | Path, *, enforce_external: bool = False) -> dict[str, Any]:
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
    hashed_imports: list[dict[str, str]] = []
    unknown_imports: list[dict[str, str]] = []
    for path in candidates:
        relative = path.relative_to(root_path).as_posix()
        runtime = _runtime_kind(path.name)
        if runtime:
            app_local.append(relative)
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
            kind = _runtime_kind(item["name"])
            if not kind:
                continue
            ref = {"pe": relative, "import_type": item["type"]}
            reverse.setdefault(item["name"].lower(), []).append(ref)
            if kind == "hashed":
                hashed_imports.append({"name": item["name"], **ref})
            elif kind == "unknown":
                unknown_imports.append({"name": item["name"], **ref})
    for refs in reverse.values():
        refs.sort(key=lambda item: (item["pe"].casefold(), item["import_type"]))
    reverse = {name: reverse[name] for name in sorted(reverse, key=str.casefold)}
    app_local.sort(key=str.casefold)
    hashed_imports.sort(key=lambda item: (item["name"].casefold(), item["pe"].casefold(), item["import_type"]))
    unknown_imports.sort(key=lambda item: (item["name"].casefold(), item["pe"].casefold(), item["import_type"]))
    inventory = {
        "schema_version": 1,
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
        },
    }
    if enforce_external and (app_local or hashed_imports or unknown_imports):
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
        raise AuditError("external runtime enforcement failed; " + "; ".join(failures))
    return inventory


def _json_bytes(inventory: dict[str, Any]) -> str:
    return json.dumps(inventory, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--enforce-external", action="store_true")
    args = parser.parse_args(argv)
    try:
        output = _json_bytes(build_inventory(args.root, enforce_external=args.enforce_external))
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
