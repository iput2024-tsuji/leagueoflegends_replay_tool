import copy
import ctypes
import hashlib
import json
import struct
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import probe_opencv_tdh as target

HEADER = {"image", "system_trace", "other"}
QUERY = {"not_found", "error", "buffer_limit", "invalid_metadata", "other_source",
         "other_event_guid", "descriptor_mismatch", "image", "pointer_size_unknown"}
VERSION = {"v0", "v1", "v2", "other_uint8"}
OPCODE = {"load", "unload", "dc_start", "dc_end", "other_uint8"}
PID = {"schema_unknown", "read_failed", "match", "other"}
FILE = {"schema_unknown", "read_failed", "c1xx_expected_path", "c1xx_other_path",
        "c2_expected_path", "c2_other_path", "other_basename"}
PATH = {"nt_device", "nt_dos", "extended", "unc", "dos_absolute", "relative", "other"}
IMAGE_GUID = "2cb15d1d-5fc1-11d2-abe1-00a0c911f518"
SYSTEM_GUID = "9e814aad-3204-11d2-9a82-006008a86939"


def _empty():
    return {"events": 0, "header_provider": {}, "query": {},
            "image": {"provider": {}, "version": {}, "opcode": {}, "pid": {}, "header_match": 0},
            "child": {"version": {}, "file_name": {}, "path_form": {}, "header_match": 0}}


def _one_image():
    return {"events": 1, "header_provider": {"image": 1}, "query": {"image": 1},
            "image": {"provider": {"system_trace": 1}, "version": {"v1": 1}, "opcode": {"load": 1},
                      "pid": {"match": 1}, "header_match": 1},
            "child": {"version": {"v1": 1}, "file_name": {"c1xx_expected_path": 1},
                      "path_form": {"dos_absolute": 1}, "header_match": 1}}


def _set(value, path, replacement):
    for key in path[:-1]:
        value = value[key]
    value[path[-1]] = replacement


def _metadata_buffer(names=("ProcessId", "FileName"), *, source=1):
    # Windows SDK TRACE_EVENT_INFO prefix 112 and EVENT_PROPERTY_INFO stride 24.
    data = bytearray(112 + 24 * len(names))
    data[:16] = uuid.UUID(SYSTEM_GUID).bytes_le
    data[16:32] = uuid.UUID(IMAGE_GUID).bytes_le
    data[34], data[37] = 2, 10
    struct.pack_into("<I", data, 48, source)
    struct.pack_into("<II", data, 100, len(names), len(names))
    for index, name in enumerate(names):
        name_offset = len(data)
        data.extend(name.encode("utf-16-le") + b"\0\0")
        struct.pack_into("<IIHHIHHI", data, 112 + 24 * index,
                         0, name_offset, 8 if index == 0 else 1, 0, 0, 1, 0, 0)
    return bytes(data)


def test_empty_fixed_report_is_valid_without_claiming_observed_events():
    report = _empty()
    assert target.validate_report(report) == report


@pytest.mark.parametrize("category", sorted(HEADER))
def test_each_header_category_preserves_event_and_query_totals(category):
    report = _empty()
    report.update(events=1, header_provider={category: 1}, query={} if category == "other" else {"not_found": 1})
    assert target.validate_report(report) == report


@pytest.mark.parametrize("category", sorted(QUERY))
def test_each_query_category_is_fixed_and_counted(category):
    report = _one_image() if category == "image" else _empty()
    report.update(events=1, header_provider={"image": 1}, query={category: 1})
    assert target.validate_report(report) == report


@pytest.mark.parametrize("category", sorted(VERSION))
def test_each_image_and_child_version_category_is_reachable(category):
    report = _one_image()
    report["image"]["version"] = report["child"]["version"] = {category: 1}
    assert target.validate_report(report) == report


@pytest.mark.parametrize("category", sorted(HEADER))
def test_metadata_control_provider_classification_does_not_exclude_image_queries(category):
    report = _one_image()
    report["image"]["provider"] = {category: 1}
    assert target.validate_report(report) == report


@pytest.mark.parametrize("category", sorted(OPCODE))
def test_each_image_opcode_category_is_reachable(category):
    report = _one_image()
    report["image"]["opcode"] = {category: 1}
    assert target.validate_report(report) == report


@pytest.mark.parametrize("category", sorted(PID))
def test_each_pid_category_preserves_child_population(category):
    report = _one_image()
    report["image"]["pid"] = {category: 1}
    if category != "match":
        report["child"] = _empty()["child"]
    assert target.validate_report(report) == report


