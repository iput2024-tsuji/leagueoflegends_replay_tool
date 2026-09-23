import struct
from pathlib import Path

import pytest

from scripts import opencv_pe_comparison as target

_PE_OFFSET = 0x80
_COFF_OFFSET = _PE_OFFSET + 4
_OPTIONAL_OFFSET = _COFF_OFFSET + 20
_DIRECTORIES_OFFSET = _OPTIONAL_OFFSET + 112
_DEBUG_OFFSET = 0x400
_CODEVIEW_OFFSET = 0x460
_TYPE_12_OFFSET = 0x4C0
_TYPE_13_OFFSET = 0x4E0
_IMPORT_OFFSET = 0x540


def _make_pe(
    *,
    timestamp: int = 1,
    guid: bytes = b"G" * 16,
    pdb_path: bytes = b"C:\\build\\cv2.pdb",
) -> bytearray:
    data = bytearray(0xA00)
    data[:2] = b"MZ"
    struct.pack_into("<I", data, 0x3C, _PE_OFFSET)
    data[_PE_OFFSET : _PE_OFFSET + 4] = b"PE\0\0"
    struct.pack_into(
        "<HHIIIHH",
        data,
        _COFF_OFFSET,
        0x8664,
        2,
        timestamp,
        0,
        0,
        0xF0,
        0x2022,
    )

    struct.pack_into("<H", data, _OPTIONAL_OFFSET, 0x20B)
    struct.pack_into("<I", data, _OPTIONAL_OFFSET + 4, 0x200)
    struct.pack_into("<I", data, _OPTIONAL_OFFSET + 8, 0x600)
    struct.pack_into("<I", data, _OPTIONAL_OFFSET + 16, 0x1000)
    struct.pack_into("<I", data, _OPTIONAL_OFFSET + 20, 0x1000)
    struct.pack_into("<Q", data, _OPTIONAL_OFFSET + 24, 0x180000000)
    struct.pack_into("<II", data, _OPTIONAL_OFFSET + 32, 0x1000, 0x200)
    struct.pack_into("<HH", data, _OPTIONAL_OFFSET + 40, 10, 0)
    struct.pack_into("<HH", data, _OPTIONAL_OFFSET + 48, 10, 0)
    struct.pack_into("<II", data, _OPTIONAL_OFFSET + 56, 0x3000, 0x200)
    struct.pack_into("<H", data, _OPTIONAL_OFFSET + 68, 3)
    struct.pack_into(
        "<QQQQII",
        data,
        _OPTIONAL_OFFSET + 72,
        0x100000,
        0x1000,
        0x100000,
        0x1000,
        0,
        16,
    )
    struct.pack_into("<II", data, _DIRECTORIES_OFFSET + 8, 0x2140, 0x20)
    struct.pack_into("<II", data, _DIRECTORIES_OFFSET + 6 * 8, 0x2000, 3 * 28)

    sections_offset = _OPTIONAL_OFFSET + 0xF0
    struct.pack_into(
        "<8sIIIIIIHHI",
        data,
        sections_offset,
        b".text\0\0\0",
        0x200,
        0x1000,
        0x200,
        0x200,
        0,
        0,
        0,
        0,
        0x60000020,
    )
    struct.pack_into(
        "<8sIIIIIIHHI",
        data,
        sections_offset + 40,
        b".rdata\0\0",
        0x600,
        0x2000,
        0x600,
        0x400,
        0,
        0,
        0,
        0,
        0x40000040,
    )
    data[0x200:0x400] = b"T" * 0x200
    data[0x580:0x5A0] = b"D" * 0x20
    data[_IMPORT_OFFSET : _IMPORT_OFFSET + 0x20] = b"I" * 0x20

    codeview = b"RSDS" + guid + struct.pack("<I", 1) + pdb_path + b"\0"
    payloads = (
        (2, _CODEVIEW_OFFSET, codeview),
        (12, _TYPE_12_OFFSET, b"L" * 0x20),
        (13, _TYPE_13_OFFSET, b"R" * 0x20),
    )
    for index, (kind, raw_offset, payload) in enumerate(payloads):
        rva = 0x2000 + raw_offset - 0x400
        struct.pack_into(
            "<IIHHIIII",
            data,
            _DEBUG_OFFSET + index * 28,
            0,
            timestamp,
            0,
            0,
            kind,
            len(payload),
            rva,
            raw_offset,
        )
        data[raw_offset : raw_offset + len(payload)] = payload
    return data


