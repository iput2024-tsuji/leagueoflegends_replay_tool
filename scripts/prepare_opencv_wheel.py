"""Build and verify the pinned IPP-free OpenCV wheel.

The caller is responsible for placing the hash-verified source archives and
build-tool wheels in ``source_dir``.  This module deliberately does not fetch
anything: a release build must make its network boundary explicit.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

PROVENANCE_NAME = "opencv-wheel-provenance.json"
POLICY_KEY = "opencv_source_build_policy"
REQUIRED_PYTHON = "3.14.6"
REQUIRED_GENERATOR = "Visual Studio 17 2022"
REQUIRED_TOOLSET_NAME = "v143"
REQUIRED_TOOLSET = "v143"
REQUIRED_TOOLSET_VERSION = "14.44.35207"
REQUIRED_WINDOWS_SDK = "10.0.26100.0"
REQUIRED_CMAKE = "3.31.6"
COMPILER_FLAG_ENVIRONMENT = (
    "CL", "_CL_", "LINK", "_LINK_", "CFLAGS", "CXXFLAGS", "CPPFLAGS", "LDFLAGS",
    "SKBUILD_BUILD_OPTIONS", "CMAKE_TOOLCHAIN_FILE", "OPENCV_CMAKE_HOOKS_DIR",
)
REQUIRED_CMAKE_ARGS = (
    "-DWITH_IPP=OFF",
    "-DBUILD_IPP_IW=OFF",
    "-DBUILD_opencv_gapi=OFF",
    "-DWITH_ADE=OFF",
    "-DPYTHON3_LIMITED_API=ON",
    "-DCMAKE_SYSTEM_VERSION=10.0.26100.0",
    "-DBUILD_WITH_STATIC_CRT=OFF",
)
VERSION_PY_BYTES = (
    b'opencv_version = "4.13.0.90"\n'
    b"contrib = False\n"
    b"headless = False\n"
    b"rolling = False\n"
    b"ci_build = True\n"
)
BUILD_COMMAND = (
    "<build-python>",
    "setup.py",
    "bdist_wheel",
    "--py-limited-api=cp37",
    "--dist-dir",
    "<output-dir>",
)
EXPECTED_FFMPEG = {
    "opencv_videoio_ffmpeg.dll": "47730de2286110b0d1250ff9cf50ce56",
    "opencv_videoio_ffmpeg_64.dll": "3248b4663ffef770cdb54ec8b9d16a28",
    "ffmpeg_version.cmake": "8862c87496e2e8c375965e1277dee1c7",
}
DOWNLOAD_CACHE_GITIGNORE = b"*\n"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_WHEEL = re.compile(
    r"^opencv_python-(?P<version>[A-Za-z0-9_.+!-]+?)-(?P<tags>[^/]+)\.whl$",
    re.IGNORECASE,
)


class OpenCVWheelError(ValueError):
    """An invalid input, build result, or provenance record."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _regular(path: Path, label: str) -> None:
    if path.is_symlink() or not path.is_file():
        raise OpenCVWheelError(f"{label} is not a regular file: {path}")


def _directory(path: Path, label: str) -> None:
    if path.is_symlink() or not path.is_dir():
        raise OpenCVWheelError(f"{label} is not a regular directory: {path}")


def _policy(lock: dict[str, Any]) -> dict[str, Any]:
    raw = lock.get(POLICY_KEY)
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise OpenCVWheelError(f"{POLICY_KEY} schema_version must be 1")
    required = {
        "schema_version", "component", "recipe", "python_version", "platform",
        "output_filename", "expected_byte_identical", "expected_wheel_sha256",
        "expected_semantic_manifest_sha256", "source_artifacts",
        "build_artifacts", "build_environment",
    }
    if set(raw) != required:
        raise OpenCVWheelError(f"{POLICY_KEY} fields differ from the required schema")
    if (
        raw["component"] != "opencv-python"
        or raw["python_version"] != REQUIRED_PYTHON
        or raw["platform"] != "win_amd64"
        or raw["recipe"] != "scripts/prepare_opencv_wheel.py"
    ):
        raise OpenCVWheelError("OpenCV policy identity or recipe is invalid")
    output_filename = raw["output_filename"]
    if not isinstance(output_filename, str) or Path(output_filename).name != output_filename:
        raise OpenCVWheelError("OpenCV output filename is invalid")
    for key in ("expected_wheel_sha256", "expected_semantic_manifest_sha256"):
        digest = raw[key]
        if digest is not None and (
            not isinstance(digest, str) or _SHA256.fullmatch(digest) is None
        ):
            raise OpenCVWheelError(f"OpenCV {key} is invalid")
    if raw["expected_byte_identical"] is not None and not isinstance(
        raw["expected_byte_identical"], bool
    ):
        raise OpenCVWheelError("OpenCV expected_byte_identical is invalid")
    if (
        raw["expected_byte_identical"] is False
        and raw["expected_wheel_sha256"] is not None
    ):
        raise OpenCVWheelError(
            "A non-byte-identical OpenCV build cannot fix one wheel SHA256"
        )
    if (
        raw["expected_byte_identical"] is True
        and raw["expected_wheel_sha256"] is None
    ):
        raise OpenCVWheelError(
            "A byte-identical OpenCV build must fix its wheel SHA256"
        )
    environment = raw["build_environment"]
    if not isinstance(environment, dict) or set(environment) != {
        "generator",
        "msvc_toolset",
        "expected_msvc_toolset_version",
        "windows_sdk",
        "cmake_version",
        "cmake_build_parallel_level",
        "python_hash_seed",
        "build_packages",
        "cmake_args",
    }:
        raise OpenCVWheelError("OpenCV build environment fields are invalid")
    if environment["cmake_args"] != list(REQUIRED_CMAKE_ARGS):
        raise OpenCVWheelError(
            "OpenCV CMake flags must disable IPP, G-API, and ADE and enable "
            "the CPython Limited API"
        )
    if {
        "generator": environment["generator"],
        "msvc_toolset": environment["msvc_toolset"],
        "expected_msvc_toolset_version": environment["expected_msvc_toolset_version"],
        "windows_sdk": environment["windows_sdk"],
        "cmake_version": environment["cmake_version"],
        "cmake_build_parallel_level": environment[
            "cmake_build_parallel_level"
        ],
        "python_hash_seed": environment["python_hash_seed"],
    } != {
        "generator": REQUIRED_GENERATOR,
        "msvc_toolset": REQUIRED_TOOLSET,
        "expected_msvc_toolset_version": REQUIRED_TOOLSET_VERSION,
        "windows_sdk": REQUIRED_WINDOWS_SDK,
        "cmake_version": REQUIRED_CMAKE,
        "cmake_build_parallel_level": "2",
        "python_hash_seed": "0",
    }:
        raise OpenCVWheelError("OpenCV toolchain differs from the fixed policy")
    build_packages = environment["build_packages"]
    if (
        not isinstance(build_packages, dict)
        or not build_packages
        or not all(
            isinstance(name, str)
            and name
            and isinstance(version, str)
            and version
            for name, version in build_packages.items()
        )
    ):
        raise OpenCVWheelError("OpenCV build package versions are invalid")
    for key in ("source_artifacts", "build_artifacts"):
        entries = raw[key]
        if not isinstance(entries, list) or not entries:
            raise OpenCVWheelError(f"OpenCV policy {key} must be non-empty")
        seen: set[str] = set()
        for item in entries:
            expected_fields = (
                {"filename", "url", "size", "sha256", "role"}
                if key == "source_artifacts"
                else {"filename", "url", "size", "sha256"}
            )
            if not isinstance(item, dict) or set(item) != expected_fields:
                raise OpenCVWheelError(f"Invalid OpenCV policy {key} entry")
            filename = item["filename"]
            if (
                not isinstance(filename, str)
                or not filename
                or Path(filename).name != filename
                or filename.casefold() in seen
            ):
                raise OpenCVWheelError(f"Unsafe or duplicate OpenCV input filename: {filename}")
            seen.add(filename.casefold())
            if not isinstance(item["size"], int) or item["size"] <= 0:
                raise OpenCVWheelError(f"Invalid OpenCV input size: {filename}")
            if not isinstance(item["sha256"], str) or not _SHA256.fullmatch(item["sha256"]):
                raise OpenCVWheelError(f"Invalid OpenCV input SHA256: {filename}")
            if not isinstance(item["url"], str) or not item["url"].startswith("https://"):
                raise OpenCVWheelError(f"Invalid OpenCV input URL: {filename}")
            if key == "source_artifacts" and (not isinstance(item["role"], str) or not item["role"]):
                raise OpenCVWheelError(f"Invalid OpenCV input role: {filename}")
    roles = {str(item["role"]) for item in raw["source_artifacts"]}
    if roles != {"opencv-python", "opencv", "opencv-3rdparty"}:
        raise OpenCVWheelError(
            "source_archives must contain exactly opencv-python, opencv, "
            "and opencv-3rdparty roles"
        )
    return raw


def source_build_policy(lock: dict[str, Any]) -> dict[str, Any] | None:
    """Return the validated OpenCV source-build policy, if configured."""
    if POLICY_KEY not in lock:
        return None
    return _policy(lock)


