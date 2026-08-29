"""Create deterministic experimental wheels that use the external VC runtime."""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

from scripts import pe_runtime_audit

HASHED_RUNTIME = "msvcp140-a4c2229bdc2a2a630acdc095b4d86008.dll"
PROVENANCE_NAME = "external-vc-runtime-wheel-provenance.json"
REQUIRED_PYTHON = (3, 14, 6)

EXPECTED: dict[str, dict[str, Any]] = {
    "numpy-2.4.1-cp314-cp314-win_amd64.whl": {
        "component": "numpy",
        "distribution": "numpy",
        "url": (
            "https://files.pythonhosted.org/packages/7e/bb/"
            "c6513edcce5a831810e2dddc0d3452ce84d208af92405a0c2e58fd8e7881/"
            "numpy-2.4.1-cp314-cp314-win_amd64.whl"
        ),
        "size": 12_438_590,
        "sha256": (
            "7d5d7999df434a038d75a748275cd6c0094b0ecdb0837342b332a82defc4dc4d"
        ),
        "record": "numpy-2.4.1.dist-info/RECORD",
        "affected": {
            "numpy/_core/_multiarray_umath.cp314-win_amd64.pyd",
            "numpy/fft/_pocketfft_umath.cp314-win_amd64.pyd",
        },
        "runtime_members": {f"numpy.libs/{HASHED_RUNTIME}"},
        "source_archives": [
            {
                "filename": "numpy-2.4.1.tar.gz",
                "url": (
                    "https://files.pythonhosted.org/packages/24/62/"
                    "ae72ff66c0f1fd959925b4c11f8c2dea61f47f6acaea75a08512cdfe3fed/"
                    "numpy-2.4.1.tar.gz"
                ),
                "sha256": (
                    "a1ceafc5042451a858231588a104093474c6a5c57dcc724841f5c888d237d690"
                ),
                "size": 20_721_320,
            },
            {
                "filename": "openblas-libs-v0.3.30.0.7.tar.gz",
                "url": (
                    "https://codeload.github.com/MacPython/openblas-libs/tar.gz/"
                    "refs/tags/v0.3.30.0.7"
                ),
                "sha256": (
                    "f5051a23674867774bc6c619799cfc40185963876695143c3bd0b3c67a7dece3"
                ),
                "size": 409_819,
            },
            {
                "filename": "OpenBLAS-b5456c1.tar.gz",
                "url": (
                    "https://codeload.github.com/OpenMathLib/OpenBLAS/tar.gz/"
                    "b5456c1b41ea88d4e0041778aa8ec09ee2a111a0"
                ),
                "sha256": (
                    "0a17959ade3b0fc0b7d4af7b67bc5fc7823da643ad3f0699c9c84e795e6c204a"
                ),
                "size": 24_730_332,
            },
        ],
    },
    "pandas-3.0.2-cp314-cp314-win_amd64.whl": {
        "component": "pandas",
        "distribution": "pandas",
        "url": (
            "https://files.pythonhosted.org/packages/db/60/"
            "aba6a38de456e7341285102bede27514795c1eaa353bc0e7638b6b785356/"
            "pandas-3.0.2-cp314-cp314-win_amd64.whl"
        ),
        "size": 9_865_893,
        "sha256": (
            "b35d14bb5d8285d9494fe93815a9e9307c0876e10f1e8e89ac5b88f728ec8dcf"
        ),
        "record": "pandas-3.0.2.dist-info/RECORD",
        "affected": {"pandas/_libs/window/aggregations.cp314-win_amd64.pyd"},
        "runtime_members": {f"pandas.libs/{HASHED_RUNTIME}"},
        "source_archives": [
            {
                "filename": "pandas-3.0.2.tar.gz",
                "url": (
                    "https://files.pythonhosted.org/packages/da/99/"
                    "b342345300f13440fe9fe385c3c481e2d9a595ee3bab4d3219247ac94e9a/"
                    "pandas-3.0.2.tar.gz"
                ),
                "sha256": (
                    "f4753e73e34c8d83221ba58f232433fca2748be8b18dbca02d242ed153945043"
                ),
                "size": 4_645_855,
            }
        ],
    },
    "pyqt6_qt6-6.10.2-py3-none-win_amd64.whl": {
        "component": "qt",
        "distribution": "PyQt6-Qt6",
        "url": (
            "https://files.pythonhosted.org/packages/06/8e/"
            "595f215876d507417cc8565e05519916d3b0b76baedea6a1e4e5105633fc/"
            "pyqt6_qt6-6.10.2-py3-none-win_amd64.whl"
        ),
        "size": 78_433_821,
        "sha256": (
            "c4b7f7d66cc58bddf1bc1ca28dfcf7a45f58cfcb11d81d13a0510409dd4957ac"
        ),
        "record": "pyqt6_qt6-6.10.2.dist-info/RECORD",
        "affected": set(),
        "runtime_members": {
            "PyQt6/Qt6/bin/concrt140.dll",
            "PyQt6/Qt6/bin/msvcp140.dll",
            "PyQt6/Qt6/bin/msvcp140_1.dll",
            "PyQt6/Qt6/bin/msvcp140_2.dll",
            "PyQt6/Qt6/bin/vcruntime140.dll",
            "PyQt6/Qt6/bin/vcruntime140_1.dll",
        },
        "source_archives": [
            {
                "filename": "qtbase-everywhere-src-6.10.2.tar.xz",
                "url": (
                    "https://download.qt.io/official_releases/qt/6.10/6.10.2/"
                    "submodules/qtbase-everywhere-src-6.10.2.tar.xz"
                ),
                "sha256": (
                    "aeb78d29291a2b5fd53cb55950f8f5065b4978c25fb1d77f627d695ab9adf21e"
                ),
                "size": 50_374_380,
            },
            {
                "filename": "qtsvg-everywhere-src-6.10.2.tar.xz",
                "url": (
                    "https://download.qt.io/official_releases/qt/6.10/6.10.2/"
                    "submodules/qtsvg-everywhere-src-6.10.2.tar.xz"
                ),
                "sha256": (
                    "f07ff80f38caf235187200345392ca7479445ddf49a36c3694cd52a735dad6e1"
                ),
                "size": 2_614_740,
            },
            {
                "filename": "qtimageformats-everywhere-src-6.10.2.tar.xz",
                "url": (
                    "https://download.qt.io/official_releases/qt/6.10/6.10.2/"
                    "submodules/qtimageformats-everywhere-src-6.10.2.tar.xz"
                ),
                "sha256": (
                    "8b8f9c718638081e7b3c000e7f31910140b1202a98e98df5d1b496fe6f639d67"
                ),
                "size": 2_032_388,
            },
        ],
    },
    "scikit_learn-1.8.0-cp314-cp314-win_amd64.whl": {
        "component": "scikit-learn",
        "distribution": "scikit-learn",
        "url": (
            "https://files.pythonhosted.org/packages/76/18/"
            "a8def8f91b18cd1ba6e05dbe02540168cb24d47e8dcf69e8d00b7da42a08/"
            "scikit_learn-1.8.0-cp314-cp314-win_amd64.whl"
        ),
        "size": 8_096_518,
        "sha256": (
            "56079a99c20d230e873ea40753102102734c5953366972a71d5cb39a32bc40c6"
        ),
        "record": "scikit_learn-1.8.0.dist-info/RECORD",
        "affected": set(),
        "runtime_members": {
            "sklearn/.libs/msvcp140.dll",
            "sklearn/.libs/vcomp140.dll",
        },
        "source_archives": [
            {
                "filename": "scikit_learn-1.8.0.tar.gz",
                "url": (
                    "https://files.pythonhosted.org/packages/0e/d4/"
                    "40988bf3b8e34feec1d0e6a051446b1f66225f8529b9309becaeef62b6c4/"
                    "scikit_learn-1.8.0.tar.gz"
                ),
                "sha256": (
                    "9bccbb3b40e3de10351f8f5068e105d0f4083b1a65fa07b6634fbc401a6287fd"
                ),
                "size": 7_335_585,
            }
        ],
    },
}