@pytest.mark.parametrize("category", sorted(FILE))
def test_each_file_category_only_counts_path_forms_for_known_basenames(category):
    report = _one_image()
    report["child"]["file_name"] = {category: 1}
    if category in {"schema_unknown", "read_failed", "other_basename"}:
        report["child"]["path_form"] = {}
    assert target.validate_report(report) == report


@pytest.mark.parametrize("category", sorted(PATH))
def test_each_fixed_path_form_is_reachable(category):
    report = _one_image()
    report["child"]["path_form"] = {category: 1}
    assert target.validate_report(report) == report


@pytest.mark.parametrize(("path", "value"), [
    (("events",), True), (("events",), -1), (("events",), 1_000_001),
    (("events",), 0), (("events",), "1"), (("events",), 1.0),
    (("header_provider",), {"image": 0}), (("header_provider",), {"private-provider": 1}),
    (("query",), {}), (("query",), {"image": True}), (("query",), {"private-status": 1}),
    (("image", "provider"), {}), (("image", "version"), {}), (("image", "opcode"), {}), (("image", "pid"), {}),
    (("image", "header_match"), 2), (("image", "header_match"), True),
    (("child", "version"), {}), (("child", "file_name"), {}),
    (("child", "version"), {"v2": 1}), (("child", "header_match"), 0),
    (("child", "path_form"), {}), (("child", "path_form"), {"dos_absolute": 2}),
    (("child", "header_match"), 2), (("child", "header_match"), -1),
])
def test_report_rejects_unknown_types_and_broken_accounting(path, value):
    report = _one_image()
    _set(report, path, value)
    with pytest.raises(ValueError):
        target.validate_report(report)


@pytest.mark.parametrize("path", [(), ("image",), ("child",)])
def test_report_rejects_unknown_fixed_keys_without_exposing_them(path):
    report = _one_image()
    value = report
    for key in path:
        value = value[key]
    value["private-secret-key"] = "private-secret-value"
    with pytest.raises(ValueError) as caught:
        target.validate_report(report)
    assert "private-secret" not in str(caught.value)


@pytest.mark.parametrize("key", ["events", "header_provider", "query", "image", "child"])
def test_report_requires_every_fixed_top_level_key(key):
    report = _empty()
    del report[key]
    with pytest.raises(ValueError):
        target.validate_report(report)


def test_capacity_fixture_uses_only_fixed_vocabulary_and_ten_digit_counts():
    maximum = target.maximum_report()
    assert set(maximum) == set(_empty())
    assert set(maximum["header_provider"]) == HEADER and set(maximum["query"]) == QUERY
    assert set(maximum["image"]) == set(_empty()["image"])
    assert set(maximum["child"]) == set(_empty()["child"])
    for section, key, labels in (("image", "provider", HEADER), ("image", "version", VERSION), ("image", "opcode", OPCODE),
                                 ("image", "pid", PID), ("child", "version", VERSION),
                                 ("child", "file_name", FILE), ("child", "path_form", PATH)):
        assert set(maximum[section][key]) == labels

    def counters(value):
        if isinstance(value, dict):
            return [count for item in value.values() for count in counters(item)]
        return [value]

    assert set(counters(maximum)) == {9999999999}
    with pytest.raises(ValueError):
        target.validate_report(maximum)


def test_metadata_parses_fixed_sdk_fields_and_keeps_names_internal():
    info = target._metadata(_metadata_buffer())
    assert (info["source"], info["provider_guid"], info["event_guid"], info["version"], info["opcode"]) == (
        1, SYSTEM_GUID, IMAGE_GUID, 2, 10,
    )
    assert info["properties"] == [
        {"name": "ProcessId", "flags": 0, "count": 1, "in_type": 8, "top_level": True},
        {"name": "FileName", "flags": 0, "count": 1, "in_type": 1, "top_level": True},
    ]


@pytest.mark.parametrize(("offset", "value"), [
    (100, 257), (104, 3), (116, 113), (116, 112), (116, 0xFFFFFFFE),
    (52, 1), (52, 112), (52, 0xFFFFFFFE),
])
def test_metadata_rejects_out_of_bounds_arrays_and_utf16_offsets(offset, value):
    data = bytearray(_metadata_buffer())
    struct.pack_into("<I", data, offset, value)
    with pytest.raises(ValueError):
        target._metadata(bytes(data))


@pytest.mark.parametrize("data", [b"", b"\0" * 111, _metadata_buffer()[:-1], _metadata_buffer()[:-2]])
def test_metadata_rejects_truncation_and_missing_name_terminator(data):
    with pytest.raises(ValueError):
        target._metadata(data)


