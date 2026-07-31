import base64
import hashlib
import importlib.metadata
import io
import json
import os
import platform
import shutil
import stat
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from scripts import prepare_release_assets as release_assets
from scripts.prepare_release_assets import (
    ReleaseAssetError,
    binary_archive_records,
    create_release_assets,
    create_verified_binary_manifest,
    fetch_verified_sources,
    partition_sources,
    release_gate_errors,
    source_archive_records,
    validate_application_source,
    verify_binary_manifest,
    verify_license_archive,
    verify_release_asset_list,
    verify_source_part,
)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _wheel_bytes(name: str = "demo", version: str = "1.0") -> bytes:
    metadata_path = f"{name}-{version}.dist-info/METADATA"
    wheel_path = f"{name}-{version}.dist-info/WHEEL"
    record_path = f"{name}-{version}.dist-info/RECORD"
    metadata = (
        f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"
    ).encode()
    wheel_metadata = (
        b"Wheel-Version: 1.0\nGenerator: tests\nRoot-Is-Purelib: false\n"
        b"Tag: cp314-cp314-win_amd64\n"
    )
    members = {metadata_path: metadata, wheel_path: wheel_metadata}
    rows = []
    for member_path, payload in members.items():
        encoded_digest = base64.urlsafe_b64encode(
            hashlib.sha256(payload).digest()
        ).rstrip(b"=").decode("ascii")
        rows.append(f"{member_path},sha256={encoded_digest},{len(payload)}")
    record = "\n".join([*rows, f"{record_path},,"]) + "\n"
    wheel_buffer = io.BytesIO()
    with zipfile.ZipFile(wheel_buffer, "w") as archive:
        for member_path, payload in members.items():
            archive.writestr(member_path, payload)
        archive.writestr(record_path, record)
    return wheel_buffer.getvalue()


def _wheel_contents(wheel: bytes) -> list[dict[str, object]]:
    with zipfile.ZipFile(io.BytesIO(wheel)) as archive:
        return [
            {
                "path": info.filename,
                "size": info.file_size,
                "sha256": _sha(archive.read(info)),
            }
            for info in sorted(archive.infolist(), key=lambda item: item.filename)
            if not info.is_dir()
        ]


def _installable_wheel_bytes(name: str = "demo", version: str = "1.0") -> bytes:
    abi = "cp" + "".join(sys.version.split()[0].split(".")[:2])
    dist_info = f"{name}-{version}.dist-info"
    members = {
        f"{name}/__init__.py": b"VALUE = 'installed from locked wheel'\n",
        f"{dist_info}/METADATA": (
            f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"
        ).encode(),
        f"{dist_info}/WHEEL": (
            "Wheel-Version: 1.0\n"
            "Generator: LoLReplayTool tests\n"
            "Root-Is-Purelib: false\n"
            f"Tag: {abi}-{abi}-win_amd64\n"
        ).encode(),
    }
    record_path = f"{dist_info}/RECORD"
    record_rows = []
    for member_path, payload in members.items():
        encoded_digest = base64.urlsafe_b64encode(
            hashlib.sha256(payload).digest()
        ).rstrip(b"=").decode("ascii")
        record_rows.append(
            f"{member_path},sha256={encoded_digest},{len(payload)}"
        )
    record = "\n".join([*record_rows, f"{record_path},,"]) + "\n"
    wheel_buffer = io.BytesIO()
    with zipfile.ZipFile(wheel_buffer, "w") as archive:
        for member_path, payload in members.items():
            archive.writestr(member_path, payload)
        archive.writestr(record_path, record)
    return wheel_buffer.getvalue()


def _binary_lock(wheel: bytes) -> dict[str, object]:
    lock = _lock(b"one", b"two")
    python_version = sys.version.split()[0]
    expected_abi = "cp" + "".join(python_version.split(".")[:2])
    lock["python"]["release_version"] = python_version
    component = lock["runtime_components"][0]
    component["distribution"] = "demo"
    component["artifact_patterns"] = ["_internal/demo/**/*.pyd"]
    component["binary_archive"] = {
        "filename": f"demo-1.0-{expected_abi}-{expected_abi}-win_amd64.whl",
        "url": "https://example.invalid/demo.whl",
        "sha256": _sha(wheel),
        "size": len(wheel),
        "contents": _wheel_contents(wheel),
    }
    lock["release_binary_policy"] = {
        "python_implementation": "CPython",
        "python_version": python_version,
        "abi": expected_abi,
        "platform": "win_amd64",
        "pip_version": "26.0",
        "required_components": ["demo"],
    }
    return lock


def _write_source_zip(path: Path, version: str = "1.2.3") -> None:
    files = {
        "VERSION": (version + "\n").encode(),
        "LICENSE": b"GPL\n",
        "main.py": b"print('test')\n",
    }
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in files.items():
            info = zipfile.ZipInfo(name)
            info.external_attr = 0o100644 << 16
            archive.writestr(info, data)


def _review() -> dict[str, object]:
    return {
        "review_completed": True,
        "evidence": "legal-review-record-1",
        "scope": "automated upstream download",
        "reviewer": "repository administrator",
        "date": "2026-07-31",
    }


def _lock(first: bytes, second: bytes, *, legal_gate: bool = False):
    return {
        "schema_version": 1,
        "historical_remediation": _review(),
        "python": {
            "component": "python",
            "release_version": "3.14.6",
            "license": "PSF-2.0",
            "release_legal_review_required": legal_gate,
            "source_status": "verified_corresponding_source",
            "license_materials_exception": {
                **_review(),
                "reason": "isolated test fixture",
            },
            "source_archives": [
                {
                    "filename": "python-source.tar.gz",
                    "url": "https://example.invalid/python-source.tar.gz",
                    "sha256": _sha(first),
                    "size": len(first),
                }
            ],
        },
        "runtime_components": [
            {
                "component": "demo",
                "version": "1.0",
                "license": "MIT",
                "source_status": "verified_corresponding_source",
                "license_materials_exception": {
                    **_review(),
                    "reason": "isolated test fixture",
                },
                "source_archives": [
                    {
                        "filename": "demo-source.tar.gz",
                        "url": "https://example.invalid/demo-source.tar.gz",
                        "sha256": _sha(second),
                        "size": len(second),
                    }
                ],
            }
        ],
        "build_components": [],
        "runtime_downloads": [
            {
                "component": "download-only",
                "version": "2.0",
                "bundled_in_installer": False,
                "release_legal_review_required": legal_gate,
                "legal_review": _review(),
            }
        ],
    }


def _write_distribution(root: Path) -> None:
    for name in (
        "LICENSE",
        "QT_RELINKING.md",
        "SOURCE_OFFER.md",
        "THIRD_PARTY_NOTICES.md",
        "VERSION",
    ):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("material\n", encoding="utf-8")
    licenses = root / "licenses"
    licenses.mkdir()
    (licenses / "components.json").write_text("{}\n", encoding="utf-8")
    (licenses / "distribution-manifest.json").write_text("{}\n", encoding="utf-8")
    (licenses / "build-provenance.json").write_text(
        json.dumps({"git_source": {"commit": "a" * 40}}) + "\n",
        encoding="utf-8",
    )


