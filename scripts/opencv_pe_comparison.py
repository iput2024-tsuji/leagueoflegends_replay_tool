"""Fail-closed, read-only comparison metadata for OpenCV's cv2.pyd."""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any


class OpenCVPEComparisonError(ValueError):
    pass


METHOD = "opencv-cv2-coff-debug-metadata-v1"
_NAMES = (
    "coff_timestamp",
    "debug_timestamp_2",
    "debug_timestamp_12",
    "debug_timestamp_13",
    "codeview_guid",
)
_FIELD_SIZES = (4, 4, 4, 4, 16)
_READ = 0x40000000
_WRITE = 0x80000000
_EXECUTE = 0x20000000


def _fail(message: str) -> None:
    raise OpenCVPEComparisonError(message)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_sha256(value: Any) -> bool:
    if not isinstance(value, str) or len(value) != 64:
        return False
    try:
        decoded = bytes.fromhex(value)
    except ValueError:
        return False
    return len(decoded) == 32


def _overlap(left: tuple[int, int], right: tuple[int, int]) -> bool:
    return left[0] < right[1] and right[0] < left[1]


def inspect_cv2_pe(path: Path) -> dict[str, Any]:
    try:
        import pefile

        data = path.read_bytes()
        pe = pefile.PE(data=data, fast_load=False)
    except Exception as exc:
        _fail(f"cannot parse cv2 PE: {exc}")

    if (
        pe.FILE_HEADER.Machine != 0x8664
        or pe.OPTIONAL_HEADER.Magic != 0x20B
        or not pe.FILE_HEADER.Characteristics & 0x2000
    ):
        _fail("cv2 PE must be an AMD64 PE32+ DLL")

    header_end = (
        pe.DOS_HEADER.e_lfanew
        + 4
        + 20
        + pe.FILE_HEADER.SizeOfOptionalHeader
        + pe.FILE_HEADER.NumberOfSections * 40
    )
    if (
        pe.DOS_HEADER.e_lfanew < 0x40
        or data[pe.DOS_HEADER.e_lfanew : pe.DOS_HEADER.e_lfanew + 4] != b"PE\0\0"
        or pe.FILE_HEADER.SizeOfOptionalHeader != 0xF0
        or pe.OPTIONAL_HEADER.NumberOfRvaAndSizes != 16
        or len(pe.OPTIONAL_HEADER.DATA_DIRECTORY) != 16
        or len(pe.sections) != pe.FILE_HEADER.NumberOfSections
        or header_end > pe.OPTIONAL_HEADER.SizeOfHeaders
        or pe.OPTIONAL_HEADER.SizeOfHeaders > len(data)
    ):
        _fail("cv2 PE headers are invalid or truncated")

    raw_sections: list[tuple[int, int]] = []
    virtual_sections: list[tuple[int, int]] = []
    for section in pe.sections:
        raw_start = section.PointerToRawData
        raw_end = raw_start + section.SizeOfRawData
        virtual_start = section.VirtualAddress
        virtual_end = virtual_start + max(section.Misc_VirtualSize, section.SizeOfRawData)
        if (
            not section.SizeOfRawData
            or raw_start < pe.OPTIONAL_HEADER.SizeOfHeaders
            or raw_end > len(data)
            or virtual_end <= virtual_start
        ):
            _fail("cv2 PE section is invalid or truncated")
        raw_sections.append((raw_start, raw_end))
        virtual_sections.append((virtual_start, virtual_end))
    if any(
        _overlap(left, right)
        for index, left in enumerate(raw_sections)
        for right in raw_sections[index + 1 :]
    ) or any(
        _overlap(left, right)
        for index, left in enumerate(virtual_sections)
        for right in virtual_sections[index + 1 :]
    ):
        _fail("cv2 PE sections overlap")

    def mapped_range(rva: int, size: int) -> tuple[int, int, Any]:
        if not rva or not size:
            _fail("cv2 PE range is empty")
        virtual_end = rva + size
        for section, (raw_start, raw_end), (virtual_start, section_virtual_end) in zip(
            pe.sections, raw_sections, virtual_sections, strict=True
        ):
            if virtual_start <= rva and virtual_end <= section_virtual_end:
                offset = raw_start + rva - virtual_start
                end = offset + size
                if end > raw_end or pe.get_offset_from_rva(rva) != offset:
                    break
                return offset, end, section
        _fail("cv2 PE range is not fully mapped")

    directory_ranges: list[tuple[int, int, int]] = []
    for index, directory in enumerate(pe.OPTIONAL_HEADER.DATA_DIRECTORY):
        if index == 4:
            if directory.VirtualAddress or directory.Size:
                _fail("signed cv2 PE is not supported")
            continue
        if bool(directory.VirtualAddress) != bool(directory.Size):
            _fail("cv2 PE data directory is incomplete")
        if directory.VirtualAddress:
            start, end, _ = mapped_range(directory.VirtualAddress, directory.Size)
            directory_ranges.append((index, start, end))

    debug_dir = pe.OPTIONAL_HEADER.DATA_DIRECTORY[6]
    if not debug_dir.VirtualAddress or debug_dir.Size != 3 * 28:
        _fail("cv2 debug directory must contain exactly three entries")
    debug_offset, debug_end, debug_section = mapped_range(
        debug_dir.VirtualAddress, debug_dir.Size
    )
    if (
        not debug_section.Characteristics & _READ
        or debug_section.Characteristics & (_WRITE | _EXECUTE)
    ):
        _fail("debug directory is not in non-executable read-only storage")

    entries: list[tuple[int, Any]] = []
    for offset in range(debug_offset, debug_end, 28):
        entry = pefile.Structure(pe.__IMAGE_DEBUG_DIRECTORY_format__)
        try:
            entry.__unpack__(data[offset : offset + 28])
        except Exception as exc:
            _fail(f"cannot parse cv2 debug directory: {exc}")
        entries.append((offset, entry))
    if [entry.Type for _, entry in entries] != [2, 12, 13]:
        _fail("cv2 debug directory types must be ordered 2, 12, and 13")

    timestamp = pe.FILE_HEADER.TimeDateStamp
    fields: list[tuple[str, int, int]] = [
        ("coff_timestamp", pe.DOS_HEADER.e_lfanew + 8, 4)
    ]
    storage_ranges: list[tuple[int, int]] = [(debug_offset, debug_end)]
    codeview_offset = -1
    codeview_end = -1
    for offset, entry in entries:
        if entry.TimeDateStamp != timestamp:
            _fail("debug timestamp differs from COFF timestamp")
        payload_start, payload_end, payload_section = mapped_range(
            entry.AddressOfRawData, entry.SizeOfData
        )
        if (
            payload_start != entry.PointerToRawData
            or not payload_section.Characteristics & _READ
            or payload_section.Characteristics & (_WRITE | _EXECUTE)
        ):
            _fail("debug payload is not correctly mapped read-only storage")
        payload_range = (payload_start, payload_end)
        if any(_overlap(payload_range, existing) for existing in storage_ranges):
            _fail("cv2 debug storage overlaps")
        storage_ranges.append(payload_range)
        fields.append((f"debug_timestamp_{entry.Type}", offset + 4, 4))
        if entry.Type == 2:
            codeview_offset, codeview_end = payload_start, payload_end

    for index, start, end in directory_ranges:
        if index != 6 and any(
            _overlap((start, end), storage) for storage in storage_ranges
        ):
            _fail("cv2 debug storage overlaps another PE data directory")

    codeview = data[codeview_offset:codeview_end]
    if len(codeview) < 25 or codeview[:4] != b"RSDS":
        _fail("CodeView record is not bounded RSDS")
    if int.from_bytes(codeview[20:24], "little") != 1:
        _fail("CodeView age must be 1")
    pdb = codeview[24:]
    nul = pdb.find(b"\0")
    if (
        nul < 1
        or nul != len(pdb) - 1
        or any(byte < 0x20 or byte > 0x7E for byte in pdb[:nul])
        or not pdb[:nul].lower().endswith(b"cv2.pdb")
    ):
        _fail("CodeView PDB path must be bounded ASCII ending cv2.pdb")
    fields.append(("codeview_guid", codeview_offset + 4, 16))

    ranges = [(start, start + size) for _, start, size in fields]
    if any(start < 0 or end > len(data) for start, end in ranges):
        _fail("metadata field is out of bounds")
    if any(
        _overlap(left, right)
        for index, left in enumerate(ranges)
        for right in ranges[index + 1 :]
    ):
        _fail("metadata fields overlap")

    normalized = bytearray(data)
    for start, end in ranges:
        normalized[start:end] = b"\0" * (end - start)
    return {
        "method": METHOD,
        "size": len(data),
        "raw_sha256": hashlib.sha256(data).hexdigest(),
        "normalized_sha256": hashlib.sha256(normalized).hexdigest(),
        "fields": [
            {
                "name": name,
                "offset": start,
                "size": size,
                "hex": data[start : start + size].hex(),
            }
            for name, start, size in fields
        ],
    }