DELVEWHEEL_ARTIFACTS = {
    "delvewheel-1.13.0-py3-none-any.whl": {
        "size": 59_411,
        "sha256": (
            "eb8c34dee5d8816516befde73bf5cf9ea6142955f41d3baf25381c0136b28608"
        ),
    },
    "delvewheel-1.13.0.tar.gz": {
        "size": 63_742,
        "sha256": (
            "440601f289c953d5b60e96af8a8dbe2729584d90b663e39c5eade6b9043b48f6"
        ),
    },
}

LOADER = "sklearn/_distributor_init.py"
LOADER_SHA256 = "81c3269d5a0d57301c9eb529d74856f91f06ac935d50e9db84dccb8f7e90c70c"
LOADER_REPLACEMENT = b"""\
'''Preload the externally installed vcomp140.dll and msvcp140.dll.'''


import os
from ctypes import WinDLL


if os.name == "nt":
    WinDLL("vcomp140.dll")
    WinDLL("msvcp140.dll")
"""

_WINDOWS_RESERVED_NAMES = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{index}" for index in range(1, 10)),
    *(f"lpt{index}" for index in range(1, 10)),
}


class WheelError(ValueError):
    """Wheel input or transformation failed closed."""


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_members(archive: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    result: list[zipfile.ZipInfo] = []
    seen: set[str] = set()
    for info in archive.infolist():
        name = info.filename
        path = PurePosixPath(name)
        parts = path.parts
        canonical = path.as_posix()
        is_directory = info.is_dir()
        expected_name = canonical + "/" if is_directory else canonical
        unsafe_part = any(
            not part
            or part in {".", ".."}
            or part.endswith((" ", "."))
            or part.split(".", 1)[0].casefold() in _WINDOWS_RESERVED_NAMES
            for part in parts
        )
        if (
            not name
            or path.is_absolute()
            or expected_name != name
            or unsafe_part
            or "\\" in name
            or ":" in name
            or "\x00" in name
        ):
            raise WheelError(f"unsafe wheel member: {name!r}")
        folded = canonical.casefold()
        if folded in seen:
            raise WheelError(f"duplicate wheel member on Windows: {name}")
        seen.add(folded)
        unix_type = (
            ((info.external_attr >> 16) & 0o170000)
            if info.create_system == 3
            else 0
        )
        if is_directory:
            if unix_type not in {0, 0o040000}:
                raise WheelError(f"non-directory wheel member: {name}")
            continue
        if unix_type not in {0, 0o100000}:
            raise WheelError(f"non-regular wheel member: {name}")
        if info.flag_bits & 0x1:
            raise WheelError(f"encrypted wheel member: {name}")
        if name.endswith(("RECORD.jws", "RECORD.p7s")):
            raise WheelError(f"signed wheel is unsupported: {name}")
        result.append(info)
    return result


def _archive_lock_fields(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "filename": entry.get("filename"),
        "url": entry.get("url"),
        "size": entry.get("size"),
        "sha256": entry.get("sha256"),
    }


def _lock_entries(lock: Path) -> dict[str, dict[str, Any]]:
    data = json.loads(lock.read_text(encoding="utf-8"))
    components = data.get("runtime_components")
    if not isinstance(components, list):
        raise WheelError("components lock has no runtime_components list")
    entries: dict[str, dict[str, Any]] = {}
    for value in components:
        if not isinstance(value, dict):
            raise WheelError("components lock contains a non-object component")
        archive = value.get("binary_archive")
        if not isinstance(archive, dict) or archive.get("filename") not in EXPECTED:
            continue
        name = archive["filename"]
        if name in entries:
            raise WheelError(f"duplicate wheel definition in lock: {name}")
        manifest = EXPECTED[name]
        expected_archive = {
            "filename": name,
            "url": manifest["url"],
            "size": manifest["size"],
            "sha256": manifest["sha256"],
        }
        if value.get("component") != manifest["component"]:
            raise WheelError(f"locked component differs for {name}")
        if _archive_lock_fields(archive) != expected_archive:
            raise WheelError(f"locked wheel metadata differs for {name}")
        if value.get("source_archives") != manifest["source_archives"]:
            raise WheelError(f"locked source archives differ for {name}")
        entries[name] = {
            **expected_archive,
            "source_archives": manifest["source_archives"],
        }
    missing = set(EXPECTED) - set(entries)
    if missing:
        raise WheelError(f"lock missing wheels: {sorted(missing)}")
    return entries


def validate_inputs(input_dir: Path, lock: Path) -> dict[str, Path]:
    entries = _lock_entries(lock)
    try:
        candidates = list(input_dir.iterdir())
    except OSError as exc:
        raise WheelError(f"wheel input directory is unavailable: {input_dir}") from exc
    wheels = {
        path.name: path
        for path in candidates
        if path.is_file() and path.suffix.casefold() == ".whl"
    }
    if set(wheels) != set(EXPECTED):
        raise WheelError(f"wheel candidate set differs: {sorted(wheels)}")
    for name, path in wheels.items():
        expected = entries[name]
        if (
            path.stat().st_size != expected["size"]
            or _sha(path) != expected["sha256"]
        ):
            raise WheelError(f"locked hash or size mismatch: {name}")
    return wheels


def _validate_tool_artifacts(tool_dir: Path) -> tuple[Path, dict[str, Any]]:
    evidence: dict[str, Any] = {}
    for name, expected in DELVEWHEEL_ARTIFACTS.items():
        path = tool_dir / name
        if not path.is_file():
            raise WheelError(f"delvewheel artifact missing: {name}")
        if (
            path.stat().st_size != expected["size"]
            or _sha(path) != expected["sha256"]
        ):
            raise WheelError(f"delvewheel artifact differs: {name}")
        evidence[name] = dict(expected)
    return tool_dir / "delvewheel-1.13.0-py3-none-any.whl", evidence


def _tool_environment(tool_wheel: Path) -> dict[str, str]:
    environment = os.environ.copy()
    existing = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = str(tool_wheel.resolve()) + (
        os.pathsep + existing if existing else ""
    )
    return environment


def _check_delvewheel(tool_wheel: Path) -> str:
    environment = _tool_environment(tool_wheel)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import delvewheel; from delvewheel._version import __version__; "
                "print(delvewheel.__file__); print(__version__)"
            ),
        ],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    lines = result.stdout.splitlines()
    if len(lines) != 2:
        raise WheelError("delvewheel identity output is malformed")
    location, version = lines
    if not location.casefold().startswith(str(tool_wheel.resolve()).casefold()):
        raise WheelError("delvewheel was not imported from the locked wheel")
    if version != "1.13.0":
        raise WheelError(f"delvewheel version must be 1.13.0, got {version}")
    return version


