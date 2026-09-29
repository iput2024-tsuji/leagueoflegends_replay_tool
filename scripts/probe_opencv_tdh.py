"""Bounded, aggregate-only TDH observation of the stopped sampling probe trace.

The native reader runs only in its isolated Windows CI child. Importing this
module, parsing metadata and validating its report never loads a Windows DLL.
"""

from __future__ import annotations

import ctypes as C
import json
import ntpath
import os
import stat
import struct
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

EVENT_MAX = 1_000_000
METADATA_MAX = 1024 * 1024
PROPERTY_MAX = 256
FILE_LIMIT = 64 * 1024 * 1024
IMAGE_PROVIDER = "2cb15d1d-5fc1-11d2-abe1-00a0c911f518"
SYSTEM_TRACE_PROVIDER = "9e814aad-3204-11d2-9a82-006008a86939"
VERSION_KEYS = ("v0", "v1", "v2", "other_uint8")
OPCODE_KEYS = ("load", "unload", "dc_start", "dc_end", "other_uint8")
HEADER_KEYS = ("image", "system_trace", "other")
QUERY_KEYS = (
    "not_found", "error", "buffer_limit", "invalid_metadata", "other_source",
    "other_event_guid", "descriptor_mismatch", "image", "pointer_size_unknown",
)
PID_KEYS = ("schema_unknown", "read_failed", "match", "other")
FILE_NAME_KEYS = (
    "schema_unknown", "read_failed", "c1xx_expected_path", "c1xx_other_path",
    "c2_expected_path", "c2_other_path", "other_basename",
)
PATH_KEYS = ("nt_device", "nt_dos", "extended", "unc", "dos_absolute", "relative", "other")


class TdhError(ValueError):
    """Internal fixed codes; the CLI never prints exception details."""


# Fixed-width SDK declarations, naturally aligned for Windows x64. The callback
# fallback permits pure parser/validator tests on non-Windows hosts.
U8, U16, U32, U64 = C.c_uint8, C.c_uint16, C.c_uint32, C.c_uint64
I32, I64 = C.c_int32, C.c_int64
CALLBACK = getattr(C, "WINFUNCTYPE", C.CFUNCTYPE)


class GUID(C.Structure):
    _fields_ = [("Data1", U32), ("Data2", U16), ("Data3", U16), ("Data4", U8 * 8)]


class EVENT_DESCRIPTOR(C.Structure):
    _fields_ = [
        ("Id", U16), ("Version", U8), ("Channel", U8), ("Level", U8),
        ("Opcode", U8), ("Task", U16), ("Keyword", U64),
    ]


class EVENT_HEADER(C.Structure):
    _fields_ = [
        ("Size", U16), ("HeaderType", U16), ("Flags", U16), ("EventProperty", U16),
        ("ThreadId", U32), ("ProcessId", U32), ("TimeStamp", I64), ("ProviderId", GUID),
        ("EventDescriptor", EVENT_DESCRIPTOR), ("ProcessorTime", U64), ("ActivityId", GUID),
    ]


class ETW_BUFFER_CONTEXT(C.Structure):
    _fields_ = [("ProcessorIndex", U16), ("LoggerId", U16)]


class EVENT_RECORD(C.Structure):
    _fields_ = [
        ("EventHeader", EVENT_HEADER), ("BufferContext", ETW_BUFFER_CONTEXT),
        ("ExtendedDataCount", U16), ("UserDataLength", U16), ("ExtendedData", C.c_void_p),
        ("UserData", C.c_void_p), ("UserContext", C.c_void_p),
    ]


class EVENT_TRACE_HEADER(C.Structure):
    _fields_ = [
        ("Size", U16), ("FieldTypeFlags", U16), ("Version", U32), ("ThreadId", U32),
        ("ProcessId", U32), ("TimeStamp", I64), ("Guid", GUID), ("ProcessorTime", U64),
    ]


