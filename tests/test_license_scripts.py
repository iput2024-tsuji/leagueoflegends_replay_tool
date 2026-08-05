import ast
import copy
import hashlib
import importlib.util
import json
import marshal
import os
import shutil
import struct
import subprocess
import sys
import sysconfig
import types
import zipfile
import zlib
from importlib import metadata
from pathlib import Path

import pytest

from scripts import (
    check_license_compliance as compliance,
    collect_licenses as license_collector,
    installer_content_audit,
    pyinstaller_runtime_policy as runtime_policy,
)
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


def _stdlib_root() -> Path:
    return Path(sysconfig.get_path("stdlib"))


def _stdlib_source_record(relative: str = "linecache.py") -> dict[str, object]:
    source = _stdlib_root() / relative
    return {
        "path": relative,
        "size": source.stat().st_size,
        "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
    }


def _verified_stdlib_pyc(
    source_record: dict[str, object],
    *,
    source_bytes: bytes | None = None,
    magic: bytes | None = None,
    optimization: int = 0,
    trailing: bytes = b"",
) -> bytes:
    relative = str(source_record["path"])
    if source_bytes is None:
        source_bytes = (_stdlib_root() / relative).read_bytes()
    code = compile(
        source_bytes,
        relative,
        "exec",
        dont_inherit=True,
        optimize=optimization,
    )
    return (
        (magic if magic is not None else importlib.util.MAGIC_NUMBER)
        + struct.pack("<I", 1)
        + b"\0" * 8
        + marshal.dumps(code)
        + trailing
    )


def _synthetic_runtime_policy_summary(
    root: Path,
    package_manifest: dict[str, object],
    *,
    raw_records: list[dict[str, object]] | None = None,
    collect_toc_sha256: str = "0" * 64,
) -> dict[str, object]:
    records = [] if raw_records is None else raw_records
    normalized_digest = runtime_policy._canonical_json_sha256(records)
    summary: dict[str, object] = {
        "toc_files": {
            "Analysis-00.toc": {"sha256": "1" * 64},
            "COLLECT-00.toc": {"sha256": collect_toc_sha256},
        },
        "runtime_policy_audit": {
            "artifact": {
                "filename": runtime_policy.RUNTIME_POLICY_AUDIT_FILENAME,
                "size": 1,
                "sha256": "2" * 64,
                "payload_sha256": "3" * 64,
            },
            "policy": runtime_policy.RUNTIME_POLICY_NAME,
            "pyinstaller_version": "6.18.0",
            "raw_inventory_sha256": normalized_digest,
            "policy_result_sha256": normalized_digest,
            "normalized_raw_inventory_sha256": normalized_digest,
            "raw_binary_count": len(records),
            "retained_binary_count": len(records),
            "excluded_binary_count": 0,
            "allowed_source_boundaries": [],
            "excluded_binaries": [],
            "raw_binaries": records,
        },
        "provenance_binding": {
            "runtime_policy_audit_sha256": "2" * 64,
            "runtime_policy_payload_sha256": "3" * 64,
            "raw_inventory_sha256": normalized_digest,
            "policy_result_sha256": normalized_digest,
            "normalized_raw_inventory_sha256": normalized_digest,
            "analysis_toc_sha256": "1" * 64,
            "collect_toc_sha256": collect_toc_sha256,
            "build_provenance_sha256": package_manifest.get(
                "build_provenance_sha256"
            ),
        },
    }
    return compliance._write_runtime_policy_attestation(root, summary)


def _write_existing_inventory(root: Path) -> Path:
    manifest_path = root / compliance.MANIFEST_RELATIVE_PATH
    package_manifest = json.loads(
        (root / "licenses" / "python-packages.json").read_text(encoding="utf-8")
    )
    lock = _component_lock()
    base_library = root / "_internal" / "base_library.zip"
    if not base_library.exists():
        base_library.parent.mkdir(parents=True, exist_ok=True)
        source_record = _stdlib_source_record()
        package_manifest["python_native_runtime"]["stdlib_python_sources"][
            "artifacts"
        ] = [source_record]
        with zipfile.ZipFile(base_library, "w") as archive:
            archive.writestr(
                str(source_record["path"]) + "c",
                _verified_stdlib_pyc(source_record),
            )
    base_errors, base_summary = compliance._validate_base_library_archive(
        base_library,
        package_manifest,
    )
    assert base_errors == []
    assert base_summary is not None
    pyinstaller_summary = _synthetic_runtime_policy_summary(
        root,
        package_manifest,
    )
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
                "pyinstaller_build": pyinstaller_summary,
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


def test_validate_package_manifest_accepts_locked_installer_license(
    monkeypatch,
    tmp_path,
):
    full_lock = _component_lock()
    lock = {
        "application": full_lock["application"],
        "python": {
            "component": "python",
            "license": "PSF-2.0",
            "release_version": "3.14.6",
        },
        "runtime_components": [],
        "build_components": [],
        "installer_components": full_lock["installer_components"],
    }
    distribution = tmp_path / "distribution"
    licenses = distribution / "custom-license-destination"
    licenses.mkdir(parents=True)
    components_path = licenses / "components.json"
    components_path.write_text(json.dumps(lock), encoding="utf-8")
    installer_entry = license_collector.collect_installer_component_license(
        lock["installer_components"][0],
        licenses,
        seen_targets={},
    )
    python_license = licenses / "python-packages" / "Python" / "LICENSE.txt"
    python_license.parent.mkdir(parents=True)
    python_license.write_text(
        "Python Software Foundation License\nPermission to use, copy, modify, "
        "and distribute this software is granted.\n",
        encoding="utf-8",
    )
    python_relative = "python-packages/Python/LICENSE.txt"
    native_runtime = {"fixture": True}
    monkeypatch.setattr(
        compliance,
        "probe_python_native_runtime",
        lambda _lock: native_runtime,
    )
    manifest_path = licenses / "python-packages.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "build_python_version": "3.14.6",
                "release_python_version": "3.14.6",
                "requirements_sha256": sha256_file(compliance.RUNTIME_REQUIREMENTS),
                "component_lock_sha256": sha256_file(components_path),
                "python_native_runtime": native_runtime,
                "packages": [
                    {
                        "component": "python",
                        "name": "Python",
                        "version": "3.14.6",
                        "expected_license": "PSF-2.0",
                        "license_files": [python_relative],
                        "license_file_sha256": {
                            python_relative: sha256_file(python_license)
                        },
                        "substantive_license_files": [python_relative],
                    },
                    installer_entry,
                ],
            }
        ),
        encoding="utf-8",
    )

    assert validate_package_manifest(manifest_path, lock) == []
    assert not (distribution / "licenses" / "inno-setup" / "LICENSE.txt").exists()

    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["packages"][1]["license_files"] = ["inno-setup/OTHER.txt"]
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    errors = validate_package_manifest(manifest_path, lock)
    assert any(
        "License path differs from installer component lock" in error
        for error in errors
    )


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