def _patch_loader(path: Path) -> tuple[str, str]:
    before = path.read_bytes()
    before_sha = hashlib.sha256(before).hexdigest()
    if before_sha != LOADER_SHA256:
        raise WheelError("scikit-learn loader content drifted")
    path.write_bytes(LOADER_REPLACEMENT)
    after = path.read_bytes()
    if (
        b".libs" in after
        or b"abspath" in after
        or after.count(b'WinDLL("vcomp140.dll")') != 1
        or after.count(b'WinDLL("msvcp140.dll")') != 1
    ):
        raise WheelError("scikit-learn loader patch is incomplete")
    return before_sha, hashlib.sha256(after).hexdigest()


def _replace_needed(path: Path, tool_wheel: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "delvewheel",
            "replace-needed",
            "-change",
            HASHED_RUNTIME,
            "MSVCP140.dll",
            str(path),
        ],
        capture_output=True,
        text=True,
        env=_tool_environment(tool_wheel),
    )
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise WheelError(
            f"delvewheel replace-needed failed for {path.name}: {detail}"
        )


def _record_bytes(files: dict[str, bytes], record: str) -> bytes:
    rows: list[list[str]] = []
    for name in sorted(files, key=lambda value: (value.casefold(), value)):
        if name == record:
            rows.append([name, "", ""])
            continue
        digest = base64.urlsafe_b64encode(hashlib.sha256(files[name]).digest())
        encoded = digest.rstrip(b"=").decode("ascii")
        rows.append([name, f"sha256={encoded}", str(len(files[name]))])
    stream = io.StringIO(newline="")
    csv.writer(stream, lineterminator="\n").writerows(rows)
    return stream.getvalue().encode("utf-8")