def _create_asset_set(
    tmp_path: Path,
    monkeypatch,
    *,
    provenance_seal: str | None = "actual",
) -> tuple[dict[str, object], Path]:
    first = b"python source"
    second = b"demo source"
    lock_path = tmp_path / "components.json"
    lock_path.write_text(json.dumps(_lock(first, second)), encoding="utf-8")
    monkeypatch.setattr(release_assets, "COMPONENTS_FILE", lock_path)
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "python-source.tar.gz").write_bytes(first)
    (cache / "demo-source.tar.gz").write_bytes(second)
    installer = tmp_path / "LoLReplayTool-Setup-1.2.3.exe"
    installer.write_bytes(b"installer")
    source = tmp_path / "LoLReplayTool-source-1.2.3.zip"
    _write_source_zip(source)
    source_files = {
        "VERSION": b"1.2.3\n",
        "LICENSE": b"GPL\n",
        "main.py": b"print('test')\n",
    }
    source_blobs = {
        hashlib.sha1(
            b"blob " + str(len(data)).encode("ascii") + b"\0" + data,
            usedforsecurity=False,
        ).hexdigest(): data
        for data in source_files.values()
    }
    path_blobs = {
        path: next(
            object_id
            for object_id, data in source_blobs.items()
            if data == contents
        )
        for path, contents in source_files.items()
    }

    def fake_git_output(*arguments, binary=False):
        if arguments == ("rev-parse", f"{'a' * 40}^{{commit}}"):
            return "a" * 40 + "\n"
        if arguments == ("ls-tree", "-r", "-z", "a" * 40):
            return b"".join(
                f"100644 blob {path_blobs[path]}\t{path}\0".encode()
                for path in sorted(path_blobs)
            )
        if arguments[:2] == ("cat-file", "blob"):
            return source_blobs[str(arguments[2])]
        raise AssertionError((arguments, binary))

    monkeypatch.setattr(release_assets, "_git_output", fake_git_output)
    distribution = tmp_path / "distribution"
    _write_distribution(distribution)
    build_provenance_sha256 = release_assets.sha256_file(
        distribution / "licenses" / "build-provenance.json"
    )
    if provenance_seal != "actual":
        build_provenance_sha256 = provenance_seal
    output = tmp_path / "release"
    payload = create_release_assets(
        version="1.2.3",
        source_commit="a" * 40,
        installer=installer,
        application_source=source,
        distribution_root=distribution,
        output_dir=output,
        components_file=lock_path,
        cache_dir=cache,
        enforce_release_gates=False,
        build_provenance_sha256=build_provenance_sha256,
    )
    return payload, output


@pytest.mark.parametrize("provenance_seal", [None, "0" * 64])
def test_create_release_assets_requires_exact_external_provenance_seal(
    monkeypatch,
    tmp_path,
    provenance_seal,
):
    with pytest.raises(ReleaseAssetError, match="externally sealed"):
        _create_asset_set(
            tmp_path,
            monkeypatch,
            provenance_seal=provenance_seal,
        )


def _replace_zip_member(path: Path, member_name: str, replacement: bytes) -> None:
    temporary = path.with_suffix(path.suffix + ".replacement")
    with zipfile.ZipFile(path) as source, zipfile.ZipFile(temporary, "w") as target:
        for info in source.infolist():
            data = replacement if info.filename == member_name else source.read(info)
            target.writestr(info, data)
    temporary.replace(path)


def test_create_release_assets_uses_fixed_names_and_hashes(monkeypatch, tmp_path):
    payload, output = _create_asset_set(tmp_path, monkeypatch)

    names = [Path(path).name for path in payload["assets"]]
    assert payload["source_commit"] == "a" * 40
    assert names == [
        "LoLReplayTool-Setup-1.2.3.exe",
        "LoLReplayTool-source-1.2.3.zip",
        "LoLReplayTool-third-party-sources-1.2.3-01.zip",
        "LoLReplayTool-license-materials-1.2.3.zip",
        "SHA256SUMS.txt",
    ]
    checksums = (output / "SHA256SUMS.txt").read_text(encoding="ascii")
    assert checksums.count("\n") == 4
    assert "LoLReplayTool-third-party-sources-1.2.3-01.zip" in checksums
    with zipfile.ZipFile(output / "LoLReplayTool-third-party-sources-1.2.3-01.zip") as archive:
        index = json.loads(archive.read("SOURCE_INDEX.json"))
        assert {record["component"] for record in index["sources"]} == {
            "python",
            "demo",
        }
        assert index["runtime_downloads_not_bundled"][0]["bundled_in_installer"] is False
        assert archive.read("sources/python-source.tar.gz") == b"python source"
    with zipfile.ZipFile(output / "LoLReplayTool-license-materials-1.2.3.zip") as archive:
        assert "QT_RELINKING.md" in archive.namelist()
        assert "licenses/distribution-manifest.json" in archive.namelist()
        license_index = json.loads(archive.read("LICENSE_INDEX.json"))
        assert {record["path"] for record in license_index["files"]} >= {
            "LICENSE",
            "licenses/components.json",
        }

    verify_release_asset_list(Path(payload["asset_list"]))


def test_release_assets_fail_closed_on_hash_mismatch(tmp_path):
    expected = b"expected"
    lock = _lock(expected, b"demo")
    records = source_archive_records(lock)
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / records[0]["filename"]).write_bytes(b"tampered")

    with pytest.raises(ReleaseAssetError, match="SHA256 mismatch"):
        fetch_verified_sources(records[:1], cache)


def test_source_lock_rejects_unsafe_duplicate_and_non_https_names():
    lock = _lock(b"one", b"two")
    lock["runtime_components"][0]["source_archives"][0]["filename"] = "../bad.tgz"
    with pytest.raises(ReleaseAssetError, match="Unsafe"):
        source_archive_records(lock)

    lock = _lock(b"one", b"two")
    lock["runtime_components"][0]["source_archives"][0]["filename"] = "PYTHON-SOURCE.TAR.GZ"
    with pytest.raises(ReleaseAssetError, match="Duplicate"):
        source_archive_records(lock)

    lock = _lock(b"one", b"two")
    lock["runtime_components"][0]["source_archives"][0]["url"] = "http://example.test"
    with pytest.raises(ReleaseAssetError, match="HTTPS"):
        source_archive_records(lock)

    lock = _lock(b"one", b"two")
    del lock["runtime_components"][0]["source_archives"][0]["size"]
    with pytest.raises(ReleaseAssetError, match="declared size"):
        source_archive_records(lock)

    for unsafe_name in ("CON.zip", "source. ", "LPT1.tar.gz"):
        lock = _lock(b"one", b"two")
        lock["runtime_components"][0]["source_archives"][0]["filename"] = unsafe_name
        with pytest.raises(ReleaseAssetError, match="Unsafe"):
            source_archive_records(lock)


