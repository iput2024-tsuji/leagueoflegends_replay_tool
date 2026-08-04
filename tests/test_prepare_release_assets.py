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


def _runtime_zip_bytes(
    entries: list[tuple[str, bytes, int | None]] | None = None,
) -> bytes:
    if entries is None:
        entries = [
            ("python.exe", b"locked-python", None),
            ("Lib/os.py", b"# locked stdlib\n", None),
        ]
    runtime_buffer = io.BytesIO()
    with zipfile.ZipFile(runtime_buffer, "w") as archive:
        for name, payload, external_attr in entries:
            info = zipfile.ZipInfo(name)
            info.filename = name
            info.orig_filename = name
            if external_attr is not None:
                info.external_attr = external_attr
            archive.writestr(info, payload)
    payload = runtime_buffer.getvalue()
    for name, _data, _external_attr in entries:
        if "\\" in name:
            payload = payload.replace(
                name.replace("\\", "/").encode(),
                name.encode(),
            )
    return payload


def _write_runtime_lock(
    tmp_path: Path,
    archive: bytes,
    *,
    declared_size: int | None = None,
    declared_sha256: str | None = None,
) -> tuple[Path, dict[str, object]]:
    lock = json.loads(
        release_assets.COMPONENTS_FILE.read_text(encoding="utf-8")
    )
    python_lock = lock["python"]
    version = python_lock["release_version"]
    profile = python_lock["windows_native_runtime_profiles"][version]
    archive_lock = profile["official_binary_archive"]
    archive_lock["size"] = len(archive) if declared_size is None else declared_size
    archive_lock["sha256"] = declared_sha256 or _sha(archive)
    components_file = tmp_path / "components.json"
    components_file.write_text(json.dumps(lock), encoding="utf-8")
    return components_file, lock


def _runtime_opener(archive: bytes, expected_url: str):
    def opener(request, *, timeout):
        assert request.full_url == expected_url
        assert timeout == 120
        return io.BytesIO(archive)

    return opener


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
        "runtime_downloads": [],
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
        json.dumps(
            {
                "schema_version": 1,
                "git_source": {"commit": "a" * 40},
            }
        )
        + "\n",
        encoding="utf-8",
    )


def _create_asset_set(
    tmp_path: Path,
    monkeypatch,
    *,
    provenance_seal: str | None = "actual",
    runtime_downloads: list[dict[str, object]] | None = None,
    enforce_release_gates: bool = False,
) -> tuple[dict[str, object], Path]:
    inno_identity = "b" * 64
    monkeypatch.setattr(
        release_assets,
        "validate_inno_component_lock",
        lambda _lock: {},
    )
    monkeypatch.setattr(
        release_assets,
        "validate_inno_build_provenance",
        lambda _provenance, _lock: inno_identity,
    )
    first = b"python source"
    second = b"demo source"
    lock_path = tmp_path / "components.json"
    lock = _lock(first, second)
    if runtime_downloads is not None:
        lock["runtime_downloads"] = runtime_downloads
    lock_path.write_text(json.dumps(lock), encoding="utf-8")
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
        enforce_release_gates=enforce_release_gates,
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
    assert payload["installer_build"] == {
        "component": "inno-setup",
        "version": "6.7.3",
        "inno_setup_provenance_sha256": "b" * 64,
        "installer_sha256": _sha(b"installer"),
    }
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
        assert "runtime_downloads_not_bundled" not in index
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


def test_prepare_python_runtime_extracts_verified_archive_transactionally(
    tmp_path,
):
    archive = _runtime_zip_bytes()
    components_file, lock = _write_runtime_lock(tmp_path, archive)
    python_lock = lock["python"]
    version = python_lock["release_version"]
    archive_lock = python_lock["windows_native_runtime_profiles"][version][
        "official_binary_archive"
    ]
    output_dir = tmp_path / "prepared" / "runtime"
    cache_dir = tmp_path / "cache"

    python_executable = release_assets.prepare_python_runtime(
        components_file=components_file,
        cache_dir=cache_dir,
        output_dir=output_dir,
        opener=_runtime_opener(archive, archive_lock["url"]),
    )

    assert python_executable == output_dir.resolve() / "python.exe"
    assert python_executable.read_bytes() == b"locked-python"
    assert (output_dir / "Lib" / "os.py").read_bytes() == b"# locked stdlib\n"
    assert (cache_dir / archive_lock["filename"]).read_bytes() == archive
    assert not list(output_dir.parent.glob(f".{output_dir.name}-*"))