def _write_wheel(files: dict[str, bytes], output: Path, record: str) -> None:
    records = [name for name in files if name.endswith(".dist-info/RECORD")]
    if records != [record]:
        raise WheelError(f"wheel RECORD set differs: {records}")
    files[record] = _record_bytes(files, record)
    with zipfile.ZipFile(
        output,
        "w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
    ) as archive:
        for name in sorted(files, key=lambda value: (value.casefold(), value)):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            archive.writestr(info, files[name], compresslevel=9)


def _audit_tree(root: Path, *, phase: str) -> dict[str, Any]:
    try:
        return pe_runtime_audit.build_inventory(root)
    except (pe_runtime_audit.AuditError, OSError) as exc:
        raise WheelError(f"PE audit failed ({phase}): {exc}") from exc


def _hashed_refs(inventory: dict[str, Any]) -> set[tuple[str, str, str]]:
    return {
        (item["pe"], item["name"].casefold(), item["import_type"])
        for item in inventory["summary"]["hashed_imports"]
    }


def _validate_before(inventory: dict[str, Any], manifest: dict[str, Any]) -> None:
    expected_affected = {
        (path, HASHED_RUNTIME, "normal") for path in manifest["affected"]
    }
    actual_affected = _hashed_refs(inventory)
    if actual_affected != expected_affected:
        raise WheelError(
            "unexpected hashed Runtime import set for "
            f"{manifest['distribution']}: {sorted(actual_affected)}"
        )
    app_local = set(inventory["summary"]["app_local_runtime_files"])
    if app_local != manifest["runtime_members"]:
        raise WheelError(
            "unexpected app-local Runtime set for "
            f"{manifest['distribution']}: {sorted(app_local)}"
        )
    if inventory["summary"]["unknown_runtime_imports"]:
        raise WheelError(
            f"unknown Runtime import before repair: {manifest['distribution']}"
        )


