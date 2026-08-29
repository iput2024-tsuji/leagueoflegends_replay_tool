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
import stat
import subprocess
import sys
import tempfile
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Any

from scripts.binary_install_policy import (
    BinaryInstallPolicyError,
    external_vc_runtime_policy,
)

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
        "output_size": 11_986_254,
        "output_sha256": (
            "9dc25f88035a08173708074f82f25478c0417040dfffc853521bc4e4f6346f03"
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
        "output_size": 9_500_776,
        "output_sha256": (
            "47b6bf18d390f68c187779f30d4a8cd7788d922bc414b5af994b04a552fa7ff2"
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
        "output_size": 75_519_530,
        "output_sha256": (
            "b1a5dfcd0d181285cf79fea6a8a507f4b5c171c095edf75049ff57c8b2e0dc40"
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
        "output_size": 7_682_065,
        "output_sha256": (
            "d619c4153217ee20e61b04bf64e1717efa982be29cae8e6a82cda1c565c4ad69"
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
        "url": (
            "https://files.pythonhosted.org/packages/13/f9/"
            "8163f9145012263743dee49333f3a39bf214b83dac0ede7b06bb9ff7287c/"
            "delvewheel-1.13.0-py3-none-any.whl"
        ),
        "size": 59_411,
        "sha256": (
            "eb8c34dee5d8816516befde73bf5cf9ea6142955f41d3baf25381c0136b28608"
        ),
    },
    "delvewheel-1.13.0.tar.gz": {
        "url": (
            "https://files.pythonhosted.org/packages/3c/ba/"
            "4ce769b09cdee86c576a35fc17bb954fa22f81037884a028c978a8479fe1/"
            "delvewheel-1.13.0.tar.gz"
        ),
        "size": 63_742,
        "sha256": (
            "440601f289c953d5b60e96af8a8dbe2729584d90b663e39c5eade6b9043b48f6"
        ),
    },
}
PEFILE_ARTIFACT = {
    "filename": "pefile-2024.8.26-py3-none-any.whl",
    "url": (
        "https://files.pythonhosted.org/packages/54/16/"
        "12b82f791c7f50ddec566873d5bdd245baa1491bac11d15ffb98aecc8f8b/"
        "pefile-2024.8.26-py3-none-any.whl"
    ),
    "size": 74_766,
    "sha256": "76f8b485dcd3b1bb8166f1128d395fa3d87af26360c2358fb75b80019b957c6f",
}
PEFILE_VERSION = "2024.8.26"
TOOL_ARTIFACTS = {
    **DELVEWHEEL_ARTIFACTS,
    PEFILE_ARTIFACT["filename"]: {
        key: PEFILE_ARTIFACT[key]
        for key in ("url", "size", "sha256")
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


def _path_is_link_or_reparse(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return False
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0) & reparse_flag
    )


def _require_directory(path: Path, label: str) -> None:
    if _path_is_link_or_reparse(path) or not path.is_dir():
        raise WheelError(f"{label} is unavailable or linked: {path}")


def _require_regular_file(path: Path, label: str) -> None:
    if _path_is_link_or_reparse(path) or not path.is_file():
        raise WheelError(f"{label} is unavailable or linked: {path}")


def _require_locked_file(
    path: Path,
    *,
    size: int,
    sha256: str,
    label: str,
) -> None:
    _require_regular_file(path, label)
    if path.stat().st_size != size or _sha(path) != sha256:
        raise WheelError(f"locked hash or size mismatch: {label}")


@contextmanager
def _temporary_import_paths(paths: list[Path]) -> Iterator[None]:
    original = list(sys.path)
    sys.path[:0] = [str(path.resolve()) for path in paths]
    try:
        yield
    finally:
        sys.path[:] = original


def _pe_runtime_audit_module() -> Any:
    from scripts import pe_runtime_audit

    return pe_runtime_audit


def _load_locked_pe_runtime_audit(pefile_wheel: Path) -> Any:
    _require_locked_file(
        pefile_wheel,
        size=int(PEFILE_ARTIFACT["size"]),
        sha256=str(PEFILE_ARTIFACT["sha256"]),
        label=f"pefile artifact {pefile_wheel.name}",
    )
    with _temporary_import_paths([pefile_wheel]):
        audit = _pe_runtime_audit_module()
    pefile_location = Path(audit.pefile.__file__).resolve()
    pefile_prefix = str(pefile_wheel.resolve()).rstrip("\\/") + os.sep
    if not str(pefile_location).casefold().startswith(pefile_prefix.casefold()):
        raise WheelError("pefile was not imported from the locked wheel")
    if audit.pefile.__version__ != PEFILE_VERSION:
        raise WheelError(
            f"pefile version must be {PEFILE_VERSION}, got "
            f"{audit.pefile.__version__}"
        )
    return audit


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
    try:
        policy = external_vc_runtime_policy(data)
    except BinaryInstallPolicyError as exc:
        raise WheelError(str(exc)) from exc
    if policy is None:
        raise WheelError("components lock has no external VC++ Runtime policy")
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
        output = policy["wheels"].get(str(value.get("component")))
        if output != {
            "filename": name,
            "size": manifest["output_size"],
            "sha256": manifest["output_sha256"],
        }:
            raise WheelError(f"locked external Runtime wheel differs for {name}")
        entries[name] = {
            **expected_archive,
            "source_archives": manifest["source_archives"],
        }
    missing = set(EXPECTED) - set(entries)
    if missing:
        raise WheelError(f"lock missing wheels: {sorted(missing)}")
    expected_components = {value["component"] for value in EXPECTED.values()}
    if set(policy["required_components"]) != expected_components:
        raise WheelError("external Runtime component set differs from recipe")
    expected_tools = {
        name: {"filename": name, **value}
        for name, value in TOOL_ARTIFACTS.items()
    }
    actual_tools = {
        value["filename"]: value for value in policy["tool_artifacts"]
    }
    if actual_tools != expected_tools:
        raise WheelError("external Runtime tool artifacts differ from recipe")
    return entries


def validate_inputs(input_dir: Path, lock: Path) -> dict[str, Path]:
    _require_directory(input_dir, "wheel input directory")
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
        _require_locked_file(
            path,
            size=expected["size"],
            sha256=expected["sha256"],
            label=name,
        )
    return wheels


def _validate_tool_artifacts(
    tool_dir: Path,
) -> tuple[Path, Path, dict[str, Any]]:
    _require_directory(tool_dir, "delvewheel artifact directory")
    evidence: dict[str, Any] = {}
    for name, expected in TOOL_ARTIFACTS.items():
        path = tool_dir / name
        _require_locked_file(
            path,
            size=expected["size"],
            sha256=expected["sha256"],
            label=f"delvewheel artifact {name}",
        )
        evidence[name] = {
            "size": expected["size"],
            "sha256": expected["sha256"],
        }
    return (
        tool_dir / "delvewheel-1.13.0-py3-none-any.whl",
        tool_dir / str(PEFILE_ARTIFACT["filename"]),
        evidence,
    )


def _tool_environment(tool_wheel: Path, pefile_wheel: Path) -> dict[str, str]:
    environment = os.environ.copy()
    existing = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = os.pathsep.join(
        (str(tool_wheel.resolve()), str(pefile_wheel.resolve()))
    ) + (
        os.pathsep + existing if existing else ""
    )
    return environment


def _check_delvewheel(tool_wheel: Path, pefile_wheel: Path) -> str:
    expected = DELVEWHEEL_ARTIFACTS.get(tool_wheel.name)
    if expected is None:
        raise WheelError(f"unexpected delvewheel artifact: {tool_wheel.name}")
    _require_locked_file(
        tool_wheel,
        size=expected["size"],
        sha256=expected["sha256"],
        label=f"delvewheel artifact {tool_wheel.name}",
    )
    _require_locked_file(
        pefile_wheel,
        size=int(PEFILE_ARTIFACT["size"]),
        sha256=str(PEFILE_ARTIFACT["sha256"]),
        label=f"pefile artifact {pefile_wheel.name}",
    )
    environment = _tool_environment(tool_wheel, pefile_wheel)
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


def _replace_needed(path: Path, tool_wheel: Path, pefile_wheel: Path) -> None:
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
        env=_tool_environment(tool_wheel, pefile_wheel),
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


def _audit_tree(root: Path, *, phase: str, audit: Any) -> dict[str, Any]:
    try:
        return audit.build_inventory(root)
    except (audit.AuditError, OSError) as exc:
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
    pefile_wheel: Path,
    audit: Any,
) -> None:
    _require_locked_file(
        source,
        size=manifest["size"],
        sha256=manifest["sha256"],
        label=f"wheel input {source.name}",
    )
    expected_tool = DELVEWHEEL_ARTIFACTS.get(tool_wheel.name)
    if expected_tool is None:
        raise WheelError(f"unexpected delvewheel artifact: {tool_wheel.name}")
    _require_locked_file(
        tool_wheel,
        size=expected_tool["size"],
        sha256=expected_tool["sha256"],
        label=f"delvewheel artifact {tool_wheel.name}",
    )
    _require_locked_file(
        pefile_wheel,
        size=int(PEFILE_ARTIFACT["size"]),
        sha256=str(PEFILE_ARTIFACT["sha256"]),
        label=f"pefile artifact {pefile_wheel.name}",
    )
    with tempfile.TemporaryDirectory(prefix="vc-wheel-") as temp:
        root = Path(temp)
        with zipfile.ZipFile(source) as archive:
            infos = _safe_members(archive)
            files = {info.filename: archive.read(info) for info in infos}
        for name, content in files.items():
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)

        before_inventory = _audit_tree(root, phase="before", audit=audit)
        _validate_before(before_inventory, manifest)

        affected = sorted(manifest["affected"])
        for relative in affected:
            target = root / relative
            _replace_needed(target, tool_wheel, pefile_wheel)
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

        after_inventory = _audit_tree(root, phase="after", audit=audit)
        _validate_after(after_inventory, manifest)
        _write_wheel(files, output, manifest["record"])
        output_size = output.stat().st_size
        output_sha256 = _sha(output)
        if (
            output_size != manifest["output_size"]
            or output_sha256 != manifest["output_sha256"]
        ):
            raise WheelError(
                f"deterministic output differs for {manifest['distribution']}: "
                f"size={output_size} sha256={output_sha256}"
            )
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
                "output_sha256": output_sha256,
                "output_size": output_size,
            }
        )