def test_metadata_preserves_unknown_schema_without_inventing_supported_properties():
    data = bytearray(_metadata_buffer(("private-name", "Other"), source=99))
    struct.pack_into("<I", data, 112, 2)
    struct.pack_into("<H", data, 128, 2)
    struct.pack_into("<I", data, 104, 1)
    info = target._metadata(bytes(data))
    assert info["source"] == 99
    assert info["properties"][0]["flags"] == 2 and info["properties"][0]["count"] == 2
    assert info["properties"][1]["top_level"] is False
    assert all(item["name"] == "" for item in info["properties"])
    assert "private-name" not in json.dumps(info)


def test_omitted_metadata_property_name_is_not_invented():
    data = bytearray(_metadata_buffer())
    struct.pack_into("<I", data, 116, 0)
    assert target._metadata(bytes(data))["properties"][0]["name"] == ""


def test_metadata_accepts_the_property_count_boundary():
    info = target._metadata(_metadata_buffer(tuple("Field" + str(index) for index in range(256))))
    assert len(info["properties"]) == 256
    assert all(row["top_level"] for row in info["properties"])


@pytest.mark.parametrize("size", [1024 * 1024, 1024 * 1024 + 1])
def test_metadata_buffer_has_a_fixed_allocation_boundary(size):
    prefix = _metadata_buffer()
    data = prefix + b"\0" * (size - len(prefix))
    if size == 1024 * 1024:
        assert target._metadata(data)["event_guid"] == IMAGE_GUID
    else:
        with pytest.raises(ValueError):
            target._metadata(data)


def _record(provider=IMAGE_GUID, *, flags=0x40, pid=999, version=2, opcode=10):
    record = target.EVENT_RECORD()
    record.EventHeader.ProviderId = target.GUID.from_buffer_copy(uuid.UUID(provider).bytes_le)
    record.EventHeader.Flags = flags
    record.EventHeader.ProcessId = pid
    record.EventHeader.EventDescriptor.Version = version
    record.EventHeader.EventDescriptor.Opcode = opcode
    return ctypes.pointer(record)


def _fake_record_reads(monkeypatch, metadata, *, pid=43, filename="C:\\private\\c1xx.dll"):
    calls = []

    def query(api, record, context):
        calls.append(("metadata", context.ParameterValue, context.ParameterType, context.ParameterSize))
        return "image", copy.deepcopy(metadata)

    def prop(api, record, context, name, limit):
        calls.append((name, context.ParameterValue, context.ParameterType, context.ParameterSize))
        return struct.pack("<I", pid) if name == "ProcessId" else (filename + "\0").encode("utf-16-le")

    monkeypatch.setattr(target, "_query", query)
    monkeypatch.setattr(target, "_property", prop)
    return calls


@pytest.mark.parametrize(("flags", "pointer_size"), [(0x20, 4), (0x40, 8), (0x120, 4), (0x140, 8)])
def test_record_pointer_width_context_reaches_metadata_and_each_property(monkeypatch, flags, pointer_size):
    calls = _fake_record_reads(monkeypatch, target._metadata(_metadata_buffer()))
    report = _empty()
    target._observe_record(_record(flags=flags), object(), 43, Path("C:/compiler/cl.exe"), report)
    assert calls == [(name, pointer_size, 3, 0) for name in ("metadata", "ProcessId", "FileName")]
    assert report["image"]["pid"] == {"match": 1} and report["child"]["header_match"] == 0
    assert target.validate_report(report) == report


@pytest.mark.parametrize("flags", [0, 0x60, 0x100, 0x160])
def test_unknown_record_pointer_width_never_queries_tdh(monkeypatch, flags):
    monkeypatch.setattr(target, "_query", lambda *a: pytest.fail("unknown width must not query TDH"))
    report = _empty()
    target._observe_record(_record(flags=flags), object(), 43, Path("C:/compiler/cl.exe"), report)
    assert report["query"] == {"pointer_size_unknown": 1}
    assert report["image"] == _empty()["image"]
    assert target.validate_report(report) == report


def test_other_header_provider_never_queries_or_publishes_its_guid(monkeypatch):
    secret = "11111111-aaaa-bbbb-cccc-222222222222"
    monkeypatch.setattr(target, "_query", lambda *a: pytest.fail("out-of-scope header must not query"))
    report = _empty()
    target._observe_record(_record(secret), object(), 43, Path("C:/compiler/cl.exe"), report)
    assert report["header_provider"] == {"other": 1} and report["query"] == {}
    assert secret not in json.dumps(report)