def test_identical_shared_source_is_deduplicated_and_binary_archive_ignored():
    lock = _lock(b"shared", b"demo")
    shared = dict(lock["python"]["source_archives"][0])
    lock["runtime_components"][0]["source_archives"] = [shared]
    lock["runtime_components"][0]["binary_archive"] = {
        "filename": "wheel.whl",
        "url": "https://example.invalid/wheel.whl",
        "sha256": _sha(b"wheel"),
        "size": 5,
    }

    records = source_archive_records(lock)

    assert [record["filename"] for record in records] == ["python-source.tar.gz"]
    assert [reference["component"] for reference in records[0]["component_references"]] == ["python", "demo"]


@pytest.mark.parametrize(
    ("downloaded", "message"),
    [
        (b"too long", "exceeds declared size"),
        (b"x", "size mismatch"),
    ],
)
def test_download_aborts_on_streamed_size_mismatch(tmp_path, downloaded, message):
    expected = b"four"
    record = source_archive_records(_lock(expected, b"demo"))[0]

    def opener(_request, *, timeout):
        assert timeout == 120
        return io.BytesIO(downloaded)

    cache = tmp_path / "cache"
    with pytest.raises(ReleaseAssetError, match=message):
        fetch_verified_sources([record], cache, opener=opener)

    assert not (cache / f"{record['filename']}.partial").exists()


def test_release_legal_gate_is_explicit():
    errors = release_gate_errors(_lock(b"one", b"two", legal_gate=True))

    assert any(error.startswith("python:") for error in errors)
    assert any(error.startswith("download-only:") for error in errors)


def test_missing_runtime_and_vendored_sources_are_release_gates():
    lock = _lock(b"one", b"two")
    lock["runtime_components"].append(
        {
            "component": "missing",
            "version": "1",
            "license": "MIT AND bundled component licenses",
        }
    )

    errors = release_gate_errors(lock)

    assert "missing: no verified exact source archive is locked" in errors
    assert any(error.startswith("missing: source coverage for wheel-vendored") for error in errors)
    with pytest.raises(ReleaseAssetError, match="no verified exact source archive"):
        source_archive_records(lock)


def test_source_status_is_required_even_when_an_archive_is_locked():
    lock = _lock(b"one", b"two")
    del lock["runtime_components"][0]["source_status"]

    errors = release_gate_errors(lock)

    assert "demo: source_status is not verified_corresponding_source" in errors


def test_historical_remediation_requires_complete_review_evidence():
    lock = _lock(b"one", b"two")
    lock["historical_remediation"]["evidence"] = ""

    assert any(
        error.startswith("v0.5.2-historical-remediation:")
        for error in release_gate_errors(lock)
    )


def test_native_wheel_metadata_is_required_and_tampering_fails(
    monkeypatch,
    tmp_path,
):
    wheel = _wheel_bytes()
    lock = _lock(b"one", b"two")
    component = lock["runtime_components"][0]
    component["distribution"] = "demo"
    component["artifact_patterns"] = ["_internal/demo/**/*.pyd"]
    lock["release_binary_policy"] = {
        "python_implementation": "CPython",
        "python_version": "3.14.6",
        "abi": "cp314",
        "platform": "win_amd64",
        "pip_version": "26.0",
        "required_components": ["demo"],
    }
    monkeypatch.setattr(
        release_assets,
        "REQUIRED_RELEASE_BINARY_COMPONENTS",
        frozenset({"demo"}),
    )
    assert any("binary_archive metadata is missing" in error for error in release_gate_errors(lock))

    component["binary_archive"] = {
        "filename": "demo-1.0-cp314-cp314-win_amd64.whl",
        "url": "https://example.invalid/demo.whl",
        "sha256": _sha(wheel),
        "size": len(wheel),
    }
    assert binary_archive_records(lock)[0]["component"] == "demo"
    component["binary_archive"]["contents"] = _wheel_contents(wheel)
    assert binary_archive_records(lock)[0]["contents"] == _wheel_contents(wheel)
    lock_path = tmp_path / "components.json"
    lock_path.write_text(json.dumps(lock), encoding="utf-8")
    cache = tmp_path / "binary-cache"
    manifest = cache / "verified-binaries.json"

    def opener(_request, *, timeout):
        assert timeout == 120
        return io.BytesIO(wheel)

    create_verified_binary_manifest(
        lock_path,
        cache,
        manifest,
        opener=opener,
    )
    binary_payload = json.loads(manifest.read_text(encoding="utf-8"))
    assert binary_payload["archives"][0]["contents"] == component[
        "binary_archive"
    ]["contents"]
    assert binary_payload["archives"][0]["verified_contents"] == _wheel_contents(
        wheel
    )
    verify_binary_manifest(manifest, components_file=lock_path)
    (cache / component["binary_archive"]["filename"]).write_bytes(b"tampered")
    with pytest.raises(ReleaseAssetError, match="size mismatch|SHA256 mismatch"):
        verify_binary_manifest(manifest, components_file=lock_path)


def test_wheel_record_digest_tampering_fails_even_when_archive_hash_is_locked(
    monkeypatch,
    tmp_path,
):
    valid_wheel = _wheel_bytes()
    source = zipfile.ZipFile(io.BytesIO(valid_wheel))
    tampered_buffer = io.BytesIO()
    with zipfile.ZipFile(tampered_buffer, "w") as archive:
        for info in source.infolist():
            if info.filename != "demo-1.0.dist-info/RECORD":
                archive.writestr(info, source.read(info))
        archive.writestr(
            "demo-1.0.dist-info/RECORD",
            "demo-1.0.dist-info/METADATA,sha256=invalid,1\n"
            "demo-1.0.dist-info/RECORD,,\n",
        )
    source.close()
    wheel = tampered_buffer.getvalue()
    lock = _binary_lock(wheel)
    monkeypatch.setattr(
        release_assets,
        "REQUIRED_RELEASE_BINARY_COMPONENTS",
        frozenset({"demo"}),
    )
    lock_path = tmp_path / "components.json"
    lock_path.write_text(json.dumps(lock), encoding="utf-8")

    with pytest.raises(ReleaseAssetError, match="RECORD digest or size differs"):
        create_verified_binary_manifest(
            lock_path,
            tmp_path / "binary-cache",
            tmp_path / "binary-cache" / "verified-binaries.json",
            opener=lambda _request, *, timeout: io.BytesIO(wheel),
        )


