import hashlib
import io
import json
import tarfile
import zipfile
from pathlib import Path

import pytest

from scripts import prepare_opencv_wheel as target


def _archive(
    path: Path,
    root: str,
    files: dict[str, bytes],
    *,
    directories: tuple[str, ...] = (),
) -> None:
    with tarfile.open(path, "w:gz") as archive:
        directory = tarfile.TarInfo(root)
        directory.type = tarfile.DIRTYPE
        archive.addfile(directory)
        for name in directories:
            directory = tarfile.TarInfo(f"{root}/{name}")
            directory.type = tarfile.DIRTYPE
            archive.addfile(directory)
        for name, data in files.items():
            info = tarfile.TarInfo(f"{root}/{name}")
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))


def _wheel(
    path: Path,
    *,
    version: str = "4.13.0.90",
    pe_bytes: bytes = b"fake-pe",
    extra_name: str | None = None,
    extra_bytes: bytes = b"",
) -> None:
    metadata = (
        "Metadata-Version: 2.1\nName: opencv-python\n"
        f"Version: {version}\n"
    ).encode()

    def write(archive: zipfile.ZipFile, name: str, data: bytes | str) -> None:
        info = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
        archive.writestr(info, data)

    with zipfile.ZipFile(path, "w") as archive:
        write(archive, "cv2/cv2.pyd", pe_bytes)
        write(
            archive,
            "cv2/opencv_videoio_ffmpeg4130_64.dll",
            b"fake-ffmpeg",
        )
        if extra_name is not None:
            write(archive, extra_name, extra_bytes)
        write(archive, "opencv_python-4.13.0.90.dist-info/METADATA", metadata)
        write(
            archive,
            "opencv_python-4.13.0.90.dist-info/WHEEL",
            "Wheel-Version: 1.0\n",
        )


def _lock(tmp_path: Path) -> tuple[dict, Path, Path, Path]:
    source = tmp_path / "opencv-python.tar.gz"
    opencv = tmp_path / "opencv.tar.gz"
    thirdparty = tmp_path / "opencv-3rdparty.tar.gz"
    tool = tmp_path / "setuptools.whl"
    _archive(
        source,
        "opencv-python-root",
        {"setup.py": b"# setup"},
        directories=("opencv",),
    )
    _archive(opencv, "opencv-root", {"CMakeLists.txt": b"# cmake"})
    _archive(thirdparty, "opencv-3rdparty-root", {"ffmpeg": b"binary"})
    tool.write_bytes(b"build tool")

    def record(path: Path, role: str) -> dict:
        return {
            "filename": path.name,
            "url": "https://example.invalid/" + path.name,
            "size": path.stat().st_size,
            "sha256": target._sha256(path),
            "role": role,
        }

    policy = {
        "schema_version": 1,
        "component": "opencv-python",
        "recipe": "scripts/prepare_opencv_wheel.py",
        "python_version": "3.14.6",
        "platform": "win_amd64",
        "output_filename": "opencv_python-4.13.0.90-cp37-abi3-win_amd64.whl",
        "expected_byte_identical": None,
        "expected_wheel_sha256": None,
        "expected_semantic_manifest_sha256": None,
        "source_artifacts": [
            record(source, "opencv-python"),
            record(opencv, "opencv"),
            record(thirdparty, "opencv-3rdparty"),
        ],
        "build_artifacts": [
            {
                key: value
                for key, value in record(tool, "build-tool").items()
                if key != "role"
            }
        ],
        "build_environment": {
            "generator": "Visual Studio 17 2022",
            "msvc_toolset": target.REQUIRED_TOOLSET,
            "expected_msvc_toolset_version": target.REQUIRED_TOOLSET_VERSION,
            "windows_sdk": "10.0.26100.0",
            "cmake_version": "3.31.6",
            "cmake_build_parallel_level": "2",
            "build_packages": {
                "cmake": "3.31.6",
                "distro": "1.9.0",
                "numpy": "2.3.2",
                "packaging": "26.0",
                "pefile": "2024.8.26",
                "pip": "26.1.2",
                "scikit-build": "0.18.1",
                "setuptools": "81.0.0",
                "wheel": "0.46.1",
            },
            "cmake_args": list(target.REQUIRED_CMAKE_ARGS),
            "python_hash_seed": "0",
        },
    }
    lock_path = tmp_path / "components.json"
    lock_path.write_text(json.dumps({target.POLICY_KEY: policy}), encoding="utf-8")
    return policy, lock_path, source, opencv


def _prebuild_environment() -> dict:
    return {
        "cmake_version": "3.31.6",
        "build_packages": {
            "cmake": "3.31.6",
            "distro": "1.9.0",
            "numpy": "2.3.2",
            "packaging": "26.0",
            "pefile": "2024.8.26",
            "pip": "26.1.2",
            "scikit-build": "0.18.1",
            "setuptools": "81.0.0",
            "wheel": "0.46.1",
        },
        "python_version": "3.14.6",
    }


def _configured_toolchain() -> dict:
    return {
        "cmake_cache": {
            "CMAKE_GENERATOR": target.REQUIRED_GENERATOR,
            "CMAKE_GENERATOR_TOOLSET": target.REQUIRED_TOOLSET_NAME,
            "CMAKE_SYSTEM_VERSION": target.REQUIRED_WINDOWS_SDK,
            "WITH_IPP": "OFF",
            "BUILD_IPP_IW": "OFF",
            "BUILD_opencv_gapi": "OFF",
            "WITH_ADE": "OFF",
            "PYTHON3_LIMITED_API": "ON",
            "WITH_FFMPEG": "ON",
            "BUILD_SHARED_LIBS": "OFF",
            "BUILD_WITH_STATIC_CRT": "OFF",
        },
        "compiler": {
            "filename": "cl.exe",
            "msvc_toolset_version": "14.44.35207",
            "sha256": "a" * 64,
            "size": 1,
        },
        "c_compiler": {
            "filename": "cl.exe",
            "msvc_toolset_version": "14.44.35207",
            "sha256": "a" * 64,
            "size": 1,
        },
        "msbuild_project": {
            "path": "_skbuild/win-amd64-3.14/cmake-build/ALL_BUILD.vcxproj",
            "size": 1,
            "sha256": "b" * 64,
            "platform_toolsets": [target.REQUIRED_TOOLSET_NAME],
            "windows_target_platform_versions": [target.REQUIRED_WINDOWS_SDK],
        },
        "selected_msvc_toolset_version": "14.44.35207",
        "compile_projects": [
            {
                "path": "_skbuild/win-amd64-3.14/cmake-build/modules/python3/opencv_python3.vcxproj",
                "size": 1,
                "sha256": "c" * 64,
                "runtime_library": "MultiThreadedDLL",
            }
        ],
    }