def run(input_dir: Path, output_dir: Path, lock: Path, tool_dir: Path) -> None:
    if sys.version_info[:3] != REQUIRED_PYTHON:
        required = ".".join(map(str, REQUIRED_PYTHON))
        actual = ".".join(map(str, sys.version_info[:3]))
        raise WheelError(f"requires Python {required}, got {actual}")
    _require_regular_file(lock, "component lock")
    if os.path.lexists(output_dir):
        raise WheelError(f"output directory already exists: {output_dir}")
    wheels = validate_inputs(input_dir, lock)
    locked = _lock_entries(lock)
    tool_wheel, pefile_wheel, tool_evidence = _validate_tool_artifacts(tool_dir)
    audit = _load_locked_pe_runtime_audit(pefile_wheel)
    delvewheel_version = _check_delvewheel(tool_wheel, pefile_wheel)
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    _require_directory(output_dir.parent, "output parent directory")
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
                "pefile_version": audit.pefile.__version__,
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
                pefile_wheel,
                audit,
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
        validate_output_directory(staging, lock)
        staging.replace(output_dir)
        committed = True
    finally:
        if not committed:
            shutil.rmtree(staging, ignore_errors=True)


def validate_provenance_payload(
    provenance: dict[str, Any],
    lock: Path,
) -> dict[str, Any]:
    """Validate a transformation record without trusting its enclosing file."""

    entries = _lock_entries(lock)
    if not isinstance(provenance, dict) or set(provenance) != {
        "schema",
        "python",
        "script_sha256",
        "components_lock_sha256",
        "tools",
        "transformation",
        "inputs",
        "wheels",
    }:
        raise WheelError("transformation provenance fields differ")
    if (
        provenance.get("schema") != "external-vc-runtime-wheel-provenance/v1"
        or provenance.get("python") != sys.version.split()[0]
        or provenance.get("script_sha256") != _sha(Path(__file__))
        or provenance.get("components_lock_sha256") != _sha(lock)
    ):
        raise WheelError("transformation provenance identity differs")
    expected_tool_evidence = {
        name: {"size": value["size"], "sha256": value["sha256"]}
        for name, value in TOOL_ARTIFACTS.items()
    }
    if provenance.get("tools") != {
        "delvewheel_version": "1.13.0",
        "artifacts": expected_tool_evidence,
        "pefile_version": PEFILE_VERSION,
    }:
        raise WheelError("transformation tool provenance differs")
    expected_transformation = {
        "replace_needed": [HASHED_RUNTIME, "MSVCP140.dll"],
        "scikit_loader": [
            LOADER_SHA256,
            hashlib.sha256(LOADER_REPLACEMENT).hexdigest(),
        ],
    }
    if provenance.get("transformation") != expected_transformation:
        raise WheelError("transformation recipe provenance differs")
    expected_inputs = {
        name: {
            "url": entries[name]["url"],
            "size": entries[name]["size"],
            "sha256": entries[name]["sha256"],
            "source_archives": entries[name]["source_archives"],
        }
        for name in sorted(entries)
    }
    if provenance.get("inputs") != expected_inputs:
        raise WheelError("transformation input provenance differs")
    raw_wheels = provenance.get("wheels")
    if not isinstance(raw_wheels, list) or len(raw_wheels) != len(EXPECTED):
        raise WheelError("transformation wheel records differ")
    records = {
        record.get("filename"): record
        for record in raw_wheels
        if isinstance(record, dict)
    }
    if set(records) != set(EXPECTED) or len(records) != len(raw_wheels):
        raise WheelError("transformation wheel record set differs")
    record_keys = {
        "filename",
        "component",
        "input_size",
        "input_sha256",
        "affected_pe",
        "removed_runtime_members",
        "loader_sha256",
        "pe_before",
        "pe_after",
        "output_sha256",
        "output_size",
    }
    for name, manifest in EXPECTED.items():
        record = records[name]
        if set(record) != record_keys:
            raise WheelError(f"transformation wheel fields differ: {name}")
        expected_loader = (
            [
                LOADER_SHA256,
                hashlib.sha256(LOADER_REPLACEMENT).hexdigest(),
            ]
            if manifest["component"] == "scikit-learn"
            else None
        )
        if (
            record.get("component") != manifest["distribution"]
            or record.get("input_size") != manifest["size"]
            or record.get("input_sha256") != manifest["sha256"]
            or record.get("affected_pe") != sorted(manifest["affected"])
            or record.get("removed_runtime_members")
            != sorted(manifest["runtime_members"])
            or record.get("loader_sha256") != expected_loader
            or record.get("output_size") != manifest["output_size"]
            or record.get("output_sha256") != manifest["output_sha256"]
            or not isinstance(record.get("pe_before"), dict)
            or not isinstance(record.get("pe_after"), dict)
        ):
            raise WheelError(f"transformation wheel provenance differs: {name}")
        _validate_before(record["pe_before"], manifest)
        _validate_after(record["pe_after"], manifest)
    return provenance