class EVENT_TRACE(C.Structure):
    _fields_ = [
        ("Header", EVENT_TRACE_HEADER), ("InstanceId", U32), ("ParentInstanceId", U32),
        ("ParentGuid", GUID), ("MofData", C.c_void_p), ("MofLength", U32), ("ClientContext", U32),
    ]


class TIME_ZONE_INFORMATION(C.Structure):
    _fields_ = [
        ("Bias", I32), ("StandardName", U16 * 32), ("StandardDate", U16 * 8),
        ("StandardBias", I32), ("DaylightName", U16 * 32), ("DaylightDate", U16 * 8),
        ("DaylightBias", I32),
    ]


class TRACE_LOGFILE_HEADER(C.Structure):
    _fields_ = [
        ("BufferSize", U32), ("Version", U32), ("ProviderVersion", U32), ("NumberOfProcessors", U32),
        ("EndTime", I64), ("TimerResolution", U32), ("MaximumFileSize", U32), ("LogFileMode", U32),
        ("BuffersWritten", U32), ("LogInstanceGuid", GUID), ("LoggerName", C.c_void_p),
        ("LogFileName", C.c_void_p), ("TimeZone", TIME_ZONE_INFORMATION), ("BootTime", I64),
        ("PerfFreq", I64), ("StartTime", I64), ("ReservedFlags", U32), ("BuffersLost", U32),
    ]


class EVENT_TRACE_LOGFILEW(C.Structure):
    pass


BUFFER_CALLBACK = CALLBACK(U32, C.POINTER(EVENT_TRACE_LOGFILEW))
EVENT_RECORD_CALLBACK = CALLBACK(None, C.POINTER(EVENT_RECORD))
EVENT_TRACE_LOGFILEW._fields_ = [
    ("LogFileName", C.c_wchar_p), ("LoggerName", C.c_wchar_p), ("CurrentTime", I64),
    ("BuffersRead", U32), ("ProcessTraceMode", U32), ("CurrentEvent", EVENT_TRACE),
    ("LogfileHeader", TRACE_LOGFILE_HEADER), ("BufferCallback", BUFFER_CALLBACK),
    ("BufferSize", U32), ("Filled", U32), ("EventsLost", U32),
    ("EventRecordCallback", EVENT_RECORD_CALLBACK), ("IsKernelTrace", U32), ("Context", C.c_void_p),
]


class TDH_CONTEXT(C.Structure):
    _fields_ = [("ParameterValue", U64), ("ParameterType", U32), ("ParameterSize", U32)]


class PROPERTY_DATA_DESCRIPTOR(C.Structure):
    _fields_ = [("PropertyName", U64), ("ArrayIndex", U32), ("Reserved", U32)]


def _empty_report() -> dict:
    return {
        "events": 0, "header_provider": {}, "query": {},
        "image": {"provider": {}, "version": {}, "opcode": {}, "pid": {}, "header_match": 0},
        "child": {"version": {}, "file_name": {}, "path_form": {}, "header_match": 0},
    }