def _validate_after(inventory: dict[str, Any], manifest: dict[str, Any]) -> None:
    summary = inventory["summary"]
    if (
        summary["app_local_runtime_files"]
        or summary["hashed_imports"]
        or summary["unknown_runtime_imports"]
    ):
        raise WheelError(
            f"external Runtime policy failed after repair: {manifest['distribution']}"
        )
    files = {item["path"]: item for item in inventory["files"]}
    for path in manifest["affected"]:
        imports = files.get(path, {}).get("imports", [])
        normal_msvcp = [
            item
            for item in imports
            if item["name"].casefold() == "msvcp140.dll"
            and item["type"] == "normal"
        ]
        if len(normal_msvcp) != 1:
            raise WheelError(
                f"normal MSVCP140.dll reference missing after relink: {path}"
            )


def transform_wheel(
    source: Path,
    output: Path,
    manifest: dict[str, Any],
    provenance: dict[str, Any],
    tool_wheel: Path,
) -> None:
    with tempfile.TemporaryDirectory(prefix="vc-wheel-") as temp:
        root = Path(temp)
        with zipfile.ZipFile(source) as archive:
            infos = _safe_members(archive)
            files = {info.filename: archive.read(info) for info in infos}
        for name, content in files.items():
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)

        before_inventory = _audit_tree(root, phase="before")
        _validate_before(before_inventory, manifest)

        affected = sorted(manifest["affected"])
        for relative in affected:
            target = root / relative
            _replace_needed(target, tool_wheel)
            files[relative] = target.read_bytes()

        loader_hashes: tuple[str, str] | None = None
        if manifest["component"] == "scikit-learn":
            target = root / LOADER
            loader_hashes = _patch_loader(target)
            files[LOADER] = target.read_bytes()

        removed = sorted(manifest["runtime_members"])
        for name in removed:
            del files[name]
            (root / name).unlink()

        after_inventory = _audit_tree(root, phase="after")
        _validate_after(after_inventory, manifest)
        _write_wheel(files, output, manifest["record"])
        provenance["wheels"].append(
            {
                "filename": source.name,
                "component": manifest["distribution"],
                "input_size": source.stat().st_size,
                "input_sha256": _sha(source),
                "affected_pe": affected,
                "removed_runtime_members": removed,
                "loader_sha256": loader_hashes,
                "pe_before": before_inventory,
                "pe_after": after_inventory,
                "output_sha256": _sha(output),
                "output_size": output.stat().st_size,
            }
        )