def _probes() -> dict:
    return {
        "api": "ok",
        "build_information_sha256": "b" * 64,
        "ffmpeg": "enabled",
        "ffmpeg_build_information_lines": ["FFMPEG: YES (prebuilt binaries)"],
        "ipp": "disabled",
        "ipp_build_information_lines": [],
        "opencv_version": "4.13.0.90",
        "video_reader_backend": "FFMPEG",
        "video_writer_backend": "FFMPEG",
    }


def _ffmpeg_records() -> list[dict]:
    return [
        {
            "filename": filename,
            "md5": md5,
            "cache_path": f"ffmpeg/{md5}-{filename}",
            "size": len(b"fake-ffmpeg") if filename.endswith("_64.dll") else 1,
            "sha256": (
                hashlib.sha256(b"fake-ffmpeg").hexdigest()
                if filename.endswith("_64.dll")
                else hashlib.sha256(b"x").hexdigest()
            ),
        }
        for filename, md5 in target.EXPECTED_FFMPEG.items()
    ]


def _pe_inventory(wheel: Path) -> dict:
    contents = [
        item
        for item in target._wheel_contents(wheel)
        if str(item["path"]).endswith((".pyd", ".dll"))
    ]
    files = []
    for content in contents:
        imports = (
            [{"name": "VCRUNTIME140.dll", "type": "normal"}]
            if str(content["path"]).casefold().endswith("cv2.pyd")
            else []
        )
        files.append({**content, "imports": imports})
    runtime_reverse = {
        "vcruntime140.dll": [{"pe": "cv2/cv2.pyd", "import_type": "normal"}]
    }
    return {
        "schema_version": 2,
        "tool": {
            "name": "pe_runtime_audit",
            "pefile_version": "2024.8.26",
        },
        "files": files,
        "runtime_reverse": runtime_reverse,
        "summary": {
            "pe_files": len(contents),
            "import_count": 1,
            "runtime_import_count": 1,
            "app_local_runtime_files": [],
            "hashed_imports": [],
            "unknown_runtime_imports": [],
            "app_local_icu_files": [],
            "icu_imports": [],
        },
    }


def _mock_build_dependencies(monkeypatch) -> None:
    monkeypatch.setenv("ImageOS", "win22")
    monkeypatch.setenv("ImageVersion", "20260831.1")
    monkeypatch.setattr(
        target,
        "_validate_build_environment",
        lambda *args: _prebuild_environment(),
    )
    monkeypatch.setattr(
        target,
        "_prepare_build_venv",
        lambda work, artifacts: Path("python.exe"),
    )
    def fake_preseed(_root, destination):
        records = _ffmpeg_records()
        destination.mkdir(parents=True)
        (destination.parent / ".gitignore").write_bytes(
            target.DOWNLOAD_CACHE_GITIGNORE
        )
        for record in records:
            payload = (
                b"fake-ffmpeg"
                if record["filename"].endswith("_64.dll")
                else b"x"
            )
            (destination / Path(record["cache_path"]).name).write_bytes(payload)
        return records

    monkeypatch.setattr(target, "_preseed_ffmpeg", fake_preseed)
    def fake_probe(python, wheel, diagnostics_file=None):
        if diagnostics_file is not None:
            diagnostics_file.write_text("test build information", encoding="utf-8")
        return _probes()
    monkeypatch.setattr(target, "_probe_wheel", fake_probe)
    def fake_configured_toolchain(source):
        _write_msbuild_project(source, target.REQUIRED_WINDOWS_SDK)
        _write_dynamic_project(source, "modules/python3/opencv_python3.vcxproj")
        record = target._capture_dynamic_crt_projects(source)[0]
        result = _configured_toolchain()
        result["compile_projects"] = [record]
        return result
    monkeypatch.setattr(target, "_capture_configured_toolchain", fake_configured_toolchain)
    monkeypatch.setattr(
        target,
        "_pe_inventory",
        lambda wheel, work_dir, python=None: _pe_inventory(wheel),
    )


@pytest.mark.parametrize(
    ("disabled_flag", "enabled_flag"),
    [
        ("-DWITH_IPP=OFF", "-DWITH_IPP=ON"),
        ("-DWITH_ADE=OFF", "-DWITH_ADE=ON"),
        ("-DPYTHON3_LIMITED_API=ON", "-DPYTHON3_LIMITED_API=OFF"),
    ],
)
def test_policy_requires_exact_disabled_features(
    tmp_path, disabled_flag, enabled_flag
):
    _policy_data, lock_path, _source, _opencv = _lock(tmp_path)
    payload = json.loads(lock_path.read_text(encoding="utf-8"))
    cmake_args = payload[target.POLICY_KEY]["build_environment"]["cmake_args"]
    cmake_args[cmake_args.index(disabled_flag)] = enabled_flag
    lock_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(target.OpenCVWheelError, match="CMake flags"):
        target._load_lock(lock_path)


def test_configured_toolchain_rejects_ade_enabled(tmp_path, monkeypatch):
    cache = dict(_configured_toolchain()["cmake_cache"])
    cache["WITH_ADE"] = "ON"
    monkeypatch.setattr(target, "_read_cmake_cache", lambda source: cache)

    with pytest.raises(target.OpenCVWheelError, match="feature flags differ"):
        target._capture_configured_toolchain(tmp_path)


def test_configured_toolchain_rejects_limited_api_disabled(tmp_path, monkeypatch):
    cache = dict(_configured_toolchain()["cmake_cache"])
    cache["PYTHON3_LIMITED_API"] = "OFF"
    monkeypatch.setattr(target, "_read_cmake_cache", lambda source: cache)

    with pytest.raises(target.OpenCVWheelError, match="feature flags differ"):
        target._capture_configured_toolchain(tmp_path)


