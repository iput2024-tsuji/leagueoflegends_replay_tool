import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import pe_runtime_audit as audit


def _pe(*, normal=(), delay=()):
    def entries(names):
        return [SimpleNamespace(dll=name) for name in names]

    return SimpleNamespace(
        DIRECTORY_ENTRY_IMPORT=entries(normal),
        DIRECTORY_ENTRY_DELAY_IMPORT=entries(delay),
    )


@pytest.fixture
def fake_pe(monkeypatch):
    def install(mapping):
        monkeypatch.setattr(
            audit.pefile,
            "PE",
            lambda path, fast_load=False: mapping[Path(path).name],
        )

    return install


def test_inventory_covers_delay_case_and_reverse_order(tmp_path, fake_pe):
    (tmp_path / "z").mkdir()
    (tmp_path / "z" / "b.DLL").write_bytes(b"b")
    (tmp_path / "a.exe").write_bytes(b"a")
    fake_pe(
        {
            "a.exe": _pe(
                normal=(b"MSVCP140.dll",), delay=(b"vcruntime140.dll",)
            ),
            "b.DLL": _pe(normal=(b"MSVCP140.dll",)),
        }
    )
    result = audit.build_inventory(tmp_path)
    assert result["schema_version"] == 2
    assert result["tool"] == {
        "name": "pe_runtime_audit",
        "pefile_version": audit.pefile.__version__,
    }
    assert [item["path"] for item in result["files"]] == ["a.exe", "z/b.DLL"]
    assert result["runtime_reverse"]["msvcp140.dll"] == [
        {"pe": "a.exe", "import_type": "normal"},
        {"pe": "z/b.DLL", "import_type": "normal"},
    ]
    assert result["runtime_reverse"]["vcruntime140.dll"][0]["import_type"] == "delay"


def test_hashed_unknown_and_app_local_are_recorded_and_enforced(tmp_path, fake_pe):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "msvcp140-a4c2229b.dll").write_bytes(b"runtime")
    (tmp_path / "x.pyd").write_bytes(b"x")
    fake_pe(
        {
            "msvcp140-a4c2229b.dll": _pe(),
            "x.pyd": _pe(
                normal=(b"VCOMP140-custom.dll", b"msvcp140-a4c2229b.dll")
            ),
        }
    )
    result = audit.build_inventory(tmp_path)
    assert result["summary"]["app_local_runtime_files"] == ["sub/msvcp140-a4c2229b.dll"]
    assert len(result["summary"]["hashed_imports"]) == 1
    assert len(result["summary"]["unknown_runtime_imports"]) == 1
    with pytest.raises(audit.AuditError) as exc_info:
        audit.build_inventory(tmp_path, enforce_external=True)
    message = str(exc_info.value)
    assert "sub/msvcp140-a4c2229b.dll" in message
    assert "x.pyd -> msvcp140-a4c2229b.dll (normal)" in message
    assert "x.pyd -> VCOMP140-custom.dll (normal)" in message


def test_invalid_import_and_parse_failure_fail_closed(tmp_path, fake_pe):
    (tmp_path / "bad.dll").write_bytes(b"bad")
    fake_pe({"bad.dll": _pe(normal=(b"\xff.dll",))})
    with pytest.raises(audit.AuditError):
        audit.build_inventory(tmp_path)
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        audit.pefile,
        "PE",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            audit.pefile.PEFormatError("bad")
        ),
    )
    with pytest.raises(audit.AuditError):
        audit.build_inventory(tmp_path)
    monkeypatch.undo()


@pytest.mark.parametrize(
    "name",
    [
        "MSVCP140-a4c2229b.dll",
        "msvcp140_1-a4c2229b.dll",
        "msvcp140_2-a4c2229b.dll",
        "VCRUNTIME140_1-a4c2229b.dll",
        "VCOMP140-a4c2229b.dll",
        "CONCRT140-a4c2229b.dll",
    ],
)
def test_known_runtime_bases_with_hash_are_classified_as_hashed(name):
    assert audit._runtime_kind(name) == "hashed"


