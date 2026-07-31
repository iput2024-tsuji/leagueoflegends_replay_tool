import hashlib
import json
import zipfile
from pathlib import Path

import pytest

from scripts.prepare_release_assets import (
    ReleaseAssetError,
    create_release_assets,
    partition_sources,
    release_gate_errors,
    source_archive_records,
    validate_application_source,
)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_source_zip(path: Path, version: str = "1.2.3") -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("VERSION", version + "\n")
        archive.writestr("LICENSE", "GPL\n")
        archive.writestr("main.py", "print('test')\n")


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


def test_create_release_assets_uses_fixed_names_and_hashes(tmp_path):
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
        installer=installer,
        application_source=source,
        distribution_root=distribution,
        output_dir=output,
        components_file=lock_path,
        cache_dir=cache,
    )

    names = [Path(path).name for path in payload["assets"]]
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
    with zipfile.ZipFile(
        output / "LoLReplayTool-third-party-sources-1.2.3-01.zip"
    ) as archive:
        index = json.loads(archive.read("SOURCE_INDEX.json"))
        assert {record["component"] for record in index["sources"]} == {
            "python",
            "demo",
        }
        assert index["runtime_downloads_not_bundled"][0][
            "bundled_in_installer"
        ] is False
        assert archive.read("sources/python-source.tar.gz") == first
    with zipfile.ZipFile(
        output / "LoLReplayTool-license-materials-1.2.3.zip"
    ) as archive:
        assert "QT_RELINKING.md" in archive.namelist()
        assert "licenses/distribution-manifest.json" in archive.namelist()


def test_release_assets_fail_closed_on_hash_mismatch(tmp_path):
    expected = b"expected"
    lock = _lock(expected, b"demo")
    records = source_archive_records(lock)
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / records[0]["filename"]).write_bytes(b"tampered")

    from scripts.prepare_release_assets import fetch_verified_sources

    with pytest.raises(ReleaseAssetError, match="SHA256 mismatch"):
        fetch_verified_sources(records[:1], cache)


def test_source_lock_rejects_unsafe_duplicate_and_non_https_names():
    lock = _lock(b"one", b"two")
    lock["runtime_components"][0]["source_archives"][0]["filename"] = "../bad.tgz"
    with pytest.raises(ReleaseAssetError, match="Unsafe"):
        source_archive_records(lock)

    lock = _lock(b"one", b"two")
    lock["runtime_components"][0]["source_archives"][0][
        "filename"
    ] = "PYTHON-SOURCE.TAR.GZ"
    with pytest.raises(ReleaseAssetError, match="Duplicate"):
        source_archive_records(lock)

    lock = _lock(b"one", b"two")
    lock["runtime_components"][0]["source_archives"][0]["url"] = "http://example.test"
    with pytest.raises(ReleaseAssetError, match="HTTPS"):
        source_archive_records(lock)


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
    assert any(
        error.startswith("missing: source coverage for wheel-vendored")
        for error in errors
    )
    with pytest.raises(ReleaseAssetError, match="no verified exact source archive"):
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
