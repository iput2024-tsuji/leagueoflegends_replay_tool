import copy
import json
import os
import sys
import zipfile
from pathlib import Path

import pytest

from scripts import check_license_compliance as compliance, collect_licenses as license_collector
from scripts.check_license_compliance import (
    canonicalize_distribution_name,
    parse_collect_toc,
    sha256_file,
    validate_distribution,
    validate_package_manifest,
)
from scripts.collect_licenses import (
    is_license_file,
    parse_requirement_names,
    parse_requirement_pins,
    safe_component_name,
    safe_relative_path,
)


def _component_lock():
    return json.loads(
        license_collector.COMPONENTS_FILE.read_text(encoding="utf-8")
    )


def _write_distribution_materials(root: Path) -> None:
    if os.name != "nt":
        pytest.skip("Packaged license fixture requires the Windows release runtime")
    license_collector.collect_licenses(root / "licenses")


def _write_existing_inventory(root: Path) -> Path:
    manifest_path = root / compliance.MANIFEST_RELATIVE_PATH
    package_manifest = json.loads(
        (root / "licenses" / "python-packages.json").read_text(encoding="utf-8")
    )
    lock = _component_lock()
    base_library = root / "_internal" / "base_library.zip"
    if not base_library.exists():
        base_library.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(base_library, "w") as archive:
            archive.writestr("encodings/__init__.pyc", b"compiled")
    base_errors, base_summary = compliance._validate_base_library_archive(
        base_library,
        package_manifest,
    )
    assert base_errors == []
    assert base_summary is not None
    files = []
    for path in root.rglob("*"):
        if path.is_file() and path != manifest_path:
            files.append(
                {
                    "path": path.relative_to(root).as_posix(),
                    "size": path.stat().st_size,
                    "sha256": sha256_file(path),
                    "component": (
                        "license-materials"
                        if path.relative_to(root).as_posix().startswith("licenses/")
                        else "lol-replay-tool"
                    ),
                }
            )
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "statement": "Technical inventory; license texts control.",
                "build_python_version": package_manifest["build_python_version"],
                "release_python_version": lock["python"]["release_version"],
                "component_lock_sha256": sha256_file(
                    root / "licenses" / "components.json"
                ),
                "pyinstaller_collect_toc_sha256": "0" * 64,
                "python_base_library": base_summary,
                "files": files,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return manifest_path