def test_fixed_qt_system_icu_import_is_recorded_and_required(tmp_path, fake_pe):
    qt_core = tmp_path / "_internal" / "PyQt6" / "Qt6" / "bin" / "Qt6Core.dll"
    qt_core.parent.mkdir(parents=True)
    qt_core.write_bytes(b"qt")
    fake_pe({"Qt6Core.dll": _pe(normal=(b"icuuc.dll",))})

    result = audit.build_inventory(tmp_path, require_qt_system_icu=True)

    assert result["summary"]["app_local_icu_files"] == []
    assert result["summary"]["icu_imports"] == [
        {
            "name": "icuuc.dll",
            "pe": "_internal/PyQt6/Qt6/bin/Qt6Core.dll",
            "import_type": "normal",
        }
    ]


@pytest.mark.parametrize(
    ("normal", "delay", "extra_pe", "expected_fragment"),
    [
        ((), (), False, "actual none"),
        ((), (b"icuuc.dll",), False, "icuuc.dll (delay)"),
        ((b"icuin.dll",), (), False, "icuin.dll (normal)"),
        ((b"icuuc78.dll",), (), False, "icuuc78.dll (normal)"),
        ((b"icuuc.dll", b"icuuc.dll"), (), False, "icuuc.dll (normal)"),
        ((b"icuuc.dll",), (), True, "helper.pyd -> icuuc.dll"),
    ],
)
def test_qt_system_icu_import_graph_fails_closed_on_any_difference(
    tmp_path,
    fake_pe,
    normal,
    delay,
    extra_pe,
    expected_fragment,
):
    qt_core = tmp_path / "_internal" / "PyQt6" / "Qt6" / "bin" / "Qt6Core.dll"
    qt_core.parent.mkdir(parents=True)
    qt_core.write_bytes(b"qt")
    mapping = {"Qt6Core.dll": _pe(normal=normal, delay=delay)}
    if extra_pe:
        helper = tmp_path / "_internal" / "helper.pyd"
        helper.write_bytes(b"helper")
        mapping["helper.pyd"] = _pe(normal=(b"icuuc.dll",))
    fake_pe(mapping)

    with pytest.raises(audit.AuditError) as exc_info:
        audit.build_inventory(tmp_path, require_qt_system_icu=True)

    message = str(exc_info.value)
    assert "Qt system ICU import graph differs" in message
    assert expected_fragment in message


@pytest.mark.parametrize(
    "relative",
    [
        "icuuc.dll",
        "_internal/ICUDT78.DLL",
        "_internal/icuuc.78.dll",
        "_internal/PyQt6/Qt6/bin/icuin.dll",
        "nested/icu.dll",
    ],
)
def test_app_local_icu_files_are_rejected_case_insensitively(
    tmp_path, fake_pe, relative
):
    target = tmp_path / Path(relative)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"icu")
    fake_pe({target.name: _pe()})

    result = audit.build_inventory(tmp_path)
    assert result["summary"]["app_local_icu_files"] == [relative]
    with pytest.raises(audit.AuditError, match="app-local ICU files"):
        audit.build_inventory(tmp_path, enforce_external=True)


def test_cli_output_is_deterministic_and_output_file_is_quiet(tmp_path, fake_pe, capsys):
    (tmp_path / "a.exe").write_bytes(b"a")
    fake_pe({"a.exe": _pe(normal=(b"MSVCP140.dll",))})
    assert audit.main([str(tmp_path)]) == 0
    first = capsys.readouterr()
    assert first.err == ""
    parsed = json.loads(first.out)
    assert "root" not in first.out and "timestamp" not in first.out
    out = tmp_path / "inventory.json"
    assert audit.main([str(tmp_path), "--output", str(out)]) == 0
    second = capsys.readouterr()
    assert second.out == ""
    assert json.loads(out.read_text(encoding="utf-8")) == parsed