@pytest.mark.parametrize(("name", "change"), [
    (name, change) for name in ("ProcessId", "FileName")
    for change in ("missing", "duplicate", "nested", "flags", "array", "type", "case")
])
def test_property_selection_requires_unique_exact_leaf_scalar_schema(monkeypatch, name, change):
    metadata = target._metadata(_metadata_buffer())
    index = 0 if name == "ProcessId" else 1
    prop = metadata["properties"][index]
    if change == "missing":
        metadata["properties"].pop(index)
    elif change == "duplicate":
        metadata["properties"].append(dict(prop))
    else:
        key, value = {"nested": ("top_level", False), "flags": ("flags", 1),
                      "array": ("count", 2), "type": ("in_type", 999), "case": ("name", name.lower())}[change]
        prop[key] = value
    calls = _fake_record_reads(monkeypatch, metadata)
    report = _empty()
    target._observe_record(_record(), object(), 43, Path("C:/compiler/cl.exe"), report)
    assert name not in [call[0] for call in calls]
    if name == "ProcessId":
        assert report["image"]["pid"] == {"schema_unknown": 1}
        assert report["child"] == _empty()["child"]
        assert "FileName" not in [call[0] for call in calls]
    else:
        assert report["child"]["file_name"] == {"schema_unknown": 1}
    assert target.validate_report(report) == report


def test_record_accounting_never_borrows_payload_or_header_match_from_another_event(monkeypatch):
    metadata = target._metadata(_metadata_buffer())
    report = _empty()
    _fake_record_reads(monkeypatch, metadata, pid=99)
    target._observe_record(_record(pid=43), object(), 43, Path("C:/compiler/cl.exe"), report)
    _fake_record_reads(monkeypatch, metadata, pid=43)
    target._observe_record(_record(pid=99), object(), 43, Path("C:/compiler/cl.exe"), report)
    assert report["image"]["header_match"] == 1 and report["image"]["pid"] == {"other": 1, "match": 1}
    assert report["child"]["header_match"] == 0 and sum(report["child"]["file_name"].values()) == 1
    assert target.validate_report(report) == report


@pytest.mark.parametrize(("filename", "category", "path_form"), [
    (r"C:\compiler\c1xx.dll", "c1xx_expected_path", {"dos_absolute": 1}),
    (r"C:\compiler\c2.dll", "c2_expected_path", {"dos_absolute": 1}),
    (r"C:\elsewhere\c1xx.dll", "c1xx_other_path", {"dos_absolute": 1}),
    (r"C:\elsewhere\c2.dll", "c2_other_path", {"dos_absolute": 1}),
    (r"C:\compiler\private-other.dll", "other_basename", {}),
])
def test_observed_filename_uses_explicit_compiler_context_and_only_known_basename_path_forms(
    monkeypatch, filename, category, path_form,
):
    calls = _fake_record_reads(monkeypatch, target._metadata(_metadata_buffer()), filename=filename)
    report = _empty()
    target._observe_record(_record(), object(), 43, Path("C:/compiler/cl.exe"), report)
    assert report["child"]["file_name"] == {category: 1}
    assert report["child"]["path_form"] == path_form
    assert [call[0] for call in calls] == ["metadata", "ProcessId", "FileName"]
    assert target.validate_report(report) == report
    assert filename not in json.dumps(report) and hashlib.sha256(filename.encode()).hexdigest() not in json.dumps(report)


def test_unreadable_payload_pid_does_not_read_filename_or_create_child_counts(monkeypatch):
    monkeypatch.setattr(target, "_query", lambda *a: ("image", target._metadata(_metadata_buffer())))
    reads = []

    def prop(api, record, context, name, limit):
        reads.append(name)
        return None

    monkeypatch.setattr(target, "_property", prop)
    report = _empty()
    target._observe_record(_record(pid=43), object(), 43, Path("C:/compiler/cl.exe"), report)
    assert report["image"]["pid"] == {"read_failed": 1} and reads == ["ProcessId"]
    assert report["child"] == _empty()["child"]
    assert target.validate_report(report) == report


def test_private_control_guids_names_and_paths_produce_identical_public_aggregates_without_io(monkeypatch):
    reports = []
    secrets = []
    for suffix, guid in (("one", "11111111-aaaa-bbbb-cccc-222222222222"),
                         ("two", "33333333-dddd-eeee-ffff-444444444444")):
        metadata = target._metadata(_metadata_buffer())
        metadata["provider_guid"] = guid
        metadata["properties"].append({"name": "private-" + suffix, "flags": 0, "count": 1,
                                       "in_type": 1, "top_level": True})
        filename = "C:\\private-" + suffix + "\\c1xx.dll"
        secrets.extend((guid, "private-" + suffix, filename))
        _fake_record_reads(monkeypatch, metadata, filename=filename)
        with monkeypatch.context() as isolated:
            for method in ("open", "stat", "resolve", "exists"):
                isolated.setattr(Path, method, lambda *a, **k: pytest.fail("observed path IO is forbidden"))
            report = _empty()
            target._observe_record(_record(), object(), 43, Path("C:/compiler/cl.exe"), report)
        assert report["image"]["provider"] == {"other": 1}
        assert report["child"]["file_name"] == {"c1xx_other_path": 1}
        reports.append(report)
    assert reports[0] == reports[1]
    public = json.dumps(reports[0])
    for secret in secrets:
        assert secret not in public and hashlib.sha256(secret.encode()).hexdigest() not in public