def validate_record(record: Any, *, raw_sha256: str, size: int) -> None:
    if (
        not isinstance(record, dict)
        or set(record)
        != {"method", "size", "raw_sha256", "normalized_sha256", "fields"}
        or not _is_int(size)
        or size <= 0
        or not _is_sha256(raw_sha256)
    ):
        _fail("invalid cv2 PE comparison record")
    if (
        record["method"] != METHOD
        or not _is_int(record["size"])
        or record["size"] != size
        or record["raw_sha256"] != raw_sha256
        or not _is_sha256(record["raw_sha256"])
        or not _is_sha256(record["normalized_sha256"])
    ):
        _fail("cv2 PE comparison record identity differs")

    fields = record["fields"]
    if (
        not isinstance(fields, list)
        or len(fields) != len(_NAMES)
        or any(not isinstance(item, dict) for item in fields)
        or [item.get("name") for item in fields] != list(_NAMES)
    ):
        _fail("invalid cv2 PE comparison fields")
    ranges: list[tuple[int, int]] = []
    for item, expected_size in zip(fields, _FIELD_SIZES, strict=True):
        if (
            set(item) != {"name", "offset", "size", "hex"}
            or not _is_int(item["offset"])
            or item["offset"] < 0
            or not _is_int(item["size"])
            or item["size"] != expected_size
            or item["offset"] + item["size"] > size
            or not isinstance(item["hex"], str)
            or len(item["hex"]) != item["size"] * 2
        ):
            _fail("invalid cv2 PE comparison field")
        try:
            decoded = bytes.fromhex(item["hex"])
        except ValueError:
            _fail("invalid cv2 PE comparison field hex")
        if len(decoded) != item["size"]:
            _fail("invalid cv2 PE comparison field hex")
        ranges.append((item["offset"], item["offset"] + item["size"]))
    if any(
        _overlap(left, right)
        for index, left in enumerate(ranges)
        for right in ranges[index + 1 :]
    ):
        _fail("overlapping cv2 PE comparison fields")


def semantic_record(record: dict[str, Any]) -> dict[str, Any]:
    raw_sha256 = record.get("raw_sha256") if isinstance(record, dict) else None
    size = record.get("size") if isinstance(record, dict) else None
    validate_record(record, raw_sha256=raw_sha256, size=size)
    return {
        "method": record["method"],
        "normalized_sha256": record["normalized_sha256"],
        "fields": [
            {"name": item["name"], "offset": item["offset"], "size": item["size"]}
            for item in record["fields"]
        ],
    }