def test_locked_wheel_runtime_read_error_fails_closed(monkeypatch, tmp_path):
    wheel_path = tmp_path / "demo-1.0-cp314-cp314-win_amd64.whl"
    wheel_path.write_bytes(b"not-empty")

    def reject_archive(_path):
        raise RuntimeError("encrypted member")

    monkeypatch.setattr(release_assets.zipfile, "ZipFile", reject_archive)

    with pytest.raises(ReleaseAssetError, match="Cannot inspect locked wheel"):
        release_assets._verify_wheel_metadata(
            wheel_path,
            {"distribution": "demo", "version": "1.0"},
        )


def test_repository_lock_requires_exact_ruff_windows_wheel():
    lock = json.loads(
        release_assets.COMPONENTS_FILE.read_text(encoding="utf-8")
    )
    records = {
        record["component"]: record for record in binary_archive_records(lock)
    }

    assert "ruff" in release_assets.REQUIRED_RELEASE_BINARY_COMPONENTS
    assert "ruff" in lock["release_binary_policy"]["required_components"]
    assert records["ruff"] == {
        "component": "ruff",
        "distribution": "ruff",
        "version": "0.15.12",
        "filename": "ruff-0.15.12-py3-none-win_amd64.whl",
        "url": "https://files.pythonhosted.org/packages/33/f1/9614e03e1cdcbf9437570b5400ced8a720b5db22b28d8e0f1bda429f660d/ruff-0.15.12-py3-none-win_amd64.whl",
        "sha256": "c87a162d61ab3adca47c03f7f717c68672edec7d1b5499e652331780fe74950d",
        "size": 11837758,
        "contents": [
            {
                "path": "ruff-0.15.12.data/scripts/ruff.exe",
                "size": 32350208,
                "sha256": "ccfbe6e11d75c3c2b6b419adf1fd018de519055543d28d261caad3cf78335754",
            }
        ],
    }


def test_binary_install_plan_binds_requirements_and_locked_wheel(
    monkeypatch,
    tmp_path,
):
    wheel = _wheel_bytes()
    lock = _binary_lock(wheel)
    monkeypatch.setattr(
        release_assets,
        "REQUIRED_RELEASE_BINARY_COMPONENTS",
        frozenset({"demo"}),
    )
    lock_path = tmp_path / "components.json"
    lock_path.write_text(json.dumps(lock), encoding="utf-8")
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("demo==1.0\n", encoding="utf-8")
    cache = tmp_path / "binary-cache"
    generated = tmp_path / "release-requirements.txt"
    plan_path = tmp_path / "release-install-plan.json"

    def opener(_request, *, timeout):
        assert timeout == 120
        return io.BytesIO(wheel)

    plan = release_assets.prepare_binary_install(
        components_file=lock_path,
        requirements_file=requirements,
        cache_dir=cache,
        output_requirements=generated,
        output_plan=plan_path,
        opener=opener,
    )

    wheel_path = cache / lock["runtime_components"][0]["binary_archive"]["filename"]
    assert generated.read_text(encoding="utf-8").splitlines()[-1] == (
        f"demo @ {wheel_path.resolve().as_uri()}#sha256={_sha(wheel)}"
    )
    assert plan["requirements"][0]["source"] == "locked-wheel"
    assert plan["requirements"][0]["size"] == len(wheel)
    assert plan["generated_requirements_sha256"] == release_assets.sha256_file(
        generated
    )


def test_binary_install_attestation_rejects_plan_and_report_tampering(
    monkeypatch,
    tmp_path,
):
    if os.name != "nt":
        pytest.skip("Release build attestation is Windows-specific")
    wheel = _wheel_bytes()
    lock = _binary_lock(wheel)
    monkeypatch.setattr(
        release_assets,
        "REQUIRED_RELEASE_BINARY_COMPONENTS",
        frozenset({"demo"}),
    )
    monkeypatch.setattr(release_assets.platform, "machine", lambda: "AMD64")
    monkeypatch.setattr(
        release_assets,
        "probe_python_native_runtime",
        lambda _lock: {"python_version": sys.version.split()[0]},
    )
    monkeypatch.setattr(
        release_assets,
        "_verify_bootstrap_pip_environment",
        lambda _runtime: {
            "filename": "pip.whl",
            "version": "26.0",
            "size": 1,
            "sha256": "a" * 64,
            "inventory_sha256": "b" * 64,
            "files": [],
        },
    )
    monkeypatch.setattr(
        release_assets,
        "_verify_installed_distribution_from_wheel",
        lambda _distribution, _wheel: {
            "inventory_sha256": "d" * 64,
            "files": [],
        },
    )
    monkeypatch.setattr(
        release_assets,
        "_verify_environment_file_ownership",
        lambda _allowed: None,
    )
    monkeypatch.setattr(
        release_assets,
        "capture_git_source_identity",
        lambda: {
            "commit": "a" * 40,
            "tree": "b" * 40,
            "tracked_file_count": 1,
            "tracked_inventory_sha256": "c" * 64,
        },
    )
    lock_path = tmp_path / "components.json"
    lock_path.write_text(json.dumps(lock), encoding="utf-8")
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("demo==1.0\n", encoding="utf-8")
    cache = tmp_path / "binary-cache"
    generated = tmp_path / "release-requirements.txt"
    plan_path = tmp_path / "release-install-plan.json"
    report_path = tmp_path / "pip-report.json"
    provenance_path = tmp_path / "build-provenance.json"

    release_assets.prepare_binary_install(
        components_file=lock_path,
        requirements_file=requirements,
        cache_dir=cache,
        output_requirements=generated,
        output_plan=plan_path,
        opener=lambda _request, *, timeout: io.BytesIO(wheel),
    )
    binary_archive = lock["runtime_components"][0]["binary_archive"]
    wheel_path = cache / binary_archive["filename"]
    wheel_url = wheel_path.resolve().as_uri()
    direct_url = json.dumps(
        {
            "url": wheel_url,
            "archive_info": {"hashes": {"sha256": _sha(wheel)}},
        }
    )

    class FakeDistribution:
        version = "1.0"

        @staticmethod
        def read_text(name):
            return direct_url if name == "direct_url.json" else None

    monkeypatch.setattr(
        importlib.metadata,
        "distribution",
        lambda _name: FakeDistribution(),
    )
    report = {
        "version": "1",
        "pip_version": "26.0",
        "install": [
            {
                "download_info": {
                    "url": wheel_url,
                    "archive_info": {"hashes": {"sha256": _sha(wheel)}},
                },
                "is_direct": True,
                "is_yanked": False,
                "requested": True,
                "metadata": {"name": "demo", "version": "1.0"},
            }
        ],
    }
    report_path.write_text(json.dumps(report), encoding="utf-8")

    provenance = release_assets.attest_binary_install(
        components_file=lock_path,
        plan_path=plan_path,
        pip_report_path=report_path,
        output_provenance=provenance_path,
    )
    assert provenance["installed_binaries"][0]["sha256"] == _sha(wheel)
    assert provenance["installed_binaries"][0]["size"] == len(wheel)

    generated_text = generated.read_text(encoding="utf-8")
    generated.write_text("demo==9.9\n", encoding="utf-8")
    with pytest.raises(ReleaseAssetError, match="requirements hash differs"):
        release_assets.attest_binary_install(
            components_file=lock_path,
            plan_path=plan_path,
            pip_report_path=report_path,
            output_provenance=provenance_path,
        )

    generated.write_text(generated_text, encoding="utf-8")
    report["install"][0]["download_info"]["archive_info"]["hashes"][
        "sha256"
    ] = "0" * 64
    report_path.write_text(json.dumps(report), encoding="utf-8")
    with pytest.raises(ReleaseAssetError, match="report SHA256 differs"):
        release_assets.attest_binary_install(
            components_file=lock_path,
            plan_path=plan_path,
            pip_report_path=report_path,
            output_provenance=provenance_path,
        )