@pytest.mark.parametrize("key", ["BUILD_SHARED_LIBS", "BUILD_WITH_STATIC_CRT"])
def test_configured_toolchain_rejects_static_runtime_cache(tmp_path, key):
    _write_configured_toolchain(tmp_path)
    cache_path = next(tmp_path.glob("_skbuild/*/cmake-build/CMakeCache.txt"))
    text = cache_path.read_text(encoding="utf-8")
    cache_path.write_text(text.replace(f"{key}:INTERNAL=OFF", f"{key}:INTERNAL=ON"), encoding="utf-8")

    with pytest.raises(target.OpenCVWheelError, match="feature flags differ"):
        target._capture_configured_toolchain(tmp_path)


def _write_configured_toolchain(tmp_path: Path, *, selected: str | None = None) -> Path:
    build = tmp_path / "_skbuild" / "win-amd64-3.14" / "cmake-build"
    build.mkdir(parents=True)
    compiler = tmp_path / "MSVC" / "14.44.35207" / "bin" / "Hostx64" / "x64" / "cl.exe"
    compiler.parent.mkdir(parents=True)
    compiler.write_bytes(b"cl")
    cache = {
        "CMAKE_GENERATOR": target.REQUIRED_GENERATOR,
        "CMAKE_GENERATOR_TOOLSET": target.REQUIRED_TOOLSET_NAME,
        "CMAKE_SYSTEM_VERSION": target.REQUIRED_WINDOWS_SDK,
        "WITH_IPP": "OFF",
        "BUILD_IPP_IW": "OFF",
        "BUILD_opencv_gapi": "OFF",
        "WITH_ADE": "OFF",
        "PYTHON3_LIMITED_API": "ON",
        "WITH_FFMPEG": "ON",
        "BUILD_SHARED_LIBS": "OFF",
        "BUILD_WITH_STATIC_CRT": "OFF",
        "CMAKE_CXX_COMPILER": str(compiler),
        "CMAKE_C_COMPILER": str(compiler),
    }
    if selected is not None:
        cache["CMAKE_VS_PLATFORM_TOOLSET_VERSION"] = selected
    (build / "CMakeCache.txt").write_text(
        "\n".join(f"{key}:INTERNAL={value}" for key, value in cache.items()),
        encoding="utf-8",
    )
    _write_msbuild_project(tmp_path, target.REQUIRED_WINDOWS_SDK)
    _write_dynamic_project(tmp_path, "modules/python3/opencv_python3.vcxproj")
    return compiler


def test_configured_toolchain_uses_compiler_evidence_when_cache_version_missing(tmp_path):
    compiler = _write_configured_toolchain(tmp_path)

    observed = target._capture_configured_toolchain(tmp_path)

    assert observed["selected_msvc_toolset_version"] == "14.44.35207"
    assert observed["compiler"]["filename"] == "cl.exe"
    assert observed["compiler"]["size"] == compiler.stat().st_size
    assert observed["c_compiler"] == observed["compiler"]
    assert "compile_projects" in observed


def test_configured_toolchain_rejects_cache_version_mismatch(tmp_path):
    _write_configured_toolchain(tmp_path, selected="14.45")

    with pytest.raises(target.OpenCVWheelError, match="selected MSVC toolset differs"):
        target._capture_configured_toolchain(tmp_path)


@pytest.mark.parametrize("language", ["C", "CXX"])
def test_configured_toolchain_uses_generated_compiler_file_when_cache_missing(tmp_path, language):
    compiler = _write_configured_toolchain(tmp_path)
    cache_path = next(tmp_path.glob("_skbuild/*/cmake-build/CMakeCache.txt"))
    cache_path.write_text(
        "\n".join(
            line for line in cache_path.read_text(encoding="utf-8").splitlines()
            if not line.startswith(f"CMAKE_{language}_COMPILER:")
        ),
        encoding="utf-8",
    )
    generated = cache_path.parent / "CMakeFiles" / "3.31.6" / f"CMake{language}Compiler.cmake"
    generated.parent.mkdir(parents=True)
    generated.write_text(
        f'set(CMAKE_{language}_COMPILER "{compiler}")\n', encoding="utf-8"
    )

    observed = target._capture_configured_toolchain(tmp_path)

    assert observed["compiler"]["filename"] == "cl.exe"
    assert observed["c_compiler"] == observed["compiler"]


def test_configured_toolchain_rejects_different_c_compiler(tmp_path):
    compiler = _write_configured_toolchain(tmp_path)
    other = tmp_path / "other" / "MSVC" / "14.44.35207" / "bin" / "cl.exe"
    other.parent.mkdir(parents=True)
    other.write_bytes(b"different compiler")
    cache_path = next(tmp_path.glob("_skbuild/*/cmake-build/CMakeCache.txt"))
    cache_path.write_text(cache_path.read_text(encoding="utf-8").replace(
        f"CMAKE_C_COMPILER:INTERNAL={compiler}",
        f"CMAKE_C_COMPILER:INTERNAL={other}",
    ), encoding="utf-8")
    with pytest.raises(target.OpenCVWheelError, match=r"C and C\+\+ compilers differ"):
        target._capture_configured_toolchain(tmp_path)


def test_cmake_cache_keeps_colons_in_quoted_variable_names(tmp_path):
    cache_dir = tmp_path / "_skbuild" / "win-amd64-3.14" / "cmake-build"
    cache_dir.mkdir(parents=True)
    (cache_dir / "CMakeCache.txt").write_text(
        '\n'.join(
            [
                '"HAVE_CXX_ARCH:AVX":INTERNAL=1',
                '"HAVE_CXX_ARCH:AVX2":INTERNAL=1',
                "WITH_IPP:BOOL=OFF",
            ]
        ),
        encoding="utf-8",
    )

    assert target._read_cmake_cache(tmp_path) == {
        '"HAVE_CXX_ARCH:AVX"': "1",
        '"HAVE_CXX_ARCH:AVX2"': "1",
        "WITH_IPP": "OFF",
    }


def test_cmake_cache_rejects_duplicate_quoted_variable_names(tmp_path):
    cache_dir = tmp_path / "_skbuild" / "win-amd64-3.14" / "cmake-build"
    cache_dir.mkdir(parents=True)
    (cache_dir / "CMakeCache.txt").write_text(
        '"HAVE_CXX_ARCH:AVX2":INTERNAL=1\n'
        '"HAVE_CXX_ARCH:AVX2":INTERNAL=0\n',
        encoding="utf-8",
    )

    with pytest.raises(target.OpenCVWheelError, match="Duplicate OpenCV CMake"):
        target._read_cmake_cache(tmp_path)