def validate_report(value) -> dict:
    """Reject unknown fields and non-conserving counters before public output."""
    def keys(item, allowed):
        if type(item) is not dict or set(item) != set(allowed):
            raise ValueError("invalid_report")

    def count(item):
        if type(item) is not int or not 0 <= item <= EVENT_MAX:
            raise ValueError("invalid_report")
        return item

    def histogram(item, allowed):
        if type(item) is not dict or not set(item) <= set(allowed):
            raise ValueError("invalid_report")
        for number in item.values():
            if count(number) == 0:
                raise ValueError("invalid_report")
        return sum(item.values())

    keys(value, ("events", "header_provider", "query", "image", "child"))
    keys(value["image"], ("provider", "version", "opcode", "pid", "header_match"))
    keys(value["child"], ("version", "file_name", "path_form", "header_match"))
    events = count(value["events"])
    headers, query, image, child = (value[key] for key in ("header_provider", "query", "image", "child"))
    headers_total = histogram(headers, HEADER_KEYS)
    queries_total = histogram(query, QUERY_KEYS)
    image_count = query.get("image", 0)
    child_count = image["pid"].get("match", 0) if type(image["pid"]) is dict else -1
    checks = (
        headers_total == events,
        queries_total == headers.get("image", 0) + headers.get("system_trace", 0),
        histogram(image["provider"], HEADER_KEYS) == image_count,
        histogram(image["version"], VERSION_KEYS) == image_count,
        histogram(image["opcode"], OPCODE_KEYS) == image_count,
        histogram(image["pid"], PID_KEYS) == image_count,
        count(image["header_match"]) <= image_count,
        histogram(child["version"], VERSION_KEYS) == child_count,
        histogram(child["file_name"], FILE_NAME_KEYS) == child_count,
        count(child["header_match"]) <= min(child_count, image["header_match"]),
        child["header_match"] >= image["header_match"] - (image_count - child_count),
        all(number <= image["version"].get(key, 0) for key, number in child["version"].items()),
        histogram(child["path_form"], PATH_KEYS) == sum(
            child["file_name"].get(key, 0) for key in FILE_NAME_KEYS[2:6]
        ),
    )
    if not all(checks):
        raise ValueError("invalid_report")
    return value


def maximum_report() -> dict:
    """Capacity fixture only: ten-digit counters deliberately exceed EVENT_MAX."""
    maximum = 9_999_999_999
    return {
        "events": maximum,
        "header_provider": dict.fromkeys(HEADER_KEYS, maximum),
        "query": dict.fromkeys(QUERY_KEYS, maximum),
        "image": {
            "provider": dict.fromkeys(HEADER_KEYS, maximum),
            "version": dict.fromkeys(VERSION_KEYS, maximum),
            "opcode": dict.fromkeys(OPCODE_KEYS, maximum),
            "pid": dict.fromkeys(PID_KEYS, maximum), "header_match": maximum,
        },
        "child": {
            "version": dict.fromkeys(VERSION_KEYS, maximum),
            "file_name": dict.fromkeys(FILE_NAME_KEYS, maximum),
            "path_form": dict.fromkeys(PATH_KEYS, maximum), "header_match": maximum,
        },
    }


def _metadata(data: bytes) -> dict:
    """Parse bounded SDK metadata; strings remain internal and are never logged."""
    if not 112 <= len(data) <= METADATA_MAX:
        raise TdhError("invalid_metadata")
    count, top_count = struct.unpack_from("<II", data, 100)
    end = 112 + count * 24
    if not top_count <= count <= PROPERTY_MAX or end > len(data):
        raise TdhError("invalid_metadata")

    names = {0: ""}

    def text(offset):
        if offset in names:
            return names[offset]
        if offset % 2 or not end <= offset < len(data):
            raise TdhError("invalid_metadata")
        stop = offset
        while stop + 1 < len(data):
            if data[stop:stop + 2] == b"\0\0":
                try:
                    value = data[offset:stop].decode("utf-16-le", errors="strict")
                except UnicodeError:
                    break
                # Preserve only the two names used for schema selection. Cache
                # their classification, including unknown names, by offset.
                names[offset] = value if value in ("ProcessId", "FileName") else ""
                return names[offset]
            stop += 2
        raise TdhError("invalid_metadata")

    for position in (52, 56, 60, 64, 68, 72, 76, 80, 92, 96):
        text(struct.unpack_from("<I", data, position)[0])
    xml_offset, xml_size = struct.unpack_from("<II", data, 84)
    if (xml_offset or xml_size) and not end <= xml_offset <= xml_offset + xml_size <= len(data):
        raise TdhError("invalid_metadata")
    properties = []
    for index in range(count):
        position = 112 + index * 24
        flags, name_offset, first, second, extra, number, length = struct.unpack_from("<IIHHIHH", data, position)
        name = text(name_offset)
        if flags & 1:
            if second and not top_count <= first <= first + second <= count:
                raise TdhError("invalid_metadata")
        elif flags & 0x80:
            if not end <= extra <= len(data) - 4:
                raise TdhError("invalid_metadata")
            schema_size = struct.unpack_from("<H", data, extra + 2)[0]
            if extra + 4 + schema_size > len(data):
                raise TdhError("invalid_metadata")
        else:
            text(extra)
        if flags & 4 and number >= count or flags & 2 and length >= count:
            raise TdhError("invalid_metadata")
        properties.append({
            "name": name, "flags": flags, "count": number,
            "in_type": first, "top_level": index < top_count,
        })
    return {
        "provider_guid": str(uuid.UUID(bytes_le=data[:16])),
        "event_guid": str(uuid.UUID(bytes_le=data[16:32])),
        "source": struct.unpack_from("<I", data, 48)[0],
        "version": data[34], "opcode": data[37], "properties": properties,
    }