def test_real_venv_attests_local_wheel_pip_report_and_direct_url(
    monkeypatch,
    tmp_path,
):
    if os.name != "nt" or platform.machine().casefold() not in {
        "amd64",
        "x86_64",
    }:
        pytest.skip("Release build attestation requires Windows amd64")

    venv_dir = tmp_path / "v"
    subprocess.run(
        [sys.executable, "-m", "venv", str(venv_dir)],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    venv_python = venv_dir / "Scripts" / "python.exe"
    pip_version = subprocess.run(
        [
            str(venv_python),
            "-c",
            "import importlib.metadata as m; print(m.version('pip'))",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout.strip()

    wheel = _installable_wheel_bytes()
    lock = _binary_lock(wheel)
    lock["release_binary_policy"]["pip_version"] = pip_version
    monkeypatch.setattr(
        release_assets,
        "REQUIRED_RELEASE_BINARY_COMPONENTS",
        frozenset({"demo"}),
    )
    lock_path = tmp_path / "components.json"
    lock_path.write_text(json.dumps(lock), encoding="utf-8")
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("demo==1.0\n", encoding="utf-8")
    cache = tmp_path / "binary-cache"
    generated = tmp_path / "release-requirements.txt"
    plan_path = tmp_path / "release-install-plan.json"
    report_path = tmp_path / "pip-report.json"
    provenance_path = tmp_path / "build-provenance.json"
    release_assets.prepare_binary_install(
        components_file=lock_path,
        requirements_file=requirements,
        cache_dir=cache,
        output_requirements=generated,
        output_plan=plan_path,
        opener=lambda _request, *, timeout: io.BytesIO(wheel),
    )

    subprocess.run(
        [
            str(venv_python),
            "-m",
            "pip",
            "--isolated",
            "--disable-pip-version-check",
            "install",
            "--no-input",
            "--no-cache-dir",
            "--require-virtualenv",
            "--no-deps",
            "--only-binary=:all:",
            "--require-hashes",
            "--force-reinstall",
            "--no-index",
            "--report",
            str(report_path),
            "-r",
            str(generated),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    subprocess.run(
        [str(venv_python), "-m", "pip", "check"],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )

    attestation_script = (
        "import sys; from importlib import metadata; from pathlib import Path; "
        "from scripts import prepare_release_assets as assets; "
        "assets.REQUIRED_RELEASE_BINARY_COMPONENTS=frozenset({'demo'}); "
        "pip_wheel=next((Path(sys.base_prefix)/'Lib/ensurepip/_bundled').glob('pip-*.whl')); "
        "pip_inventory=assets._verify_installed_distribution_from_wheel('pip', pip_wheel); "
        "bootstrap={'filename':pip_wheel.name,'version':metadata.version('pip'),"
        "'size':pip_wheel.stat().st_size,'sha256':assets.sha256_file(pip_wheel),"
        "**pip_inventory}; "
        "assets.probe_python_native_runtime=lambda lock: "
        "{'python_version': sys.version.split()[0], 'test_fixture': True}; "
        "assets._verify_bootstrap_pip_environment=lambda runtime: bootstrap; "
        "assets.capture_git_source_identity=lambda: "
        "{'commit':'a'*40,'tree':'b'*40,'tracked_file_count':1,"
        "'tracked_inventory_sha256':'c'*64}; "
        "assets.attest_binary_install(components_file=Path(sys.argv[1]), "
        "plan_path=Path(sys.argv[2]), pip_report_path=Path(sys.argv[3]), "
        "output_provenance=Path(sys.argv[4]))"
    )
    subprocess.run(
        [
            str(venv_python),
            "-c",
            attestation_script,
            str(lock_path),
            str(plan_path),
            str(report_path),
            str(provenance_path),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
        cwd=Path(__file__).resolve().parents[1],
    )

    provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    installed = provenance["installed_binaries"]
    assert provenance["pip_version"] == pip_version
    assert len(installed) == 1
    assert installed[0]["component"] == "demo"
    assert installed[0]["size"] == len(wheel)
    assert installed[0]["sha256"] == _sha(wheel)
    assert len(installed[0]["installed_files_sha256"]) == 64

    report = json.loads(report_path.read_text(encoding="utf-8"))
    installed_report = report["install"][0]
    expected_wheel = (
        cache / lock["runtime_components"][0]["binary_archive"]["filename"]
    )
    assert installed_report["requested"] is True
    assert installed_report["is_direct"] is True
    assert installed_report["is_yanked"] is False
    assert installed_report["download_info"]["url"] == expected_wheel.resolve().as_uri()
    assert installed_report["download_info"]["archive_info"]["hashes"][
        "sha256"
    ] == _sha(wheel)

    installed_file_checks = (
        "import os,sys,zipfile; from importlib import metadata; "
        "from pathlib import Path; "
        "from scripts import prepare_release_assets as a; "
        "wheel=Path(sys.argv[1]); "
        "dist=metadata.distribution('demo'); "
        "source=Path(dist.locate_file('demo/__init__.py')); "
        "original=zipfile.ZipFile(wheel).read('demo/__init__.py'); "
        "source.write_bytes(b'tampered'); "
        "\ntry: a._verify_installed_distribution_from_wheel('demo',wheel)\n"
        "except a.ReleaseAssetError: pass\n"
        "else: raise AssertionError('tampered installed file accepted')\n"
        "source.write_bytes(original); source.unlink(); "
        "\ntry: a._verify_installed_distribution_from_wheel('demo',wheel)\n"
        "except a.ReleaseAssetError: pass\n"
        "else: raise AssertionError('missing installed file accepted')\n"
        "source.parent.mkdir(parents=True,exist_ok=True); source.write_bytes(original); "
        "demo=a._verify_installed_distribution_from_wheel('demo',wheel); "
        "pipwheel=next((Path(sys.base_prefix)/'Lib/ensurepip/_bundled').glob('pip-*.whl')); "
        "pip=a._verify_installed_distribution_from_wheel('pip',pipwheel); "
        "allowed={os.path.normcase(str((Path(sys.prefix)/i['path']).resolve())) "
        "for i in [*demo['files'],*pip['files']]}; "
        "rogue=Path(dist.locate_file('rogue-owned-by-nobody.py')); "
        "rogue.write_text('rogue',encoding='utf-8'); "
        "\ntry: a._verify_environment_file_ownership(allowed)\n"
        "except a.ReleaseAssetError: pass\n"
        "else: raise AssertionError('rogue installed file accepted')\n"
        "rogue.unlink()"
    )
    subprocess.run(
        [str(venv_python), "-c", installed_file_checks, str(expected_wheel)],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
        cwd=Path(__file__).resolve().parents[1],
    )


def test_release_binary_policy_rejects_wrong_wheel_platform(monkeypatch):
    wheel = _wheel_bytes()
    lock = _binary_lock(wheel)
    monkeypatch.setattr(
        release_assets,
        "REQUIRED_RELEASE_BINARY_COMPONENTS",
        frozenset({"demo"}),
    )
    lock["runtime_components"][0]["binary_archive"]["filename"] = (
        "demo-1.0-cp314-cp314-manylinux_x86_64.whl"
    )

    assert any(
        "binary wheel platform" in error for error in release_gate_errors(lock)
    )


@pytest.mark.parametrize("field", ["url", "sha256", "size"])
def test_release_binary_archive_required_fields_are_gates(monkeypatch, field):
    wheel = _wheel_bytes()
    lock = _binary_lock(wheel)
    monkeypatch.setattr(
        release_assets,
        "REQUIRED_RELEASE_BINARY_COMPONENTS",
        frozenset({"demo"}),
    )
    del lock["runtime_components"][0]["binary_archive"][field]

    assert any(
        f"binary_archive.{field}" in error for error in release_gate_errors(lock)
    )


@pytest.mark.parametrize(
    "field",
    ["python_implementation", "python_version", "abi", "platform"],
)
def test_release_binary_policy_required_fields_are_gates(monkeypatch, field):
    wheel = _wheel_bytes()
    lock = _binary_lock(wheel)
    monkeypatch.setattr(
        release_assets,
        "REQUIRED_RELEASE_BINARY_COMPONENTS",
        frozenset({"demo"}),
    )
    del lock["release_binary_policy"][field]

    assert any(
        f"release_binary_policy: {field}" in error
        for error in release_gate_errors(lock)
    )


def test_license_material_hash_is_a_release_gate():
    lock = _lock(b"one", b"two")
    component = lock["runtime_components"][0]
    component.pop("license_materials_exception")
    component["license_materials"] = [{"path": "licenses/demo/LICENSE"}]

    assert any(
        "invalid license material SHA256" in error
        for error in release_gate_errors(lock)
    )


def test_structural_qt_gates_cannot_be_cleared_by_legal_flag():
    lock = _lock(b"one", b"two")
    lock["runtime_components"].append(
        {
            "component": "qt",
            "version": "6.10.2",
            "license": "LGPL-3.0-only",
            "release_legal_review_required": False,
            "source_status": "upstream_reference_not_verified_corresponding_source",
            "wheel_build_provenance_verified": False,
            "source_archives": [
                {
                    "filename": "qt.tar.xz",
                    "url": "https://example.invalid/qt.tar.xz",
                    "sha256": _sha(b"qt"),
                    "size": 2,
                }
            ],
        }
    )

    errors = release_gate_errors(lock)

    assert "qt: source_status is not verified_corresponding_source" in errors
    assert "qt: wheel_build_provenance_verified is not verified" in errors
    assert "qt: Qt third-party notices are not verified" in errors


def test_runtime_download_review_requires_complete_evidence():
    lock = _lock(b"one", b"two")
    lock["runtime_downloads"][0]["legal_review"] = {
        "review_completed": True,
        "evidence": "",
        "scope": "download",
        "reviewer": "admin",
        "date": "2026-07-31",
    }

    errors = release_gate_errors(lock)

    assert any("runtime download legal review evidence" in error for error in errors)


def test_source_exception_requires_structured_completed_review():
    lock = _lock(b"one", b"two")
    component = lock["runtime_components"][0]
    component["source_archives"] = []
    component["source_archive_exception_reviewed"] = True
    component["source_archive_exception_reason"] = "System redistribution exception"
    component["source_archive_exception_review"] = {
        **_review(),
        "evidence": "",
    }
    assert any("no verified exact source archive" in error for error in release_gate_errors(lock))

    component["source_archive_exception_review"] = _review()
    assert not any("no verified exact source archive" in error for error in release_gate_errors(lock))
    source_archive_records(lock)


def test_source_partitioner_splits_before_target_size(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.write_bytes(b"123")
    second.write_bytes(b"4567")

    parts = partition_sources(
        [
            ({"filename": "first"}, first),
            ({"filename": "second"}, second),
        ],
        target_size=5,
    )

    assert [[item[1].name for item in part] for part in parts] == [
        ["first"],
        ["second"],
    ]


def test_application_source_validation_rejects_traversal_and_wrong_version(
    tmp_path,
):
    source = tmp_path / "source.zip"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("../LICENSE", "GPL")
        archive.writestr("VERSION", "1.2.3")
    with pytest.raises(ReleaseAssetError, match="Unsafe entry"):
        validate_application_source(source, "1.2.3")

    _write_source_zip(source, "9.9.9")
    with pytest.raises(ReleaseAssetError, match="VERSION mismatch"):
        validate_application_source(source, "1.2.3")


@pytest.mark.parametrize(
    "unsafe_member",
    [
        "/absolute.txt",
        "C:/absolute.txt",
        "NUL.txt",
        "directory./file.txt",
        "directory /file.txt",
    ],
)
def test_application_source_rejects_windows_unsafe_members(tmp_path, unsafe_member):
    source = tmp_path / "source.zip"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("VERSION", "1.2.3\n")
        archive.writestr("LICENSE", "GPL\n")
        archive.writestr(unsafe_member, "unsafe")

    with pytest.raises(ReleaseAssetError, match="Unsafe"):
        validate_application_source(source, "1.2.3")


@pytest.mark.parametrize("external_attr", [(stat.S_IFLNK | 0o777) << 16, 0x400])
def test_application_source_rejects_links_and_reparse_entries(
    tmp_path,
    external_attr,
):
    source = tmp_path / "source.zip"
    special = zipfile.ZipInfo("special")
    special.create_system = 3
    special.external_attr = external_attr
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("VERSION", "1.2.3\n")
        archive.writestr("LICENSE", "GPL\n")
        archive.writestr(special, "target")

    with pytest.raises(ReleaseAssetError, match="Unsafe special entry"):
        validate_application_source(source, "1.2.3")


def test_application_source_runs_crc_test(tmp_path):
    source = tmp_path / "source.zip"
    _write_source_zip(source)
    raw = source.read_bytes()
    marker = b"print('test')"
    assert marker in raw
    source.write_bytes(raw.replace(marker, b"print('tast')", 1))

    with pytest.raises(ReleaseAssetError, match="CRC failure"):
        validate_application_source(source, "1.2.3")


def test_application_source_commit_and_blob_must_match(monkeypatch, tmp_path):
    source = tmp_path / "source.zip"
    _write_source_zip(source)

    def mismatched_git(*arguments, binary=False):
        if arguments[0] == "rev-parse":
            return "b" * 40 + "\n"
        raise AssertionError((arguments, binary))

    monkeypatch.setattr(release_assets, "_git_output", mismatched_git)
    with pytest.raises(ReleaseAssetError, match="does not resolve exactly"):
        validate_application_source(source, "1.2.3", "a" * 40)


def test_application_source_validates_git_tree_modes_and_blob_bytes(
    monkeypatch,
    tmp_path,
):
    source = tmp_path / "source.zip"
    _write_source_zip(source)
    source_files = {
        "VERSION": b"1.2.3\n",
        "LICENSE": b"GPL\n",
        "main.py": b"print('test')\n",
    }
    blob_by_path = {
        path: hashlib.sha1(
            b"blob " + str(len(data)).encode("ascii") + b"\0" + data,
            usedforsecurity=False,
        ).hexdigest()
        for path, data in source_files.items()
    }

    def verified_git(*arguments, binary=False):
        if arguments[0] == "rev-parse":
            return "a" * 40 + "\n"
        if arguments[:3] == ("ls-tree", "-r", "-z"):
            return b"".join(
                f"100644 blob {blob_by_path[path]}\t{path}\0".encode()
                for path in sorted(blob_by_path)
            )
        if arguments[:2] == ("cat-file", "blob"):
            object_id = str(arguments[2])
            path = next(
                path for path, digest in blob_by_path.items() if digest == object_id
            )
            return source_files[path]
        raise AssertionError((arguments, binary))

    monkeypatch.setattr(release_assets, "_git_output", verified_git)
    validate_application_source(source, "1.2.3", "a" * 40)

    _replace_zip_member(source, "main.py", b"print('forged')\n")
    with pytest.raises(ReleaseAssetError, match="differs from Git blob"):
        validate_application_source(source, "1.2.3", "a" * 40)


def test_generated_archive_indexes_detect_member_tampering(monkeypatch, tmp_path):
    _payload, output = _create_asset_set(tmp_path, monkeypatch)
    source_part = output / "LoLReplayTool-third-party-sources-1.2.3-01.zip"
    _replace_zip_member(
        source_part,
        "sources/python-source.tar.gz",
        b"tampered source",
    )
    with pytest.raises(ReleaseAssetError, match="Source index mismatch"):
        verify_source_part(source_part)

    license_archive = output / "LoLReplayTool-license-materials-1.2.3.zip"
    _replace_zip_member(license_archive, "LICENSE", b"tampered license")
    with pytest.raises(ReleaseAssetError, match="License index mismatch"):
        verify_license_archive(license_archive)


def test_release_source_parts_must_match_checkout_component_lock(
    monkeypatch,
    tmp_path,
):
    payload, output = _create_asset_set(tmp_path, monkeypatch)
    source_part = output / "LoLReplayTool-third-party-sources-1.2.3-01.zip"
    replacement = source_part.with_suffix(".replacement.zip")
    with zipfile.ZipFile(source_part) as source, zipfile.ZipFile(
        replacement,
        "w",
    ) as target:
        index = json.loads(source.read("SOURCE_INDEX.json"))
        index["sources"][0]["component"] = "forged-component"
        for info in source.infolist():
            data = (
                json.dumps(index).encode()
                if info.filename == "SOURCE_INDEX.json"
                else source.read(info)
            )
            target.writestr(info, data)
    replacement.replace(source_part)
    checksums = output / "SHA256SUMS.txt"
    lines = checksums.read_text(encoding="ascii").splitlines()
    checksums.write_text(
        "\n".join(
            (
                f"{release_assets.sha256_file(source_part)}  {source_part.name}"
                if line.endswith(f"  {source_part.name}")
                else line
            )
            for line in lines
        )
        + "\n",
        encoding="ascii",
    )

    with pytest.raises(ReleaseAssetError, match="checkout component lock"):
        verify_release_asset_list(Path(payload["asset_list"]))


def test_license_archive_rejects_indexed_empty_material(monkeypatch, tmp_path):
    _payload, output = _create_asset_set(tmp_path, monkeypatch)
    license_archive = output / "LoLReplayTool-license-materials-1.2.3.zip"
    replacement = license_archive.with_suffix(".replacement.zip")
    with zipfile.ZipFile(license_archive) as source:
        index = json.loads(source.read("LICENSE_INDEX.json"))
        for record in index["files"]:
            if record["path"] == "LICENSE":
                record["size"] = 0
                record["sha256"] = _sha(b"")
        with zipfile.ZipFile(replacement, "w") as target:
            for info in source.infolist():
                if info.filename == "LICENSE":
                    data = b""
                elif info.filename == "LICENSE_INDEX.json":
                    data = json.dumps(index).encode()
                else:
                    data = source.read(info)
                target.writestr(info, data)
    replacement.replace(license_archive)

    with pytest.raises(ReleaseAssetError, match="Invalid size"):
        verify_license_archive(license_archive)


def test_sha256sums_detects_tampered_asset(monkeypatch, tmp_path):
    payload, _output = _create_asset_set(tmp_path, monkeypatch)
    installer = next(Path(asset) for asset in payload["assets"] if Path(asset).suffix == ".exe")
    installer.write_bytes(b"tampered installer")

    with pytest.raises(ReleaseAssetError, match="SHA256SUMS mismatch"):
        verify_release_asset_list(Path(payload["asset_list"]))


def test_sha256sums_requires_exact_release_asset_set(monkeypatch, tmp_path):
    payload, output = _create_asset_set(tmp_path, monkeypatch)
    checksum_path = output / "SHA256SUMS.txt"
    lines = checksum_path.read_text(encoding="ascii").splitlines()
    checksum_path.write_text("\n".join(lines[:-1]) + "\n", encoding="ascii")

    with pytest.raises(ReleaseAssetError, match="asset set mismatch"):
        verify_release_asset_list(Path(payload["asset_list"]))


def test_release_asset_allowlist_requires_installer_and_rejects_extra(
    monkeypatch,
    tmp_path,
):
    payload, output = _create_asset_set(tmp_path, monkeypatch)
    asset_list = Path(payload["asset_list"])
    stored = json.loads(asset_list.read_text(encoding="utf-8"))
    stored["assets"] = [
        path for path in stored["assets"] if not Path(path).name.endswith(".exe")
    ]
    asset_list.write_text(json.dumps(stored), encoding="utf-8")

    with pytest.raises(ReleaseAssetError, match="Setup-1.2.3.exe.*exactly once"):
        verify_release_asset_list(asset_list)

    extra_case = tmp_path / "extra-case"
    extra_case.mkdir()
    payload, output = _create_asset_set(extra_case, monkeypatch)
    asset_list = Path(payload["asset_list"])
    stored = json.loads(asset_list.read_text(encoding="utf-8"))
    extra = output / "unexpected.zip"
    extra.write_bytes(b"unexpected")
    stored["assets"].append(str(extra.resolve()))
    asset_list.write_text(json.dumps(stored), encoding="utf-8")

    with pytest.raises(ReleaseAssetError, match="Unexpected Release assets"):
        verify_release_asset_list(asset_list)


def test_release_asset_allowlist_rejects_duplicate_and_gapped_source_parts(
    monkeypatch,
    tmp_path,
):
    payload, _output = _create_asset_set(tmp_path, monkeypatch)
    asset_list = Path(payload["asset_list"])
    stored = json.loads(asset_list.read_text(encoding="utf-8"))
    source_part = next(
        path
        for path in stored["assets"]
        if "third-party-sources" in Path(path).name
    )
    stored["assets"].append(source_part)
    asset_list.write_text(json.dumps(stored), encoding="utf-8")

    with pytest.raises(ReleaseAssetError, match="Duplicate release asset filename"):
        verify_release_asset_list(asset_list)

    gap_case = tmp_path / "gap-case"
    gap_case.mkdir()
    payload, output = _create_asset_set(gap_case, monkeypatch)
    asset_list = Path(payload["asset_list"])
    stored = json.loads(asset_list.read_text(encoding="utf-8"))
    old_part = next(
        Path(path)
        for path in stored["assets"]
        if "third-party-sources" in Path(path).name
    )
    new_part = output / old_part.name.replace("-01.zip", "-02.zip")
    old_part.rename(new_part)
    stored["assets"] = [
        str(new_part.resolve()) if Path(path) == old_part else path
        for path in stored["assets"]
    ]
    asset_list.write_text(json.dumps(stored), encoding="utf-8")

    with pytest.raises(ReleaseAssetError, match="contiguous from 01"):
        verify_release_asset_list(asset_list)


def test_release_asset_list_can_be_reverified_after_safe_relocation(
    monkeypatch,
    tmp_path,
):
    payload, _output = _create_asset_set(tmp_path, monkeypatch)
    relocated = tmp_path / "relocated"
    relocated.mkdir()
    for asset in payload["assets"]:
        source = Path(asset)
        shutil.copy2(source, relocated / source.name)
    asset_list = Path(payload["asset_list"])
    relocated_list = relocated / asset_list.name
    shutil.copy2(asset_list, relocated_list)

    verify_release_asset_list(relocated_list, asset_dir=relocated)


def test_release_workflow_requires_manual_approval_and_remote_verification():
    workflow = (Path(__file__).resolve().parents[1] / ".github" / "workflows" / "release.yml").read_text(
        encoding="utf-8"
    )

    assert "workflow_dispatch:" in workflow
    assert "publish_confirmation:" in workflow
    assert "name: release" in workflow
    assert "gh release create $tag" in workflow
    assert "--draft" in workflow
    assert "gh release upload $tag $asset" in workflow
    assert "--clobber" not in workflow
    assert "$remote.digest" in workflow
    assert "-F draft=false" in workflow
    assert "--release `\n            --toc" in workflow
    assert "release_commit: ${{ steps.release_inputs.outputs.release_commit }}" in workflow
    assert "EXPECTED_RELEASE_COMMIT: ${{ needs.prepare.outputs.release_commit }}" in workflow
    assert "--source-commit $env:RELEASE_COMMIT" in workflow
    assert workflow.count("Assert-TagStillImmutable") >= 3
    assert workflow.count(
        "actions/setup-python@ece7cb06caefa5fff74198d8649806c4678c61a1"
    ) == 2
    assert workflow.count(
        "actions/checkout@d23441a48e516b6c34aea4fa41551a30e30af803"
    ) == 2
    assert (
        "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a"
        in workflow
    )
    assert (
        "actions/download-artifact@37930b1c2abaa49bbe596cd826c3c89aef350131"
        in workflow
    )
    assert "prepare-binary-install" in workflow
    assert "attest-binary-install" in workflow
    assert "verify-python-runtime" in workflow
    assert "verify-bootstrap-pip" in workflow
    assert "--only-binary=:all:" in workflow
    assert "--require-hashes" in workflow
    assert "--no-index" in workflow
    assert "--no-deps" in workflow
    assert "--report $pipReport" in workflow
    assert "pip install -r requirements-dev.txt" not in workflow
    assert "-PythonExe $env:RELEASE_PYTHON" in workflow
    assert "-BuildProvenance $env:BUILD_PROVENANCE" in workflow
    assert "-BuildProvenanceSha256 $env:SEALED_BUILD_PROVENANCE_SHA256" in workflow
    assert workflow.count("--components .\\compliance\\components.json") >= 6
    assert "--build-provenance-sha256 $env:BUILD_PROVENANCE_SHA256" in workflow
    assert "EXPECTED_BUILD_PROVENANCE_SHA256:" in workflow
    assert '$pipVersion -cne "26.1.2"' in workflow
    assert "choco install innosetup --version=6.7.3" in workflow
    assert "retention-days: 7" in workflow
    assert workflow.count("persist-credentials: false") == 2
    assert "gh release edit" not in workflow
    assert "push:\n    tags" not in workflow
    assert workflow.count("git merge-base --is-ancestor") == 2


def test_normal_ci_verifies_windows_outputs_without_distributing_artifacts():
    workflow = (
        Path(__file__).resolve().parents[1]
        / ".github"
        / "workflows"
        / "ci.yml"
    ).read_text(encoding="utf-8")

    assert "name: Build Windows artifact" in workflow
    assert "run: .\\scripts\\build.ps1" in workflow
    assert "Run packaged self-check" in workflow
    assert "run: .\\scripts\\build_installer.ps1 -SkipTests -SkipBuild" in workflow
    assert "actions/upload-artifact@" not in workflow
    assert "Compress-Archive" not in workflow
    assert "LoLReplayTool-installer" not in workflow


def test_build_scripts_accept_verified_python_and_provenance():
    build = Path("scripts/build.ps1").read_text(encoding="utf-8")
    installer = Path("scripts/build_installer.ps1").read_text(encoding="utf-8")

    for script in (build, installer):
        assert "[string]$PythonExe" in script
        assert "[string]$BuildProvenance" in script
        assert "[string]$BuildProvenanceSha256" in script
        assert "Resolve-Path -LiteralPath $PythonExe" in script
        assert "Resolve-Path -LiteralPath $BuildProvenance" in script
        assert "Assert-BuildProvenance" in script
    assert '"--build-provenance", $resolvedBuildProvenance' in build
    assert '"--build-provenance-sha256", $BuildProvenanceSha256' in build
    assert '$buildArgs += @("-PythonExe", $selectedPython)' in installer
    assert '"-BuildProvenance", $resolvedBuildProvenance' in installer
    assert '"-BuildProvenanceSha256", $BuildProvenanceSha256' in installer