@pytest.mark.parametrize(
    ("declared_size", "declared_sha256", "message"),
    [
        (1, None, "size mismatch"),
        (None, "0" * 64, "SHA256 mismatch"),
    ],
)
def test_prepare_python_runtime_rejects_size_or_hash_mismatch_without_output(
    tmp_path,
    declared_size,
    declared_sha256,
    message,
):
    archive = _runtime_zip_bytes()
    if declared_size is not None:
        declared_size = len(archive) + declared_size
    components_file, lock = _write_runtime_lock(
        tmp_path,
        archive,
        declared_size=declared_size,
        declared_sha256=declared_sha256,
    )
    version = lock["python"]["release_version"]
    archive_lock = lock["python"]["windows_native_runtime_profiles"][version][
        "official_binary_archive"
    ]
    output_dir = tmp_path / "runtime"

    with pytest.raises(ReleaseAssetError, match=message):
        release_assets.prepare_python_runtime(
            components_file=components_file,
            cache_dir=tmp_path / "cache",
            output_dir=output_dir,
            opener=_runtime_opener(archive, archive_lock["url"]),
        )

    assert not os.path.lexists(output_dir)


def test_prepare_python_runtime_rejects_truncated_zip_without_output(tmp_path):
    archive = b"not-a-zip"
    components_file, lock = _write_runtime_lock(tmp_path, archive)
    version = lock["python"]["release_version"]
    archive_lock = lock["python"]["windows_native_runtime_profiles"][version][
        "official_binary_archive"
    ]
    output_dir = tmp_path / "runtime"

    with pytest.raises(ReleaseAssetError, match="Cannot extract locked Python"):
        release_assets.prepare_python_runtime(
            components_file=components_file,
            cache_dir=tmp_path / "cache",
            output_dir=output_dir,
            opener=_runtime_opener(archive, archive_lock["url"]),
        )

    assert not os.path.lexists(output_dir)


def test_prepare_python_runtime_wraps_download_error_without_output(tmp_path):
    archive = _runtime_zip_bytes()
    components_file, _lock_payload = _write_runtime_lock(tmp_path, archive)
    output_dir = tmp_path / "runtime"

    def failing_opener(_request, *, timeout):
        assert timeout == 120
        raise OSError("network unavailable")

    with pytest.raises(
        ReleaseAssetError,
        match="Cannot prepare locked Python runtime archive",
    ):
        release_assets.prepare_python_runtime(
            components_file=components_file,
            cache_dir=tmp_path / "cache",
            output_dir=output_dir,
            opener=failing_opener,
        )

    assert not os.path.lexists(output_dir)


@pytest.mark.parametrize(
    "unsafe_name",
    [
        "/absolute.py",
        "../escape.py",
        "C:/escape.py",
        "//server/share/escape.py",
        "..\\escape.py",
    ],
)
def test_prepare_python_runtime_rejects_unsafe_member_without_output(
    tmp_path,
    unsafe_name,
):
    archive = _runtime_zip_bytes(
        [
            ("python.exe", b"locked-python", None),
            (unsafe_name, b"unsafe", None),
        ]
    )
    components_file, lock = _write_runtime_lock(tmp_path, archive)
    version = lock["python"]["release_version"]
    archive_lock = lock["python"]["windows_native_runtime_profiles"][version][
        "official_binary_archive"
    ]
    output_dir = tmp_path / "runtime"

    with pytest.raises(ReleaseAssetError, match="Unsafe|Backslash"):
        release_assets.prepare_python_runtime(
            components_file=components_file,
            cache_dir=tmp_path / "cache",
            output_dir=output_dir,
            opener=_runtime_opener(archive, archive_lock["url"]),
        )

    assert not os.path.lexists(output_dir)


@pytest.mark.parametrize(
    "external_attr",
    [
        (stat.S_IFLNK | 0o777) << 16,
        (stat.S_IFIFO | 0o644) << 16,
        ((stat.S_IFREG | 0o644) << 16) | 0x400,
    ],
)
def test_prepare_python_runtime_rejects_special_member_without_output(
    tmp_path,
    external_attr,
):
    archive = _runtime_zip_bytes(
        [
            ("python.exe", b"locked-python", None),
            ("Lib/special", b"target", external_attr),
        ]
    )
    components_file, lock = _write_runtime_lock(tmp_path, archive)
    version = lock["python"]["release_version"]
    archive_lock = lock["python"]["windows_native_runtime_profiles"][version][
        "official_binary_archive"
    ]
    output_dir = tmp_path / "runtime"

    with pytest.raises(ReleaseAssetError, match="Unsafe special entry"):
        release_assets.prepare_python_runtime(
            components_file=components_file,
            cache_dir=tmp_path / "cache",
            output_dir=output_dir,
            opener=_runtime_opener(archive, archive_lock["url"]),
        )

    assert not os.path.lexists(output_dir)