def _runtime_allowed() -> bool:
    return (os.name == "nt" and C.sizeof(C.c_void_p) == 8
            and os.environ.get("GITHUB_ACTIONS") == "true"
            and os.environ.get("GITHUB_EVENT_NAME") == "pull_request")


def _native():
    if not _runtime_allowed():
        raise TdhError("unsupported_runtime")
    advapi = C.WinDLL("advapi32.dll", winmode=0x800)
    tdh = C.WinDLL("tdh.dll", winmode=0x800)
    api = SimpleNamespace(advapi=advapi, tdh=tdh)
    declarations = (
        (advapi, "OpenTraceW", [C.POINTER(EVENT_TRACE_LOGFILEW)], U64),
        (advapi, "ProcessTrace", [C.POINTER(U64), U32, C.c_void_p, C.c_void_p], U32),
        (advapi, "CloseTrace", [U64], U32),
        (tdh, "TdhGetEventInformation", [C.POINTER(EVENT_RECORD), U32, C.POINTER(TDH_CONTEXT),
                                         C.c_void_p, C.POINTER(U32)], U32),
        (tdh, "TdhGetPropertySize", [C.POINTER(EVENT_RECORD), U32, C.POINTER(TDH_CONTEXT),
                                    U32, C.POINTER(PROPERTY_DATA_DESCRIPTOR), C.POINTER(U32)], U32),
        (tdh, "TdhGetProperty", [C.POINTER(EVENT_RECORD), U32, C.POINTER(TDH_CONTEXT),
                                U32, C.POINTER(PROPERTY_DATA_DESCRIPTOR), U32, C.c_void_p], U32),
    )
    for library, name, args, result in declarations:
        function = getattr(library, name)
        function.argtypes, function.restype = args, result
        setattr(api, name, function)
    return api


def _query(api, record, context) -> tuple[str, dict | None]:
    size = U32()
    status = api.TdhGetEventInformation(record, 1, C.byref(context), None, C.byref(size))
    if status == 1168:
        return "not_found", None
    if status != 122:
        return "error", None
    if size.value > METADATA_MAX:
        return "buffer_limit", None
    if size.value < 112:
        return "invalid_metadata", None
    allocated = size.value
    buffer = C.create_string_buffer(allocated)
    status = api.TdhGetEventInformation(record, 1, C.byref(context), buffer, C.byref(size))
    if status == 1168:
        return "not_found", None
    if status == 122 or size.value > allocated:
        return "buffer_limit", None
    if status:
        return "error", None
    try:
        metadata = _metadata(buffer.raw[:size.value])
    except ValueError:
        return "invalid_metadata", None
    if metadata["source"] != 1:
        return "other_source", None
    if metadata["event_guid"] != IMAGE_PROVIDER:
        return "other_event_guid", None
    descriptor = record.contents.EventHeader.EventDescriptor
    if (metadata["version"], metadata["opcode"]) != (descriptor.Version, descriptor.Opcode):
        return "descriptor_mismatch", None
    return "image", metadata


