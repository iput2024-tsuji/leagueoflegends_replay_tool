from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import zipfile
from copy import deepcopy
from pathlib import Path

import pytest

from scripts import prepare_external_vc_runtime_wheels as target

ORIGINAL_LOADER = base64.b64decode(
    "DQonJydIZWxwZXIgdG8gcHJlbG9hZCB2Y29tcDE0MC5kbGwgYW5kIG1zdmNwMTQw"
    "LmRsbCB0byBwcmV2ZW50DQoibm90IGZvdW5kIiBlcnJvcnMuDQoNCk9uY2UgdmNv"
    "bXAxNDAuZGxsIGFuZCBtc3ZjcDE0MC5kbGwgYXJlDQpwcmVsb2FkZWQsIHRoZSBu"
    "YW1lc3BhY2UgaXMgbWFkZSBhdmFpbGFibGUgdG8gYW55IHN1YnNlcXVlbnQNCnZj"
    "b21wMTQwLmRsbCBhbmQgbXN2Y3AxNDAuZGxsLiBUaGlzIGlzDQpjcmVhdGVkIGFz"
    "IHBhcnQgb2YgdGhlIHNjcmlwdHMgdGhhdCBidWlsZCB0aGUgd2hlZWwuDQonJycN"
    "Cg0KDQppbXBvcnQgb3MNCmltcG9ydCBvcy5wYXRoIGFzIG9wDQpmcm9tIGN0eXBl"
    "cyBpbXBvcnQgV2luRExMDQoNCg0KaWYgb3MubmFtZSA9PSAibnQiOg0KICAgIGxp"
    "YnNfcGF0aCA9IG9wLmpvaW4ob3AuZGlybmFtZShfX2ZpbGVfXyksICIubGlicyIp"
    "DQogICAgdmNvbXAxNDBfZGxsX2ZpbGVuYW1lID0gb3Auam9pbihsaWJzX3BhdGgs"
    "ICJ2Y29tcDE0MC5kbGwiKQ0KICAgIG1zdmNwMTQwX2RsbF9maWxlbmFtZSA9IG9w"
    "LmpvaW4obGlic19wYXRoLCAibXN2Y3AxNDAuZGxsIikNCiAgICBXaW5ETEwob3Au"
    "YWJzcGF0aCh2Y29tcDE0MF9kbGxfZmlsZW5hbWUpKQ0KICAgIFdpbkRMTChvcC5h"
    "YnNwYXRoKG1zdmNwMTQwX2RsbF9maWxlbmFtZSkpDQo="
)


def _archive(*names: str) -> zipfile.ZipFile:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        for name in names:
            archive.writestr(name, b"x")
    stream.seek(0)
    return zipfile.ZipFile(stream)


def _lock_payload() -> dict[str, object]:
    components = []
    for name, manifest in target.EXPECTED.items():
        components.append(
            {
                "component": manifest["component"],
                "binary_archive": {
                    "filename": name,
                    "url": manifest["url"],
                    "size": manifest["size"],
                    "sha256": manifest["sha256"],
                },
                "source_archives": deepcopy(manifest["source_archives"]),
            }
        )
    return {"runtime_components": components}


@pytest.mark.parametrize(
    "name",
    [
        "../x",
        "/x",
        "a/../b",
        "C:/x",
        "./x",
        "a//b",
        "NUL.txt",
        "trailing.",
        "trailing ",
    ],
)
def test_safe_members_rejects_windows_unsafe_paths(name: str) -> None:
    with pytest.raises(target.WheelError):
        target._safe_members(_archive(name))


def test_safe_members_rejects_backslash_from_foreign_zip() -> None:
    with _archive("safe") as archive:
        archive.infolist()[0].filename = "a\\b"
        with pytest.raises(target.WheelError):
            target._safe_members(archive)


def test_safe_members_rejects_case_insensitive_duplicates() -> None:
    with _archive("Package/file", "package/FILE") as archive:
        with pytest.raises(target.WheelError, match="duplicate"):
            target._safe_members(archive)


def test_safe_members_rejects_symlinks() -> None:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        info = zipfile.ZipInfo("link")
        info.create_system = 3
        info.external_attr = 0o120777 << 16
        archive.writestr(info, b"target")
    stream.seek(0)
    with zipfile.ZipFile(stream) as archive:
        with pytest.raises(target.WheelError, match="non-regular"):
            target._safe_members(archive)


def test_safe_members_accepts_but_skips_directory_entries() -> None:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.mkdir("package/")
        archive.writestr("package/file", b"x")
    stream.seek(0)
    with zipfile.ZipFile(stream) as archive:
        members = target._safe_members(archive)

    assert [member.filename for member in members] == ["package/file"]


def test_repository_lock_matches_fixed_manifest() -> None:
    entries = target._lock_entries(Path("compliance/components.json"))

    assert set(entries) == set(target.EXPECTED)


def test_lock_rejects_duplicate_wheel_definition(tmp_path) -> None:
    payload = _lock_payload()
    payload["runtime_components"].append(
        deepcopy(payload["runtime_components"][0])  # type: ignore[index,union-attr]
    )
    lock = tmp_path / "components.json"
    lock.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(target.WheelError, match="duplicate"):
        target._lock_entries(lock)