def validate_output_directory(output_dir: Path, lock: Path) -> dict[str, Any]:
    """Revalidate fixed output hashes and the complete transformation record."""

    _require_regular_file(lock, "component lock")
    _require_directory(output_dir, "output directory")
    expected_outputs = set(EXPECTED) | {PROVENANCE_NAME}
    candidates = list(output_dir.iterdir())
    actual_outputs = {path.name for path in candidates if path.is_file()}
    if actual_outputs != expected_outputs or any(
        _path_is_link_or_reparse(path) or not path.is_file()
        for path in candidates
    ):
        raise WheelError(f"output set differs: {sorted(actual_outputs)}")
    provenance_path = output_dir / PROVENANCE_NAME
    try:
        provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WheelError(f"cannot read transformation provenance: {exc}") from exc
    validate_provenance_payload(provenance, lock)
    for name, manifest in EXPECTED.items():
        output = output_dir / name
        try:
            _require_locked_file(
                output,
                size=manifest["output_size"],
                sha256=manifest["output_sha256"],
                label=f"transformation wheel {name}",
            )
        except WheelError as exc:
            raise WheelError(f"transformation wheel bytes differ: {name}") from exc
    return provenance


def validate_embedded_provenance_record(
    record: Any,
    lock: Path,
) -> dict[str, Any]:
    """Validate the self-contained transformation evidence in build provenance."""

    if not isinstance(record, dict) or set(record) != {
        "provenance_sha256",
        "provenance",
    }:
        raise WheelError("embedded transformation provenance fields differ")
    provenance = record.get("provenance")
    serialized = (
        json.dumps(
            provenance,
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
        )
        + "\n"
    ).encode("utf-8")
    if record.get("provenance_sha256") != hashlib.sha256(serialized).hexdigest():
        raise WheelError("embedded transformation provenance SHA256 differs")
    return validate_provenance_payload(provenance, lock)


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
        BinaryInstallPolicyError,
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