def _property(api, record, context, name: str, limit: int) -> bytes | None:
    # Only the two fixed property names reach TDH; metadata names are never used
    # to build a descriptor. One bounded read per property is sufficient here.
    name_buffer = C.create_unicode_buffer(name)
    descriptor = PROPERTY_DATA_DESCRIPTOR(C.addressof(name_buffer), 0xffffffff, 0)
    size = U32()
    if api.TdhGetPropertySize(record, 1, C.byref(context), 1, C.byref(descriptor), C.byref(size)):
        return None
    if name == "ProcessId" and size.value != 4:
        return None
    if not 1 <= size.value <= limit or name == "FileName" and (size.value < 2 or size.value % 2):
        return None
    buffer = C.create_string_buffer(size.value)
    if api.TdhGetProperty(record, 1, C.byref(context), 1, C.byref(descriptor), size.value, buffer):
        return None
    return buffer.raw


def _schema(metadata: dict, name: str, in_type: int) -> bool:
    matches = [item for item in metadata["properties"] if item["name"] == name]
    return (len(matches) == 1 and matches[0]["top_level"] and matches[0]["flags"] == 0
            and matches[0]["count"] == 1 and matches[0]["in_type"] == in_type)


def _increment(histogram: dict, key: str) -> None:
    histogram[key] = histogram.get(key, 0) + 1


def _version(value: int) -> str:
    return f"v{value}" if value in (0, 1, 2) else "other_uint8"


def _path_form(value: str) -> str:
    lower = value.lower()
    if lower.startswith("\\device\\"):
        return "nt_device"
    if lower.startswith("\\??\\"):
        return "nt_dos"
    if lower.startswith("\\\\?\\"):
        return "extended"
    if lower.startswith("\\\\.\\"):
        return "other"
    if lower.startswith("\\\\"):
        return "unc"
    if len(value) >= 3 and value[0].isascii() and value[0].isalpha() and value[1] == ":" and value[2] in "\\/":
        return "dos_absolute"
    if not value.startswith(("\\", "/")):
        return "relative"
    return "other"


def _observe_record(record, api, child_pid: int, compiler: Path, report: dict) -> None:
    if report["events"] >= EVENT_MAX:
        raise TdhError("event_limit")
    report["events"] += 1
    header = record.contents.EventHeader
    provider = str(uuid.UUID(bytes_le=bytes(header.ProviderId)))
    category = {IMAGE_PROVIDER: "image", SYSTEM_TRACE_PROVIDER: "system_trace"}.get(provider, "other")
    _increment(report["header_provider"], category)
    if category == "other":
        return
    pointer_flag = header.Flags & 0x60
    if pointer_flag not in (0x20, 0x40):
        _increment(report["query"], "pointer_size_unknown")
        return
    context = TDH_CONTEXT(4 if pointer_flag == 0x20 else 8, 3, 0)
    outcome, metadata = _query(api, record, context)
    _increment(report["query"], outcome)
    if outcome != "image":
        return
    image = report["image"]
    _increment(image["provider"], {IMAGE_PROVIDER: "image", SYSTEM_TRACE_PROVIDER: "system_trace"}.get(
        metadata["provider_guid"], "other"))
    _increment(image["version"], _version(metadata["version"]))
    _increment(image["opcode"], {10: "load", 2: "unload", 3: "dc_start", 4: "dc_end"}.get(
        metadata["opcode"], "other_uint8"))
    header_match = int(header.ProcessId == child_pid)
    image["header_match"] += header_match
    pid_kind = "schema_unknown"
    if _schema(metadata, "ProcessId", 8):
        payload = _property(api, record, context, "ProcessId", 4)
        pid_kind = "read_failed" if payload is None else "match" if int.from_bytes(payload, "little") == child_pid else "other"
    _increment(image["pid"], pid_kind)
    if pid_kind != "match":
        return
    child = report["child"]
    child["header_match"] += header_match
    _increment(child["version"], _version(metadata["version"]))
    file_kind = "schema_unknown"
    if _schema(metadata, "FileName", 1):
        payload = _property(api, record, context, "FileName", 8194)
        file_kind = "read_failed"
        if payload is not None:
            try:
                filename = payload.decode("utf-16-le", errors="strict")
            except UnicodeError:
                filename = ""
            if filename.endswith("\0") and "\0" not in filename[:-1]:
                filename = filename[:-1]
                basename = ntpath.basename(filename).lower()
                file_kind = "other_basename"
                if basename in ("c1xx.dll", "c2.dll"):
                    expected = ntpath.join(ntpath.dirname(str(compiler)), basename)
                    same = ntpath.normcase(ntpath.normpath(filename)) == ntpath.normcase(ntpath.normpath(expected))
                    file_kind = basename[:-4] + ("_expected_path" if same else "_other_path")
                    _increment(child["path_form"], _path_form(filename))
    _increment(child["file_name"], file_kind)