def _write_minimal_pyinstaller_tocs(tmp_path: Path) -> dict[str, Path]:
    repository_root = Path(__file__).resolve().parents[1]
    build_dir = tmp_path / "build"
    build_dir.mkdir()
    main = str(repository_root / "main.py")
    input_datas = [
        (
            "assets\\app\\app.ico",
            str(repository_root / "assets" / "app" / "app.ico"),
            "DATA",
        ),
        (
            "config\\champion_aliases.json",
            str(repository_root / "config" / "champion_aliases.json"),
            "DATA",
        ),
        (
            "config\\setting.sample.json",
            str(repository_root / "config" / "setting.sample.json"),
            "DATA",
        ),
    ]
    demo_module = ("demo", main, "PYMODULE")
    struct_module = (
        "struct",
        str(_stdlib_root() / "struct.py"),
        "PYMODULE",
    )
    demo_binary = tmp_path / "demo.dll"
    demo_binary.write_bytes(b"demo binary")
    binary = ("demo.dll", str(demo_binary), "BINARY")
    wheel_vcomp = tmp_path / "sklearn" / ".libs" / "vcomp140.dll"
    wheel_vcomp.parent.mkdir(parents=True)
    wheel_vcomp.write_bytes(b"locked wheel vcomp")
    wheel_vcomp_entry = (
        "sklearn\\.libs\\vcomp140.dll",
        str(wheel_vcomp),
        "BINARY",
    )
    data = ("base_library.zip", str(tmp_path / "base_library.zip"), "DATA")
    runtime_scripts = [
        (name, str(tmp_path / f"{name}.py"), "PYSOURCE")
        for name in (
            "pyi_rth_inspect",
            "pyi_rth_pkgutil",
            "pyi_rth_multiprocessing",
            "pyi_rth_setuptools",
            "pyi_rth_pkgres",
            "pyi_rth_pyqt6",
        )
    ]
    analysis_scripts = [*runtime_scripts, ("main", main, "PYSOURCE")]
    analysis = (
        [main],
        [str(repository_root)],
        ["mpv"],
        [],
        {},
        [],
        [],
        False,
        {},
        0,
        [],
        input_datas,
        sys.version,
        analysis_scripts,
        [struct_module, demo_module],
        [binary, wheel_vcomp_entry],
        [],
        [],
        [data],
        [],
    )
    pyz = (str(build_dir / "PYZ-00.pyz"), [demo_module])
    local_modules = [
        (
            name,
            str(build_dir / "localpycs" / f"{name}.pyc"),
            "PYMODULE",
        )
        for name in (
            "struct",
            "pyimod01_archive",
            "pyimod02_importers",
            "pyimod03_ctypes",
            "pyimod04_pywin32",
        )
    ]
    bootstrap = (
        "pyiboot01_bootstrap",
        str(tmp_path / "pyiboot01_bootstrap.py"),
        "PYSOURCE",
    )
    pkg_entries = [
        ("pyi-contents-directory _internal", "", "OPTION"),
        ("PYZ-00.pyz", str(build_dir / "PYZ-00.pyz"), "PYZ"),
        *local_modules,
        bootstrap,
        *analysis_scripts,
    ]
    pkg = (
        str(build_dir / "LoLReplayTool.pkg"),
        {
            "BINARY": True,
            "DATA": True,
            "EXECUTABLE": True,
            "EXTENSION": True,
            "PYMODULE": True,
            "PYSOURCE": True,
            "PYZ": False,
            "SPLASH": True,
            "SYMLINK": False,
        },
        pkg_entries,
        "python314.dll",
        True,
        False,
        False,
        [],
        None,
        None,
        None,
    )
    exe = (
        str(build_dir / "LoLReplayTool.exe"),
        False,
        False,
        True,
        [str(repository_root / "assets" / "app" / "app.ico")],
        None,
        False,
        False,
        b"manifest",
        True,
        False,
        None,
        None,
        None,
        str(build_dir / "LoLReplayTool.pkg"),
        pkg_entries,
        [],
        False,
        False,
        0,
        [("runw.exe", str(tmp_path / "runw.exe"), "EXECUTABLE")],
        str(Path(sys.base_prefix) / "python314.dll"),
    )
    collect = (
        [
            (
                "LoLReplayTool.exe",
                str(build_dir / "LoLReplayTool.exe"),
                "EXECUTABLE",
            ),
            binary,
            wheel_vcomp_entry,
            data,
        ],
    )
    payloads = {
        "Analysis-00.toc": analysis,
        "PYZ-00.toc": pyz,
        "PKG-00.toc": pkg,
        "EXE-00.toc": exe,
        "COLLECT-00.toc": collect,
    }
    paths = {}
    for name, payload in payloads.items():
        path = build_dir / name
        path.write_text(repr(payload), encoding="utf-8")
        paths[name] = path
    runtime_policy.apply_windows_runtime_policy(
        analysis[15],
        audit_path=build_dir / runtime_policy.RUNTIME_POLICY_AUDIT_FILENAME,
    )
    return paths


def test_complete_pyinstaller_toc_parser_accepts_exact_cross_links(tmp_path):
    paths = _write_minimal_pyinstaller_tocs(tmp_path)

    parsed = compliance._parse_pyinstaller_tocs(paths["COLLECT-00.toc"])

    assert set(parsed["paths"]) == set(compliance.PYINSTALLER_TOC_FILES)
    assert parsed["pyz_entries"][0][0] == "demo"
    assert parsed["pkg_entries"] == parsed["exe"][15]


@pytest.mark.parametrize(
    "name",
    [
        "api-ms-win-core-file-l1-2-0.dll",
        "api-ms-win-crt-runtime-l1-1-0.dll",
        "ucrtbase.dll",
        "VCOMP140.DLL",
    ],
)
def test_complete_pyinstaller_toc_parser_rejects_excluded_host_runtime(
    tmp_path,
    name,
):
    paths = _write_minimal_pyinstaller_tocs(tmp_path)
    analysis = list(
        ast.literal_eval(paths["Analysis-00.toc"].read_text(encoding="utf-8"))
    )
    collect = ast.literal_eval(
        paths["COLLECT-00.toc"].read_text(encoding="utf-8")
    )
    runtime = (name, str(tmp_path / name), "BINARY")
    analysis[15] = [*analysis[15], runtime]
    collect[0].append(runtime)
    paths["Analysis-00.toc"].write_text(
        repr(tuple(analysis)),
        encoding="utf-8",
    )
    paths["COLLECT-00.toc"].write_text(repr(collect), encoding="utf-8")

    with pytest.raises(ValueError, match="excluded host Windows runtime"):
        compliance._parse_pyinstaller_tocs(paths["COLLECT-00.toc"])


@pytest.mark.parametrize(
    "missing_name",
    compliance.PYINSTALLER_TOC_FILES,
)
def test_complete_pyinstaller_toc_parser_rejects_each_missing_toc(
    tmp_path,
    missing_name,
):
    paths = _write_minimal_pyinstaller_tocs(tmp_path)
    paths[missing_name].unlink()

    with pytest.raises(ValueError, match="missing or unsafe"):
        compliance._parse_pyinstaller_tocs(paths["COLLECT-00.toc"])


