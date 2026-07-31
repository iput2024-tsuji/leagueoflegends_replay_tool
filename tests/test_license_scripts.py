import json
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
from src.license_info import REQUIRED_DISTRIBUTION_DOCUMENTS


def _component_lock():
    return json.loads(
        license_collector.COMPONENTS_FILE.read_text(encoding="utf-8")
    )


def _write_distribution_materials(root: Path) -> None:
    lock = _component_lock()
    for relative_path in REQUIRED_DISTRIBUTION_DOCUMENTS:
        if relative_path == "licenses/python-packages.json":
            continue
        path = root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("test\n", encoding="utf-8")

    components_path = root / "licenses" / "components.json"
    components_path.parent.mkdir(parents=True, exist_ok=True)
    components_path.write_bytes(license_collector.COMPONENTS_FILE.read_bytes())

    packages = [
        {
            "component": "python",
            "name": "Python",
            "version": "3.14.2",
            "expected_license": lock["python"]["license"],
        }
    ]
    for section in ("runtime_components", "build_components"):
        for component in lock[section]:
            if not component.get("distribution"):
                continue
            packages.append(
                {
                    "component": component["component"],
                    "name": component["distribution"],
                    "version": component["version"],
                    "expected_license": component["license"],
                }
            )
    for package in packages:
        relative_license = (
            f"python-packages/{safe_component_name(package['name'])}/LICENSE"
        )
        path = root / "licenses" / relative_license
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("license\n", encoding="utf-8")
        package["observed_license"] = package["expected_license"]
        package["license_files"] = [relative_license]

    package_manifest = {
        "schema_version": 1,
        "build_python_version": "3.14.2",
        "release_python_version": lock["python"]["release_version"],
        "requirements_sha256": sha256_file(license_collector.RUNTIME_REQUIREMENTS),
        "component_lock_sha256": sha256_file(components_path),
        "packages": packages,
    }
    (root / "licenses" / "python-packages.json").write_text(
        json.dumps(package_manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _write_existing_inventory(root: Path) -> Path:
    manifest_path = root / compliance.MANIFEST_RELATIVE_PATH
    files = []
    for path in root.rglob("*"):
        if path.is_file() and path != manifest_path:
            files.append(
                {
                    "path": path.relative_to(root).as_posix(),
                    "size": path.stat().st_size,
                    "sha256": sha256_file(path),
                    "component": "test",
                }
            )
    manifest_path.write_text(
        json.dumps({"schema_version": 1, "files": files}, indent=2) + "\n",
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


def test_safe_component_and_relative_paths():
    assert safe_component_name("..") == "unknown"
    assert safe_component_name("PyQt6 sip") == "PyQt6-sip"
    assert safe_relative_path("demo.dist-info/LICENSE") == Path(
        "demo.dist-info/LICENSE"
    )
    for unsafe in ("../LICENSE", "C:/LICENSE", "/LICENSE", "bad:name/LICENSE"):
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
    metadata_license.write_text("MIT license", encoding="utf-8")

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
    with pytest.raises(RuntimeError, match="AUTHORS alone"):
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
                "packages": [
                    {
                        "name": "demo",
                        "license_files": ["../LICENSE", "python-packages/demo/AUTHORS"],
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
    executable.write_bytes(b"exe")
    native.parent.mkdir(parents=True)
    native.write_bytes(b"native")

    exe_source = tmp_path / "build.exe"
    native_source = tmp_path / "_demo.pyd"
    exe_source.write_bytes(b"source-exe")
    native_source.write_bytes(b"source-native")
    toc = tmp_path / "COLLECT-00.toc"
    toc.write_text(
        repr(
            (
                [
                    ("LoLReplayTool.exe", str(exe_source), "EXECUTABLE"),
                    ("aiohttp/_demo.pyd", str(native_source), "EXTENSION"),
                ],
            )
        ),
        encoding="utf-8",
    )
    owners = {
        compliance._path_key(exe_source): "lol-replay-tool",
        compliance._path_key(native_source): "aiohttp",
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

    native.write_bytes(b"tampered")
    assert any(
        "Manifest size differs" in error or "Manifest SHA256 differs" in error
        for error in validate_distribution(root)
    )


def test_unknown_toc_file_and_extra_dist_file_fail(monkeypatch, tmp_path):
    root = tmp_path / "distribution"
    _write_distribution_materials(root)
    executable = root / "LoLReplayTool.exe"
    executable.write_bytes(b"exe")
    (root / "extra.bin").write_bytes(b"extra")
    toc = tmp_path / "COLLECT-00.toc"
    toc.write_text(
        repr(([("LoLReplayTool.exe", str(tmp_path / "unknown"), "EXECUTABLE")],)),
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

    assert any("Release build Python must be 3.14.6" in error for error in errors)
    assert any("Release legal gate remains for qt" in error for error in errors)
    assert any("runtime download obs-studio" in error for error in errors)