def _write_msbuild_project(tmp_path: Path, sdk: str) -> None:
    project = (
        tmp_path
        / "_skbuild"
        / "win-amd64-3.14"
        / "cmake-build"
        / "ALL_BUILD.vcxproj"
    )
    project.parent.mkdir(parents=True, exist_ok=True)
    project.write_text(
        """<?xml version="1.0" encoding="utf-8"?>
<Project xmlns="http://schemas.microsoft.com/developer/msbuild/2003">
  <PropertyGroup Label="Globals">
    <WindowsTargetPlatformVersion>"""
        + sdk
        + """</WindowsTargetPlatformVersion>
  </PropertyGroup>
  <PropertyGroup Condition="'$(Configuration)'=='Debug'">
    <PlatformToolset>v143</PlatformToolset>
  </PropertyGroup>
  <PropertyGroup Condition="'$(Configuration)'=='Release'">
    <PlatformToolset>v143</PlatformToolset>
  </PropertyGroup>
</Project>
""",
        encoding="utf-8",
    )


def test_msbuild_project_records_selected_sdk_and_toolset(tmp_path):
    _write_msbuild_project(tmp_path, target.REQUIRED_WINDOWS_SDK)

    observed = target._capture_msbuild_project(tmp_path)

    assert observed["platform_toolsets"] == [target.REQUIRED_TOOLSET_NAME]
    assert observed["windows_target_platform_versions"] == [
        target.REQUIRED_WINDOWS_SDK
    ]
    assert observed["path"].endswith("/cmake-build/ALL_BUILD.vcxproj")
    assert len(observed["sha256"]) == 64


def test_msbuild_project_rejects_wrong_sdk(tmp_path):
    _write_msbuild_project(tmp_path, "10.0.22621.0")

    with pytest.raises(target.OpenCVWheelError, match="MSBuild project toolchain"):
        target._capture_msbuild_project(tmp_path)


def test_msbuild_project_rejects_missing_project(tmp_path):
    with pytest.raises(target.OpenCVWheelError, match="found 0"):
        target._capture_msbuild_project(tmp_path)


def test_msbuild_project_rejects_multiple_projects(tmp_path):
    _write_msbuild_project(tmp_path, target.REQUIRED_WINDOWS_SDK)
    first = next(tmp_path.glob("_skbuild/*/cmake-build/ALL_BUILD.vcxproj"))
    second = tmp_path / "_skbuild" / "other" / "cmake-build" / first.name
    second.parent.mkdir(parents=True)
    second.write_text(first.read_text(encoding="utf-8"), encoding="utf-8")

    with pytest.raises(target.OpenCVWheelError, match="found 2"):
        target._capture_msbuild_project(tmp_path)


def test_msbuild_project_rejects_invalid_xml(tmp_path):
    _write_msbuild_project(tmp_path, target.REQUIRED_WINDOWS_SDK)
    project = next(tmp_path.glob("_skbuild/*/cmake-build/ALL_BUILD.vcxproj"))
    project.write_text("<Project>", encoding="utf-8")

    with pytest.raises(target.OpenCVWheelError, match="Cannot inspect"):
        target._capture_msbuild_project(tmp_path)


def test_msbuild_project_rejects_wrong_toolset(tmp_path):
    _write_msbuild_project(tmp_path, target.REQUIRED_WINDOWS_SDK)
    project = next(tmp_path.glob("_skbuild/*/cmake-build/ALL_BUILD.vcxproj"))
    project.write_text(
        project.read_text(encoding="utf-8").replace("v143", "v142"),
        encoding="utf-8",
    )

    with pytest.raises(target.OpenCVWheelError, match="MSBuild project toolchain"):
        target._capture_msbuild_project(tmp_path)


def _write_dynamic_project(
    root: Path,
    relative: str,
    *,
    runtime: str = "MultiThreadedDLL",
    include: str | None = "modules/python3/opencv_python3.cpp",
    item_groups: int = 1,
    per_file_runtime: str | None = None,
    additional_options: str | None = None,
) -> Path:
    project = root / "_skbuild" / "win-amd64-3.14" / "cmake-build" / relative
    project.parent.mkdir(parents=True, exist_ok=True)
    item = ""
    if include is not None:
        override = (
            f"<RuntimeLibrary>{per_file_runtime}</RuntimeLibrary>"
            if per_file_runtime
            else ""
        )
        item = f"<ClCompile Include=\"{include}\">{override}</ClCompile>"
    groups = "".join(
        f"<ItemDefinitionGroup Condition=\"'$(Configuration)|$(Platform)'=='Release|x64'\"><ClCompile><RuntimeLibrary>{runtime}</RuntimeLibrary>{f'<AdditionalOptions>{additional_options}</AdditionalOptions>' if additional_options is not None else ''}</ClCompile></ItemDefinitionGroup>"
        for _ in range(item_groups)
    )
    project.write_text(
        "<Project xmlns=\"http://schemas.microsoft.com/developer/msbuild/2003\">"
        + groups
        + f"<ItemGroup>{item}</ItemGroup></Project>",
        encoding="utf-8",
    )
    return project


def test_dynamic_crt_projects_capture_compile_projects(tmp_path):
    _write_dynamic_project(tmp_path, "modules/python3/opencv_python3.vcxproj")
    _write_dynamic_project(tmp_path, "modules/core/opencv_core.vcxproj")

    records = target._capture_dynamic_crt_projects(tmp_path)

    assert {record["path"] for record in records} == {
        "_skbuild/win-amd64-3.14/cmake-build/modules/python3/opencv_python3.vcxproj",
        "_skbuild/win-amd64-3.14/cmake-build/modules/core/opencv_core.vcxproj",
    }
    assert all(record["runtime_library"] == "MultiThreadedDLL" for record in records)
    assert all(len(record["sha256"]) == 64 for record in records)


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"runtime": "MultiThreaded"}, "RuntimeLibrary"),
        ({"include": None}, "ClCompile"),
        ({"item_groups": 2}, "RuntimeLibrary"),
        ({"per_file_runtime": "MultiThreaded"}, "RuntimeLibrary"),
    ],
)
def test_dynamic_crt_projects_reject_invalid_runtime_configuration(tmp_path, kwargs, match):
    _write_dynamic_project(
        tmp_path, "modules/python3/opencv_python3.vcxproj", **kwargs
    )
    with pytest.raises(target.OpenCVWheelError, match=match):
        target._capture_dynamic_crt_projects(tmp_path)