@pytest.mark.parametrize(
    ("target", "mutation", "message"),
    [
        (
            "Analysis-00.toc",
            lambda payload: payload[:-1],
            "Analysis TOC structure",
        ),
        (
            "PYZ-00.toc",
            lambda payload: (payload[0], []),
            "pure set differs from PYZ",
        ),
        (
            "EXE-00.toc",
            lambda payload: (*payload[:15], [], *payload[16:]),
            "EXE/PKG TOC relationship",
        ),
        (
            "COLLECT-00.toc",
            lambda payload: ([payload[0][0]],),
            "binary/data set differs from COLLECT",
        ),
    ],
)
def test_complete_pyinstaller_toc_parser_rejects_cross_link_tampering(
    tmp_path,
    target,
    mutation,
    message,
):
    paths = _write_minimal_pyinstaller_tocs(tmp_path)
    payload = ast.literal_eval(paths[target].read_text(encoding="utf-8"))
    paths[target].write_text(repr(mutation(payload)), encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        compliance._parse_pyinstaller_tocs(paths["COLLECT-00.toc"])


@pytest.mark.parametrize(
    ("index", "value"),
    [
        (4, {"demo": {}}),
        (5, ["demo"]),
        (6, ["runtime-hook.py"]),
        (7, True),
        (8, {"demo": "py"}),
        (9, 1),
    ],
)
def test_pyinstaller_analysis_rejects_nondefault_build_options(
    tmp_path,
    index,
    value,
):
    paths = _write_minimal_pyinstaller_tocs(tmp_path)
    analysis = list(
        ast.literal_eval(paths["Analysis-00.toc"].read_text(encoding="utf-8"))
    )
    analysis[index] = value
    paths["Analysis-00.toc"].write_text(
        repr(tuple(analysis)),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="build options differ"):
        compliance._parse_pyinstaller_tocs(paths["COLLECT-00.toc"])


@pytest.mark.parametrize("mutation", ["omit-main", "rogue-script"])
def test_pyinstaller_pkg_and_analysis_joint_script_tampering_fails(
    tmp_path,
    mutation,
):
    paths = _write_minimal_pyinstaller_tocs(tmp_path)
    analysis = ast.literal_eval(paths["Analysis-00.toc"].read_text(encoding="utf-8"))
    pkg = ast.literal_eval(paths["PKG-00.toc"].read_text(encoding="utf-8"))
    exe = ast.literal_eval(paths["EXE-00.toc"].read_text(encoding="utf-8"))
    analysis_scripts = list(analysis[13])
    pkg_entries = list(pkg[2])
    if mutation == "omit-main":
        analysis_scripts = [entry for entry in analysis_scripts if entry[0] != "main"]
        pkg_entries = [entry for entry in pkg_entries if entry[0] != "main"]
    else:
        rogue = ("rogue", str(Path(__file__).resolve()), "PYSOURCE")
        analysis_scripts.insert(-1, rogue)
        pkg_entries.insert(-1, rogue)
    analysis = (*analysis[:13], analysis_scripts, *analysis[14:])
    pkg = (*pkg[:2], pkg_entries, *pkg[3:])
    exe = (*exe[:15], pkg_entries, *exe[16:])
    paths["Analysis-00.toc"].write_text(repr(analysis), encoding="utf-8")
    paths["PKG-00.toc"].write_text(repr(pkg), encoding="utf-8")
    paths["EXE-00.toc"].write_text(repr(exe), encoding="utf-8")

    with pytest.raises(ValueError, match="runtime/application scripts differ"):
        compliance._parse_pyinstaller_tocs(paths["COLLECT-00.toc"])


def test_pyinstaller_pkg_compression_policy_is_exact(tmp_path):
    paths = _write_minimal_pyinstaller_tocs(tmp_path)
    pkg = ast.literal_eval(paths["PKG-00.toc"].read_text(encoding="utf-8"))
    compression = {**pkg[1], "PYZ": True}
    paths["PKG-00.toc"].write_text(
        repr((pkg[0], compression, *pkg[2:])),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="compression or build flags differ"):
        compliance._parse_pyinstaller_tocs(paths["COLLECT-00.toc"])


def test_pyinstaller_carchive_chain_is_bidirectional(tmp_path):
    pkg = tmp_path / "app.pkg"
    pyz = tmp_path / "app.pyz"
    pkg.write_bytes(b"pkg")
    pyz.write_bytes(b"pyz")
    entries = [
        ("pyi-contents-directory _internal", "", "OPTION"),
        ("PYZ-00.pyz", str(pyz), "PYZ"),
        ("main", "main.py", "PYSOURCE"),
    ]

    class FakeCArchive:
        toc = {
            "PYZ.pyz": (0, 3, 3, 0, "z"),
            "main": (3, 4, 4, 1, "s"),
        }

        @staticmethod
        def raw_pkg_data():
            return b"pkg"

        @staticmethod
        def extract(name):
            assert name == "PYZ.pyz"
            return b"pyz"

    assert (
        compliance._verify_carchive_chain(FakeCArchive(), pkg, pyz, entries)
        == b"pyz"
    )


@pytest.mark.parametrize(
    ("pkg_bytes", "pyz_bytes", "toc", "message"),
    [
        (b"forged", b"pyz", {"PYZ.pyz": (0, 3, 3, 0, "z")}, "CArchive differs"),
        (b"pkg", b"forged", {"PYZ.pyz": (0, 3, 3, 0, "z")}, "embedded PYZ differs"),
        (b"pkg", b"pyz", {}, "member set differs"),
        (b"pkg", b"pyz", {"PYZ.pyz": (0, 3, 3, 0, "m")}, "member type differs"),
    ],
)
def test_pyinstaller_carchive_chain_rejects_archive_tampering(
    tmp_path,
    pkg_bytes,
    pyz_bytes,
    toc,
    message,
):
    pkg = tmp_path / "app.pkg"
    pyz = tmp_path / "app.pyz"
    pkg.write_bytes(b"pkg")
    pyz.write_bytes(b"pyz")

    class FakeCArchive:
        @staticmethod
        def raw_pkg_data():
            return pkg_bytes

        @staticmethod
        def extract(_name):
            return pyz_bytes

    FakeCArchive.toc = toc
    with pytest.raises(ValueError, match=message):
        compliance._verify_carchive_chain(
            FakeCArchive(),
            pkg,
            pyz,
            [("PYZ-00.pyz", str(pyz), "PYZ")],
        )


def _synthetic_carchive(tmp_path: Path, *, compressed_suffix: bytes = b""):
    code = b"bootstrap-code"
    compressed_code = zlib.compress(code) + compressed_suffix
    pyz = b"PYZ-data"
    payload = compressed_code + pyz
    toc_entry_format = "!IIIIBc"
    toc_header_size = struct.calcsize(toc_entry_format)

    def toc_entry(offset, compressed_size, size, compressed, type_code, name):
        encoded_name = name.encode("utf-8") + b"\0"
        entry_length = toc_header_size + len(encoded_name)
        entry_length = (entry_length + 15) // 16 * 16
        return struct.pack(
            toc_entry_format,
            entry_length,
            offset,
            compressed_size,
            size,
            compressed,
            type_code.encode("ascii"),
        ) + encoded_name.ljust(entry_length - toc_header_size, b"\0")

    code_toc = toc_entry(0, len(compressed_code), len(code), 1, "m", "bootstrap")
    option_toc = toc_entry(
        len(compressed_code),
        0,
        0,
        0,
        "o",
        "pyi-contents-directory _internal",
    )
    pyz_toc = toc_entry(
        len(compressed_code),
        len(pyz),
        len(pyz),
        0,
        "z",
        "PYZ.pyz",
    )
    toc = code_toc + option_toc + pyz_toc
    cookie_format = "!8sIIII64s"
    cookie = struct.pack(
        cookie_format,
        b"MEI\014\013\012\013\016",
        len(payload) + len(toc) + struct.calcsize(cookie_format),
        len(payload),
        len(toc),
        sys.version_info.major * 100 + sys.version_info.minor,
        b"python314.dll",
    )
    raw = payload + toc + cookie
    prefix = b"locked-prefix"
    final_exe = tmp_path / "app.exe"
    final_exe.write_bytes(prefix + raw)

    class FakeCArchive:
        _start_offset = len(prefix)
        _end_offset = len(prefix) + len(raw)
        toc = {
            "bootstrap": (0, len(compressed_code), len(code), 1, "m"),
            "PYZ.pyz": (len(compressed_code), len(pyz), len(pyz), 0, "z"),
        }

        def __init__(self, raw_pkg):
            self.raw_pkg = raw_pkg

        def raw_pkg_data(self):
            return self.raw_pkg

        def extract(self, name):
            offset, length, _size, compressed, _type = self.toc[name]
            value = self.raw_pkg[offset : offset + length]
            return zlib.decompress(value) if compressed else value

    offsets = {
        "toc": len(payload),
        "option": len(payload) + len(code_toc),
        "cookie": len(payload) + len(toc),
    }
    return raw, FakeCArchive, final_exe, offsets


def test_carchive_layout_independently_parses_cookie_toc_and_payloads(tmp_path):
    raw, fake_type, final_exe, _offsets = _synthetic_carchive(tmp_path)

    summary = compliance._verify_carchive_layout(
        fake_type(raw),
        final_exe,
        python_library="python314.dll",
        options=["pyi-contents-directory _internal"],
    )

    assert summary["archive_size"] == len(raw)
    assert summary["toc_offset"] > 0


def test_carchive_layout_rejects_trailing_compressed_stream_bytes(tmp_path):
    raw, fake_type, final_exe, _offsets = _synthetic_carchive(
        tmp_path,
        compressed_suffix=b"rogue",
    )

    with pytest.raises(ValueError, match="compressed stream is not exact"):
        compliance._verify_carchive_layout(
            fake_type(raw),
            final_exe,
            python_library="python314.dll",
            options=["pyi-contents-directory _internal"],
        )


@pytest.mark.parametrize("mutation", ["cookie", "member-length", "option-offset"])
def test_carchive_layout_rejects_cookie_member_and_option_tampering(
    tmp_path,
    mutation,
):
    raw, fake_type, final_exe, offsets = _synthetic_carchive(tmp_path)
    tampered = bytearray(raw)
    if mutation == "cookie":
        struct.pack_into("!I", tampered, offsets["cookie"] + 8, len(raw) + 1)
    elif mutation == "member-length":
        struct.pack_into("!I", tampered, offsets["toc"] + 8, len(raw))
    else:
        struct.pack_into("!I", tampered, offsets["option"] + 4, 1)

    with pytest.raises(ValueError):
        compliance._verify_carchive_layout(
            fake_type(bytes(tampered)),
            final_exe,
            python_library="python314.dll",
            options=["pyi-contents-directory _internal"],
        )


def _synthetic_pyz(*, type_codes=(0, 1, 3), gap=b""):
    entries = [
        ("demo", "demo.py", "PYMODULE"),
        ("package", "package/__init__.py", "PYMODULE"),
        ("namespace", "-", "PYMODULE"),
    ]
    payload = bytearray(b"\0" * 17 + gap)
    toc = []
    raw_members = {}
    code_members = {}
    for (name, _source, _entry_type), type_code in zip(
        entries,
        type_codes,
        strict=True,
    ):
        offset = len(payload)
        if type_code == 3:
            compressed = b""
            raw_code = None
            code = None
        else:
            code = compile(f"VALUE = {len(toc)}\n", f"{name}.py", "exec")
            raw_code = marshal.dumps(code)
            compressed = zlib.compress(raw_code)
            payload.extend(compressed)
        toc.append((name, (type_code, offset, len(compressed))))
        raw_members[name] = raw_code
        code_members[name] = code
    toc_offset = len(payload)
    payload.extend(marshal.dumps(toc))
    payload[:4] = b"PYZ\0"
    payload[4:8] = importlib.util.MAGIC_NUMBER
    payload[8:12] = struct.pack("!i", toc_offset)

    class FakePYZ:
        def __init__(self):
            self.toc = dict(toc)

        def extract(self, name, raw=False):
            return raw_members[name] if raw else code_members[name]

    return bytes(payload), FakePYZ(), entries


def test_pyz_layout_requires_exact_header_types_ranges_and_eof():
    raw, reader, entries = _synthetic_pyz()

    summary = compliance._verify_pyz_layout(raw, reader, entries)

    assert summary["archive_size"] == len(raw)
    assert summary["toc_offset"] > 17


@pytest.mark.parametrize("mutation", ["magic", "reserved", "trailing", "gap", "type"])
def test_pyz_layout_rejects_independent_archive_tampering(mutation):
    if mutation == "gap":
        raw, reader, entries = _synthetic_pyz(gap=b"x")
    elif mutation == "type":
        raw, reader, entries = _synthetic_pyz(type_codes=(1, 1, 3))
    else:
        raw, reader, entries = _synthetic_pyz()
        changed = bytearray(raw)
        if mutation == "magic":
            changed[0] ^= 1
        elif mutation == "reserved":
            changed[12] = 1
        else:
            changed.extend(b"rogue")
        raw = bytes(changed)

    with pytest.raises(ValueError):
        compliance._verify_pyz_layout(raw, reader, entries)


def _build_test_pe(tmp_path: Path, monkeypatch):
    from PIL import Image
    from PyInstaller import config
    from PyInstaller.utils.win32 import icon, winmanifest, winresource, winutils

    monkeypatch.setitem(config.CONF, "workpath", str(tmp_path))
    bootloader = Path(
        metadata.distribution("PyInstaller").locate_file(
            "PyInstaller/bootloader/Windows-64bit-intel/runw.exe"
        )
    )
    executable = tmp_path / "test.exe"
    shutil.copyfile(bootloader, executable)
    winresource.remove_all_resources(str(executable))
    icon_path = tmp_path / "test.ico"
    with Image.new("RGBA", (16, 16), (24, 96, 192, 255)) as image:
        image.save(icon_path, format="ICO", sizes=[(16, 16)])
    icon.CopyIcons(str(executable), [str(icon_path)])
    manifest = winmanifest.create_application_manifest(None, False, False)
    winmanifest.write_manifest_to_executable(str(executable), manifest)
    winutils.set_exe_build_timestamp(str(executable), 1_700_000_000)
    archive_start = executable.stat().st_size
    with executable.open("ab") as stream:
        stream.write(b"synthetic-carchive")
    winutils.update_exe_pe_checksum(str(executable))
    return bootloader, executable, manifest, icon_path, archive_start


@pytest.mark.skipif(os.name != "nt", reason="PE resource construction is Windows-only")
def test_final_pe_is_derived_from_locked_bootloader(monkeypatch, tmp_path):
    bootloader, executable, manifest, icon_path, archive_start = _build_test_pe(
        tmp_path,
        monkeypatch,
    )

    summary = compliance._verify_bootloader_pe(
        bootloader,
        executable,
        manifest=manifest,
        icon_path=icon_path,
        carchive_start=archive_start,
    )

    assert summary["overlay_start"] == archive_start
    assert set(summary["section_hashes"]) >= {".text", ".rdata", ".reloc"}
    assert summary["resource_layout"]["verified_padding_bytes"] > 0


@pytest.mark.skipif(os.name != "nt", reason="PE resource construction is Windows-only")
@pytest.mark.parametrize(
    "tamper",
    [
        "text",
        "manifest",
        "resource-internal-padding",
        "resource-padding",
        "overlay",
    ],
)
def test_final_pe_rejects_code_resource_and_overlay_tampering(
    monkeypatch,
    tmp_path,
    tamper,
):
    import pefile
    from PyInstaller.utils.win32 import winutils

    bootloader, executable, manifest, icon_path, archive_start = _build_test_pe(
        tmp_path,
        monkeypatch,
    )
    if tamper == "text":
        pe = pefile.PE(str(executable))
        offset = next(
            section.PointerToRawData
            for section in pe.sections
            if section.Name.rstrip(b"\0") == b".text"
        )
        pe.close()
    elif tamper == "manifest":
        pe = pefile.PE(str(executable))
        manifest_entry = next(
            type_entry
            for type_entry in pe.DIRECTORY_ENTRY_RESOURCE.entries
            if type_entry.id == 24
        )
        record = manifest_entry.directory.entries[0].directory.entries[0].data.struct
        offset = pe.get_offset_from_rva(record.OffsetToData)
        pe.close()
    elif tamper == "resource-internal-padding":
        pe = pefile.PE(str(executable))
        payload_ranges = sorted(
            (
                pe.get_offset_from_rva(language_entry.data.struct.OffsetToData),
                pe.get_offset_from_rva(language_entry.data.struct.OffsetToData)
                + language_entry.data.struct.Size,
            )
            for type_entry in pe.DIRECTORY_ENTRY_RESOURCE.entries
            for name_entry in type_entry.directory.entries
            for language_entry in name_entry.directory.entries
        )
        offset = None
        for (_previous_start, previous_end), (next_start, _next_end) in zip(
            payload_ranges,
            payload_ranges[1:],
            strict=False,
        ):
            padding_size = next_start - previous_end
            if 1 <= padding_size <= 3:
                padding = bytes(pe.__data__[previous_end:next_start])
                assert padding == b"PADDING"[:padding_size]
                offset = previous_end
                break
        assert offset is not None
        pe.close()
    elif tamper == "resource-padding":
        pe = pefile.PE(str(executable))
        resource_section = next(
            section
            for section in pe.sections
            if section.Name.rstrip(b"\0") == b".rsrc"
        )
        assert resource_section.Misc_VirtualSize < resource_section.SizeOfRawData
        offset = (
            resource_section.PointerToRawData + resource_section.SizeOfRawData - 1
        )
        raw_padding_index = (
            resource_section.SizeOfRawData
            - resource_section.Misc_VirtualSize
            - 1
        )
        expected_padding_byte = b"PADDINGXXPADDING"[
            raw_padding_index % len(b"PADDINGXXPADDING")
        ]
        pe.close()
    else:
        offset = None
        archive_start += 1
    if offset is not None:
        with executable.open("r+b") as stream:
            stream.seek(offset)
            original = stream.read(1)
            if tamper == "resource-padding":
                assert original == bytes([expected_padding_byte])
            stream.seek(offset)
            stream.write(bytes([original[0] ^ 1]))
        winutils.update_exe_pe_checksum(str(executable))

    with pytest.raises(ValueError):
        compliance._verify_bootloader_pe(
            bootloader,
            executable,
            manifest=manifest,
            icon_path=icon_path,
            carchive_start=archive_start,
        )


@pytest.mark.parametrize("actual_names", [set(), {"demo", "rogue"}])
def test_pyinstaller_pyz_member_set_rejects_omission_and_rogue(actual_names):
    reader = type("FakePYZ", (), {"toc": {name: (0, 0, 1) for name in actual_names}})()

    with pytest.raises(ValueError, match="module set differs"):
        compliance._verify_pyz_member_set(
            reader,
            [("demo", "demo.py", "PYMODULE")],
        )


def test_embedded_marshaled_code_rejects_trailing_and_source_tampering(tmp_path):
    source = tmp_path / "module.py"
    source.write_bytes(b"VALUE = 1\n")
    expected_filename = "module.py"
    code = compile(
        source.read_bytes(),
        expected_filename,
        "exec",
        dont_inherit=True,
    )
    raw = marshal.dumps(code)

    assert (
        compliance._verified_marshaled_code(
            raw,
            source,
            label="test payload",
            expected_filename=expected_filename,
        )
        == code
    )
    with pytest.raises(ValueError, match="trailing bytes"):
        compliance._verified_marshaled_code(
            raw + b"rogue",
            source,
            label="test payload",
            expected_filename=expected_filename,
        )
    source.write_bytes(b"VALUE = 2\n")
    with pytest.raises(ValueError, match="differs from verified source"):
        compliance._verified_marshaled_code(
            raw,
            source,
            label="test payload",
            expected_filename=expected_filename,
        )


@pytest.mark.parametrize("nested", [False, True])
def test_embedded_marshaled_code_rejects_unexpected_filenames(tmp_path, nested):
    source = tmp_path / "module.py"
    source.write_bytes(b"def value():\n    return 1\n")
    expected_filename = "module.py"
    code = compile(
        source.read_bytes(),
        expected_filename,
        "exec",
        dont_inherit=True,
        optimize=0,
    )
    if nested:
        constants = tuple(
            value.replace(co_filename=r"\\attacker\share\module.py")
            if isinstance(value, types.CodeType)
            else value
            for value in code.co_consts
        )
        code = code.replace(co_consts=constants)
    else:
        code = code.replace(co_filename=r"C:\untrusted\module.py")

    with pytest.raises(ValueError, match="differs from verified source"):
        compliance._verified_marshaled_code(
            marshal.dumps(code),
            source,
            label="test payload",
            expected_filename=expected_filename,
        )


def test_embedded_marshaled_code_rejects_optimized_payload(tmp_path):
    source = tmp_path / "module.py"
    source.write_bytes(b'"""module docs"""\nassert True\nVALUE = 1\n')
    expected_filename = "module.py"
    optimized = compile(
        source.read_bytes(),
        expected_filename,
        "exec",
        dont_inherit=True,
        optimize=2,
    )

    with pytest.raises(ValueError, match="differs from verified source"):
        compliance._verified_marshaled_code(
            marshal.dumps(optimized),
            source,
            label="test payload",
            expected_filename=expected_filename,
        )


def test_toc_and_dist_are_bidirectionally_inventoried(monkeypatch, tmp_path):
    root = tmp_path / "distribution"
    _write_distribution_materials(root)
    executable = root / "LoLReplayTool.exe"
    native = root / "_internal" / "aiohttp" / "_demo.pyd"
    base_library = root / "_internal" / "base_library.zip"
    executable.write_bytes(b"exe")
    native.parent.mkdir(parents=True)
    native.write_bytes(b"native")
    source_record = _stdlib_source_record()
    with zipfile.ZipFile(base_library, "w") as archive:
        archive.writestr(
            str(source_record["path"]) + "c",
            _verified_stdlib_pyc(source_record),
        )

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
    package_manifest = json.loads(
        (root / "licenses" / "python-packages.json").read_text(encoding="utf-8")
    )
    pyinstaller_summary = _synthetic_runtime_policy_summary(
        root,
        package_manifest,
        raw_records=[
            {
                "raw_index": 0,
                "destination": "aiohttp/_demo.pyd",
                "source": "aiohttp/Lib/site-packages/aiohttp/_demo.pyd",
                "source_component": "aiohttp",
                "type": "EXTENSION",
                "size": native_source.stat().st_size,
                "sha256": sha256_file(native_source),
                "decision": "retained",
            }
        ],
        collect_toc_sha256=sha256_file(toc),
    )
    monkeypatch.setattr(
        compliance,
        "_validate_pyinstaller_build",
        lambda *_args, **_kwargs: ([], pyinstaller_summary),
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


@pytest.mark.parametrize(
    "relative",
    [
        "obs-portable/bin/64bit/obs64.exe",
        "_internal/OBS-Studio/data.txt",
        "ffmpeg.exe",
        "tools/ffprobe.exe",
        "downloads/OBS-Studio-32.1.2-Windows-x64.zip",
        "downloads/OBS-Studio-32.1.2-Windows-x64-Installer.exe",
        "downloads/OBS-Studio-32.1.2-Windows-x64.msi",
        "downloads/ffmpeg-8.1.1-essentials_build.7z",
    ],
)
def test_user_provided_runtime_is_rejected_with_or_without_toc(tmp_path, relative):
    root = tmp_path / "distribution"
    _write_distribution_materials(root)
    runtime = root / Path(relative)
    runtime.parent.mkdir(parents=True, exist_ok=True)
    runtime.write_bytes(b"external runtime")
    _write_existing_inventory(root)
    toc = tmp_path / "COLLECT-00.toc"
    toc.write_text(repr(([],)), encoding="utf-8")

    for errors in (
        validate_distribution(root),
        validate_distribution(root, toc_path=toc),
    ):
        assert any(
            "User-provided OBS/standalone FFmpeg must not be bundled" in error
            for error in errors
        )


def test_opencv_ffmpeg_dll_is_not_treated_as_standalone_ffmpeg(tmp_path):
    root = tmp_path / "distribution"
    bundled_library = root / "_internal" / "cv2" / "opencv_videoio_ffmpeg4140_64.dll"
    bundled_library.parent.mkdir(parents=True)
    bundled_library.write_bytes(b"opencv library")
    physical = compliance._physical_files(root)

    assert compliance._forbidden_user_runtime_errors(root, physical) == []


def test_build_script_fails_closed_on_user_provided_runtime_artifacts():
    script = Path("scripts/build.ps1").read_text(encoding="utf-8")

    assert "forbiddenRuntimePaths" in script
    assert "ffmpeg.exe" in script
    assert "obs64.exe" in script
    assert "exe|msi|zip|7z" in script
    assert "throw \"利用者が用意するOBS／standalone FFmpeg" in script


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


def test_current_manifest_requires_runtime_policy_audit_and_binding(tmp_path):
    root = tmp_path / "distribution"
    _write_distribution_materials(root)
    manifest_path = _write_existing_inventory(root)
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["pyinstaller_build"].pop("runtime_policy_audit")
    payload["pyinstaller_build"].pop("provenance_binding")
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    errors = validate_distribution(root)

    assert any("runtime policy audit binding is incomplete" in error for error in errors)


def test_installer_audit_rejects_matching_payloads_without_runtime_policy_binding(
    tmp_path,
):
    distribution = tmp_path / "distribution"
    installed = tmp_path / "installed"
    _write_distribution_materials(distribution)
    manifest_path = _write_existing_inventory(distribution)
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["pyinstaller_build"].pop("runtime_policy_audit")
    payload["pyinstaller_build"].pop("provenance_binding")
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    shutil.copytree(distribution, installed)

    errors = installer_content_audit.audit_installer_payload(
        distribution,
        installed,
    )

    assert any(
        "runtime policy audit binding is incomplete" in error for error in errors
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
    assert any("numpy: native_source_coverage_verified" in error for error in errors)
    assert "Release build provenance is missing." in errors
    assert (
        "Release validation requires an externally sealed build provenance SHA256."
        in errors
    )


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
    assert observed["runtime_source"] == "official_binary_archive"

    source_tampered = copy.deepcopy(lock)
    source_profile = source_tampered["python"][
        "windows_native_runtime_profiles"
    ][sys.version.split()[0]]
    source_profile["runtime_source"] = "official_actions_archive"
    with pytest.raises(RuntimeError, match="native runtime source"):
        license_collector.probe_python_native_runtime(source_tampered)

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


def test_runtime_download_lock_must_be_empty_for_user_provided_tools():
    lock = _component_lock()
    lock["runtime_downloads"] = [
        {
            "component": "unexpected-runtime",
            "url": "https://example.invalid/runtime.zip",
        }
    ]

    errors = compliance._validate_runtime_download_lock(lock)

    assert errors == ["Runtime download component set must be empty for user-provided tools."]


def test_installer_build_self_check_has_an_explicit_timeout():
    installer = Path("scripts/build_installer.ps1").read_text(encoding="utf-8")
    runner = Path("scripts/run_packaged_self_check.ps1").read_text(encoding="utf-8")

    assert "[switch]$SkipSelfCheck" in installer
    assert "if (-not $SkipSelfCheck)" in installer
    assert "$SkipSelfCheck -and -not $SkipBuild" in installer
    assert 'Join-Path $scriptDir "run_packaged_self_check.ps1"' in installer
    assert "-TimeoutSeconds 60" in installer
    assert "Start-Process" not in installer

    assert '[int]$TimeoutSeconds = 60' in runner
    assert '[guid]::NewGuid().ToString("N")' in runner
    assert "$env:LOL_REPLAY_TOOL_DATA_DIR = $selfCheckDir" in runner
    assert ".WaitForExit($TimeoutSeconds * 1000)" in runner
    assert "taskkill.exe" in runner
    assert '[int]$TaskkillTimeoutSeconds = 10' in runner
    assert ".WaitForExit($TaskkillTimeout * 1000)" in runner
    assert '"/PID"' in runner
    assert '"/T"' in runner
    assert '"/F"' in runner
    assert ".Kill($true)" in runner
    assert "catch [System.Management.Automation.MethodException]" in runner
    assert "$Process.Kill()" in runner
    assert "& taskkill" not in runner
    assert "-RedirectStandardOutput" in runner
    assert "-RedirectStandardError" in runner
    assert "Remove-Item -LiteralPath $selfCheckDir -Recurse -Force" in runner
    assert "finally" in runner
    assert "-Wait `" not in runner


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
    source_record = _stdlib_source_record()
    package_manifest = {
        "python_native_runtime": {
            "stdlib_python_sources": {
                "inventory_sha256": "a" * 64,
                "artifacts": [source_record],
            }
        }
    }
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr(
            str(source_record["path"]) + "c",
            _verified_stdlib_pyc(source_record),
        )

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


@pytest.mark.parametrize(
    ("payload_factory", "expected"),
    [
        (lambda record: b"compiled", "pyc header is truncated"),
        (
            lambda record: _verified_stdlib_pyc(record, magic=b"BAD!"),
            "pyc magic differs",
        ),
        (
            lambda record: _verified_stdlib_pyc(record, trailing=b"rogue"),
            "trailing bytes",
        ),
        (
            lambda record: _verified_stdlib_pyc(
                record,
                source_bytes=b"raise RuntimeError('rogue')\n",
            ),
            "bytecode differs from verified stdlib source",
        ),
        (
            lambda record: _verified_stdlib_pyc(record, optimization=2),
            "bytecode differs from verified stdlib source",
        ),
    ],
)
def test_base_library_rejects_unverified_bytecode(
    tmp_path,
    payload_factory,
    expected,
):
    source_record = _stdlib_source_record()
    archive_path = tmp_path / "base_library.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr(
            str(source_record["path"]) + "c",
            payload_factory(source_record),
        )
    package_manifest = {
        "python_native_runtime": {
            "stdlib_python_sources": {
                "inventory_sha256": "a" * 64,
                "artifacts": [source_record],
            }
        }
    }

    errors, summary = compliance._validate_base_library_archive(
        archive_path,
        package_manifest,
    )

    assert summary is not None
    assert any(expected in error for error in errors)


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


def test_system_vcomp_is_not_accepted_as_a_redistributable_source(
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
        is None
    )


def _write_runtime_binary(path: Path, payload: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


def _locked_wheel_vcomp(tmp_path: Path) -> tuple[str, str, str]:
    source = _write_runtime_binary(
        tmp_path / "sklearn" / ".libs" / "vcomp140.dll",
        b"locked wheel vcomp",
    )
    return (r"sklearn\.libs\vcomp140.dll", str(source), "BINARY")


def test_windows_runtime_policy_excludes_only_approved_host_sources(
    monkeypatch,
    tmp_path,
):
    windows_root = tmp_path / "Windows"
    system32 = windows_root / "System32"
    sdk_root = tmp_path / "Windows Kits" / "10"
    sdk_redist = sdk_root / "Redist" / "ucrt" / "DLLs" / "x64"
    monkeypatch.setenv("SystemRoot", str(windows_root))
    monkeypatch.delenv("WINDIR", raising=False)
    monkeypatch.setenv("UniversalCRTSdkDir", str(sdk_root))
    monkeypatch.delenv("WindowsSdkDir", raising=False)

    system_api = _write_runtime_binary(
        system32 / "api-ms-win-core-file-l1-2-0.dll",
        b"system api",
    )
    sdk_ucrt = _write_runtime_binary(
        sdk_redist / "ucrtbase.dll",
        b"sdk ucrt",
    )
    system_vcomp = _write_runtime_binary(
        system32 / "VCOMP140.DLL",
        b"system vcomp",
    )
    wheel_vcomp = _locked_wheel_vcomp(tmp_path)
    demo = _write_runtime_binary(tmp_path / "demo.pyd", b"extension")
    binaries = [
        (
            "API-MS-WIN-CORE-FILE-L1-2-0.DLL",
            str(system_api),
            "BINARY",
        ),
        ("ucrtbase.dll", str(sdk_ucrt), "BINARY"),
        ("vcomp140.dll", str(system_vcomp), "BINARY"),
        wheel_vcomp,
        ("demo.pyd", str(demo), "EXTENSION"),
    ]
    audit_path = tmp_path / runtime_policy.RUNTIME_POLICY_AUDIT_FILENAME

    filtered = runtime_policy.apply_windows_runtime_policy(
        binaries,
        audit_path=audit_path,
    )
    summary = runtime_policy.validate_windows_runtime_policy_audit(
        audit_path,
        filtered,
    )

    assert filtered == [wheel_vcomp, ("demo.pyd", str(demo), "EXTENSION")]
    assert summary["raw_binary_count"] == 5
    assert summary["retained_binary_count"] == 2
    assert summary["excluded_binary_count"] == 3
    assert summary["allowed_source_boundaries"] == [
        "windows-system32",
        "windows-sdk-ucrt-redist",
    ]
    assert {record["source_boundary"] for record in summary["excluded_binaries"]} == {
        "windows-system32",
        "windows-sdk-ucrt-redist",
    }
    assert all(
        not Path(record["source"]).is_absolute()
        for record in summary["excluded_binaries"]
    )
    assert runtime_policy.is_windows_os_runtime_name("ucrtbase.dll")
    assert runtime_policy.is_windows_os_runtime_name(
        "API-MS-WIN-CORE-FILE-L1-2-0.DLL"
    )
    assert not runtime_policy.is_windows_os_runtime_name(
        r"package\ucrtbase.dll"
    )


def test_windows_runtime_policy_fails_closed_for_unapproved_same_name_source(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("SystemRoot", str(tmp_path / "Windows"))
    monkeypatch.delenv("UniversalCRTSdkDir", raising=False)
    monkeypatch.delenv("WindowsSdkDir", raising=False)
    wheel_ucrt = _write_runtime_binary(
        tmp_path / "wheel" / "ucrtbase.dll",
        b"wheel ucrt",
    )

    with pytest.raises(RuntimeError, match="unapproved source"):
        runtime_policy.apply_windows_runtime_policy(
            [
                ("ucrtbase.dll", str(wheel_ucrt), "BINARY"),
                _locked_wheel_vcomp(tmp_path),
            ]
        )


def test_windows_runtime_policy_source_boundaries_are_unique_and_stable(tmp_path):
    windows_root = tmp_path / "Windows"
    universal_sdk = tmp_path / "Kits" / "Universal"
    windows_sdk = tmp_path / "Kits" / "Windows"
    distinct = runtime_policy.allowed_windows_runtime_source_boundaries(
        {
            "SystemRoot": str(windows_root),
            "UniversalCRTSdkDir": str(universal_sdk),
            "WindowsSdkDir": str(windows_sdk),
        }
    )
    assert [name for name, _path in distinct] == [
        "windows-system32",
        "windows-sdk-ucrt-redist-universal-crt-sdk",
        "windows-sdk-ucrt-redist-windows-sdk",
    ]

    same = runtime_policy.allowed_windows_runtime_source_boundaries(
        {
            "SystemRoot": str(windows_root),
            "UniversalCRTSdkDir": str(universal_sdk),
            "WindowsSdkDir": str(universal_sdk),
        }
    )
    assert [name for name, _path in same] == [
        "windows-system32",
        "windows-sdk-ucrt-redist",
    ]

    case_variant = runtime_policy.allowed_windows_runtime_source_boundaries(
        {
            "SystemRoot": str(windows_root),
            "UniversalCRTSdkDir": str(universal_sdk),
            "WindowsSdkDir": str(universal_sdk).upper(),
        }
    )
    assert [name for name, _path in case_variant] == [
        "windows-system32",
        "windows-sdk-ucrt-redist",
    ]


def test_windows_runtime_policy_rejects_ambiguous_explicit_boundaries(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    assert runtime_policy._normalized_source_boundaries(
        [("same", first), ("same", first)]
    ) == [("same", first.resolve())]

    with pytest.raises(RuntimeError, match="name refers to different paths"):
        runtime_policy._normalized_source_boundaries(
            [("same", first), ("same", second)]
        )
    with pytest.raises(RuntimeError, match="path has different names"):
        runtime_policy._normalized_source_boundaries(
            [("first", first), ("second", first)]
        )
    with pytest.raises(RuntimeError, match="Overlapping"):
        runtime_policy._normalized_source_boundaries(
            [("parent", first), ("child", first / "nested")]
        )


def test_windows_runtime_policy_rejects_leaf_reparse_source(tmp_path):
    target = _write_runtime_binary(tmp_path / "target.dll", b"target")
    link = tmp_path / "linked.dll"
    try:
        link.symlink_to(target)
    except OSError as exc:
        pytest.skip(
            "Creating a file symlink/reparse point is unavailable on this "
            f"runner: {exc}"
        )

    with pytest.raises(RuntimeError, match="regular non-reparse file"):
        runtime_policy.apply_windows_runtime_policy(
            [("linked.dll", str(link), "BINARY"), _locked_wheel_vcomp(tmp_path)]
        )


def test_windows_runtime_policy_rejects_parent_reparse_boundary_escape(
    request,
    tmp_path,
):
    approved = tmp_path / "approved"
    approved.mkdir()
    outside = tmp_path / "outside"
    source = _write_runtime_binary(outside / "ucrtbase.dll", b"outside")
    linked_parent = approved / "redirected"
    junction_created = False
    try:
        linked_parent.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        if os.name != "nt" or shutil.which("cmd") is None:
            pytest.skip(
                "Creating a directory symlink/reparse point is unavailable on "
                f"this runner: {exc}"
            )
        junction = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(linked_parent), str(outside)],
            check=False,
            capture_output=True,
            text=True,
        )
        if junction.returncode != 0:
            pytest.skip(
                "Creating a parent junction is unavailable on this runner: "
                f"{junction.stderr.strip() or junction.stdout.strip()}"
            )
        junction_created = True

    def remove_parent_reparse() -> None:
        try:
            linked_parent.lstat()
        except FileNotFoundError:
            return
        if junction_created:
            linked_parent.rmdir()
        else:
            linked_parent.unlink()

    request.addfinalizer(remove_parent_reparse)

    with pytest.raises(RuntimeError, match="unapproved source"):
        runtime_policy.apply_windows_runtime_policy(
            [
                ("ucrtbase.dll", str(linked_parent / source.name), "BINARY"),
                _locked_wheel_vcomp(tmp_path),
            ],
            source_boundaries=[("approved", approved)],
        )


def test_windows_runtime_policy_rejects_source_replacement_while_hashing(
    monkeypatch,
    tmp_path,
):
    source = _write_runtime_binary(tmp_path / "demo.pyd", b"original")
    replacement = _write_runtime_binary(
        tmp_path / "replacement.pyd",
        b"replacement has a different identity and size",
    )
    original_hash = runtime_policy._sha256_file

    def replace_after_hash(path):
        digest = original_hash(path)
        if path == source.resolve():
            os.replace(replacement, source)
        return digest

    monkeypatch.setattr(runtime_policy, "_sha256_file", replace_after_hash)

    with pytest.raises(RuntimeError, match="changed while hashing"):
        runtime_policy.apply_windows_runtime_policy(
            [("demo.pyd", str(source), "EXTENSION"), _locked_wheel_vcomp(tmp_path)]
        )


def test_windows_runtime_policy_audit_detects_source_and_payload_tampering(
    tmp_path,
):
    wheel_vcomp = _locked_wheel_vcomp(tmp_path)
    demo = _write_runtime_binary(tmp_path / "demo.pyd", b"extension")
    filtered = [wheel_vcomp, ("demo.pyd", str(demo), "EXTENSION")]
    audit_path = tmp_path / runtime_policy.RUNTIME_POLICY_AUDIT_FILENAME
    runtime_policy.apply_windows_runtime_policy(filtered, audit_path=audit_path)

    original = audit_path.read_text(encoding="utf-8")
    payload = json.loads(original)
    payload["retained_raw_indexes"] = [0]
    audit_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="canonical payload SHA256"):
        runtime_policy.validate_windows_runtime_policy_audit(audit_path, filtered)

    audit_path.write_text(original, encoding="utf-8")
    demo.write_bytes(b"changed after audit")
    with pytest.raises(ValueError, match="source changed after audit"):
        runtime_policy.validate_windows_runtime_policy_audit(audit_path, filtered)


def test_windows_runtime_policy_rejects_partition_and_casefold_duplicates(
    tmp_path,
):
    wheel_vcomp = _locked_wheel_vcomp(tmp_path)
    first = _write_runtime_binary(tmp_path / "first.dll", b"first")
    second = _write_runtime_binary(tmp_path / "second.dll", b"second")
    with pytest.raises(RuntimeError, match="duplicate binary destination"):
        runtime_policy.apply_windows_runtime_policy(
            [
                ("Demo.dll", str(first), "BINARY"),
                ("demo.DLL", str(second), "BINARY"),
                wheel_vcomp,
            ]
        )

    audit_path = tmp_path / runtime_policy.RUNTIME_POLICY_AUDIT_FILENAME
    runtime_policy.apply_windows_runtime_policy(
        [wheel_vcomp, ("first.dll", str(first), "BINARY")],
        audit_path=audit_path,
    )
    payload = json.loads(audit_path.read_text(encoding="utf-8"))
    payload["retained_raw_indexes"] = [0]
    unsigned = dict(payload)
    unsigned.pop("payload_sha256")
    payload["payload_sha256"] = runtime_policy._canonical_json_sha256(unsigned)
    audit_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="partition differs"):
        runtime_policy.validate_windows_runtime_policy_audit(
            audit_path,
            [wheel_vcomp, ("first.dll", str(first), "BINARY")],
        )


def test_windows_runtime_policy_audit_rejects_bool_and_unhashable_fields(tmp_path):
    wheel_vcomp = _locked_wheel_vcomp(tmp_path)
    first = _write_runtime_binary(tmp_path / "first.dll", b"first")
    filtered = [wheel_vcomp, ("first.dll", str(first), "BINARY")]
    audit_path = tmp_path / runtime_policy.RUNTIME_POLICY_AUDIT_FILENAME
    runtime_policy.apply_windows_runtime_policy(filtered, audit_path=audit_path)
    original = json.loads(audit_path.read_text(encoding="utf-8"))

    def write_signed(payload):
        unsigned = dict(payload)
        unsigned.pop("payload_sha256", None)
        payload["payload_sha256"] = runtime_policy._canonical_json_sha256(
            unsigned
        )
        audit_path.write_text(json.dumps(payload), encoding="utf-8")

    boolean_raw_index = copy.deepcopy(original)
    boolean_raw_index["raw_binaries"][0]["raw_index"] = False
    write_signed(boolean_raw_index)
    with pytest.raises(ValueError, match="raw indexes are not contiguous"):
        runtime_policy.validate_windows_runtime_policy_audit(audit_path, filtered)

    boolean_schema = copy.deepcopy(original)
    boolean_schema["schema_version"] = True
    write_signed(boolean_schema)
    with pytest.raises(ValueError, match="Unsupported.*schema"):
        runtime_policy.validate_windows_runtime_policy_audit(audit_path, filtered)

    boolean_retained_index = copy.deepcopy(original)
    boolean_retained_index["retained_raw_indexes"][0] = False
    write_signed(boolean_retained_index)
    with pytest.raises(ValueError, match="decision sets are invalid"):
        runtime_policy.validate_windows_runtime_policy_audit(audit_path, filtered)

    unhashable_type = copy.deepcopy(original)
    unhashable_type["raw_binaries"][0]["type"] = []
    write_signed(unhashable_type)
    with pytest.raises(ValueError, match="source or type is invalid"):
        runtime_policy.validate_windows_runtime_policy_audit(audit_path, filtered)


def test_windows_runtime_policy_fails_without_exact_wheel_vcomp(tmp_path):
    demo = _write_runtime_binary(tmp_path / "demo.pyd", b"demo")
    with pytest.raises(RuntimeError, match="exactly one locked scikit-learn"):
        runtime_policy.apply_windows_runtime_policy(
            [("demo.pyd", str(demo), "EXTENSION")]
        )

    duplicate = _locked_wheel_vcomp(tmp_path)
    with pytest.raises(RuntimeError, match="duplicate binary destination"):
        runtime_policy.apply_windows_runtime_policy([duplicate, duplicate])


def _fake_analysis_type(save_guts):
    analysis_type = type(
        "Analysis",
        (),
        {
            "_GUTS": tuple(
                (name, None) for name in runtime_policy.ANALYSIS_GUTS_FIELDS
            ),
            "_save_guts": save_guts,
        },
    )
    analysis_type.__module__ = "PyInstaller.building.build_main"
    return analysis_type


def _fake_analysis(tmp_path: Path, save_guts):
    analysis = _fake_analysis_type(save_guts)()
    for field in runtime_policy.ANALYSIS_GUTS_FIELDS:
        setattr(analysis, field, [])
    analysis.binaries = [_locked_wheel_vcomp(tmp_path)]
    analysis.datas = []
    analysis.tocfilename = str((tmp_path / "Analysis-00.toc").resolve())
    return analysis


def test_private_analysis_contract_is_saved_and_re_read(tmp_path):
    def save_guts(self):
        payload = tuple(
            getattr(self, name) for name in runtime_policy.ANALYSIS_GUTS_FIELDS
        )
        Path(self.tocfilename).write_text(repr(payload), encoding="utf-8")

    analysis = _fake_analysis(tmp_path, save_guts)

    audit_path = runtime_policy.apply_windows_runtime_policy_to_analysis(analysis)

    assert audit_path.name == runtime_policy.RUNTIME_POLICY_AUDIT_FILENAME
    assert audit_path.is_file()
    saved = ast.literal_eval(Path(analysis.tocfilename).read_text(encoding="utf-8"))
    assert saved[runtime_policy.ANALYSIS_BINARY_INDEX] == analysis.binaries


def test_private_analysis_contract_fails_on_shape_or_saved_toc_change(tmp_path):
    def save_wrong_guts(self):
        payload = [
            getattr(self, name) for name in runtime_policy.ANALYSIS_GUTS_FIELDS
        ]
        payload[runtime_policy.ANALYSIS_BINARY_INDEX] = []
        Path(self.tocfilename).write_text(repr(tuple(payload)), encoding="utf-8")

    wrong_saved = _fake_analysis(tmp_path, save_wrong_guts)
    with pytest.raises(RuntimeError, match="Saved PyInstaller Analysis binaries"):
        runtime_policy.apply_windows_runtime_policy_to_analysis(wrong_saved)

    valid = _fake_analysis(tmp_path / "shape", save_wrong_guts)
    valid._GUTS = valid._GUTS[:-1]
    with pytest.raises(RuntimeError, match="_GUTS fields changed"):
        runtime_policy.apply_windows_runtime_policy_to_analysis(valid)


def test_runtime_policy_manifest_binds_sanitized_audit_to_external_provenance(
    tmp_path,
):
    package_manifest = {"build_provenance_sha256": "a" * 64}
    retained_path = _write_runtime_binary(
        tmp_path / "_internal" / "sklearn" / ".libs" / "vcomp140.dll",
        b"locked wheel vcomp",
    )
    retained_size = retained_path.stat().st_size
    retained_sha256 = sha256_file(retained_path)
    artifact = {
        "filename": runtime_policy.RUNTIME_POLICY_AUDIT_FILENAME,
        "size": 123,
        "sha256": "b" * 64,
        "payload_sha256": "c" * 64,
    }
    raw_records = [
        {
            "raw_index": 0,
            "destination": "VCOMP140.DLL",
            "source": "windows-system32/VCOMP140.DLL",
            "source_component": "windows-system32",
            "type": "BINARY",
            "size": 12,
            "sha256": "f" * 64,
            "decision": "excluded",
            "reason": "redundant-root-vcomp",
        },
        {
            "raw_index": 1,
            "destination": "sklearn/.libs/vcomp140.dll",
            "source": "scikit-learn/Lib/site-packages/sklearn/.libs/vcomp140.dll",
            "source_component": "microsoft-vc-runtime",
            "type": "BINARY",
            "size": retained_size,
            "sha256": retained_sha256,
            "decision": "retained",
        },
    ]
    normalized_digest = hashlib.sha256(
        json.dumps(
            raw_records,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    summary = {
        "toc_files": {
            "Analysis-00.toc": {"sha256": "2" * 64},
            "COLLECT-00.toc": {"sha256": "3" * 64},
        },
        "runtime_policy_audit": {
            "artifact": artifact,
            "policy": runtime_policy.RUNTIME_POLICY_NAME,
            "pyinstaller_version": metadata.version("PyInstaller"),
            "raw_inventory_sha256": "d" * 64,
            "policy_result_sha256": "e" * 64,
            "normalized_raw_inventory_sha256": normalized_digest,
            "raw_binary_count": 2,
            "retained_binary_count": 1,
            "excluded_binary_count": 1,
            "allowed_source_boundaries": ["windows-system32"],
            "raw_binaries": raw_records,
            "excluded_binaries": [
                {
                    "raw_index": 0,
                    "destination": "VCOMP140.DLL",
                    "source": "windows-system32/VCOMP140.DLL",
                    "source_boundary": "windows-system32",
                    "type": "BINARY",
                    "size": 12,
                    "sha256": "f" * 64,
                    "reason": "redundant-root-vcomp",
                }
            ],
        },
        "provenance_binding": {
            "runtime_policy_audit_sha256": "b" * 64,
            "runtime_policy_payload_sha256": "c" * 64,
            "raw_inventory_sha256": "d" * 64,
            "policy_result_sha256": "e" * 64,
            "normalized_raw_inventory_sha256": normalized_digest,
            "analysis_toc_sha256": "2" * 64,
            "collect_toc_sha256": "3" * 64,
            "build_provenance_sha256": "a" * 64,
        },
    }

    summary = compliance._write_runtime_policy_attestation(tmp_path, summary)
    manifest_records = [
        {
            "path": "_internal/sklearn/.libs/vcomp140.dll",
            "size": retained_size,
            "sha256": retained_sha256,
            "component": "microsoft-vc-runtime",
            "toc_name": "sklearn/.libs/vcomp140.dll",
            "toc_type": "BINARY",
        },
        {
            "path": compliance.RUNTIME_POLICY_ATTESTATION_RELATIVE_PATH,
            "size": (
                tmp_path / compliance.RUNTIME_POLICY_ATTESTATION_RELATIVE_PATH
            ).stat().st_size,
            "sha256": sha256_file(
                tmp_path / compliance.RUNTIME_POLICY_ATTESTATION_RELATIVE_PATH
            ),
            "component": "license-materials",
        },
    ]
    physical = compliance._physical_files(tmp_path)
    collect_entries = [
        {
            "toc_name": "sklearn/.libs/vcomp140.dll",
            "path": "_internal/sklearn/.libs/vcomp140.dll",
            "source": str(retained_path),
            "type": "BINARY",
        }
    ]

    def validate(candidate):
        return compliance._validate_runtime_policy_manifest_binding(
            candidate,
            package_manifest,
            _component_lock(),
            tmp_path,
            physical,
            manifest_records,
            collect_entries,
        )

    assert validate(summary) == []

    wrong_policy = copy.deepcopy(summary)
    wrong_policy["runtime_policy_audit"]["policy"] = "allow-all"
    assert any("policy identifier differs" in error for error in validate(wrong_policy))

    wrong_version = copy.deepcopy(summary)
    wrong_version["runtime_policy_audit"]["pyinstaller_version"] = "0.0.0"
    assert any(
        "PyInstaller version differs" in error for error in validate(wrong_version)
    )

    unbound_result = copy.deepcopy(summary)
    unbound_result["provenance_binding"]["policy_result_sha256"] = "0" * 64
    assert any(
        "external provenance binding differs" in error
        for error in validate(unbound_result)
    )

    leaked = copy.deepcopy(summary)
    leaked["runtime_policy_audit"]["excluded_binaries"][0]["source"] = (
        "C:/Windows/System32/VCOMP140.DLL"
    )
    assert any(
        "exclusion source is unsafe" in error
        for error in validate(leaked)
    )

    unbound = copy.deepcopy(summary)
    unbound["provenance_binding"]["build_provenance_sha256"] = "0" * 64
    assert any(
        "external provenance binding differs" in error
        for error in validate(unbound)
    )

    extra_path = copy.deepcopy(summary)
    extra_path["runtime_policy_audit"]["raw_binaries"][1][
        "original_source"
    ] = r"C:\runner\venv\sklearn\.libs\vcomp140.dll"
    assert any(
        "normalized raw structure changed" in error
        for error in validate(extra_path)
    )

    rewritten = copy.deepcopy(summary)
    rewritten_raw = rewritten["runtime_policy_audit"]["raw_binaries"]
    rewritten_raw[1]["sha256"] = "0" * 64
    rewritten_digest = runtime_policy._canonical_json_sha256(rewritten_raw)
    rewritten["runtime_policy_audit"][
        "normalized_raw_inventory_sha256"
    ] = rewritten_digest
    rewritten["provenance_binding"][
        "normalized_raw_inventory_sha256"
    ] = rewritten_digest
    rewritten_errors = validate(rewritten)
    assert any("retained runtime metadata differs" in error for error in rewritten_errors)
    assert any("attestation payload differs" in error for error in rewritten_errors)

    for invalid_raw_index in ([], True, -1, 99):
        invalid_index = copy.deepcopy(summary)
        invalid_index["runtime_policy_audit"]["excluded_binaries"][0][
            "raw_index"
        ] = invalid_raw_index
        assert any(
            "exclusion raw index is invalid" in error
            for error in validate(invalid_index)
        )

    invalid_count = copy.deepcopy(summary)
    invalid_count["runtime_policy_audit"]["raw_binary_count"] = True
    assert any("counts are inconsistent" in error for error in validate(invalid_count))

    invalid_raw_record = copy.deepcopy(summary)
    invalid_raw_record["runtime_policy_audit"]["raw_binaries"][0][
        "raw_index"
    ] = False
    assert any(
        "normalized raw record is invalid" in error
        for error in validate(invalid_raw_record)
    )

    unhashable_metadata = copy.deepcopy(summary)
    unhashable_metadata["runtime_policy_audit"]["excluded_binaries"][0][
        "reason"
    ] = []
    assert any(
        "exclusion metadata is invalid" in error
        for error in validate(unhashable_metadata)
    )

    invalid_manifest_records = copy.deepcopy(manifest_records)
    invalid_manifest_records[0]["toc_type"] = []
    invalid_manifest_errors = compliance._validate_runtime_policy_manifest_binding(
        summary,
        package_manifest,
        _component_lock(),
        tmp_path,
        physical,
        invalid_manifest_records,
        collect_entries,
    )
    assert any(
        "COLLECT record type is invalid" in error
        for error in invalid_manifest_errors
    )

    excluded_path = _write_runtime_binary(
        tmp_path / "_internal" / "VCOMP140.DLL",
        b"must not be distributed",
    )
    assert excluded_path.is_file()
    errors_with_excluded = compliance._validate_runtime_policy_manifest_binding(
        summary,
        package_manifest,
        _component_lock(),
        tmp_path,
        compliance._physical_files(tmp_path),
        manifest_records,
        collect_entries,
    )
    assert any(
        "excluded runtime is present" in error for error in errors_with_excluded
    )