@pytest.mark.parametrize(
    "entries",
    [
        [
            ("python.exe", b"first", None),
            ("python.exe", b"second", None),
        ],
        [
            ("python.exe", b"first", None),
            ("PYTHON.EXE", b"second", None),
        ],
    ],
)
def test_prepare_python_runtime_rejects_duplicate_or_case_collision(
    tmp_path,
    entries,
):
    archive = _runtime_zip_bytes(entries)
    components_file, lock = _write_runtime_lock(tmp_path, archive)
    version = lock["python"]["release_version"]
    archive_lock = lock["python"]["windows_native_runtime_profiles"][version][
        "official_binary_archive"
    ]
    output_dir = tmp_path / "runtime"

    with pytest.raises(ReleaseAssetError, match="Duplicate or case-insensitive"):
        release_assets.prepare_python_runtime(
            components_file=components_file,
            cache_dir=tmp_path / "cache",
            output_dir=output_dir,
            opener=_runtime_opener(archive, archive_lock["url"]),
        )

    assert not os.path.lexists(output_dir)


def test_prepare_python_runtime_rejects_existing_nonempty_output(tmp_path):
    output_dir = tmp_path / "runtime"
    output_dir.mkdir()
    marker = output_dir / "keep.txt"
    marker.write_text("keep", encoding="utf-8")

    with pytest.raises(ReleaseAssetError, match="must not already exist"):
        release_assets.prepare_python_runtime(
            components_file=tmp_path / "unused.json",
            cache_dir=tmp_path / "cache",
            output_dir=output_dir,
        )

    assert marker.read_text(encoding="utf-8") == "keep"