def _load_lock(path: Path) -> dict[str, Any]:
    try:
        lock = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise OpenCVWheelError(f"Cannot read component lock: {exc}") from exc
    if not isinstance(lock, dict):
        raise OpenCVWheelError("Component lock must be an object")
    _policy(lock)
    return lock


def _verify_inputs(source_dir: Path, policy: dict[str, Any]) -> dict[str, Path]:
    _directory(source_dir, "OpenCV source directory")
    all_entries = [*policy["source_artifacts"], *policy["build_artifacts"]]
    result: dict[str, Path] = {}
    for item in all_entries:
        filename = str(item["filename"])
        if filename.casefold() in result:
            raise OpenCVWheelError(f"Duplicate OpenCV input: {filename}")
        path = source_dir / filename
        _regular(path, "OpenCV input")
        if path.stat().st_size != item["size"] or _sha256(path) != item["sha256"]:
            raise OpenCVWheelError(f"OpenCV input hash or size mismatch: {filename}")
        result[filename.casefold()] = path
    return result


def _validate_build_environment(
    policy: dict[str, Any], python: Path
) -> dict[str, Any]:
    if os.name != "nt" or platform.machine().casefold() not in {"amd64", "x86_64"}:
        raise OpenCVWheelError("OpenCV wheel build requires Windows amd64")
    if platform.python_version() != REQUIRED_PYTHON:
        raise OpenCVWheelError(
            f"OpenCV wheel build requires Python {REQUIRED_PYTHON}"
        )
    expected = policy["build_environment"]
    cmake = python.parent / "cmake.exe"
    _regular(cmake, "Locked OpenCV CMake executable")
    try:
        completed = subprocess.run(
            [str(cmake), "--version"],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise OpenCVWheelError(f"CMake is unavailable: {exc}") from exc
    if completed.returncode != 0:
        raise OpenCVWheelError("CMake version probe failed")
    match = re.search(r"cmake version ([0-9][^\r\n ]*)", completed.stdout)
    if match is None or match.group(1) != expected["cmake_version"]:
        raise OpenCVWheelError("CMake version differs from the OpenCV policy")
    package_names = sorted(expected["build_packages"], key=str.casefold)
    probe = (
        "import importlib.metadata as m,json,sys;"
        "print(json.dumps({name:m.version(name) for name in sys.argv[1:]},"
        "sort_keys=True))"
    )
    completed = subprocess.run(
        [str(python), "-c", probe, *package_names],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise OpenCVWheelError("OpenCV build package version probe failed")
    try:
        packages = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise OpenCVWheelError("OpenCV build package version probe is invalid") from exc
    if packages != expected["build_packages"]:
        raise OpenCVWheelError("OpenCV build package versions differ from policy")
    return {
        "cmake_version": match.group(1),
        "build_packages": packages,
        "python_version": platform.python_version(),
    }


def _prepare_build_venv(work_dir: Path, artifacts: list[Path]) -> Path:
    venv_dir = work_dir / "v"
    result = subprocess.run(
        [sys.executable, "-m", "venv", str(venv_dir)],
        check=False, capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise OpenCVWheelError("Could not create OpenCV build venv")
    python = venv_dir / "Scripts" / "python.exe"
    _regular(python, "OpenCV build Python")
    result = subprocess.run(
        [
            str(python),
            "-m",
            "pip",
            "--isolated",
            "--disable-pip-version-check",
            "install",
            "--no-input",
            "--require-virtualenv",
            "--no-cache-dir",
            "--no-index",
            "--no-deps",
            "--only-binary=:all:",
            "--force-reinstall",
            *(str(path) for path in artifacts),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise OpenCVWheelError(
            "Could not install OpenCV build dependencies: "
            + (result.stderr or result.stdout or f"exit {result.returncode}")
        )
    return python


def _probe_wheel(
    python: Path, wheel: Path, diagnostics_file: Path
) -> dict[str, Any]:
    install = subprocess.run(
        [
            str(python),
            "-m",
            "pip",
            "--isolated",
            "--disable-pip-version-check",
            "install",
            "--no-input",
            "--require-virtualenv",
            "--no-cache-dir",
            "--no-index",
            "--no-deps",
            "--only-binary=:all:",
            "--force-reinstall",
            str(wheel),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if install.returncode != 0:
        raise OpenCVWheelError(
            "Could not install generated OpenCV wheel for probe: "
            + (install.stderr or install.stdout or f"exit {install.returncode}")
        )
    code = """
import hashlib
import json
import sys
import tempfile
from pathlib import Path

import cv2
import numpy as np

info = cv2.getBuildInformation()
Path(sys.argv[1]).write_text(info, encoding="utf-8")
ipp_lines = [
    line.strip()
    for line in info.splitlines()
    if line.strip().casefold().startswith("intel ipp")
]
# The pinned OpenCV source emits these lines only inside WITH_IPP && HAVE_IPP.
# WITH_IPP=OFF therefore has one exact expected representation: no IPP lines.
if ipp_lines:
    raise SystemExit("Intel IPP build information is unexpectedly present")
ffmpeg_lines = [
    " ".join(line.split())
    for line in info.splitlines()
    if line.strip().casefold().startswith("ffmpeg:")
]
if [line.casefold() for line in ffmpeg_lines] != [
    "ffmpeg: yes (prebuilt binaries)"
]:
    raise SystemExit("FFmpeg build information differs from the fixed Windows build")
with tempfile.TemporaryDirectory(prefix="opencv-wheel-probe-") as temporary:
    path = Path(temporary) / "probe.avi"
    fourcc = cv2.VideoWriter_fourcc(*"MJPG")
    writer = cv2.VideoWriter(str(path), fourcc, 10.0, (32, 32))
    if not writer.isOpened():
        raise SystemExit("VideoWriter did not open")
    writer_backend = writer.getBackendName()
    frame = np.zeros((32, 32, 3), dtype=np.uint8)
    frame[:, :] = (0, 0, 255)
    writer.write(frame)
    writer.release()
    reader = cv2.VideoCapture(str(path))
    if not reader.isOpened():
        raise SystemExit("VideoCapture did not open")
    reader_backend = reader.getBackendName()
    ok, frame = reader.read()
    reader.release()
if not ok or frame is None:
    raise SystemExit("Synthetic video frame could not be read")
if writer_backend != "FFMPEG" or reader_backend != "FFMPEG":
    raise SystemExit("Synthetic video did not use the FFmpeg backend")
hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
mask = cv2.inRange(hsv, (0, 100, 100), (10, 255, 255))
if cv2.countNonZero(mask) == 0:
    raise SystemExit("OpenCV marker primitives failed")
print(json.dumps({
    "api": "ok",
    "build_information_sha256": hashlib.sha256(info.encode("utf-8")).hexdigest(),
    "ffmpeg": "enabled",
    "ffmpeg_build_information_lines": ffmpeg_lines,
    "ipp": "disabled",
    "ipp_build_information_lines": ipp_lines,
    "opencv_version": cv2.__version__,
    "video_reader_backend": reader_backend,
    "video_writer_backend": writer_backend,
}, sort_keys=True))
"""
    result = subprocess.run(
        [str(python), "-c", code, str(diagnostics_file)],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise OpenCVWheelError("OpenCV native probe failed: " + (result.stderr or result.stdout))
    try:
        return json.loads(result.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError) as exc:
        raise OpenCVWheelError("OpenCV native probe output is invalid") from exc


def _preseed_ffmpeg(thirdparty_root: Path, destination: Path) -> list[dict[str, Any]]:
    destination.mkdir(parents=True, exist_ok=False)
    (destination.parent / ".gitignore").write_bytes(DOWNLOAD_CACHE_GITIGNORE)
    records: list[dict[str, Any]] = []
    candidates = list(thirdparty_root.rglob("*"))
    for filename, digest in EXPECTED_FFMPEG.items():
        matches = [
            path
            for path in candidates
            if path.is_file() and path.name == filename
        ]
        if len(matches) != 1:
            raise OpenCVWheelError(
                f"Expected one FFmpeg preseed file for {filename}, found "
                f"{len(matches)}"
            )
        source = matches[0]
        if hashlib.md5(source.read_bytes()).hexdigest() != digest:
            raise OpenCVWheelError(f"FFmpeg preseed MD5 differs: {filename}")
        target = destination / f"{digest}-{filename}"
        shutil.copy2(source, target)
        records.append(
            {
                "filename": filename,
                "md5": digest,
                "cache_path": f"ffmpeg/{target.name}",
                "size": source.stat().st_size,
                "sha256": _sha256(source),
            }
        )
    return records


def _verify_download_cache(
    download_path: Path,
    ffmpeg_records: list[dict[str, Any]],
) -> None:
    expected = {
        str(item["cache_path"]): (int(item["size"]), str(item["sha256"]))
        for item in ffmpeg_records
    }
    expected[".gitignore"] = (
        len(DOWNLOAD_CACHE_GITIGNORE),
        hashlib.sha256(DOWNLOAD_CACHE_GITIGNORE).hexdigest(),
    )
    observed: set[str] = set()
    for path in download_path.rglob("*"):
        if path.is_dir():
            continue
        _regular(path, "OpenCV download cache entry")
        relative = path.relative_to(download_path).as_posix()
        if relative not in expected:
            raise OpenCVWheelError(
                f"Unexpected OpenCV build download: {relative}"
            )
        size, digest = expected[relative]
        if path.stat().st_size != size or _sha256(path) != digest:
            raise OpenCVWheelError(
                f"OpenCV build download cache differs: {relative}"
            )
        observed.add(relative)
    if observed != set(expected):
        raise OpenCVWheelError("OpenCV build download cache is incomplete")


def _safe_member(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if (
        not name
        or path.is_absolute()
        or "\\" in name
        or ":" in name
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise OpenCVWheelError(f"Unsafe source archive member: {name!r}")
    return path


def _extract_archive(archive: Path, destination: Path) -> Path:
    destination.mkdir(parents=True, exist_ok=True)
    _directory(destination, "OpenCV extraction directory")
    try:
        with tarfile.open(archive, "r:*") as stream:
            members = stream.getmembers()
            if not members:
                raise OpenCVWheelError(f"Empty OpenCV source archive: {archive.name}")
            top_levels: set[str] = set()
            seen_members: set[str] = set()
            for member in members:
                path = _safe_member(member.name)
                folded = path.as_posix().casefold()
                if folded in seen_members:
                    raise OpenCVWheelError(
                        f"Duplicate source archive member on Windows: {member.name}"
                    )
                seen_members.add(folded)
                if member.issym() or member.islnk() or not (member.isdir() or member.isfile()):
                    raise OpenCVWheelError(f"Unsupported source archive member: {member.name}")
                top_levels.add(path.parts[0])
                target = destination.joinpath(*path.parts)
                resolved = target.resolve()
                if destination.resolve() not in resolved.parents:
                    raise OpenCVWheelError(f"Source archive escapes extraction root: {member.name}")
            stream.extractall(destination, members=members)
    except (tarfile.TarError, OSError) as exc:
        raise OpenCVWheelError(f"Cannot extract OpenCV source archive: {archive.name}: {exc}") from exc
    if len(top_levels) != 1:
        raise OpenCVWheelError(f"OpenCV source archive must have one root: {archive.name}")
    root = destination / next(iter(top_levels))
    _directory(root, "OpenCV source root")
    return root


def _compose_source_tree(
    source_dir: Path, policy: dict[str, Any], work_dir: Path
) -> tuple[Path, Path]:
    roots: dict[str, Path] = {}
    short_names = {
        "opencv-python": "p",
        "opencv": "o",
        "opencv-3rdparty": "t",
    }
    for item in policy["source_artifacts"]:
        archive = source_dir / str(item["filename"])
        role = str(item["role"])
        staging = work_dir / "x" / short_names[role]
        root = _extract_archive(archive, staging)
        active_root = work_dir / short_names[role]
        root.replace(active_root)
        staging.rmdir()
        roots[role] = active_root
    (work_dir / "x").rmdir()
    python_root = roots["opencv-python"]
    opencv_root = roots["opencv"]
    target = python_root / "opencv"
    if target.is_symlink() or (target.exists() and not target.is_dir()):
        raise OpenCVWheelError(
            "opencv-python source contains an invalid opencv submodule path"
        )
    if target.exists() and any(target.iterdir()):
        raise OpenCVWheelError(
            "opencv-python source contains a non-empty unexpected opencv tree"
        )
    if target.exists():
        target.rmdir()
    opencv_root.replace(target)
    version_file = python_root / "cv2" / "version.py"
    version_file.parent.mkdir(parents=True, exist_ok=True)
    if version_file.exists() or version_file.is_symlink():
        raise OpenCVWheelError("opencv-python source has an unexpected version.py")
    version_file.write_bytes(VERSION_PY_BYTES)
    return python_root, roots["opencv-3rdparty"]


def _read_cmake_cache(source_tree: Path) -> dict[str, str]:
    candidates = sorted(
        source_tree.glob("_skbuild/*/cmake-build/CMakeCache.txt"),
        key=lambda path: path.as_posix().casefold(),
    )
    if len(candidates) != 1:
        raise OpenCVWheelError(
            f"Expected one OpenCV CMakeCache.txt, found {len(candidates)}"
        )
    _regular(candidates[0], "OpenCV CMake cache")
    result: dict[str, str] = {}
    for line in candidates[0].read_text(
        encoding="utf-8", errors="strict"
    ).splitlines():
        if not line or line.startswith(("#", "//")) or "=" not in line:
            continue
        typed_key, value = line.split("=", 1)
        # CMake quotes cache variable names that themselves contain a colon,
        # for example "HAVE_CXX_ARCH:AVX2":INTERNAL.  The cache type is the
        # final colon-separated field, not the first one.
        key = typed_key.rsplit(":", 1)[0]
        if key in result:
            raise OpenCVWheelError(f"Duplicate OpenCV CMake cache key: {key}")
        result[key] = value
    return result


def _capture_msbuild_project(source_tree: Path) -> dict[str, Any]:
    candidates = sorted(
        source_tree.glob("_skbuild/*/cmake-build/ALL_BUILD.vcxproj"),
        key=lambda path: path.as_posix().casefold(),
    )
    if len(candidates) != 1:
        raise OpenCVWheelError(
            f"Expected one OpenCV ALL_BUILD.vcxproj, found {len(candidates)}"
        )
    project = candidates[0]
    _regular(project, "OpenCV ALL_BUILD project")
    try:
        root = ET.parse(project).getroot()
    except (ET.ParseError, OSError) as exc:
        raise OpenCVWheelError(f"Cannot inspect OpenCV ALL_BUILD project: {exc}") from exc

    def values(name: str) -> list[str]:
        return sorted(
            {
                element.text.strip()
                for element in root.iter()
                if element.tag.rsplit("}", 1)[-1] == name
                and element.text
                and element.text.strip()
            }
        )

    observed = {
        "platform_toolsets": values("PlatformToolset"),
        "windows_target_platform_versions": values(
            "WindowsTargetPlatformVersion"
        ),
    }
    if observed != {
        "platform_toolsets": [REQUIRED_TOOLSET_NAME],
        "windows_target_platform_versions": [REQUIRED_WINDOWS_SDK],
    }:
        raise OpenCVWheelError(
            "OpenCV MSBuild project toolchain differs: "
            + json.dumps(observed, sort_keys=True)
        )
    return {
        "path": project.relative_to(source_tree).as_posix(),
        "size": project.stat().st_size,
        "sha256": _sha256(project),
        **observed,
    }


def _capture_compiler(
    cache: dict[str, str], source_tree: Path | None = None, *, language: str = "CXX"
) -> dict[str, Any]:
    variable = f"CMAKE_{language}_COMPILER"
    raw = cache.get(variable)
    if not raw and source_tree is not None:
        candidates = sorted(source_tree.glob(f"_skbuild/*/cmake-build/CMakeFiles/*/CMake{language}Compiler.cmake"))
        if len(candidates) != 1:
            raise OpenCVWheelError(
                f"Expected one OpenCV CMake {language} compiler file, found "
                f"{len(candidates)}"
            )
        _regular(candidates[0], f"OpenCV CMake {language} compiler file")
        matches = re.findall(
            rf'^set\({variable}\s+"([^"]+)"\)',
            candidates[0].read_text(encoding="utf-8", errors="strict"),
            re.MULTILINE,
        )
        if len(matches) != 1:
            raise OpenCVWheelError(f"OpenCV {variable} must be declared exactly once")
        raw = matches[0]
    if not raw:
        raise OpenCVWheelError(f"OpenCV generated files have no {language} compiler")
    compiler = Path(raw)
    _regular(compiler, f"OpenCV {language} compiler")
    parts = compiler.parts
    try:
        index = next(
            position
            for position, part in enumerate(parts)
            if part.casefold() == "msvc"
        )
        version = parts[index + 1]
    except (StopIteration, IndexError) as exc:
        raise OpenCVWheelError("OpenCV compiler is not from an MSVC toolset") from exc
    if version != REQUIRED_TOOLSET_VERSION:
        raise OpenCVWheelError(
            f"OpenCV selected MSVC toolset differs: {version}"
        )
    return {
        "filename": compiler.name,
        "msvc_toolset_version": version,
        "sha256": _sha256(compiler),
        "size": compiler.stat().st_size,
    }


def _capture_dynamic_crt_projects(source_tree: Path) -> list[dict[str, Any]]:
    """Check the effective Release CRT setting in every generated C/C++ target."""
    records = []
    for project in sorted(source_tree.glob("_skbuild/*/cmake-build/**/*.vcxproj")):
        relative = project.relative_to(source_tree).as_posix()
        if "CMakeFiles" in project.relative_to(source_tree).parts:
            continue  # Configure-time compiler probes are not wheel build targets.
        _regular(project, "OpenCV compile project")
        try:
            root = ET.parse(project).getroot()
        except (ET.ParseError, OSError) as exc:
            raise OpenCVWheelError(f"Cannot inspect OpenCV compile project: {relative}") from exc
        compile_items = root.findall("{*}ItemGroup/{*}ClCompile[@Include]")
        if not compile_items:
            continue  # ALL_BUILD, ZERO_CHECK, and other utility projects.
        if any(
            re.search(r'(?:^|[\s"])[/-]MTd?(?=$|[\s"])', node.text or "", re.IGNORECASE)
            for node in root.findall(".//{*}AdditionalOptions")
        ):
            raise OpenCVWheelError(f"OpenCV static CRT AdditionalOptions override: {relative}")
        if any(item.findall(".//{*}RuntimeLibrary") for item in compile_items):
            raise OpenCVWheelError(f"OpenCV per-file RuntimeLibrary override: {relative}")
        groups = [
            group
            for group in root.findall("{*}ItemDefinitionGroup")
            if re.sub(r"\s+", "", group.get("Condition", ""))
            == "'$(Configuration)|$(Platform)'=='Release|x64'"
        ]
        runtime_nodes = (
            groups[0].findall("{*}ClCompile/{*}RuntimeLibrary")
            if len(groups) == 1 else []
        )
        if (
            len(runtime_nodes) != 1
            or runtime_nodes[0].text != "MultiThreadedDLL"
            or runtime_nodes[0].attrib
        ):
            raise OpenCVWheelError(f"OpenCV Release RuntimeLibrary must be MultiThreadedDLL: {relative}")
        # Only CMake's four unambiguous configuration defaults may coexist;
        # an unconditional or differently conditioned group could override Release.
        known_runtime_nodes = []
        conditions: set[str] = set()
        for group in root.findall("{*}ItemDefinitionGroup"):
            nodes = group.findall("{*}ClCompile/{*}RuntimeLibrary")
            if not nodes:
                continue
            condition = re.sub(r"\s+", "", group.get("Condition", ""))
            if (
                condition not in {
                    f"'$(Configuration)|$(Platform)'=='{config}|x64'"
                    for config in ("Debug", "Release", "MinSizeRel", "RelWithDebInfo")
                }
                or condition in conditions
                or len(nodes) != 1
                or nodes[0].attrib
                or any(node.attrib for node in group.findall("{*}ClCompile"))
            ):
                raise OpenCVWheelError(f"OpenCV ambiguous RuntimeLibrary override: {relative}")
            conditions.add(condition)
            known_runtime_nodes.extend(nodes)
        if any(
            node not in known_runtime_nodes
            for node in root.findall(".//{*}RuntimeLibrary")
        ):
            raise OpenCVWheelError(f"OpenCV unexpected RuntimeLibrary override: {relative}")
        records.append({
            "path": relative,
            "size": project.stat().st_size,
            "sha256": _sha256(project),
            "runtime_library": "MultiThreadedDLL",
        })
    _validate_compile_projects(records)
    return records


def _validate_compile_projects(records: Any) -> None:
    if not isinstance(records, list) or not records:
        raise OpenCVWheelError("OpenCV ClCompile project evidence is missing")
    seen: set[str] = set()
    for item in records:
        if (
            not isinstance(item, dict)
            or set(item) != {"path", "size", "sha256", "runtime_library"}
            or not isinstance(item["path"], str)
            or re.fullmatch(r"_skbuild/[^/]+/cmake-build/.+\.vcxproj", item["path"]) is None
            or _safe_member(item["path"]).as_posix() != item["path"]
            or item["path"].casefold() in seen
            or "cmakefiles" in item["path"].casefold().split("/")
            or type(item["size"]) is not int
            or item["size"] <= 0
            or _SHA256.fullmatch(str(item["sha256"])) is None
            or item["runtime_library"] != "MultiThreadedDLL"
        ):
            raise OpenCVWheelError("OpenCV compile project provenance is invalid")
        seen.add(item["path"].casefold())
    if not any(path.endswith("/modules/python3/opencv_python3.vcxproj") for path in seen):
        raise OpenCVWheelError("OpenCV python3 ClCompile project evidence is missing")


def _is_required_toolset_version(value: str) -> bool:
    return value == REQUIRED_TOOLSET_VERSION


def _capture_configured_toolchain(source_tree: Path) -> dict[str, Any]:
    cache = _read_cmake_cache(source_tree)
    expected = {
        "CMAKE_GENERATOR": REQUIRED_GENERATOR,
        "CMAKE_GENERATOR_TOOLSET": REQUIRED_TOOLSET_NAME,
        "CMAKE_SYSTEM_VERSION": REQUIRED_WINDOWS_SDK,
        "WITH_IPP": "OFF",
        "BUILD_IPP_IW": "OFF",
        "BUILD_opencv_gapi": "OFF",
        "WITH_ADE": "OFF",
        "PYTHON3_LIMITED_API": "ON",
        "WITH_FFMPEG": "ON",
        "BUILD_SHARED_LIBS": "OFF",
        "BUILD_WITH_STATIC_CRT": "OFF",
    }
    observed = {key: cache.get(key) for key in expected}
    if observed != expected:
        raise OpenCVWheelError(
            "OpenCV configured toolchain or feature flags differ: "
            + json.dumps(observed, sort_keys=True)
        )
    # scikit-build selects v143; CMake's VS toolset-version variable need not
    # be cached. Reject any compiler outside the exact locked toolset instead
    # of relying on the installed runner's default minor version.
    compiler = _capture_compiler(cache, source_tree)
    c_compiler = _capture_compiler(cache, source_tree, language="C")
    if c_compiler != compiler:
        raise OpenCVWheelError("OpenCV C and C++ compilers differ")
    selected = cache.get("CMAKE_VS_PLATFORM_TOOLSET_VERSION")
    if selected and (
        not _is_required_toolset_version(selected)
        or not (
            compiler["msvc_toolset_version"] == selected
            or compiler["msvc_toolset_version"].startswith(selected + ".")
        )
    ):
        raise OpenCVWheelError(
            f"OpenCV selected MSVC toolset differs: {selected}"
        )
    selected = compiler["msvc_toolset_version"]
    return {
        "cmake_cache": expected,
        "compiler": compiler,
        "c_compiler": c_compiler,
        "msbuild_project": _capture_msbuild_project(source_tree),
        "compile_projects": _capture_dynamic_crt_projects(source_tree),
        "selected_msvc_toolset_version": selected,
    }


def _wheel_metadata(wheel: Path) -> tuple[str, str]:
    _regular(wheel, "OpenCV output wheel")
    match = _WHEEL.match(wheel.name)
    if match is None:
        raise OpenCVWheelError(f"Unexpected OpenCV wheel filename: {wheel.name}")
    try:
        with zipfile.ZipFile(wheel) as archive:
            names = archive.namelist()
            safe_names = [_safe_member(name).as_posix().casefold() for name in names]
            if len(safe_names) != len(set(safe_names)):
                raise OpenCVWheelError("OpenCV wheel contains duplicate Windows paths")
            metadata = [name for name in names if name.endswith(".dist-info/METADATA")]
            if len(metadata) != 1:
                raise OpenCVWheelError("OpenCV wheel must contain one METADATA file")
            text = archive.read(metadata[0]).decode("utf-8")
    except (OSError, zipfile.BadZipFile, UnicodeDecodeError) as exc:
        raise OpenCVWheelError(f"Cannot inspect OpenCV wheel: {exc}") from exc
    fields = dict(
        line.split(": ", 1)
        for line in text.splitlines()
        if ": " in line and line.split(": ", 1)[0] in {"Name", "Version"}
    )
    if fields.get("Name", "").casefold() != "opencv-python":
        raise OpenCVWheelError("OpenCV wheel metadata distribution differs")
    return fields.get("Name", ""), fields.get("Version", "")


def _wheel_version(wheel: Path) -> str:
    return _wheel_metadata(wheel)[1]


def _wheel_contents(wheel: Path) -> list[dict[str, Any]]:
    try:
        with zipfile.ZipFile(wheel) as archive:
            result = []
            seen: set[str] = set()
            for info in archive.infolist():
                path = _safe_member(info.filename).as_posix()
                folded = path.casefold()
                if folded in seen:
                    raise OpenCVWheelError(
                        f"OpenCV wheel contains a duplicate path: {path}"
                    )
                seen.add(folded)
                if info.is_dir():
                    continue
                data = archive.read(info)
                result.append(
                    {
                        "path": path,
                        "size": len(data),
                        "sha256": hashlib.sha256(data).hexdigest(),
                    }
                )
    except (OSError, zipfile.BadZipFile) as exc:
        raise OpenCVWheelError(f"Cannot inventory OpenCV wheel: {exc}") from exc
    return sorted(result, key=lambda item: str(item["path"]).casefold())


def _bind_ffmpeg_input(
    wheel_contents: list[dict[str, Any]],
    ffmpeg_preseed: list[dict[str, Any]],
) -> dict[str, Any]:
    matches = [
        item
        for item in wheel_contents
        if re.fullmatch(
            r"cv2/opencv_videoio_ffmpeg\d+_64\.dll",
            str(item["path"]),
            flags=re.IGNORECASE,
        )
    ]
    if len(matches) != 1:
        raise OpenCVWheelError(
            f"Expected one x64 OpenCV FFmpeg DLL in wheel, found {len(matches)}"
        )
    source = next(
        (
            item
            for item in ffmpeg_preseed
            if item["filename"] == "opencv_videoio_ffmpeg_64.dll"
        ),
        None,
    )
    if source is None or any(
        matches[0][field] != source[field] for field in ("size", "sha256")
    ):
        raise OpenCVWheelError(
            "OpenCV wheel FFmpeg DLL differs from the fixed x64 input"
        )
    return {
        "input_filename": source["filename"],
        "input_sha256": source["sha256"],
        "wheel_path": matches[0]["path"],
        "wheel_sha256": matches[0]["sha256"],
        "size": matches[0]["size"],
    }


def _semantic_manifest(provenance: dict[str, Any]) -> dict[str, Any]:
    semantic_contents = []
    for item in provenance["wheel_contents"]:
        path = str(item["path"])
        if path.casefold().endswith((".exe", ".dll", ".pyd")):
            semantic_contents.append({"path": path})
        elif PurePosixPath(path).name == "RECORD":
            semantic_contents.append({"path": path})
        else:
            semantic_contents.append(item)
    pe_files = [
        {
            "path": item["path"],
            "size": item["size"],
            "sha256": item["sha256"],
            "imports": item["imports"],
        }
        for item in provenance["pe_inventory"]["files"]
    ]
    probes = {
        key: provenance["probes"][key]
        for key in (
            "api",
            "ffmpeg",
            "ffmpeg_build_information_lines",
            "ipp",
            "ipp_build_information_lines",
            "opencv_version",
            "video_reader_backend",
            "video_writer_backend",
        )
    }
    return {
        "component": provenance["component"],
        "version": provenance["version"],
        "cmake_args": provenance["cmake_args"],
        "configured_features": provenance["observed_build_environment"][
            "configured_toolchain"
        ]["cmake_cache"],
        "wheel_contents": semantic_contents,
        "ffmpeg_wheel_binding": provenance["ffmpeg_wheel_binding"],
        "pe_files": pe_files,
        "probes": probes,
    }


def _json_sha256(payload: Any) -> str:
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()


def _reject_ipp(wheel: Path) -> None:
    try:
        with zipfile.ZipFile(wheel) as archive:
            for info in archive.infolist():
                if info.is_dir():
                    continue
                name = info.filename.casefold()
                if any(token in name for token in ("ippicv", "ippiw")):
                    raise OpenCVWheelError(f"IPP artifact remains in wheel: {info.filename}")
    except zipfile.BadZipFile as exc:
        raise OpenCVWheelError(f"Cannot scan OpenCV wheel: {exc}") from exc


def _pe_inventory(wheel: Path, work_dir: Path, python: Path | None = None) -> dict[str, Any]:
    extract = work_dir / "pe-audit"
    extract.mkdir()
    with zipfile.ZipFile(wheel) as archive:
        seen: set[str] = set()
        for info in archive.infolist():
            path = _safe_member(info.filename)
            folded = path.as_posix().casefold()
            if folded in seen:
                raise OpenCVWheelError(f"OpenCV wheel contains duplicate path: {info.filename}")
            seen.add(folded)
            target = extract.joinpath(*path.parts)
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.read(info))
    if python is not None:
        output = work_dir / "pe-inventory.json"
        result = subprocess.run(
            [str(python), "-m", "scripts.pe_runtime_audit", str(extract),
             "--enforce-external", "--output", str(output)],
            cwd=Path(__file__).resolve().parents[1], check=False,
            capture_output=True, text=True,
        )
        if result.returncode != 0 or not output.is_file():
            raise OpenCVWheelError("OpenCV wheel PE audit failed")
        try:
            return json.loads(output.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise OpenCVWheelError("OpenCV wheel PE inventory is invalid") from exc
    try:
        from scripts import pe_runtime_audit

        return pe_runtime_audit.build_inventory(extract, enforce_external=True)
    except ImportError as exc:
        raise OpenCVWheelError(f"PE audit dependency unavailable: {exc}") from exc
    except Exception as exc:
        raise OpenCVWheelError(f"OpenCV wheel PE audit failed: {exc}") from exc


def _output_wheel(output_dir: Path, filename: str) -> Path:
    wheels = sorted(output_dir.glob("opencv_python-*.whl"))
    if len(wheels) != 1:
        raise OpenCVWheelError(f"Expected one OpenCV output wheel, found {len(wheels)}")
    name, observed_version = _wheel_metadata(wheels[0])
    if wheels[0].name != filename or name.casefold() != "opencv-python":
        raise OpenCVWheelError(
            "Generated OpenCV wheel identity differs from policy: "
            f"expected {filename}, observed {wheels[0].name} "
            f"({name} {observed_version})"
        )
    return wheels[0]


def _validate_pe_inventory(
    inventory: Any,
    wheel_contents: list[dict[str, Any]],
    pefile_version: str,
) -> None:
    if not isinstance(inventory, dict) or set(inventory) != {
        "schema_version",
        "tool",
        "files",
        "runtime_reverse",
        "summary",
    }:
        raise OpenCVWheelError("OpenCV PE inventory fields are invalid")
    if inventory["schema_version"] != 2 or inventory["tool"] != {
        "name": "pe_runtime_audit",
        "pefile_version": pefile_version,
    }:
        raise OpenCVWheelError("OpenCV PE inventory tool identity is invalid")
    files = inventory["files"]
    if not isinstance(files, list) or not files:
        raise OpenCVWheelError("OpenCV PE inventory has no files")
    if any(not isinstance(item, dict) for item in files):
        raise OpenCVWheelError("OpenCV PE inventory file record is invalid")
    inventory_paths = [str(item.get("path", "")).casefold() for item in files]
    if len(inventory_paths) != len(set(inventory_paths)):
        raise OpenCVWheelError("OpenCV PE inventory has duplicate paths")
    content_by_path = {
        str(item["path"]).casefold(): item for item in wheel_contents
    }
    expected_pe_paths = {
        path
        for path in content_by_path
        if path.endswith((".exe", ".dll", ".pyd"))
    }
    actual_pe_paths = set(inventory_paths)
    if actual_pe_paths != expected_pe_paths:
        raise OpenCVWheelError("OpenCV PE inventory file set differs from wheel")
    import_count = 0
    for item in files:
        if not isinstance(item, dict) or set(item) != {
            "path",
            "size",
            "sha256",
            "imports",
        }:
            raise OpenCVWheelError("OpenCV PE inventory file record is invalid")
        path = _safe_member(str(item["path"])).as_posix()
        if not path.casefold().endswith((".exe", ".dll", ".pyd")):
            raise OpenCVWheelError("OpenCV PE inventory contains a non-PE path")
        content = content_by_path.get(path.casefold())
        if content is None or item["size"] != content["size"] or item[
            "sha256"
        ] != content["sha256"]:
            raise OpenCVWheelError("OpenCV PE inventory differs from wheel contents")
        imports = item["imports"]
        if not isinstance(imports, list):
            raise OpenCVWheelError("OpenCV PE import list is invalid")
        for imported in imports:
            if (
                not isinstance(imported, dict)
                or set(imported) != {"name", "type"}
                or not isinstance(imported["name"], str)
                or not imported["name"]
                or imported["type"] not in {"normal", "delay"}
            ):
                raise OpenCVWheelError("OpenCV PE import record is invalid")
        import_count += len(imports)
    summary = inventory["summary"]
    if not isinstance(summary, dict) or set(summary) != {
        "pe_files",
        "import_count",
        "runtime_import_count",
        "app_local_runtime_files",
        "hashed_imports",
        "unknown_runtime_imports",
        "app_local_icu_files",
        "icu_imports",
    }:
        raise OpenCVWheelError("OpenCV PE inventory summary fields are invalid")
    if (
        summary["pe_files"] != len(files)
        or summary["import_count"] != import_count
        or summary["app_local_runtime_files"] != []
        or summary["hashed_imports"] != []
        or summary["unknown_runtime_imports"] != []
        or summary["app_local_icu_files"] != []
        or summary["icu_imports"] != []
        or not isinstance(summary["runtime_import_count"], int)
        or summary["runtime_import_count"] < 0
        or not isinstance(inventory["runtime_reverse"], dict)
    ):
        raise OpenCVWheelError("OpenCV PE inventory does not enforce external Runtime")
    _require_dynamic_crt_import(inventory)


def _require_dynamic_crt_import(inventory: dict[str, Any]) -> None:
    cv2_files = [
        item for item in inventory["files"]
        if item["path"].casefold() == "cv2/cv2.pyd"
    ]
    if len(cv2_files) != 1 or not any(
        item["type"] == "normal"
        and item["name"].casefold() in {
            "vcruntime140.dll", "vcruntime140_1.dll", "msvcp140.dll",
            "msvcp140_1.dll", "msvcp140_2.dll",
        }
        for item in cv2_files[0]["imports"]
    ):
        raise OpenCVWheelError("OpenCV cv2.pyd has no normal dynamic CRT import")


def validate_output_directory(
    output_dir: Path,
    components_file: Path,
    *,
    audit_python: Path | None = None,
) -> dict[str, Any]:
    """Validate a generated wheel and return its immutable provenance payload."""
    lock = _load_lock(components_file)
    policy = _policy(lock)
    _directory(output_dir, "OpenCV output directory")
    provenance_path = output_dir / PROVENANCE_NAME
    _regular(provenance_path, "OpenCV provenance")
    expected_names = {
        str(policy["output_filename"]).casefold(),
        PROVENANCE_NAME.casefold(),
    }
    actual_names = {
        path.name.casefold()
        for path in output_dir.iterdir()
        if path.is_file() and not path.is_symlink()
    }
    if actual_names != expected_names or any(
        path.is_dir() or path.is_symlink() for path in output_dir.iterdir()
    ):
        raise OpenCVWheelError("OpenCV output directory file set differs")
    try:
        provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise OpenCVWheelError(f"Cannot read OpenCV provenance: {exc}") from exc
    wheel = _output_wheel(output_dir, str(policy["output_filename"]))
    _reject_ipp(wheel)
    expected = {
        "version": _wheel_version(wheel),
        "wheel": {
            "filename": wheel.name,
            "size": wheel.stat().st_size,
            "sha256": _sha256(wheel),
            "distribution": "opencv-python",
            "version": _wheel_version(wheel),
        },
        "wheel_contents": _wheel_contents(wheel),
    }
    for key, value in expected.items():
        if not isinstance(provenance, dict) or provenance.get(key) != value:
            raise OpenCVWheelError(f"OpenCV provenance differs for {key}")
    with tempfile.TemporaryDirectory(prefix="a-", dir=output_dir.parent) as temporary:
        actual_pe_inventory = _pe_inventory(
            wheel,
            Path(temporary),
            audit_python,
        )
    if provenance.get("pe_inventory") != actual_pe_inventory:
        raise OpenCVWheelError("OpenCV PE inventory differs from wheel")
    _validate_provenance_payload(provenance, components_file)
    return provenance


def validate_embedded_provenance_record(
    record: dict[str, Any], components_file: Path
) -> dict[str, Any]:
    """Validate the sealed ``{provenance_sha256, provenance}`` wrapper."""
    if not isinstance(record, dict) or set(record) != {"provenance_sha256", "provenance"}:
        raise OpenCVWheelError("OpenCV provenance wrapper fields are invalid")
    payload = record["provenance"]
    if not isinstance(payload, dict):
        raise OpenCVWheelError("OpenCV provenance wrapper payload is invalid")
    canonical = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if record["provenance_sha256"] != hashlib.sha256(canonical.encode("utf-8")).hexdigest():
        raise OpenCVWheelError("OpenCV provenance wrapper SHA256 differs")
    return _validate_provenance_payload(payload, components_file)


def _validate_provenance_payload(
    record: dict[str, Any], components_file: Path
) -> dict[str, Any]:
    """Strictly validate the generated wheel payload."""
    lock = _load_lock(components_file)
    policy = _policy(lock)
    required = {
        "schema_version",
        "component",
        "distribution",
        "version",
        "python_version",
        "platform",
        "cmake_args",
        "build_environment",
        "observed_build_environment",
        "command",
        "inputs",
        "wheel",
        "wheel_contents",
        "probes",
        "pe_inventory",
        "ffmpeg_preseed",
        "ffmpeg_wheel_binding",
        "opencv_download_path",
        "version_py_sha256",
        "semantic_manifest",
        "semantic_manifest_sha256",
        "repeatability",
    }
    if not isinstance(record, dict) or set(record) != required:
        raise OpenCVWheelError("OpenCV provenance fields differ from the required schema")
    expected_version = _WHEEL.match(str(policy["output_filename"]))
    if expected_version is None:
        raise OpenCVWheelError("OpenCV output filename is invalid")
    expected = {
        "schema_version": 1,
        "component": "opencv-python",
        "distribution": "opencv-python",
        "version": expected_version.group("version"),
        "python_version": REQUIRED_PYTHON,
        "platform": "win_amd64",
        "cmake_args": list(REQUIRED_CMAKE_ARGS),
        "build_environment": policy["build_environment"],
    }
    for key, value in expected.items():
        if record.get(key) != value:
            raise OpenCVWheelError(f"OpenCV provenance differs for {key}")
    if record["command"] != list(BUILD_COMMAND):
        raise OpenCVWheelError("OpenCV provenance command is invalid")
    expected_inputs = {
        str(item["filename"]): (int(item["size"]), str(item["sha256"]))
        for item in [*policy["source_artifacts"], *policy["build_artifacts"]]
    }
    actual_inputs = record["inputs"]
    if not isinstance(actual_inputs, list) or len(actual_inputs) != len(expected_inputs):
        raise OpenCVWheelError("OpenCV provenance inputs differ from policy")
    observed = {}
    for item in actual_inputs:
        if not isinstance(item, dict) or set(item) != {"filename", "size", "sha256"}:
            raise OpenCVWheelError("OpenCV provenance input record is invalid")
        observed[str(item["filename"])] = (item["size"], item["sha256"])
    if observed != expected_inputs:
        raise OpenCVWheelError("OpenCV provenance input hashes differ from policy")
    wheel = record["wheel"]
    if not isinstance(wheel, dict) or set(wheel) != {
        "filename", "size", "sha256", "distribution", "version"
    }:
        raise OpenCVWheelError("OpenCV provenance wheel record is invalid")
    if wheel["filename"] != policy["output_filename"] or wheel["distribution"] != "opencv-python" or wheel["version"] != expected_version.group("version"):
        raise OpenCVWheelError("OpenCV provenance wheel identity differs")
    if not isinstance(wheel["size"], int) or wheel["size"] <= 0 or not _SHA256.fullmatch(str(wheel["sha256"])):
        raise OpenCVWheelError("OpenCV provenance wheel digest is invalid")
    contents = record["wheel_contents"]
    if (
        not isinstance(contents, list)
        or not contents
        or any(
            not isinstance(item, dict)
            or set(item) != {"path", "size", "sha256"}
            or not isinstance(item["size"], int)
            or item["size"] < 0
            or _SHA256.fullmatch(str(item["sha256"])) is None
            for item in contents
        )
    ):
        raise OpenCVWheelError("OpenCV wheel content inventory is invalid")
    paths = [str(item["path"]).casefold() for item in contents]
    if len(paths) != len(set(paths)):
        raise OpenCVWheelError("OpenCV wheel content inventory has duplicates")
    probes = record["probes"]
    if not isinstance(probes, dict) or set(probes) != {
        "api",
        "build_information_sha256",
        "ffmpeg",
        "ffmpeg_build_information_lines",
        "ipp",
        "ipp_build_information_lines",
        "opencv_version",
        "video_reader_backend",
        "video_writer_backend",
    }:
        raise OpenCVWheelError("OpenCV native probe fields are incomplete")
    if (
        probes["api"] != "ok"
        or probes["ipp"] != "disabled"
        or probes["ffmpeg"] != "enabled"
        or probes["ffmpeg_build_information_lines"]
        != ["FFMPEG: YES (prebuilt binaries)"]
        or probes["opencv_version"] != expected_version.group("version")
        or probes["video_reader_backend"] != "FFMPEG"
        or probes["video_writer_backend"] != "FFMPEG"
        or probes["ipp_build_information_lines"] != []
        or _SHA256.fullmatch(str(probes["build_information_sha256"])) is None
    ):
        raise OpenCVWheelError("OpenCV native probes are incomplete")
    if record["opencv_download_path"] != "<work-dir>/opencv-download":
        raise OpenCVWheelError("OpenCV download path is invalid")
    if record["version_py_sha256"] != hashlib.sha256(VERSION_PY_BYTES).hexdigest():
        raise OpenCVWheelError("OpenCV version.py digest is invalid")
    ffmpeg = record["ffmpeg_preseed"]
    if not isinstance(ffmpeg, list) or len(ffmpeg) != len(EXPECTED_FFMPEG):
        raise OpenCVWheelError("OpenCV FFmpeg preseed record is incomplete")
    expected_ffmpeg = {
        (filename, md5, f"ffmpeg/{md5}-{filename}")
        for filename, md5 in EXPECTED_FFMPEG.items()
    }
    observed_ffmpeg = set()
    for item in ffmpeg:
        if (
            not isinstance(item, dict)
            or set(item) != {"filename", "md5", "cache_path", "size", "sha256"}
            or not isinstance(item["size"], int)
            or item["size"] <= 0
            or _SHA256.fullmatch(str(item["sha256"])) is None
        ):
            raise OpenCVWheelError("OpenCV FFmpeg preseed record is invalid")
        observed_ffmpeg.add((item["filename"], item["md5"], item["cache_path"]))
    if observed_ffmpeg != expected_ffmpeg:
        raise OpenCVWheelError("OpenCV FFmpeg preseed identity differs")
    binding = record["ffmpeg_wheel_binding"]
    if not isinstance(binding, dict) or set(binding) != {
        "input_filename",
        "input_sha256",
        "wheel_path",
        "wheel_sha256",
        "size",
    }:
        raise OpenCVWheelError("OpenCV FFmpeg wheel binding is invalid")
    source_ffmpeg = next(
        (
            item
            for item in ffmpeg
            if item["filename"] == "opencv_videoio_ffmpeg_64.dll"
        ),
        None,
    )
    content_ffmpeg = next(
        (
            item
            for item in contents
            if item["path"] == binding["wheel_path"]
        ),
        None,
    )
    if (
        source_ffmpeg is None
        or content_ffmpeg is None
        or binding["input_filename"] != source_ffmpeg["filename"]
        or binding["input_sha256"] != source_ffmpeg["sha256"]
        or binding["wheel_sha256"] != source_ffmpeg["sha256"]
        or binding["wheel_sha256"] != content_ffmpeg["sha256"]
        or binding["size"] != source_ffmpeg["size"]
        or binding["size"] != content_ffmpeg["size"]
    ):
        raise OpenCVWheelError("OpenCV FFmpeg wheel binding differs")
    observed_environment = record["observed_build_environment"]
    if not isinstance(observed_environment, dict) or set(observed_environment) != {
        "prebuild",
        "configured_toolchain",
        "runner_image",
        "python_hash_seed",
    }:
        raise OpenCVWheelError("OpenCV observed build environment is invalid")
    if observed_environment["python_hash_seed"] != "0":
        raise OpenCVWheelError("OpenCV observed Python hash seed differs")
    if observed_environment["prebuild"] != {
        "cmake_version": policy["build_environment"]["cmake_version"],
        "build_packages": policy["build_environment"]["build_packages"],
        "python_version": REQUIRED_PYTHON,
    }:
        raise OpenCVWheelError("OpenCV prebuild environment differs")
    configured = observed_environment["configured_toolchain"]
    if not isinstance(configured, dict) or set(configured) != {
        "cmake_cache",
        "compiler",
        "c_compiler",
        "msbuild_project",
        "compile_projects",
        "selected_msvc_toolset_version",
    }:
        raise OpenCVWheelError("OpenCV configured toolchain fields are invalid")
    if configured["cmake_cache"] != {
        "CMAKE_GENERATOR": REQUIRED_GENERATOR,
        "CMAKE_GENERATOR_TOOLSET": REQUIRED_TOOLSET_NAME,
        "CMAKE_SYSTEM_VERSION": REQUIRED_WINDOWS_SDK,
        "WITH_IPP": "OFF",
        "BUILD_IPP_IW": "OFF",
        "BUILD_opencv_gapi": "OFF",
        "WITH_ADE": "OFF",
        "PYTHON3_LIMITED_API": "ON",
        "WITH_FFMPEG": "ON",
        "BUILD_SHARED_LIBS": "OFF",
        "BUILD_WITH_STATIC_CRT": "OFF",
    }:
        raise OpenCVWheelError("OpenCV configured CMake cache differs")
    _validate_compile_projects(configured["compile_projects"])
    msbuild = configured["msbuild_project"]
    if (
        not isinstance(msbuild, dict)
        or set(msbuild) != {
            "path",
            "size",
            "sha256",
            "platform_toolsets",
            "windows_target_platform_versions",
        }
        or re.fullmatch(
            r"_skbuild/[^/]+/cmake-build/ALL_BUILD\.vcxproj",
            str(msbuild["path"]),
        )
        is None
        or msbuild["platform_toolsets"] != [REQUIRED_TOOLSET_NAME]
        or msbuild["windows_target_platform_versions"] != [REQUIRED_WINDOWS_SDK]
        or not isinstance(msbuild["size"], int)
        or msbuild["size"] <= 0
        or _SHA256.fullmatch(str(msbuild["sha256"])) is None
    ):
        raise OpenCVWheelError("OpenCV MSBuild project provenance is invalid")
    compiler = configured["compiler"]
    if (
        not isinstance(compiler, dict)
        or set(compiler) != {
            "filename",
            "msvc_toolset_version",
            "sha256",
            "size",
        }
        or str(compiler["filename"]).casefold() != "cl.exe"
        or not _is_required_toolset_version(str(compiler["msvc_toolset_version"]))
        or not _is_required_toolset_version(
            str(configured["selected_msvc_toolset_version"])
        )
        or not (
            str(compiler["msvc_toolset_version"])
            == str(configured["selected_msvc_toolset_version"])
            or str(compiler["msvc_toolset_version"]).startswith(
                str(configured["selected_msvc_toolset_version"]) + "."
            )
        )
        or not isinstance(compiler["size"], int)
        or compiler["size"] <= 0
        or _SHA256.fullmatch(str(compiler["sha256"])) is None
    ):
        raise OpenCVWheelError("OpenCV compiler provenance is invalid")
    if configured["c_compiler"] != compiler:
        raise OpenCVWheelError("OpenCV C compiler provenance differs from C++ compiler")
    runner = observed_environment["runner_image"]
    if not isinstance(runner, dict) or set(runner) != {"os", "version"} or not all(
        isinstance(value, str) and value for value in runner.values()
    ):
        raise OpenCVWheelError("OpenCV runner image provenance is invalid")
    _validate_pe_inventory(
        record["pe_inventory"],
        contents,
        policy["build_environment"]["build_packages"]["pefile"],
    )
    semantic_manifest = _semantic_manifest(record)
    if (
        record["semantic_manifest"] != semantic_manifest
        or record["semantic_manifest_sha256"] != _json_sha256(semantic_manifest)
        or (
            policy["expected_semantic_manifest_sha256"] is not None
            and record["semantic_manifest_sha256"]
            != policy["expected_semantic_manifest_sha256"]
        )
    ):
        raise OpenCVWheelError("OpenCV semantic manifest differs")
    repeatability = record["repeatability"]
    if not isinstance(repeatability, dict) or set(repeatability) != {
        "byte_identical",
        "first_wheel_sha256",
        "second_wheel_sha256",
        "semantic_equal",
        "semantic_manifest_sha256",
    }:
        raise OpenCVWheelError("OpenCV repeatability record is invalid")
    if (
        not isinstance(repeatability["byte_identical"], bool)
        or repeatability["first_wheel_sha256"] != wheel["sha256"]
        or _SHA256.fullmatch(str(repeatability["second_wheel_sha256"])) is None
        or repeatability["semantic_equal"] is not True
        or repeatability["semantic_manifest_sha256"]
        != record["semantic_manifest_sha256"]
        or repeatability["byte_identical"]
        != (
            repeatability["first_wheel_sha256"]
            == repeatability["second_wheel_sha256"]
        )
        or (
            policy["expected_wheel_sha256"] is not None
            and (
                repeatability["byte_identical"] is not True
                or repeatability["first_wheel_sha256"]
                != policy["expected_wheel_sha256"]
            )
        )
        or (
            policy["expected_byte_identical"] is not None
            and repeatability["byte_identical"]
            is not policy["expected_byte_identical"]
        )
    ):
        raise OpenCVWheelError("OpenCV repeatability evidence is invalid")
    return record


def _reject_inherited_compiler_flags() -> None:
    # CL is prepended and _CL_ appended by cl.exe itself, outside the generated
    # MSBuild RuntimeLibrary setting. LINK/_LINK_ do the same for link.exe;
    # other flags seed CMake's compiler/linker flags or inject build-time hooks.
    present = [name for name in COMPILER_FLAG_ENVIRONMENT if os.environ.get(name, "").strip()]
    if present:
        raise OpenCVWheelError("OpenCV inherited compiler flags are not allowed: " + ", ".join(present))


def _run_once(
    source_dir: Path,
    output_dir: Path,
    policy: dict[str, Any],
    work_dir: Path,
) -> tuple[dict[str, Any], Path]:
    """Build and inspect one wheel in a clean directory."""
    _reject_inherited_compiler_flags()
    inputs = _verify_inputs(source_dir, policy)
    if output_dir.exists() or output_dir.is_symlink():
        raise OpenCVWheelError(f"OpenCV output directory already exists: {output_dir}")
    work_dir.mkdir(parents=True, exist_ok=False)
    output_dir.mkdir(parents=True, exist_ok=False)
    source_tree, thirdparty_root = _compose_source_tree(
        source_dir, policy, work_dir / "s"
    )
    download_path = work_dir / "opencv-download"
    ffmpeg_records = _preseed_ffmpeg(thirdparty_root, download_path / "ffmpeg")
    build_artifacts = [
        inputs[str(item["filename"]).casefold()]
        for item in policy["build_artifacts"]
    ]
    build_python = _prepare_build_venv(work_dir, build_artifacts)
    prebuild_environment = _validate_build_environment(policy, build_python)
    runner_image = {
        "os": os.environ.get("ImageOS", ""),
        "version": os.environ.get("ImageVersion", ""),
    }
    if not all(runner_image.values()):
        raise OpenCVWheelError(
            "OpenCV formal source build requires GitHub runner image identity"
        )
    actual_command = [
        str(build_python),
        "setup.py",
        "bdist_wheel",
        "--py-limited-api=cp37",
        "--dist-dir",
        str(output_dir.resolve()),
    ]
    environment = os.environ.copy()
    for name in COMPILER_FLAG_ENVIRONMENT:
        environment.pop(name, None)
    environment["PATH"] = str(build_python.parent) + os.pathsep + environment.get(
        "PATH", ""
    )
    environment["CMAKE_ARGS"] = " ".join(REQUIRED_CMAKE_ARGS)
    # This alternate channel overrides CMAKE_ARGS in scikit-build 0.18.1.
    environment.pop("SKBUILD_CONFIGURE_OPTIONS", None)
    environment["OPENCV_DOWNLOAD_PATH"] = str(download_path.resolve())
    build_environment = policy["build_environment"]
    environment["CMAKE_GENERATOR"] = str(build_environment["generator"])
    environment["CMAKE_GENERATOR_TOOLSET"] = str(
        build_environment["msvc_toolset"]
    )
    environment["CMAKE_BUILD_PARALLEL_LEVEL"] = str(
        build_environment["cmake_build_parallel_level"]
    )
    environment["PYTHONHASHSEED"] = str(build_environment["python_hash_seed"])
    environment["CI_BUILD"] = "1"
    environment["OPENCV_PYTHON_SKIP_GIT_COMMANDS"] = "1"
    for flag in ("ENABLE_CONTRIB", "ENABLE_HEADLESS", "ENABLE_ROLLING"):
        environment[flag] = "0"
    completed = subprocess.run(
        actual_command,
        cwd=source_tree,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    (work_dir / "build.log").write_text(
        "\n".join(part for part in (completed.stdout, completed.stderr) if part),
        encoding="utf-8",
    )
    if completed.returncode != 0:
        output = "\n".join(
            part for part in (completed.stdout, completed.stderr) if part
        )
        raise OpenCVWheelError(
            "OpenCV wheel build failed: "
            + (output[-20000:] or f"exit {completed.returncode}")
        )
    _verify_download_cache(download_path, ffmpeg_records)
    wheel = _output_wheel(output_dir, str(policy["output_filename"]))
    _reject_ipp(wheel)
    configured_toolchain = _capture_configured_toolchain(source_tree)
    evidence = work_dir / "evidence"
    evidence.mkdir()
    probes = _probe_wheel(build_python, wheel, evidence / "build-information.txt")
    pe_inventory = _pe_inventory(wheel, work_dir, build_python)
    _require_dynamic_crt_import(pe_inventory)
    wheel_contents = _wheel_contents(wheel)
    ffmpeg_wheel_binding = _bind_ffmpeg_input(
        wheel_contents,
        ffmpeg_records,
    )
    # Keep only individually verified wheels as failure diagnostics, never as a
    # sealed producer artifact until the two-build comparison also passes.
    shutil.copy2(wheel, evidence / wheel.name)
    for item in [
        configured_toolchain["msbuild_project"],
        *configured_toolchain["compile_projects"],
    ]:
        project = source_tree / item["path"]
        destination = evidence / "msbuild" / item["path"]
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(project, destination)
    provenance = {
        "schema_version": 1,
        "component": "opencv-python",
        "distribution": "opencv-python",
        "version": _wheel_version(wheel),
        "python_version": REQUIRED_PYTHON,
        "platform": "win_amd64",
        "cmake_args": list(REQUIRED_CMAKE_ARGS),
        "build_environment": policy["build_environment"],
        "observed_build_environment": {
            "prebuild": prebuild_environment,
            "configured_toolchain": configured_toolchain,
            "runner_image": runner_image,
            "python_hash_seed": environment["PYTHONHASHSEED"],
        },
        "version_py_sha256": _sha256(source_tree / "cv2" / "version.py"),
        "opencv_download_path": "<work-dir>/opencv-download",
        "command": list(BUILD_COMMAND),
        "inputs": [
            {
                "filename": path.name,
                "size": path.stat().st_size,
                "sha256": _sha256(path),
            }
            for path in sorted(inputs.values(), key=lambda item: item.name.casefold())
        ],
        "wheel": {
            "filename": wheel.name,
            "size": wheel.stat().st_size,
            "sha256": _sha256(wheel),
            "distribution": "opencv-python",
            "version": _wheel_version(wheel),
        },
        "wheel_contents": wheel_contents,
        "ffmpeg_wheel_binding": ffmpeg_wheel_binding,
        "pe_inventory": pe_inventory,
        "probes": probes,
        "ffmpeg_preseed": ffmpeg_records,
    }
    return provenance, build_python


def _remove_clean_build_tree(path: Path, work_root: Path) -> None:
    if (
        path.is_symlink()
        or (hasattr(path, "is_junction") and path.is_junction())
        or not path.is_dir()
        or path.parent.resolve() != work_root.resolve()
    ):
        raise OpenCVWheelError(f"Unsafe OpenCV temporary build path: {path}")
    shutil.rmtree(path)


def run(
    source_dir: Path,
    output_dir: Path,
    components_file: Path,
    work_dir: Path,
) -> dict[str, Any]:
    """Build twice, require semantic equality, and seal the first wheel."""
    lock = _load_lock(components_file)
    policy = _policy(lock)
    _verify_inputs(source_dir, policy)
    if output_dir.exists() or output_dir.is_symlink():
        raise OpenCVWheelError(f"OpenCV output directory already exists: {output_dir}")
    if work_dir.exists() or work_dir.is_symlink():
        raise OpenCVWheelError(f"OpenCV work directory already exists: {work_dir}")
    work_dir.mkdir(parents=True)
    first_work = work_dir / "1"
    second_work = work_dir / "2"
    second_output = work_dir / "o2"
    succeeded = False
    try:
        print("OpenCV: starting clean source build 1/2", file=sys.stderr, flush=True)
        first, _first_python = _run_once(
            source_dir, output_dir, policy, first_work
        )
        (work_dir / "first-build-evidence.json").write_text(
            json.dumps(first, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        shutil.copyfile(first_work / "build.log", work_dir / "first-build.log")
        shutil.copytree(first_work / "evidence", work_dir / "first-build-diagnostics")
        _remove_clean_build_tree(first_work, work_dir)
        print("OpenCV: starting clean source build 2/2", file=sys.stderr, flush=True)
        second, second_python = _run_once(
            source_dir, second_output, policy, second_work
        )
        (work_dir / "second-build-evidence.json").write_text(
            json.dumps(second, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print("OpenCV: comparing the two verified source builds", file=sys.stderr, flush=True)
        first_semantic = _semantic_manifest(first)
        second_semantic = _semantic_manifest(second)
        if first_semantic != second_semantic:
            raise OpenCVWheelError(
                "Two clean OpenCV builds produced different semantic manifests"
            )
        if (
            first["build_environment"] != second["build_environment"]
            or first["observed_build_environment"]
            != second["observed_build_environment"]
            or first["inputs"] != second["inputs"]
            or first["ffmpeg_preseed"] != second["ffmpeg_preseed"]
            or first["version_py_sha256"] != second["version_py_sha256"]
        ):
            raise OpenCVWheelError(
                "Two clean OpenCV builds used different inputs or toolchains"
            )
        semantic_sha256 = _json_sha256(first_semantic)
        expected_semantic = policy["expected_semantic_manifest_sha256"]
        if expected_semantic is not None and semantic_sha256 != expected_semantic:
            raise OpenCVWheelError(
                "OpenCV semantic manifest SHA256 differs from the fixed policy"
            )
        first_wheel_sha256 = first["wheel"]["sha256"]
        second_wheel_sha256 = second["wheel"]["sha256"]
        byte_identical = first_wheel_sha256 == second_wheel_sha256
        expected_wheel = policy["expected_wheel_sha256"]
        if expected_wheel is not None and (
            not byte_identical or first_wheel_sha256 != expected_wheel
        ):
            raise OpenCVWheelError(
                "OpenCV wheel SHA256 differs from the fixed reproducible build"
            )
        expected_byte_identical = policy["expected_byte_identical"]
        if (
            expected_byte_identical is not None
            and byte_identical is not expected_byte_identical
        ):
            raise OpenCVWheelError(
                "OpenCV byte reproducibility result differs from the fixed policy"
            )
        first["semantic_manifest"] = first_semantic
        first["semantic_manifest_sha256"] = semantic_sha256
        first["repeatability"] = {
            "byte_identical": byte_identical,
            "first_wheel_sha256": first_wheel_sha256,
            "second_wheel_sha256": second_wheel_sha256,
            "semantic_equal": True,
            "semantic_manifest_sha256": semantic_sha256,
        }
        provenance_path = output_dir / PROVENANCE_NAME
        provenance_path.write_text(
            json.dumps(first, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        validate_output_directory(
            output_dir,
            components_file,
            audit_python=second_python,
        )
        succeeded = True
        return first
    finally:
        if succeeded and os.path.lexists(work_dir):
            _remove_clean_build_tree(work_dir, work_dir.parent)
        if not succeeded and os.path.lexists(output_dir):
            _remove_clean_build_tree(output_dir, output_dir.parent)