@pytest.mark.parametrize(("value", "kind"), [
    (r"\Device\HarddiskVolume1\c1xx.dll", "nt_device"), (r"\??\C:\lib\c1xx.dll", "nt_dos"),
    (r"\\?\C:\lib\c1xx.dll", "extended"), (r"\\server\share\c1xx.dll", "unc"),
    (r"C:\lib\c1xx.dll", "dos_absolute"), ("C:/lib/c1xx.dll", "dos_absolute"),
    (r"C:lib\c1xx.dll", "relative"), (r"lib\c1xx.dll", "relative"),
    (r"\\.\pipe\c1xx.dll", "other"), (r"\root\c1xx.dll", "other"), ("/root/c1xx.dll", "other"),
])
def test_known_basename_path_forms_are_fixed_and_do_not_normalize_device_paths(value, kind):
    assert target._path_form(value) == kind


def test_ctypes_layout_matches_the_independently_compiled_windows_x64_sdk_asserts():
    if ctypes.sizeof(ctypes.c_void_p) != 8:
        pytest.skip("reader ABI is Windows x64 only")
    for name, size in (("GUID", 16), ("EVENT_DESCRIPTOR", 16), ("EVENT_HEADER", 80),
                       ("EVENT_RECORD", 112), ("EVENT_TRACE_LOGFILEW", 448),
                       ("TDH_CONTEXT", 16), ("PROPERTY_DATA_DESCRIPTOR", 16)):
        assert ctypes.sizeof(getattr(target, name)) == size
    for cls, field, offset in ((target.EVENT_HEADER, "EventDescriptor", 40),
                               (target.EVENT_RECORD, "UserData", 96),
                               (target.EVENT_TRACE_LOGFILEW, "ProcessTraceMode", 28),
                               (target.EVENT_TRACE_LOGFILEW, "CurrentEvent", 32),
                               (target.EVENT_TRACE_LOGFILEW, "LogfileHeader", 120),
                               (target.EVENT_TRACE_LOGFILEW, "BufferCallback", 400),
                               (target.EVENT_TRACE_LOGFILEW, "EventRecordCallback", 424),
                               (target.EVENT_TRACE_LOGFILEW, "Context", 440),
                               (target.TDH_CONTEXT, "ParameterType", 8),
                               (target.TDH_CONTEXT, "ParameterSize", 12),
                               (target.PROPERTY_DATA_DESCRIPTOR, "ArrayIndex", 8)):
        assert getattr(cls, field).offset == offset


def _information_api(data, *, first=122, second=0, required=None, returned=None):
    calls = []

    def information(record, count, context, buffer, size):
        assert count == 1
        pointer_context = ctypes.cast(context, ctypes.POINTER(target.TDH_CONTEXT)).contents
        assert (pointer_context.ParameterValue, pointer_context.ParameterType, pointer_context.ParameterSize) == (8, 3, 0)
        size_pointer = ctypes.cast(size, ctypes.POINTER(target.U32))
        calls.append(buffer is not None)
        if buffer is None:
            size_pointer.contents.value = len(data) if required is None else required
            return first
        ctypes.memmove(buffer, data, min(len(data), size_pointer.contents.value))
        size_pointer.contents.value = len(data) if returned is None else returned
        return second

    return SimpleNamespace(TdhGetEventInformation=information), calls


@pytest.mark.parametrize(("options", "outcome", "call_count"), [
    ({}, "image", 2), ({"first": 1168}, "not_found", 1), ({"first": 5}, "error", 1),
    ({"first": 0}, "error", 1), ({"required": 0}, "invalid_metadata", 1),
    ({"required": 111}, "invalid_metadata", 1), ({"required": 1024 * 1024 + 1}, "buffer_limit", 1),
    ({"second": 1168}, "not_found", 2), ({"second": 122}, "buffer_limit", 2),
    ({"second": 5}, "error", 2), ({"returned": 1024 * 1024}, "buffer_limit", 2),
    ({"returned": 111}, "invalid_metadata", 2),
])
def test_metadata_query_is_two_pass_bounded_and_classifies_api_results(options, outcome, call_count):
    api, calls = _information_api(_metadata_buffer(), **options)
    result, metadata = target._query(api, _record(), target.TDH_CONTEXT(8, 3, 0))
    assert result == outcome and len(calls) == call_count
    assert (metadata is not None) == (outcome == "image")