def test_lock_rejects_manifest_drift(tmp_path) -> None:
    payload = _lock_payload()
    payload["runtime_components"][0]["binary_archive"]["sha256"] = "0" * 64  # type: ignore[index]
    lock = tmp_path / "components.json"
    lock.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(target.WheelError, match="metadata differs"):
        target._lock_entries(lock)


def test_record_is_rebuilt_with_hashes(tmp_path) -> None:
    record_name = "demo-1.0.dist-info/RECORD"
    files = {
        "demo-1.0.dist-info/METADATA": b"x",
        record_name: b"old",
    }
    target._write_wheel(files, tmp_path / "demo.whl", record_name)
    with zipfile.ZipFile(tmp_path / "demo.whl") as archive:
        rows = {
            row[0]: row[1:]
            for row in csv.reader(
                io.StringIO(archive.read(record_name).decode("utf-8"))
            )
        }
    encoded = (
        base64.urlsafe_b64encode(hashlib.sha256(b"x").digest())
        .rstrip(b"=")
        .decode("ascii")
    )
    assert rows[record_name] == ["", ""]
    assert rows["demo-1.0.dist-info/METADATA"] == [f"sha256={encoded}", "1"]


def test_write_wheel_rejects_multiple_records(tmp_path) -> None:
    record_name = "demo-1.0.dist-info/RECORD"
    files = {
        record_name: b"",
        "other-1.0.dist-info/RECORD": b"",
    }

    with pytest.raises(target.WheelError, match="RECORD set differs"):
        target._write_wheel(files, tmp_path / "demo.whl", record_name)


def test_record_output_is_byte_identical(tmp_path) -> None:
    record_name = "demo-1.0.dist-info/RECORD"
    files = {
        "demo-1.0.dist-info/METADATA": b"x",
        record_name: b"old",
    }
    first = tmp_path / "one.whl"
    second = tmp_path / "two.whl"
    target._write_wheel(dict(files), first, record_name)
    target._write_wheel(dict(files), second, record_name)

    assert first.read_bytes() == second.read_bytes()


def test_loader_patch_is_exact_and_removes_app_local_path(tmp_path) -> None:
    path = tmp_path / "_distributor_init.py"
    path.write_bytes(ORIGINAL_LOADER)

    before, after = target._patch_loader(path)

    assert before == target.LOADER_SHA256
    assert after == hashlib.sha256(target.LOADER_REPLACEMENT).hexdigest()
    assert path.read_bytes() == target.LOADER_REPLACEMENT


def test_loader_hash_is_fail_closed(tmp_path) -> None:
    path = tmp_path / "_distributor_init.py"
    path.write_bytes(b"unexpected")

    with pytest.raises(target.WheelError, match="drifted"):
        target._patch_loader(path)


def test_runtime_and_affected_sets_match_observed_wheels() -> None:
    manifests = {
        value["distribution"]: value for value in target.EXPECTED.values()
    }

    assert len(manifests["PyQt6-Qt6"]["runtime_members"]) == 6
    assert manifests["pandas"]["runtime_members"] == {
        f"pandas.libs/{target.HASHED_RUNTIME}"
    }
    assert sum(len(value["affected"]) for value in manifests.values()) == 3


def test_after_validation_rejects_app_local_runtime() -> None:
    manifest = next(
        value
        for value in target.EXPECTED.values()
        if value["distribution"] == "PyQt6-Qt6"
    )
    inventory = {
        "summary": {
            "app_local_runtime_files": ["PyQt6/Qt6/bin/msvcp140.dll"],
            "hashed_imports": [],
            "unknown_runtime_imports": [],
        },
        "files": [],
    }

    with pytest.raises(target.WheelError, match="policy failed"):
        target._validate_after(inventory, manifest)


def test_run_rejects_non_release_python(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(target.sys, "version_info", (3, 14, 7))

    with pytest.raises(target.WheelError, match="requires Python 3.14.6, got 3.14.7"):
        target.run(
            tmp_path / "input",
            tmp_path / "output",
            tmp_path / "components.json",
            tmp_path / "tools",
        )

    assert not (tmp_path / "output").exists()


def test_run_leaves_no_partial_output_on_failure(monkeypatch, tmp_path) -> None:
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    lock = tmp_path / "components.json"
    lock.write_text("{}")
    output_dir = tmp_path / "output"
    fake_wheels = {name: input_dir / name for name in target.EXPECTED}
    fake_locked = {
        name: {
            "url": manifest["url"],
            "size": manifest["size"],
            "sha256": manifest["sha256"],
            "source_archives": manifest["source_archives"],
        }
        for name, manifest in target.EXPECTED.items()
    }
    calls = 0

    monkeypatch.setattr(target, "validate_inputs", lambda *_: fake_wheels)
    monkeypatch.setattr(target, "_lock_entries", lambda *_: fake_locked)
    monkeypatch.setattr(
        target,
        "_validate_tool_artifacts",
        lambda *_: (tmp_path / "delvewheel.whl", {}),
    )
    monkeypatch.setattr(target, "_check_delvewheel", lambda *_: "1.13.0")

    def fail_after_one(
        source, output, manifest, provenance, tool_wheel
    ) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise target.WheelError("injected failure")
        output.write_bytes(b"partial")

    monkeypatch.setattr(target, "transform_wheel", fail_after_one)

    with pytest.raises(target.WheelError, match="injected failure"):
        target.run(input_dir, output_dir, lock, tmp_path)

    assert not output_dir.exists()
    assert not list(tmp_path.glob(".output-*"))