def _trace_identity(path: Path) -> tuple:
    if not path.is_absolute() or path.name != "sampling_raw.etl":
        raise TdhError("invalid_trace")
    for item in (path, *path.parents):
        if item.is_symlink() or item.is_junction():
            raise TdhError("redirected_trace")
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > FILE_LIMIT:
        raise TdhError("invalid_trace")
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def observe_trace(path: Path, child_pid: int, compiler: Path) -> dict:
    if type(child_pid) is not int or not 1 <= child_pid <= 0xffffffff:
        raise TdhError("invalid_pid")
    if not compiler.is_absolute() or compiler.name.lower() != "cl.exe":
        raise TdhError("invalid_compiler")
    api = _native()
    before = _trace_identity(path)
    report = _empty_report()
    failure = None

    @EVENT_RECORD_CALLBACK
    def event_callback(record):
        nonlocal failure
        if failure is None:
            try:
                _observe_record(record, api, child_pid, compiler, report)
            except BaseException as error:
                failure = TdhError("event_limit" if isinstance(error, TdhError) and str(error) == "event_limit"
                                   else "callback_error")

    @BUFFER_CALLBACK
    def buffer_callback(_logfile):
        return int(failure is None)

    name_buffer = C.create_unicode_buffer(str(path))
    logfile = EVENT_TRACE_LOGFILEW()
    logfile.LogFileName = C.cast(name_buffer, C.c_wchar_p)
    logfile.ProcessTraceMode = 0x10000000 | 0x1000
    logfile.EventRecordCallback, logfile.BufferCallback = event_callback, buffer_callback
    handle = api.OpenTraceW(C.byref(logfile))
    if handle == 0xffffffffffffffff:
        raise TdhError("open_trace")
    try:
        status = api.ProcessTrace(C.byref(U64(handle)), 1, None, None)
        if failure is None and status:
            failure = TdhError("process_trace")
    except BaseException:
        if failure is None:
            failure = TdhError("process_trace")
    finally:
        try:
            close_status = api.CloseTrace(handle)
            if failure is None and close_status:
                failure = TdhError("close_trace")
        except BaseException:
            if failure is None:
                failure = TdhError("close_trace")
    try:
        if _trace_identity(path) != before and failure is None:
            failure = TdhError("trace_changed")
    except (OSError, ValueError):
        if failure is None:
            failure = TdhError("trace_changed")
    if failure is not None:
        raise failure
    return validate_report(report)


def main(argv: list[str] | None = None) -> int:
    try:
        if not _runtime_allowed():
            raise TdhError("unsupported_runtime")
        args = sys.argv[1:] if argv is None else argv
        if len(args) != 6 or args[::2] != ["--trace", "--child-pid", "--compiler"]:
            raise TdhError("invalid_arguments")
        if not args[3].isascii() or not args[3].isdigit() or len(args[3]) > 10:
            raise TdhError("invalid_arguments")
        report = observe_trace(Path(args[1]), int(args[3]), Path(args[5]))
        print(json.dumps(validate_report(report), separators=(",", ":"), sort_keys=True))
        return 0
    except BaseException:
        print("TDH observation failed.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