@pytest.mark.parametrize(("change", "outcome"), [
    ("source", "other_source"), ("event_guid", "other_event_guid"),
    ("version", "descriptor_mismatch"), ("opcode", "descriptor_mismatch"),
    ("control_guid", "image"),
])
def test_query_uses_mof_event_guid_and_descriptor_but_does_not_gate_control_guid(change, outcome):
    data = bytearray(_metadata_buffer())
    if change == "source":
        struct.pack_into("<I", data, 48, 0)
    elif change in {"event_guid", "control_guid"}:
        start = 16 if change == "event_guid" else 0
        data[start:start + 16] = uuid.UUID("11111111-aaaa-bbbb-cccc-222222222222").bytes_le
    else:
        data[34 if change == "version" else 37] = 99
    api, _ = _information_api(bytes(data))
    result, metadata = target._query(api, _record(), target.TDH_CONTEXT(8, 3, 0))
    assert result == outcome and (metadata is not None) == (outcome == "image")


@pytest.mark.parametrize(("name", "size", "accepted"), [
    ("ProcessId", 0, False), ("ProcessId", 3, False), ("ProcessId", 4, True), ("ProcessId", 5, False),
    ("FileName", 0, False), ("FileName", 1, False), ("FileName", 2, True),
    ("FileName", 3, False), ("FileName", 8194, True), ("FileName", 8195, False), ("FileName", 8196, False),
])
def test_property_reads_only_exact_scalar_pid_or_bounded_even_utf16(name, size, accepted):
    calls = []

    def check(context, count, descriptor):
        assert count == 1
        context = ctypes.cast(context, ctypes.POINTER(target.TDH_CONTEXT)).contents
        assert (context.ParameterValue, context.ParameterType, context.ParameterSize) == (4, 3, 0)
        descriptor = ctypes.cast(descriptor, ctypes.POINTER(target.PROPERTY_DATA_DESCRIPTOR)).contents
        assert ctypes.wstring_at(descriptor.PropertyName) == name
        assert descriptor.ArrayIndex == 0xFFFFFFFF and descriptor.Reserved == 0

    def get_size(record, context_count, context, count, descriptor, output):
        assert context_count == 1
        check(context, count, descriptor)
        calls.append("size")
        ctypes.cast(output, ctypes.POINTER(target.U32)).contents.value = size
        return 0

    def read(record, context_count, context, count, descriptor, actual_size, output):
        assert context_count == 1 and actual_size == size
        check(context, count, descriptor)
        calls.append("read")
        ctypes.memset(output, 0, size)
        return 0

    api = SimpleNamespace(TdhGetPropertySize=get_size, TdhGetProperty=read)
    value = target._property(api, _record(), target.TDH_CONTEXT(4, 3, 0), name, 4 if name == "ProcessId" else 8194)
    assert calls == (["size", "read"] if accepted else ["size"])
    assert value == (b"\0" * size if accepted else None)


@pytest.mark.parametrize("payload", [None, b"x", b"\0\xd8\0\0", "missing terminator".encode("utf-16-le"),
                                      "C:\\private\0\\c1xx.dll\0".encode("utf-16-le")])
def test_filename_read_failure_and_malformed_utf16_never_publish_a_path(monkeypatch, payload):
    metadata = target._metadata(_metadata_buffer())
    monkeypatch.setattr(target, "_query", lambda *a: ("image", metadata))
    monkeypatch.setattr(target, "_property", lambda api, record, context, name, limit:
                        struct.pack("<I", 43) if name == "ProcessId" else payload)
    report = _empty()
    target._observe_record(_record(), object(), 43, Path("C:/compiler/cl.exe"), report)
    assert report["child"]["file_name"] == {"read_failed": 1} and report["child"]["path_form"] == {}
    assert "private" not in json.dumps(report)


