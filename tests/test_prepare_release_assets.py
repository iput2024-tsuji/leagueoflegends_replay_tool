import hashlib
import io
import json
import shutil
import stat
import zipfile
from pathlib import Path

import pytest

from scripts.prepare_release_assets import (
    ReleaseAssetError,
    create_release_assets,
    fetch_verified_sources,
    partition_sources,
    release_gate_errors,
    source_archive_records,
    validate_application_source,
    verify_license_archive,
    verify_release_asset_list,
    verify_source_part,
)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_source_zip(path: Path, version: str = "1.2.3") -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("VERSION", version + "\n")
        archive.writestr("LICENSE", "GPL\n")
        archive.writestr("main.py", "print('test')\n")


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
        "python": {
            "component": "python",
            "release_version": "3.14.6",
            "license": "PSF-2.0",
            "release_legal_review_required": legal_gate,
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


def _create_asset_set(tmp_path: Path) -> tuple[dict[str, object], Path]:
    first = b"python source"
    second = b"demo source"
    lock_path = tmp_path / "components.json"
    lock_path.write_text(json.dumps(_lock(first, second)), encoding="utf-8")
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "python-source.tar.gz").write_bytes(first)
    (cache / "demo-source.tar.gz").write_bytes(second)
    installer = tmp_path / "LoLReplayTool-Setup-1.2.3.exe"
    installer.write_bytes(b"installer")
    source = tmp_path / "LoLReplayTool-source-1.2.3.zip"
    _write_source_zip(source)
    distribution = tmp_path / "distribution"
    _write_distribution(distribution)
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
    )
    return payload, output


def _replace_zip_member(path: Path, member_name: str, replacement: bytes) -> None:
    temporary = path.with_suffix(path.suffix + ".replacement")
    with zipfile.ZipFile(path) as source, zipfile.ZipFile(temporary, "w") as target:
        for info in source.infolist():
            data = replacement if info.filename == member_name else source.read(info)
            target.writestr(info, data)
    temporary.replace(path)


def test_create_release_assets_uses_fixed_names_and_hashes(tmp_path):
    payload, output = _create_asset_set(tmp_path)

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


def test_generated_archive_indexes_detect_member_tampering(tmp_path):
    _payload, output = _create_asset_set(tmp_path)
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


def test_license_archive_rejects_indexed_empty_material(tmp_path):
    _payload, output = _create_asset_set(tmp_path)
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


def test_sha256sums_detects_tampered_asset(tmp_path):
    payload, _output = _create_asset_set(tmp_path)
    installer = next(Path(asset) for asset in payload["assets"] if Path(asset).suffix == ".exe")
    installer.write_bytes(b"tampered installer")

    with pytest.raises(ReleaseAssetError, match="SHA256SUMS mismatch"):
        verify_release_asset_list(Path(payload["asset_list"]))


def test_sha256sums_requires_exact_release_asset_set(tmp_path):
    payload, output = _create_asset_set(tmp_path)
    checksum_path = output / "SHA256SUMS.txt"
    lines = checksum_path.read_text(encoding="ascii").splitlines()
    checksum_path.write_text("\n".join(lines[:-1]) + "\n", encoding="ascii")

    with pytest.raises(ReleaseAssetError, match="asset set mismatch"):
        verify_release_asset_list(Path(payload["asset_list"]))


def test_release_asset_list_can_be_reverified_after_safe_relocation(tmp_path):
    payload, _output = _create_asset_set(tmp_path)
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