def test_prepare_python_runtime_rejects_existing_output_symlink(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    output_dir = tmp_path / "runtime-link"
    try:
        output_dir.symlink_to(target, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"Directory symlinks are unavailable: {exc}")

    with pytest.raises(ReleaseAssetError, match="must not already exist"):
        release_assets.prepare_python_runtime(
            components_file=tmp_path / "unused.json",
            cache_dir=tmp_path / "cache",
            output_dir=output_dir,
        )

    assert output_dir.is_symlink()


def test_prepare_python_runtime_rejects_parent_traversal_output(tmp_path):
    parent = tmp_path / "new-parent"
    output_dir = parent / ".."

    with pytest.raises(ReleaseAssetError, match="Unsafe Python runtime output"):
        release_assets.prepare_python_runtime(
            components_file=tmp_path / "unused.json",
            cache_dir=tmp_path / "cache",
            output_dir=output_dir,
        )

    assert not parent.exists()


@pytest.mark.parametrize(
    "overlap",
    ["same", "descendant", "ancestor", "dotdot"],
)
def test_prepare_python_runtime_rejects_cache_output_overlap_without_side_effects(
    tmp_path,
    overlap,
):
    archive = _runtime_zip_bytes()
    components_file, _lock_payload = _write_runtime_lock(tmp_path, archive)
    output_dir = tmp_path / "runtime"
    if overlap == "same":
        cache_dir = output_dir
    elif overlap == "descendant":
        cache_dir = output_dir / "cache"
    elif overlap == "ancestor":
        cache_dir = tmp_path / "cache"
        output_dir = cache_dir / "runtime"
    else:
        cache_dir = tmp_path / "alias" / ".." / "runtime" / "cache"

    with pytest.raises(ReleaseAssetError, match="must not overlap"):
        release_assets.prepare_python_runtime(
            components_file=components_file,
            cache_dir=cache_dir,
            output_dir=output_dir,
        )

    assert not os.path.lexists(output_dir)


@pytest.mark.parametrize(
    ("entries", "message"),
    [
        ([("pythonw.exe", b"python", None)], "does not contain root python.exe"),
        ([("python.exe", b"", None)], "executable is empty"),
        (
            [
                ("python.exe", b"python", None),
                ("Lib", b"not-a-directory", None),
                ("Lib/os.py", b"stdlib", None),
            ],
            "File/directory collision",
        ),
    ],
)
def test_prepare_python_runtime_rejects_incomplete_or_ambiguous_layout(
    tmp_path,
    entries,
    message,
):
    archive = _runtime_zip_bytes(entries)
    components_file, lock = _write_runtime_lock(tmp_path, archive)
    version = lock["python"]["release_version"]
    archive_lock = lock["python"]["windows_native_runtime_profiles"][version][
        "official_binary_archive"
    ]
    output_dir = tmp_path / "runtime"

    with pytest.raises(ReleaseAssetError, match=message):
        release_assets.prepare_python_runtime(
            components_file=components_file,
            cache_dir=tmp_path / "cache",
            output_dir=output_dir,
            opener=_runtime_opener(archive, archive_lock["url"]),
        )

    assert not os.path.lexists(output_dir)


@pytest.mark.parametrize("runtime_source", [None, "official_actions_archive"])
def test_release_gate_requires_exact_python_runtime_source(runtime_source):
    lock = json.loads(
        release_assets.COMPONENTS_FILE.read_text(encoding="utf-8")
    )
    version = lock["python"]["release_version"]
    profile = lock["python"]["windows_native_runtime_profiles"][version]
    if runtime_source is None:
        profile.pop("runtime_source")
    else:
        profile["runtime_source"] = runtime_source

    errors = release_gate_errors(lock)

    assert (
        "python: release native runtime source must be official_binary_archive"
        in errors
    )


def test_every_locked_windows_python_profile_declares_official_runtime_source():
    lock = json.loads(
        release_assets.COMPONENTS_FILE.read_text(encoding="utf-8")
    )
    profiles = lock["python"]["windows_native_runtime_profiles"]

    assert profiles
    assert {
        version: profile["runtime_source"]
        for version, profile in profiles.items()
    } == {
        version: "official_binary_archive" for version in profiles
    }


def test_release_gate_binds_python_runtime_to_exact_official_url():
    lock = json.loads(
        release_assets.COMPONENTS_FILE.read_text(encoding="utf-8")
    )
    version = lock["python"]["release_version"]
    profile = lock["python"]["windows_native_runtime_profiles"][version]
    profile["official_binary_archive"]["url"] = (
        f"https://www.python.org/ftp/python/{version}/unexpected.zip"
    )

    assert "python: official binary archive provenance is invalid" in (
        release_gate_errors(lock)
    )


def test_prepare_python_runtime_cli_accepts_cache_and_output_paths():
    args = release_assets._create_parser().parse_args(
        [
            "prepare-python-runtime",
            "--components",
            "components.json",
            "--cache-dir",
            "cache",
            "--output-dir",
            "runtime",
        ]
    )

    assert args.command == "prepare-python-runtime"
    assert args.components == Path("components.json")
    assert args.cache_dir == Path("cache")
    assert args.output_dir == Path("runtime")


@pytest.mark.parametrize(
    "arguments",
    [
        ["prepare-python-runtime", "--output-dir", "runtime"],
        ["prepare-python-runtime", "--cache-dir", "cache"],
    ],
)
def test_prepare_python_runtime_cli_requires_cache_and_output_paths(arguments):
    with pytest.raises(SystemExit):
        release_assets._create_parser().parse_args(arguments)


def test_prepare_python_runtime_cli_dispatches_helper(monkeypatch, tmp_path):
    components = tmp_path / "components.json"
    cache = tmp_path / "cache"
    output = tmp_path / "runtime"
    observed = {}

    def fake_prepare_python_runtime(**kwargs):
        observed.update(kwargs)
        return output / "python.exe"

    monkeypatch.setattr(
        release_assets,
        "prepare_python_runtime",
        fake_prepare_python_runtime,
    )

    assert (
        release_assets.main(
            [
                "prepare-python-runtime",
                "--components",
                str(components),
                "--cache-dir",
                str(cache),
                "--output-dir",
                str(output),
            ]
        )
        == 0
    )
    assert observed == {
        "components_file": components,
        "cache_dir": cache,
        "output_dir": output,
    }


def test_release_legal_gate_is_explicit():
    errors = release_gate_errors(_lock(b"one", b"two", legal_gate=True))

    assert any(error.startswith("python:") for error in errors)


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


def test_repository_lock_uses_exact_pyinstaller_hooks_contrib_source_archive():
    lock = json.loads(
        release_assets.COMPONENTS_FILE.read_text(encoding="utf-8")
    )
    component = next(
        item
        for item in lock["build_components"]
        if item["component"] == "pyinstaller-hooks-contrib"
    )

    assert component["source_archives"] == [
        {
            "filename": "pyinstaller_hooks_contrib-2026.0.tar.gz",
            "url": "https://files.pythonhosted.org/packages/31/8f/8052ff65067697ee80fde45b9731842e160751c41ac5690ba232c22030e8/pyinstaller_hooks_contrib-2026.0.tar.gz",
            "sha256": "0120893de491a000845470ca9c0b39284731ac6bace26f6849dea9627aaed48e",
            "size": 170311,
        }
    ]


def test_repository_lock_uses_stable_exact_libaom_release_archive():
    lock = json.loads(
        release_assets.COMPONENTS_FILE.read_text(encoding="utf-8")
    )
    component = next(
        item
        for item in lock["runtime_components"]
        if item["component"] == "opencv-ffmpeg"
    )
    archive = next(
        item
        for item in component["source_archives"]
        if "aom" in item["filename"]
    )

    assert archive == {
        "filename": "libaom-3.13.1.tar.gz",
        "url": "https://storage.googleapis.com/aom-releases/libaom-3.13.1.tar.gz",
        "sha256": "19e45a5a7192d690565229983dad900e76b513a02306c12053fb9a262cbeca7d",
        "size": 6253958,
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


@pytest.mark.parametrize("legal_gate", [False, True])
def test_runtime_downloads_are_rejected_regardless_of_legal_review(legal_gate):
    lock = _lock(b"one", b"two")
    lock["runtime_downloads"] = [
        {
            "component": "download-only",
            "version": "2.0",
            "bundled_in_installer": False,
            "release_legal_review_required": legal_gate,
            "legal_review": _review(),
        }
    ]

    errors = release_gate_errors(lock)

    assert any("runtime_downloads must be an empty list" in error for error in errors)
    assert any("does not download or redistribute" in error for error in errors)
    assert not any("runtime download legal review" in error for error in errors)


@pytest.mark.parametrize(
    ("present", "runtime_downloads"),
    [
        pytest.param(False, None, id="missing"),
        pytest.param(True, None, id="none"),
        pytest.param(True, "disabled", id="string"),
        pytest.param(True, {}, id="mapping"),
    ],
)
def test_runtime_downloads_requires_an_explicit_empty_list(
    tmp_path,
    present,
    runtime_downloads,
):
    lock = _lock(b"one", b"two")
    if present:
        lock["runtime_downloads"] = runtime_downloads
    else:
        del lock["runtime_downloads"]

    errors = release_gate_errors(lock)

    assert any("runtime_downloads must be an empty list" in error for error in errors)
    assert any("Remove every runtime_downloads entry" in error for error in errors)

    lock_path = tmp_path / "components.json"
    lock_path.write_text(json.dumps(lock), encoding="utf-8")
    with pytest.raises(
        ReleaseAssetError,
        match="Release runtime policy violation: .*runtime_downloads must be an empty list",
    ):
        release_assets.verify_release_asset_payload(
            {},
            components_file=lock_path,
        )


def test_release_asset_creation_cannot_bypass_disabled_runtime_downloads(
    monkeypatch,
    tmp_path,
):
    runtime_downloads = [
        {
            "component": "download-only",
            "version": "2.0",
            "bundled_in_installer": False,
            "release_legal_review_required": False,
            "legal_review": _review(),
        }
    ]

    with pytest.raises(
        ReleaseAssetError,
        match="Release runtime policy violation: .*does not download or redistribute",
    ):
        _create_asset_set(
            tmp_path,
            monkeypatch,
            runtime_downloads=runtime_downloads,
            enforce_release_gates=False,
        )

    assert not (tmp_path / "release").exists()


def test_release_asset_verification_rejects_disabled_runtime_downloads(
    monkeypatch,
    tmp_path,
):
    payload, _output = _create_asset_set(tmp_path, monkeypatch)
    lock_path = tmp_path / "components.json"
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    lock["runtime_downloads"] = [
        {
            "component": "download-only",
            "version": "2.0",
            "bundled_in_installer": False,
            "release_legal_review_required": False,
            "legal_review": _review(),
        }
    ]
    lock_path.write_text(json.dumps(lock), encoding="utf-8")

    with pytest.raises(
        ReleaseAssetError,
        match="Release runtime policy violation: .*does not download or redistribute",
    ):
        verify_release_asset_list(
            Path(payload["asset_list"]),
            components_file=lock_path,
        )


def test_repository_lock_disables_runtime_downloads():
    lock = json.loads(
        release_assets.COMPONENTS_FILE.read_text(encoding="utf-8")
    )

    assert lock["runtime_downloads"] == []
    assert not any(
        "runtime_downloads" in error for error in release_gate_errors(lock)
    )


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


@pytest.mark.parametrize(
    "relative",
    [
        "obs-portable/bin/64bit/obs64.exe",
        "vendor/OBS-Studio/data.txt",
        "ffmpeg.exe",
        "tools/ffprobe.exe",
        "downloads/OBS-Studio-32.1.2-Windows-x64.zip",
        "downloads/OBS-Studio-32.1.2-Windows-x64-Installer.exe",
        "downloads/OBS-Studio-32.1.2-Windows-x64.msi",
        "downloads/ffmpeg-8.1.1-essentials_build.7z",
    ],
)
def test_application_source_rejects_user_provided_runtimes(tmp_path, relative):
    source = tmp_path / "source.zip"
    _write_source_zip(source)
    with zipfile.ZipFile(source, "a") as archive:
        archive.writestr(relative, b"external runtime")

    with pytest.raises(
        ReleaseAssetError,
        match="User-provided OBS/standalone FFmpeg",
    ):
        validate_application_source(source, "1.2.3")


def test_application_source_allows_opencv_ffmpeg_library_name(tmp_path):
    source = tmp_path / "source.zip"
    _write_source_zip(source)
    with zipfile.ZipFile(source, "a") as archive:
        archive.writestr(
            "tests/fixtures/opencv_videoio_ffmpeg4140_64.dll",
            b"fixture",
        )

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
    assert workflow.count("verify-python-runtime") == 2
    assert "verify-bootstrap-pip" in workflow
    assert workflow.count("prepare-python-runtime") == 2
    assert "& $releaseBasePython -m venv $releaseVenv" in workflow
    assert (
        "& $releaseBasePython -m scripts.prepare_release_assets `\n"
        "            check-gates"
    ) in workflow
    assert (
        "& $releasePython -m scripts.prepare_release_assets `\n"
        "            prepare-binary-install"
    ) in workflow
    assert (
        "& $env:PUBLISH_PYTHON -m scripts.prepare_release_assets `\n"
        "            verify"
    ) in workflow
    bare_python_commands = [
        line.strip()
        for line in workflow.splitlines()
        if line.strip().startswith("python -m")
    ]
    assert bare_python_commands == [
        "python -m scripts.prepare_release_assets `",
        "python -m scripts.prepare_release_assets `",
    ]
    assert "python -m venv" not in workflow
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
    assert (
        "release_assets_sha256: "
        "${{ steps.release_assets.outputs.release_assets_sha256 }}"
        in workflow
    )
    assert (
        "EXPECTED_RELEASE_ASSETS_SHA256: "
        "${{ needs.prepare.outputs.release_assets_sha256 }}"
        in workflow
    )
    prepare_assets_step = workflow.index("name: Create and verify release assets")
    prepare_create = workflow.index(
        "& $env:RELEASE_PYTHON -m scripts.prepare_release_assets `\n"
        "            create",
        prepare_assets_step,
    )
    prepare_verify = workflow.index(
        "& $env:RELEASE_PYTHON -m scripts.prepare_release_assets `\n"
        "            verify",
        prepare_create,
    )
    prepare_seal = workflow.index("$releaseAssetsSha256 = (", prepare_verify)
    assert prepare_create < prepare_verify < prepare_seal
    manifest_seal = workflow.index(
        "$actualReleaseAssetsSha256 = (",
        workflow.index("name: Re-verify Release assets after transfer"),
    )
    transferred_verify = workflow.index(
        "& $env:PUBLISH_PYTHON -m scripts.prepare_release_assets `\n"
        "            verify",
        manifest_seal,
    )
    assert manifest_seal < workflow.index(
        "$env:EXPECTED_RELEASE_ASSETS_SHA256",
        manifest_seal,
    ) < transferred_verify
    assert '$pipVersion -cne "26.1.2"' in workflow
    assert "choco install innosetup" not in workflow
    assert ".\\scripts\\prepare_inno_setup.ps1" in workflow
    assert "-InnoSetupRoot $env:INNO_SETUP_ROOT" in workflow
    assert "-InnoSetupProvenance $env:INNO_SETUP_PROVENANCE" in workflow
    assert "gh release verify-asset" not in workflow
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

    windows_workflow = workflow.split("  test-windows:", maxsplit=1)[1]

    assert "name: Build Windows artifact" in windows_workflow
    assert windows_workflow.count("prepare-python-runtime") == 2
    assert windows_workflow.count("verify-python-runtime") == 2
    assert windows_workflow.count("verify-bootstrap-pip") == 2
    assert windows_workflow.count("& $basePython -m venv $venvRoot") == 2
    assert windows_workflow.count("python -m scripts.prepare_release_assets `") == 2
    assert windows_workflow.count("& $env:WINDOWS_PYTHON -m pip `") == 2
    assert windows_workflow.count("& $env:WINDOWS_PYTHON -m pytest `") == 1
    assert "python -m pip" not in windows_workflow
    assert "run: python -m" not in windows_workflow
    assert (
        "run: .\\scripts\\build.ps1 -PythonExe $env:WINDOWS_PYTHON"
        in windows_workflow
    )
    assert "& $env:WINDOWS_PYTHON -m scripts.check_license_compliance" in (
        windows_workflow
    )
    assert "-PythonExe $env:WINDOWS_PYTHON `" in windows_workflow
    assert "Run packaged self-check" in windows_workflow
    self_check_step = windows_workflow.split(
        "      - name: Run packaged self-check", maxsplit=1
    )[1].split("      - name: Build installer", maxsplit=1)[0]
    assert ".\\scripts\\run_packaged_self_check.ps1 `" in self_check_step
    assert "-AppExe .\\dist\\LoLReplayTool\\LoLReplayTool.exe `" in self_check_step
    assert "-TempRoot $env:RUNNER_TEMP `" in self_check_step
    assert "-TimeoutSeconds 60" in self_check_step
    assert "continue-on-error" not in self_check_step
    assert "if: always()" not in self_check_step
    installer_step = windows_workflow.split(
        "      - name: Build installer", maxsplit=1
    )[1].split("\n      - name:", maxsplit=1)[0]
    assert "if: always()" not in installer_step
    assert "if: failure()" not in installer_step
    assert "continue-on-error" not in installer_step
    assert windows_workflow.count("-SkipSelfCheck") == 1
    assert "-SkipBuild `\n            -SkipSelfCheck" in windows_workflow
    assert windows_workflow.index("Run packaged self-check") < windows_workflow.index(
        "-SkipSelfCheck"
    )
    assert "-SkipSelfCheck" not in Path(".github/workflows/release.yml").read_text(
        encoding="utf-8"
    )
    assert "actions/upload-artifact@" not in workflow
    assert "Compress-Archive" not in workflow
    assert "LoLReplayTool-installer" not in workflow


def test_build_scripts_accept_verified_python_and_provenance():
    build = Path("scripts/build.ps1").read_text(encoding="utf-8")
    installer = Path("scripts/build_installer.ps1").read_text(encoding="utf-8")
    spec = Path("LoLReplayTool.spec").read_text(encoding="utf-8")

    for script in (build, installer):
        assert "[string]$PythonExe" in script
        assert "[string]$BuildProvenance" in script
        assert "[string]$BuildProvenanceSha256" in script
        assert "Resolve-Path -LiteralPath $PythonExe" in script
        assert "Resolve-Path -LiteralPath $BuildProvenance" in script
        assert "Assert-BuildProvenance" in script
    assert '"--build-provenance", $resolvedBuildProvenance' in build
    assert '"--build-provenance-sha256", $BuildProvenanceSha256' in build
    assert '$buildArgs["PythonExe"] = $selectedPython' in installer
    assert '$buildArgs["BuildProvenance"] = $resolvedBuildProvenance' in installer
    assert (
        '$buildArgs["BuildProvenanceSha256"] = $BuildProvenanceSha256'
        in installer
    )
    assert '$buildArgs = @{}' in installer
    assert '@("-PythonExe", $selectedPython)' not in installer
    assert '-m PyInstaller --noconfirm --clean "LoLReplayTool.spec"' in build
    assert "apply_windows_runtime_policy(a.binaries)" in spec
    assert "a._save_guts()" in spec