def _trace_api(monkeypatch, *, open_status=123, process_status=0, close_status=0, callback_error=False):
    calls, held = [], {}

    def open_trace(pointer):
        log = ctypes.cast(pointer, ctypes.POINTER(target.EVENT_TRACE_LOGFILEW)).contents
        assert log.LoggerName is None and log.ProcessTraceMode == 0x10001000
        assert Path(log.LogFileName).name == "sampling_raw.etl"
        held["log"] = log
        calls.append("open")
        return open_status

    def process_trace(handle, count, start, stop):
        assert ctypes.cast(handle, ctypes.POINTER(target.U64)).contents.value == 123
        assert count == 1 and start is stop is None
        calls.append("process")
        log = held["log"]
        log.EventRecordCallback(_record(flags=0))
        log.EventRecordCallback(_record(flags=0))
        assert log.BufferCallback(ctypes.pointer(log)) == (0 if callback_error else 1)
        if isinstance(process_status, Exception):
            raise process_status
        return process_status

    def close_trace(handle):
        assert handle == 123
        calls.append("close")
        if isinstance(close_status, Exception):
            raise close_status
        return close_status

    api = SimpleNamespace(OpenTraceW=open_trace, ProcessTrace=process_trace, CloseTrace=close_trace)
    monkeypatch.setattr(target, "_native", lambda: api)
    return calls


@pytest.mark.parametrize(("process_status", "close_status", "error"), [
    (0, 0, None), (5, 0, "process_trace"), (RuntimeError("private-process-error"), 0, "process_trace"),
    (0, 5, "close_trace"), (0, RuntimeError("private-close-error"), "close_trace"),
    (5, RuntimeError("private-close-error"), "process_trace"),
])
def test_trace_handle_is_closed_once_and_primary_failure_is_preserved(tmp_path, monkeypatch, process_status, close_status, error):
    path = tmp_path / "sampling_raw.etl"
    path.write_bytes(b"synthetic private trace fixture; never native-read")
    calls = _trace_api(monkeypatch, process_status=process_status, close_status=close_status)
    if error:
        with pytest.raises(target.TdhError, match="^" + error + "$"):
            target.observe_trace(path, 43, tmp_path / "cl.exe")
    else:
        report = target.observe_trace(path, 43, tmp_path / "cl.exe")
        assert report["events"] == 2 and report["query"] == {"pointer_size_unknown": 2}
    assert calls == ["open", "process", "close"]


def test_open_failure_never_processes_or_closes_invalid_handle(tmp_path, monkeypatch):
    path = tmp_path / "sampling_raw.etl"
    path.write_bytes(b"fixture")
    calls = _trace_api(monkeypatch, open_status=0xFFFFFFFFFFFFFFFF)
    with pytest.raises(target.TdhError, match="^open_trace$"):
        target.observe_trace(path, 43, tmp_path / "cl.exe")
    assert calls == ["open"]


@pytest.mark.parametrize("error", [target.TdhError("event_limit"), RuntimeError("private-callback-secret")])
def test_callback_failure_stops_future_callbacks_and_closes_after_process_trace(tmp_path, monkeypatch, error):
    path = tmp_path / "sampling_raw.etl"
    path.write_bytes(b"fixture")
    calls = _trace_api(monkeypatch, callback_error=True)
    observed = []

    def observe(*args):
        observed.append("callback")
        assert calls == ["open", "process"]
        raise error

    monkeypatch.setattr(target, "_observe_record", observe)
    with pytest.raises(target.TdhError, match="^" + ("event_limit" if isinstance(error, target.TdhError) else "callback_error") + "$"):
        target.observe_trace(path, 43, tmp_path / "cl.exe")
    assert observed == ["callback"] and calls == ["open", "process", "close"]


@pytest.mark.parametrize("pid", [0, -1, True, 0x100000000, "43"])
def test_invalid_child_pid_refuses_before_loading_native_apis(tmp_path, monkeypatch, pid):
    monkeypatch.setattr(target, "_native", lambda: pytest.fail("native loading is premature"))
    with pytest.raises(target.TdhError, match="^invalid_pid$"):
        target.observe_trace(tmp_path / "sampling_raw.etl", pid, tmp_path / "cl.exe")


def test_cli_failure_never_prints_raw_exception_or_unknown_arguments(monkeypatch, capsys):
    monkeypatch.setattr(target, "_runtime_allowed", lambda: True)
    monkeypatch.setattr(target, "observe_trace", lambda *a: (_ for _ in ()).throw(RuntimeError("private-exception-text")))
    assert target.main(["--trace", "private-path", "--child-pid", "43", "--compiler", "private-compiler"]) == 1
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == "TDH observation failed.\n"


def test_cli_refuses_non_runner_before_observation(monkeypatch, capsys):
    monkeypatch.setattr(target, "_runtime_allowed", lambda: False)
    monkeypatch.setattr(target, "observe_trace", lambda *a: pytest.fail("must not run a local trace"))
    assert target.main([]) == 1
    assert "private" not in capsys.readouterr().err


def test_event_cap_refuses_before_changing_any_counter(monkeypatch):
    report = _empty()
    report.update(events=target.EVENT_MAX, header_provider={"other": target.EVENT_MAX})
    before = copy.deepcopy(report)
    monkeypatch.setattr(target, "_query", lambda *a: pytest.fail("event cap precedes TDH"))
    with pytest.raises(target.TdhError, match="^event_limit$"):
        target._observe_record(_record(), object(), 43, Path("C:/compiler/cl.exe"), report)
    assert report == before