def _inspect(tmp_path: Path, data: bytes, name: str = "cv2.pyd"):
    path = tmp_path / name
    path.write_bytes(data)
    return target.inspect_cv2_pe(path)


def _record():
    return {
        "method": target.METHOD,
        "size": 128,
        "raw_sha256": "a" * 64,
        "normalized_sha256": "b" * 64,
        "fields": [
            {
                "name": name,
                "offset": (0, 8, 16, 24, 32)[index],
                "size": size,
                "hex": "0" * (size * 2),
            }
            for index, (name, size) in enumerate(
                zip(target._NAMES, (4, 4, 4, 4, 16), strict=True)
            )
        ],
    }


def test_only_approved_metadata_changes_normalize_identically(tmp_path: Path):
    first = _inspect(tmp_path, _make_pe(timestamp=1, guid=b"A" * 16), "first.pyd")
    second = _inspect(tmp_path, _make_pe(timestamp=2, guid=b"B" * 16), "second.pyd")

    assert first["raw_sha256"] != second["raw_sha256"]
    assert first["normalized_sha256"] == second["normalized_sha256"]
    assert [field["name"] for field in first["fields"]] == list(target._NAMES)
    assert [field["size"] for field in first["fields"]] == [4, 4, 4, 4, 16]


@pytest.mark.parametrize("changed_offset", [0x200, 0x580, _IMPORT_OFFSET])
def test_code_data_and_import_changes_remain_in_hash(tmp_path: Path, changed_offset: int):
    original = _make_pe()
    changed = bytearray(original)
    changed[changed_offset] ^= 1

    first = _inspect(tmp_path, original, "first.pyd")
    second = _inspect(tmp_path, changed, "second.pyd")
    assert first["normalized_sha256"] != second["normalized_sha256"]


def test_pdb_path_change_remains_in_hash(tmp_path: Path):
    first = _inspect(tmp_path, _make_pe(pdb_path=b"C:\\one\\cv2.pdb"), "first.pyd")
    second = _inspect(tmp_path, _make_pe(pdb_path=b"D:\\two\\cv2.pdb"), "second.pyd")
    assert first["normalized_sha256"] != second["normalized_sha256"]


def _set_age(data: bytearray) -> None:
    struct.pack_into("<I", data, _CODEVIEW_OFFSET + 20, 2)


def _set_bad_codeview_signature(data: bytearray) -> None:
    data[_CODEVIEW_OFFSET : _CODEVIEW_OFFSET + 4] = b"NB10"


def _set_debug_count(data: bytearray) -> None:
    struct.pack_into("<I", data, _DIRECTORIES_OFFSET + 6 * 8 + 4, 2 * 28)


def _set_bad_debug_type(data: bytearray) -> None:
    struct.pack_into("<I", data, _DEBUG_OFFSET + 12, 14)


def _set_bad_debug_order(data: bytearray) -> None:
    struct.pack_into("<I", data, _DEBUG_OFFSET + 12, 12)
    struct.pack_into("<I", data, _DEBUG_OFFSET + 28 + 12, 2)


def _truncate(data: bytearray) -> None:
    del data[-1]


def _extend_payload_past_section(data: bytearray) -> None:
    struct.pack_into("<I", data, _DEBUG_OFFSET + 16, 0x5A1)


def _set_rva_raw_mismatch(data: bytearray) -> None:
    struct.pack_into("<I", data, _DEBUG_OFFSET + 24, _CODEVIEW_OFFSET + 1)


def _overlap_debug_payloads(data: bytearray) -> None:
    struct.pack_into("<II", data, _DEBUG_OFFSET + 28 + 20, 0x2060, _CODEVIEW_OFFSET)


def _set_signed(data: bytearray) -> None:
    struct.pack_into("<II", data, _DIRECTORIES_OFFSET + 4 * 8, 0x900, 0x20)


def _clear_dll_flag(data: bytearray) -> None:
    characteristics = struct.unpack_from("<H", data, _COFF_OFFSET + 18)[0]
    struct.pack_into("<H", data, _COFF_OFFSET + 18, characteristics & ~0x2000)


def _set_debug_storage_writable(data: bytearray) -> None:
    second_section = _OPTIONAL_OFFSET + 0xF0 + 40
    characteristics = struct.unpack_from("<I", data, second_section + 36)[0]
    struct.pack_into("<I", data, second_section + 36, characteristics | 0x80000000)