def test_dynamic_crt_projects_skip_utility_and_compiler_probe_projects(tmp_path):
    _write_dynamic_project(tmp_path, "modules/python3/opencv_python3.vcxproj")
    _write_dynamic_project(tmp_path, "ALL_BUILD.vcxproj", include=None)
    _write_dynamic_project(tmp_path, "ZERO_CHECK.vcxproj", include=None)
    _write_dynamic_project(tmp_path, "CMakeFiles/3.31.6/CompilerProbe.vcxproj")

    records = target._capture_dynamic_crt_projects(tmp_path)
    assert [record["path"] for record in records] == [
        "_skbuild/win-amd64-3.14/cmake-build/modules/python3/opencv_python3.vcxproj"
    ]


def test_dynamic_crt_projects_requires_python3_project(tmp_path):
    _write_dynamic_project(tmp_path, "modules/core/opencv_core.vcxproj")
    with pytest.raises(target.OpenCVWheelError, match="python3"):
        target._capture_dynamic_crt_projects(tmp_path)


@pytest.mark.parametrize("options", ["/MT", "/MTd", "-MT", '"/MT"'])
def test_dynamic_crt_projects_reject_static_crt_additional_options(tmp_path, options):
    _write_dynamic_project(
        tmp_path,
        "modules/python3/opencv_python3.vcxproj",
        additional_options=options,
    )
    with pytest.raises(target.OpenCVWheelError, match="static CRT|/MT"):
        target._capture_dynamic_crt_projects(tmp_path)


def test_msbuild_project_rejects_multiple_sdk_values(tmp_path):
    _write_msbuild_project(tmp_path, target.REQUIRED_WINDOWS_SDK)
    project = next(tmp_path.glob("_skbuild/*/cmake-build/ALL_BUILD.vcxproj"))
    marker = "</WindowsTargetPlatformVersion>"
    extra = (
        marker
        + "\n    <WindowsTargetPlatformVersion>10.0.22621.0"
        + marker
    )
    project.write_text(
        project.read_text(encoding="utf-8").replace(marker, extra, 1),
        encoding="utf-8",
    )

    with pytest.raises(target.OpenCVWheelError, match="MSBuild project toolchain"):
        target._capture_msbuild_project(tmp_path)


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        ("14.44", False),
        ("14.44.35207", True),
        ("14.4", False),
        ("14.45", False),
        ("", False),
    ],
)
def test_toolset_version_requires_exact_locked_version(version, expected):
    assert target._is_required_toolset_version(version) is expected


def test_input_hash_mismatch_fails_closed(tmp_path):
    _policy_data, lock_path, source, _opencv = _lock(tmp_path)
    source.write_bytes(source.read_bytes() + b"tampered")
    with pytest.raises(target.OpenCVWheelError, match="hash or size mismatch"):
        target._verify_inputs(tmp_path, target._policy(json.loads(lock_path.read_text())))


def test_archive_path_traversal_is_rejected(tmp_path):
    archive = tmp_path / "bad.tar.gz"
    with tarfile.open(archive, "w:gz") as stream:
        info = tarfile.TarInfo("root/../../escape.txt")
        info.size = 1
        stream.addfile(info, io.BytesIO(b"x"))
    destination = tmp_path / "out"
    destination.mkdir()
    with pytest.raises(target.OpenCVWheelError, match="Unsafe source archive"):
        target._extract_archive(archive, destination)


def test_nonempty_wrapper_submodule_path_is_rejected(tmp_path):
    policy, _lock_path, source, _opencv = _lock(tmp_path)
    _archive(
        source,
        "opencv-python-root",
        {
            "setup.py": b"# setup",
            "opencv/unexpected.txt": b"not a submodule placeholder",
        },
    )
    source_record = next(
        item for item in policy["source_artifacts"] if item["role"] == "opencv-python"
    )
    source_record["size"] = source.stat().st_size
    source_record["sha256"] = target._sha256(source)

    with pytest.raises(target.OpenCVWheelError, match="non-empty unexpected"):
        target._compose_source_tree(tmp_path, policy, tmp_path / "work")


def test_composed_source_tree_uses_short_active_paths(tmp_path):
    policy, _lock_path, _source, _opencv = _lock(tmp_path)
    work = tmp_path / "work"

    python_root, thirdparty_root = target._compose_source_tree(
        tmp_path,
        policy,
        work,
    )

    assert python_root == work / "p"
    assert thirdparty_root == work / "t"
    assert (python_root / "opencv" / "CMakeLists.txt").read_bytes() == b"# cmake"
    assert not (work / "o").exists()
    assert not (work / "x").exists()


def test_unexpected_build_download_is_rejected(tmp_path):
    download_path = tmp_path / "download"
    ffmpeg_path = download_path / "ffmpeg"
    ffmpeg_path.mkdir(parents=True)
    records = _ffmpeg_records()
    for record in records:
        payload = (
            b"fake-ffmpeg"
            if record["filename"].endswith("_64.dll")
            else b"x"
        )
        (ffmpeg_path / Path(record["cache_path"]).name).write_bytes(payload)
    (download_path / "unexpected.bin").write_bytes(b"network payload")

    with pytest.raises(target.OpenCVWheelError, match="Unexpected OpenCV build download"):
        target._verify_download_cache(download_path, records)


def test_download_cache_marker_content_is_fixed(tmp_path):
    download_path = tmp_path / "download"
    ffmpeg_path = download_path / "ffmpeg"
    ffmpeg_path.mkdir(parents=True)
    records = _ffmpeg_records()
    for record in records:
        payload = (
            b"fake-ffmpeg"
            if record["filename"].endswith("_64.dll")
            else b"x"
        )
        (ffmpeg_path / Path(record["cache_path"]).name).write_bytes(payload)
    (download_path / ".gitignore").write_bytes(b"tampered\n")

    with pytest.raises(target.OpenCVWheelError, match="cache differs"):
        target._verify_download_cache(download_path, records)