@pytest.mark.parametrize("name", ["raw.etl", "relogged.etl", "sampling_raw.xml"])
def test_reader_refuses_other_trace_paths_before_open_trace(tmp_path, monkeypatch, name):
    path = tmp_path / name
    path.write_bytes(b"fixture")
    calls = _trace_api(monkeypatch)
    with pytest.raises(target.TdhError, match="^invalid_trace$"):
        target.observe_trace(path, 43, tmp_path / "cl.exe")
    assert calls == []


def test_reader_refuses_redirect_before_open_trace(tmp_path, monkeypatch):
    path = tmp_path / "sampling_raw.etl"
    path.write_bytes(b"fixture")
    calls = _trace_api(monkeypatch)
    original = Path.is_junction
    monkeypatch.setattr(Path, "is_junction", lambda value: value == path or original(value))
    with pytest.raises(target.TdhError, match="^redirected_trace$"):
        target.observe_trace(path, 43, tmp_path / "cl.exe")
    assert calls == []


def test_trace_input_change_after_processing_is_rejected_after_owned_handle_close(tmp_path, monkeypatch):
    path = tmp_path / "sampling_raw.etl"
    path.write_bytes(b"fixture")
    calls = _trace_api(monkeypatch)
    original = target._trace_identity
    count = 0

    def identity(value):
        nonlocal count
        count += 1
        record = original(value)
        if count == 2:
            assert calls == ["open", "process", "close"]
            return (*record[:-1], record[-1] + 1)
        return record

    monkeypatch.setattr(target, "_trace_identity", identity)
    with pytest.raises(target.TdhError, match="^trace_changed$"):
        target.observe_trace(path, 43, tmp_path / "cl.exe")


def test_native_bindings_declare_sdk_call_types_and_load_only_system_libraries(monkeypatch):
    loaded = []
    functions = {}

    def library(name, **kwargs):
        loaded.append((name, kwargs))
        names = ("OpenTraceW", "ProcessTrace", "CloseTrace") if name == "advapi32.dll" else (
            "TdhGetEventInformation", "TdhGetPropertySize", "TdhGetProperty")
        result = SimpleNamespace()
        for function_name in names:
            def function(*args):
                pytest.fail("binding must not execute a native function")
            functions[function_name] = function
            setattr(result, function_name, function)
        return result

    monkeypatch.setattr(target, "_runtime_allowed", lambda: True)
    monkeypatch.setattr(target.C, "WinDLL", library, raising=False)
    api = target._native()
    assert loaded == [("advapi32.dll", {"winmode": 0x800}), ("tdh.dll", {"winmode": 0x800})]
    assert api.OpenTraceW.restype is target.U64
    assert api.OpenTraceW.argtypes == [ctypes.POINTER(target.EVENT_TRACE_LOGFILEW)]
    assert api.ProcessTrace.argtypes == [ctypes.POINTER(target.U64), target.U32, ctypes.c_void_p, ctypes.c_void_p]
    assert api.CloseTrace.argtypes == [target.U64]
    for name in ("ProcessTrace", "CloseTrace", "TdhGetEventInformation", "TdhGetPropertySize", "TdhGetProperty"):
        assert functions[name].restype is target.U32
    assert api.TdhGetEventInformation.argtypes[:3] == [ctypes.POINTER(target.EVENT_RECORD), target.U32,
                                                     ctypes.POINTER(target.TDH_CONTEXT)]


@pytest.mark.parametrize("args", [[], ["--trace", "secret"],
                                  ["--trace", "secret", "--child-pid", "-1", "--compiler", "secret"],
                                  ["--trace", "secret", "--child-pid", "４３", "--compiler", "secret"]])
def test_cli_invalid_arguments_do_not_start_a_reader_or_echo_secrets(monkeypatch, capsys, args):
    monkeypatch.setattr(target, "_runtime_allowed", lambda: True)
    monkeypatch.setattr(target, "observe_trace", lambda *a: pytest.fail("invalid arguments must not run"))
    assert target.main(args) == 1
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == "TDH observation failed.\n"


def test_cli_success_prints_only_valid_fixed_json(monkeypatch, capsys):
    monkeypatch.setattr(target, "_runtime_allowed", lambda: True)
    monkeypatch.setattr(target, "observe_trace", lambda *a: _empty())
    assert target.main(["--trace", "private-path", "--child-pid", "43", "--compiler", "private-compiler"]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == _empty() and captured.err == ""