def test_requirement_parser_requires_exact_pins(tmp_path):
    requirements = tmp_path / "requirements.txt"
    requirements.write_text(
        "# generated\n-r base.txt\nPyQt6==6.10.2\nobsws-python==1.8.0\n",
        encoding="utf-8",
    )

    assert parse_requirement_names(requirements) == ["PyQt6", "obsws-python"]
    assert parse_requirement_pins(requirements)["pyqt6"] == ("PyQt6", "6.10.2")

    requirements.write_text("obsws-python>=1.8\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="exact == pin"):
        parse_requirement_pins(requirements)


def test_is_license_file_rejects_code_and_authors_only():
    assert is_license_file(Path("package.dist-info/licenses/LICENSE.txt"))
    assert is_license_file(Path("package/COPYING"))
    assert not is_license_file(Path("packaging/licenses/_spdx.py"))
    assert not is_license_file(Path("package/AUTHORS"))
    assert not is_license_file(Path("package/module.py"))


def test_meaningful_license_file_rejects_placeholder(tmp_path):
    placeholder = tmp_path / "LICENSE"
    placeholder.write_text("license\n", encoding="utf-8")

    assert not license_collector.is_meaningful_license_file(placeholder)


def test_safe_component_and_relative_paths():
    assert safe_component_name("..") == "unknown"
    assert safe_component_name("PyQt6 sip") == "PyQt6-sip"
    assert safe_relative_path("demo.dist-info/LICENSE") == Path(
        "demo.dist-info/LICENSE"
    )
    for unsafe in (
        "../LICENSE",
        "C:/LICENSE",
        "/LICENSE",
        "bad:name/LICENSE",
        "CON/license.txt",
        "folder./LICENSE",
        "folder /LICENSE",
    ):
        with pytest.raises(RuntimeError, match="Unsafe"):
            safe_relative_path(unsafe)


def test_canonicalize_distribution_name_treats_separators_equally():
    assert canonicalize_distribution_name("PyQt6_sip") == "pyqt6-sip"
    assert canonicalize_distribution_name("PyQt6.sip") == "pyqt6-sip"


def test_collect_distribution_licenses_checks_version_and_is_idempotent(
    monkeypatch,
    tmp_path,
):
    metadata_license = tmp_path / "demo.dist-info" / "LICENSE"
    metadata_license.parent.mkdir(parents=True)
    metadata_license.write_text(
        "Permission is granted to use, copy, modify, and distribute this "
        "software. THE SOFTWARE IS PROVIDED AS IS WITHOUT WARRANTY.\n",
        encoding="utf-8",
    )

    class FakeDistribution:
        metadata = {"Name": "demo", "License-Expression": "MIT"}
        version = "1.0"
        files = [Path("demo.dist-info/LICENSE")]

        @staticmethod
        def locate_file(relative_path):
            return tmp_path / relative_path

    monkeypatch.setattr(
        license_collector.metadata,
        "distribution",
        lambda _distribution_name: FakeDistribution(),
    )
    destination = tmp_path / "licenses" / "python-packages"

    first = license_collector.collect_distribution_licenses(
        "demo",
        destination,
        expected_version="1.0",
        expected_license="MIT",
    )
    second = license_collector.collect_distribution_licenses(
        "demo",
        destination,
        expected_version="1.0",
        expected_license="MIT",
    )

    assert first["license_files"] == second["license_files"]
    assert second["license_files"] == [
        "python-packages/demo/demo.dist-info/LICENSE"
    ]
    with pytest.raises(RuntimeError, match="Installed version mismatch"):
        license_collector.collect_distribution_licenses(
            "demo",
            destination,
            expected_version="2.0",
        )


def test_collect_distribution_rejects_authors_only(monkeypatch, tmp_path):
    authors = tmp_path / "demo.dist-info" / "AUTHORS"
    authors.parent.mkdir(parents=True)
    authors.write_text("names", encoding="utf-8")

    class FakeDistribution:
        metadata = {"Name": "demo"}
        version = "1.0"
        files = [Path("demo.dist-info/AUTHORS")]

        @staticmethod
        def locate_file(relative_path):
            return tmp_path / relative_path

    monkeypatch.setattr(
        license_collector.metadata,
        "distribution",
        lambda _distribution_name: FakeDistribution(),
    )
    with pytest.raises(RuntimeError, match="not sufficient"):
        license_collector.collect_distribution_licenses(
            "demo",
            tmp_path / "output",
        )


def test_validate_package_manifest_rejects_traversal_and_authors_only(tmp_path):
    manifest_path = tmp_path / "licenses" / "python-packages.json"
    manifest_path.parent.mkdir()
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "build_python_version": "1.0",
                "packages": [
                    {
                        "name": "demo",
                        "license_files": ["../LICENSE", "python-packages/demo/AUTHORS"],
                        "substantive_license_files": [],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    authors = manifest_path.parent / "python-packages" / "demo" / "AUTHORS"
    authors.parent.mkdir(parents=True)
    authors.write_text("names", encoding="utf-8")

    errors = validate_package_manifest(manifest_path)

    assert any("Unsafe license path" in error for error in errors)
    assert any("AUTHORS-only" in error for error in errors)


def test_parse_collect_toc_maps_contents_directory_and_rejects_traversal(tmp_path):
    toc = tmp_path / "COLLECT-00.toc"
    toc.write_text(
        repr(
            (
                [
                    ("LoLReplayTool.exe", "build.exe", "EXECUTABLE"),
                    ("demo/data.txt", "source.txt", "DATA"),
                ],
            )
        ),
        encoding="utf-8",
    )

    entries = parse_collect_toc(toc)

    assert [entry["path"] for entry in entries] == [
        "LoLReplayTool.exe",
        "_internal/demo/data.txt",
    ]

    toc.write_text(repr(([("../escape.dll", "source", "BINARY")],)), encoding="utf-8")
    with pytest.raises(ValueError, match="Unsafe path"):
        parse_collect_toc(toc)


def test_toc_and_dist_are_bidirectionally_inventoried(monkeypatch, tmp_path):
    root = tmp_path / "distribution"
    _write_distribution_materials(root)
    executable = root / "LoLReplayTool.exe"
    native = root / "_internal" / "aiohttp" / "_demo.pyd"
    base_library = root / "_internal" / "base_library.zip"
    executable.write_bytes(b"exe")
    native.parent.mkdir(parents=True)
    native.write_bytes(b"native")
    with zipfile.ZipFile(base_library, "w") as archive:
        archive.writestr("encodings/__init__.pyc", b"compiled")

    exe_source = tmp_path / "build.exe"
    native_source = tmp_path / "_demo.pyd"
    base_source = tmp_path / "base_library.zip"
    exe_source.write_bytes(executable.read_bytes())
    native_source.write_bytes(native.read_bytes())
    base_source.write_bytes(base_library.read_bytes())
    toc = tmp_path / "COLLECT-00.toc"
    toc.write_text(
        repr(
            (
                [
                    ("LoLReplayTool.exe", str(exe_source), "EXECUTABLE"),
                    ("aiohttp/_demo.pyd", str(native_source), "EXTENSION"),
                    ("base_library.zip", str(base_source), "DATA"),
                ],
            )
        ),
        encoding="utf-8",
    )
    owners = {
        compliance._path_key(exe_source): "lol-replay-tool",
        compliance._path_key(native_source): "aiohttp",
        compliance._path_key(base_source): "python",
    }
    monkeypatch.setattr(
        compliance,
        "_distribution_source_owners",
        lambda _lock: (owners, []),
    )

    assert validate_distribution(
        root,
        toc_path=toc,
        write_manifest=True,
    ) == []
    manifest = json.loads(
        (root / compliance.MANIFEST_RELATIVE_PATH).read_text(encoding="utf-8")
    )
    by_path = {entry["path"]: entry for entry in manifest["files"]}
    assert by_path["_internal/aiohttp/_demo.pyd"]["component"] == "aiohttp"
    assert by_path["_internal/aiohttp/_demo.pyd"]["sha256"] == sha256_file(native)
    assert validate_distribution(root) == []

    native_source.write_bytes(b"tampered source")
    assert any(
        "TOC source differs from final distribution" in error
        for error in validate_distribution(root, toc_path=toc)
    )
    native_source.write_bytes(native.read_bytes())

    native.write_bytes(b"tampered")
    assert any(
        "Manifest size differs" in error or "Manifest SHA256 differs" in error
        for error in validate_distribution(root)
    )


def test_unknown_toc_file_and_extra_dist_file_fail(monkeypatch, tmp_path):
    root = tmp_path / "distribution"
    _write_distribution_materials(root)
    rogue = root / "_internal" / "rogue.pyd"
    rogue.parent.mkdir(parents=True)
    rogue.write_bytes(b"native")
    (root / "extra.bin").write_bytes(b"extra")
    toc = tmp_path / "COLLECT-00.toc"
    toc.write_text(
        repr(([("rogue.pyd", str(tmp_path / "unknown"), "EXTENSION")],)),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        compliance,
        "_distribution_source_owners",
        lambda _lock: ({}, []),
    )

    errors = validate_distribution(root, toc_path=toc)

    assert any("Unclassified packaged file" in error for error in errors)
    assert any("missing from TOC: extra.bin" in error for error in errors)


def test_existing_distribution_manifest_detects_missing_record(tmp_path):
    root = tmp_path / "distribution"
    _write_distribution_materials(root)
    manifest_path = _write_existing_inventory(root)

    assert validate_distribution(root) == []

    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["files"].pop()
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    assert any(
        "missing from manifest" in error for error in validate_distribution(root)
    )


def test_release_mode_enforces_python_and_legal_gates(tmp_path):
    root = tmp_path / "distribution"
    _write_distribution_materials(root)
    _write_existing_inventory(root)

    errors = validate_distribution(root, release=True)

    if sys.version.split()[0] != "3.14.6":
        assert any("Release build Python must be 3.14.6" in error for error in errors)
    assert any("requires the exact PyInstaller COLLECT TOC" in error for error in errors)
    assert any("gate remains for qt:" in error for error in errors)
    assert any("gate remains for obs-studio:" in error for error in errors)
    assert any("numpy: native_source_coverage_verified" in error for error in errors)
    assert "Release build provenance is missing." in errors


def test_locked_license_material_rejects_placeholder_even_with_updated_hash(tmp_path):
    root = tmp_path / "distribution"
    _write_distribution_materials(root)
    package_manifest_path = root / "licenses" / "python-packages.json"
    payload = json.loads(package_manifest_path.read_text(encoding="utf-8"))
    package = next(
        item for item in payload["packages"] if item["component"] == "aiohttp"
    )
    relative = package["substantive_license_files"][0]
    target = root / "licenses" / Path(relative)
    target.write_text("license\n", encoding="utf-8")
    package["license_file_sha256"][relative] = sha256_file(target)
    package_manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    errors = validate_distribution(root)

    assert any("placeholder" in error and "aiohttp" in error for error in errors)


def test_python_native_runtime_probe_detects_locked_hash_tampering():
    if os.name != "nt":
        pytest.skip("CPython Windows native runtime probe is Windows-specific")
    lock = _component_lock()
    observed = license_collector.probe_python_native_runtime(lock)
    assert observed["python_version"] == sys.version.split()[0]

    tampered = copy.deepcopy(lock)
    profile = tampered["python"]["windows_native_runtime_profiles"][
        sys.version.split()[0]
    ]
    artifact = next(
        artifact
        for component in profile["components"]
        for artifact in component.get("artifacts", [])
    )
    artifact["sha256"] = "0" * 64

    with pytest.raises(RuntimeError, match="native runtime artifact differs"):
        license_collector.probe_python_native_runtime(tampered)


def test_unreferenced_or_native_license_directory_file_fails(tmp_path):
    root = tmp_path / "distribution"
    _write_distribution_materials(root)
    (root / "licenses" / "unreferenced.txt").write_text(
        "not referenced",
        encoding="utf-8",
    )
    _write_existing_inventory(root)

    errors = validate_distribution(root)

    assert any("Unreferenced file in license directory" in error for error in errors)

    package_manifest_path = root / "licenses" / "python-packages.json"
    payload = json.loads(package_manifest_path.read_text(encoding="utf-8"))
    native = root / "licenses" / "python-packages" / "Python" / "payload.dll"
    native.write_bytes(b"not a license")
    payload["packages"][0]["license_files"].append(
        "python-packages/Python/payload.dll"
    )
    package_manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    errors = validate_package_manifest(package_manifest_path, _component_lock())

    assert any("Native file cannot be license material" in error for error in errors)


def test_distribution_manifest_rejects_schema_lock_and_component_tampering(tmp_path):
    root = tmp_path / "distribution"
    _write_distribution_materials(root)
    manifest_path = _write_existing_inventory(root)
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["schema_version"] = 99
    payload["component_lock_sha256"] = "f" * 64
    payload["files"][0]["component"] = "not-a-component"
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    errors = validate_distribution(root)

    assert any("manifest schema" in error for error in errors)
    assert any("component lock SHA256" in error for error in errors)
    assert any("component is invalid" in error for error in errors)


def test_packaged_project_document_must_match_repository(tmp_path):
    root = tmp_path / "distribution"
    _write_distribution_materials(root)
    _write_existing_inventory(root)
    (root / "LICENSE").write_text("not the GPL\n", encoding="utf-8")

    errors = validate_distribution(root)

    assert any("differs from repository: LICENSE" in error for error in errors)
    assert any("not the GNU GPL version 3" in error for error in errors)


def test_distribution_rejects_symlinked_material(tmp_path):
    root = tmp_path / "distribution"
    _write_distribution_materials(root)
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    link = root / "licenses" / "linked.txt"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("Creating symlinks is unavailable in this test environment")

    errors = validate_distribution(root)

    assert any("links and reparse points" in error.casefold() for error in errors)


def test_runtime_download_lock_must_match_setup_constants():
    lock = _component_lock()
    lock["runtime_downloads"][1]["archive_sha256"] = "0" * 64

    errors = compliance._validate_runtime_download_lock(lock)

    assert any("gyan-ffmpeg.archive_sha256" in error for error in errors)


def test_installer_build_self_check_has_an_explicit_timeout():
    script = Path("scripts/build_installer.ps1").read_text(encoding="utf-8")

    assert ".WaitForExit(60000)" in script
    assert ".Kill($true)" in script
    assert "-RedirectStandardOutput" in script
    assert "-Wait `" not in script


@pytest.mark.parametrize(
    ("path", "source_name", "owner", "expected"),
    [
        (
            "_internal/PyQt6/Qt6/bin/MSVCP140.dll",
            "MSVCP140.dll",
            "qt",
            "microsoft-vc-runtime",
        ),
        (
            "_internal/numpy.libs/msvcp140-a4c2229b.dll",
            "msvcp140-a4c2229b.dll",
            "numpy",
            "microsoft-vc-runtime",
        ),
        (
            "_internal/PyQt6/Qt6/bin/opengl32sw.dll",
            "opengl32sw.dll",
            "qt",
            "mesa-opengl32sw",
        ),
    ],
)
def test_native_runtime_overrides_wheel_owner(
    tmp_path,
    path,
    source_name,
    owner,
    expected,
):
    source = tmp_path / source_name
    source.write_bytes(b"native")

    assert (
        compliance._classify_toc_entry(
            {"path": path, "source": str(source), "toc_name": source_name},
            {compliance._path_key(source): owner},
        )
        == expected
    )


def test_python_runtime_classification_rejects_site_packages_rogue_file(
    monkeypatch,
    tmp_path,
):
    base = tmp_path / "Python314"
    rogue = base / "Lib" / "site-packages" / "rogue.dll"
    rogue.parent.mkdir(parents=True)
    rogue.write_bytes(b"rogue")
    monkeypatch.setattr(compliance.sys, "base_prefix", str(base))

    assert (
        compliance._classify_toc_entry(
            {
                "path": "_internal/rogue.dll",
                "source": str(rogue),
                "toc_name": "rogue.dll",
                "type": "BINARY",
            },
            {},
        )
        is None
    )


def test_python_runtime_classification_requires_exact_locked_source_and_destination(
    tmp_path,
):
    source = tmp_path / "Python314" / "DLLs" / "_asyncio.pyd"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"official core")
    locked_sources = {
        compliance._path_key(source): {
            "path": "DLLs/_asyncio.pyd",
            "size": source.stat().st_size,
            "sha256": sha256_file(source),
        }
    }
    entry = {
        "path": "_internal/_asyncio.pyd",
        "source": str(source),
        "toc_name": source.name,
        "type": "EXTENSION",
    }

    assert compliance._classify_toc_entry(entry, {}, {}) is None
    assert (
        compliance._classify_toc_entry(entry, {}, locked_sources) == "python"
    )
    assert (
        compliance._classify_toc_entry(
            {**entry, "path": "_internal/subdir/_asyncio.pyd"},
            {},
            locked_sources,
        )
        is None
    )
    rogue = source.with_name("_rogue.pyd")
    rogue.write_bytes(b"rogue")
    assert (
        compliance._classify_toc_entry(
            {**entry, "source": str(rogue), "toc_name": rogue.name},
            {},
            locked_sources,
        )
        is None
    )


def test_python_runtime_profile_rejects_python_dll_hash_mismatch(monkeypatch):
    if os.name != "nt":
        pytest.skip("Windows CPython runtime inventory is Windows-specific")
    lock = _component_lock()
    real_sha256_file = license_collector.sha256_file

    def mismatched_python_dll(path):
        if Path(path).name.casefold() == "python314.dll":
            return "0" * 64
        return real_sha256_file(path)

    monkeypatch.setattr(license_collector, "sha256_file", mismatched_python_dll)

    with pytest.raises(RuntimeError, match="Python core native runtime differs"):
        license_collector.probe_python_native_runtime(lock)


def test_base_library_members_require_verified_stdlib_sources(tmp_path):
    archive_path = tmp_path / "base_library.zip"
    package_manifest = {
        "python_native_runtime": {
            "stdlib_python_sources": {
                "inventory_sha256": "a" * 64,
                "artifacts": [
                    {
                        "path": "encodings/__init__.py",
                        "size": 1,
                        "sha256": "b" * 64,
                    }
                ],
            }
        }
    }
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("encodings/__init__.pyc", b"compiled")

    errors, summary = compliance._validate_base_library_archive(
        archive_path,
        package_manifest,
    )

    assert errors == []
    assert summary is not None
    assert summary["member_count"] == 1

    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("rogue.pyc", b"compiled")
    errors, _summary = compliance._validate_base_library_archive(
        archive_path,
        package_manifest,
    )
    assert any("no verified stdlib source" in error for error in errors)


def test_base_library_runtime_read_error_fails_closed(monkeypatch, tmp_path):
    archive_path = tmp_path / "base_library.zip"
    archive_path.write_bytes(b"not-empty")
    package_manifest = {
        "python_native_runtime": {
            "stdlib_python_sources": {
                "inventory_sha256": "a" * 64,
                "artifacts": [
                    {
                        "path": "encodings/__init__.py",
                        "size": 1,
                        "sha256": "b" * 64,
                    }
                ],
            }
        }
    }

    def reject_archive(_path):
        raise RuntimeError("encrypted member")

    monkeypatch.setattr(compliance.zipfile, "ZipFile", reject_archive)
    errors, summary = compliance._validate_base_library_archive(
        archive_path,
        package_manifest,
    )

    assert summary is None
    assert any("Cannot inspect base_library.zip" in error for error in errors)


def test_native_runtime_override_requires_owner_and_final_path(tmp_path):
    msvc = tmp_path / "VCRUNTIME140.dll"
    mesa = tmp_path / "opengl32sw.dll"
    msvc.write_bytes(b"msvc")
    mesa.write_bytes(b"mesa")

    assert (
        compliance._classify_toc_entry(
            {
                "path": "_internal/PyQt6/Qt6/bin/VCRUNTIME140.dll",
                "source": str(msvc),
                "toc_name": msvc.name,
                "type": "BINARY",
            },
            {compliance._path_key(msvc): "aiohttp"},
        )
        is None
    )
    assert (
        compliance._classify_toc_entry(
            {
                "path": "_internal/PyQt6/Qt6/bin/opengl32sw.dll",
                "source": str(mesa),
                "toc_name": mesa.name,
                "type": "BINARY",
            },
            {compliance._path_key(mesa): "numpy"},
        )
        is None
    )


def test_system_vcomp_requires_system32_source_and_exact_final_path(
    monkeypatch,
    tmp_path,
):
    system_root = tmp_path / "Windows"
    source = system_root / "System32" / "VCOMP140.DLL"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"system runtime")
    monkeypatch.setenv("SystemRoot", str(system_root))

    assert (
        compliance._classify_toc_entry(
            {
                "path": "_internal/VCOMP140.DLL",
                "source": str(source),
                "toc_name": source.name,
                "type": "BINARY",
            },
            {},
        )
        == "microsoft-vc-runtime"
    )
    assert (
        compliance._classify_toc_entry(
            {
                "path": "_internal/PyQt6/Qt6/bin/VCOMP140.DLL",
                "source": str(source),
                "toc_name": source.name,
                "type": "BINARY",
            },
            {},
        )
        is None
    )