def test_run_builds_composed_tree_and_records_provenance(tmp_path, monkeypatch):
    _policy_data, lock_path, _source, _opencv = _lock(tmp_path)
    output = tmp_path / "output"
    work = tmp_path / "work"

    _mock_build_dependencies(monkeypatch)
    monkeypatch.setenv("PYTHONHASHSEED", "random")

    def fake_run(command, *, cwd, env, check, capture_output, text):
        assert command[1:5] == [
            "setup.py",
            "bdist_wheel",
            "--py-limited-api=cp37",
            "--dist-dir",
        ]
        assert env["CMAKE_ARGS"] == (
            "-DWITH_IPP=OFF -DBUILD_IPP_IW=OFF -DBUILD_opencv_gapi=OFF "
            "-DWITH_ADE=OFF -DPYTHON3_LIMITED_API=ON "
            "-DCMAKE_SYSTEM_VERSION=10.0.26100.0 "
            "-DBUILD_WITH_STATIC_CRT=OFF"
        )
        assert env["CMAKE_GENERATOR_TOOLSET"] == target.REQUIRED_TOOLSET
        assert env["PYTHONHASHSEED"] == "0"
        assert all(
            flag not in env
            for flag in ("CL", "_CL_", "CFLAGS", "CXXFLAGS", "CPPFLAGS", "LDFLAGS")
        )
        assert "SKBUILD_CONFIGURE_OPTIONS" not in env
        assert (cwd / "opencv" / "CMakeLists.txt").is_file()
        assert (cwd / "cv2" / "version.py").read_bytes() == target.VERSION_PY_BYTES
        wheel_dir = Path(command[-1])
        _wheel(wheel_dir / "opencv_python-4.13.0.90-cp37-abi3-win_amd64.whl")
        return type("Completed", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(target.subprocess, "run", fake_run)
    provenance = target.run(tmp_path, output, lock_path, work)
    assert provenance["wheel"]["filename"].startswith("opencv_python-")
    assert provenance["command"] == list(target.BUILD_COMMAND)
    assert (output / target.PROVENANCE_NAME).is_file()
    assert provenance["repeatability"]["byte_identical"] is True
    assert provenance["repeatability"]["semantic_equal"] is True
    assert provenance["observed_build_environment"]["python_hash_seed"] == "0"
    assert target.validate_output_directory(output, lock_path)["version"] == "4.13.0.90"
    assert not work.exists()


def test_required_cmake_args_pin_dynamic_crt_and_static_libraries():
    assert "-DBUILD_WITH_STATIC_CRT=OFF" in target.REQUIRED_CMAKE_ARGS
    assert len(target.REQUIRED_CMAKE_ARGS) == len(set(target.REQUIRED_CMAKE_ARGS))


@pytest.mark.parametrize("name", ["CL", "_CL_", "CFLAGS", "CXXFLAGS", "CPPFLAGS", "LDFLAGS"])
def test_run_rejects_inherited_compiler_flags_before_build(tmp_path, monkeypatch, name):
    policy, _lock_path, _source, _opencv = _lock(tmp_path)
    monkeypatch.setenv(name, " /DTEST ")
    monkeypatch.setattr(target.subprocess, "run", lambda *args, **kwargs: pytest.fail("build started"))
    with pytest.raises(target.OpenCVWheelError, match="inherited compiler flags"):
        target._run_once(tmp_path, tmp_path / "output", policy, tmp_path / "work")


@pytest.mark.parametrize("value", [None, "random", 0])
def test_policy_requires_python_hash_seed_zero(tmp_path, value):
    _policy_data, lock_path, _source, _opencv = _lock(tmp_path)
    payload = json.loads(lock_path.read_text(encoding="utf-8"))
    environment = payload[target.POLICY_KEY]["build_environment"]
    if value is None:
        del environment["python_hash_seed"]
    else:
        environment["python_hash_seed"] = value
    lock_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(target.OpenCVWheelError, match="build environment|hash seed|toolchain"):
        target._load_lock(lock_path)


@pytest.mark.parametrize("fail_call", [1, 2])
def test_run_build_failure_removes_unsealed_output_and_work(
    tmp_path,
    monkeypatch,
    fail_call,
):
    _policy_data, lock_path, _source, _opencv = _lock(tmp_path)
    output = tmp_path / "output"
    work = tmp_path / "work"

    _mock_build_dependencies(monkeypatch)
    calls = 0

    def fake_run(command, *, cwd, env, check, capture_output, text):
        nonlocal calls
        calls += 1
        if calls == fail_call:
            return type(
                "Completed",
                (),
                {"returncode": 1, "stdout": "out", "stderr": "build failed"},
            )()
        wheel_dir = Path(command[-1])
        _wheel(wheel_dir / "opencv_python-4.13.0.90-cp37-abi3-win_amd64.whl")
        return type(
            "Completed",
            (),
            {"returncode": 0, "stdout": "", "stderr": ""},
        )()

    monkeypatch.setattr(target.subprocess, "run", fake_run)
    with pytest.raises(target.OpenCVWheelError, match="build failed"):
        target.run(tmp_path, output, lock_path, work)
    assert not output.exists()
    assert (work / str(fail_call) / "build.log").read_text() == "out\nbuild failed"
    if fail_call == 2:
        assert (work / "first-build-evidence.json").is_file()


def test_formal_build_requires_runner_image_identity(tmp_path, monkeypatch):
    _policy_data, lock_path, _source, _opencv = _lock(tmp_path)
    _mock_build_dependencies(monkeypatch)
    monkeypatch.delenv("ImageOS")
    monkeypatch.delenv("ImageVersion")

    with pytest.raises(target.OpenCVWheelError, match="runner image identity"):
        target.run(
            tmp_path,
            tmp_path / "output",
            lock_path,
            tmp_path / "work",
        )
    assert not (tmp_path / "output").exists()
    assert (tmp_path / "work").is_dir()
    assert not (tmp_path / "work" / "first-build-evidence.json").exists()


def test_output_ipp_marker_is_rejected(tmp_path, monkeypatch):
    _policy_data, lock_path, _source, _opencv = _lock(tmp_path)
    output = tmp_path / "output"
    output.mkdir()
    wheel = output / "opencv_python-4.13.0.90-cp37-abi3-win_amd64.whl"
    _wheel(wheel, extra_name="cv2/ippicv.dll", extra_bytes=b"binary")
    with pytest.raises(target.OpenCVWheelError, match="IPP artifact"):
        target._reject_ipp(wheel)


def test_output_wheel_rejects_non_limited_api_tag(tmp_path):
    output = tmp_path / "output"
    output.mkdir()
    _wheel(output / "opencv_python-4.13.0.90-cp314-cp314-win_amd64.whl")

    with pytest.raises(target.OpenCVWheelError, match="identity differs"):
        target._output_wheel(
            output,
            "opencv_python-4.13.0.90-cp37-abi3-win_amd64.whl",
        )


def test_output_provenance_tampering_is_rejected(tmp_path, monkeypatch):
    _policy_data, lock_path, _source, _opencv = _lock(tmp_path)
    output = tmp_path / "output"
    work = tmp_path / "work"

    _mock_build_dependencies(monkeypatch)

    def fake_run(command, *, cwd, env, check, capture_output, text):
        wheel_dir = Path(command[-1])
        _wheel(wheel_dir / "opencv_python-4.13.0.90-cp37-abi3-win_amd64.whl")
        return type("Completed", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(target.subprocess, "run", fake_run)
    target.run(tmp_path, output, lock_path, work)
    provenance = json.loads((output / target.PROVENANCE_NAME).read_text())
    provenance["wheel"]["sha256"] = "0" * 64
    (output / target.PROVENANCE_NAME).write_text(json.dumps(provenance), encoding="utf-8")
    with pytest.raises(target.OpenCVWheelError, match="provenance differs"):
        target.validate_output_directory(output, lock_path)


@pytest.mark.parametrize("field", ["python_hash_seed", "compile_projects"])
def test_output_observed_environment_tampering_is_rejected(tmp_path, monkeypatch, field):
    _policy_data, lock_path, _source, _opencv = _lock(tmp_path)
    output = tmp_path / "output"
    _mock_build_dependencies(monkeypatch)

    def fake_run(command, *, cwd, env, check, capture_output, text):
        wheel_dir = Path(command[-1])
        _wheel(wheel_dir / "opencv_python-4.13.0.90-cp37-abi3-win_amd64.whl")
        return type("Completed", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(target.subprocess, "run", fake_run)
    target.run(tmp_path, output, lock_path, tmp_path / "work")
    provenance_path = output / target.PROVENANCE_NAME
    provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    observed = provenance["observed_build_environment"]
    observed[field] = "random" if field == "python_hash_seed" else []
    provenance_path.write_text(json.dumps(provenance), encoding="utf-8")

    with pytest.raises(target.OpenCVWheelError, match="provenance|environment|toolchain|hash seed"):
        target.validate_output_directory(output, lock_path)


def test_output_msbuild_project_path_tampering_is_rejected(tmp_path, monkeypatch):
    _policy_data, lock_path, _source, _opencv = _lock(tmp_path)
    output = tmp_path / "output"
    _mock_build_dependencies(monkeypatch)

    def fake_run(command, *, cwd, env, check, capture_output, text):
        wheel_dir = Path(command[-1])
        _wheel(wheel_dir / "opencv_python-4.13.0.90-cp37-abi3-win_amd64.whl")
        return type("Completed", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(target.subprocess, "run", fake_run)
    target.run(tmp_path, output, lock_path, tmp_path / "work")
    provenance_path = output / target.PROVENANCE_NAME
    provenance = json.loads(provenance_path.read_text())
    provenance["observed_build_environment"]["configured_toolchain"][
        "msbuild_project"
    ]["path"] = "../cmake-build/ALL_BUILD.vcxproj"
    provenance_path.write_text(json.dumps(provenance), encoding="utf-8")

    with pytest.raises(target.OpenCVWheelError, match="MSBuild project provenance"):
        target.validate_output_directory(output, lock_path)


def test_output_pe_inventory_tampering_is_rejected(tmp_path, monkeypatch):
    _policy_data, lock_path, _source, _opencv = _lock(tmp_path)
    output = tmp_path / "output"

    _mock_build_dependencies(monkeypatch)

    def fake_run(command, *, cwd, env, check, capture_output, text):
        wheel_dir = Path(command[-1])
        _wheel(wheel_dir / "opencv_python-4.13.0.90-cp37-abi3-win_amd64.whl")
        return type("Completed", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(target.subprocess, "run", fake_run)
    target.run(tmp_path, output, lock_path, tmp_path / "work")
    provenance_path = output / target.PROVENANCE_NAME
    provenance = json.loads(provenance_path.read_text())
    pe_path = provenance["pe_inventory"]["files"][0]["path"]
    provenance["pe_inventory"]["files"][0]["imports"].append(
        {"name": "MSVCP140.dll", "type": "normal"}
    )
    provenance["pe_inventory"]["runtime_reverse"] = {
        "msvcp140.dll": [{"pe": pe_path, "import_type": "normal"}]
    }
    provenance["pe_inventory"]["summary"]["import_count"] += 1
    provenance["pe_inventory"]["summary"]["runtime_import_count"] += 1
    provenance_path.write_text(json.dumps(provenance), encoding="utf-8")

    with pytest.raises(target.OpenCVWheelError, match="PE inventory differs"):
        target.validate_output_directory(output, lock_path)


def test_duplicate_pe_inventory_path_is_rejected(tmp_path):
    _policy_data, _lock_path, _source, _opencv = _lock(tmp_path)
    wheel = tmp_path / "opencv_python-4.13.0.90-cp37-abi3-win_amd64.whl"
    _wheel(wheel)
    contents = target._wheel_contents(wheel)
    inventory = _pe_inventory(wheel)
    inventory["files"].append(dict(inventory["files"][0]))

    with pytest.raises(target.OpenCVWheelError, match="duplicate paths"):
        target._validate_pe_inventory(inventory, contents, "2024.8.26")


def test_cv2_requires_normal_vcruntime_or_msvcp_import(tmp_path):
    wheel = tmp_path / "opencv_python-4.13.0.90-cp37-abi3-win_amd64.whl"
    _wheel(wheel)
    contents = target._wheel_contents(wheel)
    inventory = _pe_inventory(wheel)
    cv2_record = next(item for item in inventory["files"] if item["path"] == "cv2/cv2.pyd")
    cv2_record["imports"] = []
    inventory["runtime_reverse"] = {}
    inventory["summary"]["import_count"] = 0
    inventory["summary"]["runtime_import_count"] = 0

    with pytest.raises(target.OpenCVWheelError, match="dynamic CRT|runtime"):
        target._validate_pe_inventory(inventory, contents, "2024.8.26")


def test_nonempty_ipp_build_information_is_rejected(tmp_path, monkeypatch):
    _policy_data, lock_path, _source, _opencv = _lock(tmp_path)
    output = tmp_path / "output"

    _mock_build_dependencies(monkeypatch)
    probes = _probes()
    probes["ipp_build_information_lines"] = ["Intel IPP: disabled"]
    monkeypatch.setattr(
        target, "_probe_wheel", lambda python, wheel, diagnostics_file=None: probes
    )

    def fake_run(command, *, cwd, env, check, capture_output, text):
        wheel_dir = Path(command[-1])
        _wheel(wheel_dir / "opencv_python-4.13.0.90-cp37-abi3-win_amd64.whl")
        return type("Completed", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(target.subprocess, "run", fake_run)
    with pytest.raises(target.OpenCVWheelError, match="native probes"):
        target.run(tmp_path, output, lock_path, tmp_path / "work")
    assert not output.exists()


def test_unexpected_ffmpeg_build_information_is_rejected(tmp_path, monkeypatch):
    _policy_data, lock_path, _source, _opencv = _lock(tmp_path)
    output = tmp_path / "output"

    _mock_build_dependencies(monkeypatch)
    probes = _probes()
    probes["ffmpeg_build_information_lines"] = ["FFMPEG: YES"]
    monkeypatch.setattr(
        target, "_probe_wheel", lambda python, wheel, diagnostics_file=None: probes
    )

    def fake_run(command, *, cwd, env, check, capture_output, text):
        wheel_dir = Path(command[-1])
        _wheel(wheel_dir / "opencv_python-4.13.0.90-cp37-abi3-win_amd64.whl")
        return type("Completed", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(target.subprocess, "run", fake_run)
    with pytest.raises(target.OpenCVWheelError, match="native probes"):
        target.run(tmp_path, output, lock_path, tmp_path / "work")
    assert not output.exists()


def test_embedded_provenance_wrapper_is_sealed(tmp_path, monkeypatch):
    _policy_data, lock_path, _source, _opencv = _lock(tmp_path)
    output = tmp_path / "output"
    _mock_build_dependencies(monkeypatch)

    def fake_run(command, *, cwd, env, check, capture_output, text):
        wheel_dir = Path(command[-1])
        _wheel(wheel_dir / "opencv_python-4.13.0.90-cp37-abi3-win_amd64.whl")
        return type(
            "Completed", (), {"returncode": 0, "stdout": "", "stderr": ""}
        )()

    monkeypatch.setattr(target.subprocess, "run", fake_run)
    payload = target.run(tmp_path, output, lock_path, tmp_path / "work")
    canonical = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    wrapper = {
        "provenance_sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        "provenance": payload,
    }
    assert target.validate_embedded_provenance_record(wrapper, lock_path) == payload
    wrapper["provenance_sha256"] = "0" * 64
    with pytest.raises(target.OpenCVWheelError, match="wrapper SHA256 differs"):
        target.validate_embedded_provenance_record(wrapper, lock_path)


def test_two_clean_builds_must_have_same_semantics(tmp_path, monkeypatch):
    _policy_data, lock_path, _source, _opencv = _lock(tmp_path)
    _mock_build_dependencies(monkeypatch)
    calls = 0

    def fake_run(command, *, cwd, env, check, capture_output, text):
        nonlocal calls
        calls += 1
        wheel_dir = Path(command[-1])
        extra_name = "cv2/generated-config.py" if calls == 2 else None
        _wheel(
            wheel_dir / "opencv_python-4.13.0.90-cp37-abi3-win_amd64.whl",
            extra_name=extra_name,
            extra_bytes=b"different",
        )
        return type(
            "Completed", (), {"returncode": 0, "stdout": "", "stderr": ""}
        )()

    monkeypatch.setattr(target.subprocess, "run", fake_run)
    with pytest.raises(target.OpenCVWheelError, match="semantic manifests"):
        target.run(
            tmp_path,
            tmp_path / "output",
            lock_path,
            tmp_path / "work",
        )
    assert not (tmp_path / "output").exists()
    assert (tmp_path / "work" / "first-build-evidence.json").is_file()
    assert (tmp_path / "work" / "second-build-evidence.json").is_file()
    assert next((tmp_path / "work" / "first-build-diagnostics").glob("*.whl")).is_file()
    assert next((tmp_path / "work" / "2" / "evidence").glob("*.whl")).is_file()


def test_two_clean_builds_must_have_identical_pe_payloads(tmp_path, monkeypatch):
    _policy_data, lock_path, _source, _opencv = _lock(tmp_path)
    _mock_build_dependencies(monkeypatch)
    calls = 0

    def fake_run(command, *, cwd, env, check, capture_output, text):
        nonlocal calls
        calls += 1
        wheel_dir = Path(command[-1])
        _wheel(
            wheel_dir / "opencv_python-4.13.0.90-cp37-abi3-win_amd64.whl",
            pe_bytes=b"second-pe" if calls == 2 else b"first-pe",
        )
        return type(
            "Completed", (), {"returncode": 0, "stdout": "", "stderr": ""}
        )()

    monkeypatch.setattr(target.subprocess, "run", fake_run)
    with pytest.raises(target.OpenCVWheelError, match="semantic manifests"):
        target.run(
            tmp_path,
            tmp_path / "output",
            lock_path,
            tmp_path / "work",
        )
    assert not (tmp_path / "output").exists()
    assert (tmp_path / "work" / "first-build-evidence.json").is_file()
    assert (tmp_path / "work" / "second-build-evidence.json").is_file()
    assert next((tmp_path / "work" / "first-build-diagnostics").glob("*.whl")).is_file()
    assert next((tmp_path / "work" / "2" / "evidence").glob("*.whl")).is_file()


def test_missing_source_build_policy_is_not_an_error():
    assert target.source_build_policy({}) is None