def run(input_dir: Path, output_dir: Path, lock: Path, tool_dir: Path) -> None:
    if sys.version_info[:3] != REQUIRED_PYTHON:
        required = ".".join(map(str, REQUIRED_PYTHON))
        actual = ".".join(map(str, sys.version_info[:3]))
        raise WheelError(f"requires Python {required}, got {actual}")
    if output_dir.exists():
        raise WheelError(f"output directory already exists: {output_dir}")
    wheels = validate_inputs(input_dir, lock)
    locked = _lock_entries(lock)
    tool_wheel, tool_evidence = _validate_tool_artifacts(tool_dir)
    delvewheel_version = _check_delvewheel(tool_wheel)
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(prefix=f".{output_dir.name}-", dir=output_dir.parent)
    )
    committed = False
    try:
        provenance: dict[str, Any] = {
            "schema": "external-vc-runtime-wheel-provenance/v1",
            "python": sys.version.split()[0],
            "script_sha256": _sha(Path(__file__)),
            "components_lock_sha256": _sha(lock),
            "tools": {
                "delvewheel_version": delvewheel_version,
                "artifacts": tool_evidence,
                "pefile_version": pe_runtime_audit.pefile.__version__,
            },
            "transformation": {
                "replace_needed": [HASHED_RUNTIME, "MSVCP140.dll"],
                "scikit_loader": [
                    LOADER_SHA256,
                    hashlib.sha256(LOADER_REPLACEMENT).hexdigest(),
                ],
            },
            "inputs": {
                name: {
                    "url": locked[name]["url"],
                    "size": locked[name]["size"],
                    "sha256": locked[name]["sha256"],
                    "source_archives": locked[name]["source_archives"],
                }
                for name in sorted(locked)
            },
            "wheels": [],
        }
        for name in sorted(wheels):
            transform_wheel(
                wheels[name],
                staging / name,
                EXPECTED[name],
                provenance,
                tool_wheel,
            )
        provenance["wheels"].sort(key=lambda item: item["filename"])
        (staging / PROVENANCE_NAME).write_text(
            json.dumps(provenance, indent=2, sort_keys=True, ensure_ascii=False)
            + "\n",
            encoding="utf-8",
            newline="\n",
        )
        expected_outputs = set(EXPECTED) | {PROVENANCE_NAME}
        actual_outputs = {path.name for path in staging.iterdir()}
        if actual_outputs != expected_outputs:
            raise WheelError(f"staged output set differs: {sorted(actual_outputs)}")
        staging.replace(output_dir)
        committed = True
    finally:
        if not committed:
            shutil.rmtree(staging, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input_dir", type=Path)
    parser.add_argument("output_dir", type=Path)
    parser.add_argument(
        "--components",
        type=Path,
        default=Path("compliance/components.json"),
    )
    parser.add_argument("--tool-artifacts", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        run(args.input_dir, args.output_dir, args.components, args.tool_artifacts)
    except (
        WheelError,
        OSError,
        subprocess.CalledProcessError,
        zipfile.BadZipFile,
        json.JSONDecodeError,
    ) as exc:
        print(f"external-vc-runtime-wheels: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