@pytest.mark.parametrize(
    "mutation",
    [
        _set_age,
        _set_bad_codeview_signature,
        _set_debug_count,
        _set_bad_debug_type,
        _set_bad_debug_order,
        _truncate,
        _extend_payload_past_section,
        _set_rva_raw_mismatch,
        _overlap_debug_payloads,
        _set_signed,
        _clear_dll_flag,
        _set_debug_storage_writable,
    ],
    ids=lambda mutation: mutation.__name__,
)
def test_unexpected_pe_structures_fail_closed(tmp_path: Path, mutation):
    data = _make_pe()
    mutation(data)
    with pytest.raises(target.OpenCVPEComparisonError):
        _inspect(tmp_path, data)


@pytest.mark.parametrize(("field_offset", "value"), [(12, 0x1000), (20, 0x300)])
def test_section_overlap_fails_closed(tmp_path: Path, field_offset: int, value: int):
    data = _make_pe()
    second_section = _OPTIONAL_OFFSET + 0xF0 + 40
    struct.pack_into("<I", data, second_section + field_offset, value)
    with pytest.raises(target.OpenCVPEComparisonError, match="sections overlap"):
        _inspect(tmp_path, data)


def test_truncated_headers_fail_closed(tmp_path: Path):
    data = _make_pe()
    struct.pack_into("<I", data, _OPTIONAL_OFFSET + 60, 0x100)
    with pytest.raises(target.OpenCVPEComparisonError, match="headers"):
        _inspect(tmp_path, data)


def test_unbounded_data_directory_fails_closed(tmp_path: Path):
    data = _make_pe()
    struct.pack_into("<II", data, _DIRECTORIES_OFFSET + 8, 0x25F0, 0x20)
    with pytest.raises(target.OpenCVPEComparisonError, match="not fully mapped"):
        _inspect(tmp_path, data)


def test_debug_storage_overlapping_other_directory_fails_closed(tmp_path: Path):
    data = _make_pe()
    struct.pack_into(
        "<II", data, _DIRECTORIES_OFFSET + 8, 0x2060, 0x10
    )
    with pytest.raises(target.OpenCVPEComparisonError, match="another PE data directory"):
        _inspect(tmp_path, data)


def test_semantic_record_strips_raw_bytes_and_hash():
    semantic = target.semantic_record(_record())
    assert set(semantic) == {"method", "normalized_sha256", "fields"}
    assert all(set(field) == {"name", "offset", "size"} for field in semantic["fields"])


def _drop_fields(record):
    record.pop("fields")


def _non_dict_field(record):
    record["fields"][0] = None


def _bad_hash(record):
    record["normalized_sha256"] = "z" * 64


def _spaced_hash(record):
    record["normalized_sha256"] = "aa " * 21 + "a"


def _spaced_field_hex(record):
    record["fields"][0]["hex"] = "00 00   "


def _bool_offset(record):
    record["fields"][0]["offset"] = True


def _bool_record_size(record):
    record["size"] = True


def _bool_field_size(record):
    record["fields"][0]["size"] = True


def _wrong_size_for_name(record):
    record["fields"][0].update(size=16, hex="0" * 32)


def _overlap_fields(record):
    record["fields"][1]["offset"] = 2


@pytest.mark.parametrize(
    "mutation",
    [
        _drop_fields,
        _non_dict_field,
        _bad_hash,
        _spaced_hash,
        _spaced_field_hex,
        _bool_offset,
        _bool_record_size,
        _bool_field_size,
        _wrong_size_for_name,
        _overlap_fields,
    ],
    ids=lambda mutation: mutation.__name__,
)
def test_validate_record_rejects_malformed_records(mutation):
    record = _record()
    mutation(record)
    with pytest.raises(target.OpenCVPEComparisonError):
        target.validate_record(record, raw_sha256="a" * 64, size=128)


@pytest.mark.parametrize("record", [None, [], True])
def test_validate_record_rejects_non_mapping(record):
    with pytest.raises(target.OpenCVPEComparisonError):
        target.validate_record(record, raw_sha256="a" * 64, size=128)


@pytest.mark.parametrize(
    ("raw_sha256", "size"),
    [("not-hex".ljust(64, "z"), 128), ("a" * 64, True)],
)
def test_validate_record_rejects_invalid_expected_identity(raw_sha256, size):
    with pytest.raises(target.OpenCVPEComparisonError):
        target.validate_record(_record(), raw_sha256=raw_sha256, size=size)


def test_inspect_missing_file_fails_closed(tmp_path: Path):
    with pytest.raises(target.OpenCVPEComparisonError):
        target.inspect_cv2_pe(tmp_path / "missing.pyd")
