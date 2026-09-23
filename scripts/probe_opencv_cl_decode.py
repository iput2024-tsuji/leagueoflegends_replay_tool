"""Observe one synthetic CL translation unit; never validate a product wheel.

Raw traces, decoder XML and command output remain in a private RUNNER_TEMP
directory. The first probe deliberately reports incomplete: real decoder schema,
event loss, clocks and ordering must be reviewed before production collection.
"""

from __future__ import annotations

import hashlib
import json
import ntpath
import os
import re
import stat
import subprocess
import tempfile
import time
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path

from scripts.prepare_opencv_wheel import REQUIRED_TOOLSET_VERSION

FILE_LIMIT = 64 * 1024 * 1024
TOTAL_LIMIT = 256 * 1024 * 1024
JSON_LIMIT = 64 * 1024
EVENT_LIMIT = 32
SCHEMA_GROUP_LIMIT = 8
BI_PROVIDER = "f78a07b0-796a-5da4-5c20-61aa526e77af"
PROCESS_PROVIDER = "3d6fa8d0-fe05-11d0-9dda-00c04fd7ba7c"
IMAGE_PROVIDER = "2cb15d1d-5fc1-11d2-abe1-00a0c911f518"
SYSTEM_TRACE_PROVIDER = "9e814aad-3204-11d2-9a82-006008a86939"
NS = "{http://schemas.microsoft.com/win/2004/08/events/event}"
TRACE_NAMESPACES = {scheme: "{" + scheme + "://schemas.microsoft.com/win/2004/08/events/trace}"
                    for scheme in ("http", "https")}
SOURCE = (
    "namespace { int transform(int value) { return (value * 17) ^ (value >> 2); } }\n"
    "int issue139_probe(int value) { return transform(value) + 1; }\n"
)


class ProbeError(ValueError):
    """Only fixed diagnostic codes, never raw command output, are public."""

    def __init__(self, code: str, *, private_file_check: dict | None = None):
        super().__init__(code)
        self.private_file_check = private_file_check


def _no_redirect(path: Path) -> None:
    for item in (path, *path.parents):
        if item.is_symlink() or item.is_junction():
            raise ProbeError("redirected_path")


def _fingerprint(path: Path) -> dict:
    _no_redirect(path)
    before = path.stat()
    if not stat.S_ISREG(before.st_mode) or before.st_size > FILE_LIMIT:
        raise ProbeError("invalid_input_file")
    with path.open("rb") as stream:
        digest = hashlib.sha256()
        size = 0
        while chunk := stream.read(min(1024 * 1024, FILE_LIMIT + 1 - size)):
            size += len(chunk)
            if size > FILE_LIMIT:
                raise ProbeError("input_file_limit")
            digest.update(chunk)
        after = os.fstat(stream.fileno())
    def identity(value):
        return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns

    if identity(before) != identity(after) or identity(after) != identity(path.stat()):
        raise ProbeError("input_changed_during_read")
    return {"path": str(path), "size": after.st_size, "sha256": digest.hexdigest()}


def _check_private_size(private: Path) -> None:
    total = 0
    for path in private.iterdir():
        _no_redirect(path)
        info = path.stat()
        regular_file = stat.S_ISREG(info.st_mode)
        exceeds_file_limit = info.st_size > FILE_LIMIT
        if not regular_file or exceeds_file_limit:
            files = {
                "probe.cpp", "probe.rsp", "probe.obj", "raw.etl", "relogged.etl", "raw.xml",
                "relogged.xml", "inspection.xml", "summary.txt", "interpreted.xml",
                "sampling_raw.etl", "sampling_raw.xml",
            }
            logs = {f"{stage}.log" for stage in (
                "checkout", "start", "compile", "stop", "relog", "decode_raw", "decode_relogged",
                "inspect_raw", "sampling_start", "sampling_compile", "sampling_stop", "sampling_decode_raw",
            )}
            category = path.name if path.name in files else "command_log" if path.name in logs else "other"
            raise ProbeError("private_file_limit", private_file_check={
                "category": category, "regular_file": regular_file, "exceeds_file_limit": exceeds_file_limit,
            })
        total += info.st_size
    if total > TOTAL_LIMIT:
        raise ProbeError("private_total_limit")


def _command(args: list[str], private: Path, stage: str, timeout: float, report: dict) -> dict:
    entry = {"stage": stage, "status": "failed", "pid": None, "returncode": None}
    report["commands"].append(entry)
    process = None
    try:
        with (private / f"{stage}.log").open("xb") as output:
            process = subprocess.Popen(
                args, cwd=private, stdin=subprocess.DEVNULL, stdout=output,
                stderr=subprocess.STDOUT, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            entry["pid"] = process.pid
            deadline = time.monotonic() + timeout
            while process.poll() is None:
                # An already oversized trace must not prevent stopping our session.
                if stage not in {"stop", "sampling_stop"}:
                    _check_private_size(private)
                if time.monotonic() >= deadline:
                    raise ProbeError("command_timeout")
                time.sleep(0.1)
            entry["returncode"] = process.returncode
        _check_private_size(private)
        if process.returncode != 0:
            raise ProbeError("command_nonzero")
        entry["status"] = "success"
        return entry
    finally:
        if process is not None and process.poll() is None:
            # Only this owned child handle; never discover/kill other processes.
            try:
                process.kill()
                process.wait(timeout=5)
            except Exception:
                entry["child_cleanup"] = "failed"


def _integer(value: str | None) -> int | None:
    if value is None or not re.fullmatch(r"(?:0x[0-9a-fA-F]+|[0-9]+)", value):
        return None
    number = int(value, 16 if value.startswith("0x") else 10)
    return number if 0 <= number <= 0xFFFFFFFF else None


def _value_shape(value: str | None) -> dict:
    # Describe the original text; this helper never alters parser input.
    stripped = value.strip() if value is not None else ""
    form = "missing" if value is None else "empty" if not value else "other"
    if value:
        if not stripped:
            form = "whitespace"
        elif re.fullmatch(r"[0-9]+", stripped):
            form = "decimal"
        elif re.fullmatch(r"0[xX][0-9a-fA-F]+", stripped):
            form = "hex"
    return {
        "present": value is not None, "form": form,
        "length": len(value) if value is not None else None,
        "sha256": hashlib.sha256(value.encode("utf-8")).hexdigest() if value is not None else None,
        "surrounding_whitespace": value is not None and value != stripped,
    }


def _time_created_shape(timestamp: ET.Element | None) -> dict:
    allowed = {"SystemTime", "RawTime", "TimeStamp", "Timestamp"}
    attributes = timestamp.attrib if timestamp is not None else {}
    return {
        "present": timestamp is not None,
        "attributes": {name: _value_shape(attributes.get(name)) for name in sorted(allowed)},
        "unknown_attribute_count": len(attributes.keys() - allowed),
    }


def _missing_guid_observation(event: ET.Element, system: ET.Element, child_pid: int) -> tuple[dict, dict]:
    def number(value):
        return _integer(value) if value is not None and len(value) <= 64 else None

    def field(nodes):
        value = (nodes[0].text or "") if len(nodes) == 1 and not len(nodes[0]) else None
        shape = _value_shape(value)
        return {"count": len(nodes), "nested_elements": sum(len(node) for node in nodes),
                "form": shape["form"], "surrounding_whitespace": shape["surrounding_whitespace"]}, shape, value

    provider = system.find(f"{NS}Provider")
    provider_names = {"Name", "EventSourceName"}
    provider_shapes = {name: _value_shape(provider.get(name)) for name in sorted(provider_names)}
    child_names = {"System", "EventData", "UserData", "RenderingInfo", "ProcessingErrorData", "BinaryEventData", "DebugData"}
    child_counts = {}
    for node in event:
        namespace, separator, local_name = node.tag.rpartition("}")
        namespace_kind = "event" if namespace + separator == NS else "other" if separator else "none"
        name_kind = local_name if local_name in child_names else "other"
        key = f"{namespace_kind}:{name_kind}"
        child_counts[key] = child_counts.get(key, 0) + 1
    structure = {
        "event_child_count": len(event),
        "event_direct_children": dict(sorted(child_counts.items())),
        "provider_attributes": {name: shape["form"] for name, shape in provider_shapes.items()},
        "other_provider_attribute_count": len(provider.attrib.keys() - provider_names),
        "system_count": len(event.findall(f"{NS}System")),
        "provider_count": len(system.findall(f"{NS}Provider")),
        "other_system_element_count": sum(node.tag not in {
            f"{NS}{name}" for name in ("Provider", "Execution", "TimeCreated", "EventID", "Version", "Task", "Opcode")
        } for node in system),
        "system_fields": {}, "payload_fields": {},
    }
    sample = {"provider_attributes": provider_shapes, "system_fields": {}, "payload_fields": {}}
    for name in ("EventID", "Version", "Task", "Opcode"):
        nodes = system.findall(f"{NS}{name}")
        if nodes:
            info, shape, value = field(nodes)
            info["uint32"] = number(value)
            structure["system_fields"][name] = info
            sample["system_fields"][name] = shape
    executions = system.findall(f"{NS}Execution")
    header = executions[0].get("ProcessID") if len(executions) == 1 and not len(executions[0]) else None
    header_pid = number(header) if structure["system_count"] == 1 else None
    sample["execution_pid"] = _value_shape(header)
    structure["execution_count"] = len(executions)
    structure["execution_pid_form"] = sample["execution_pid"]["form"]
    structure["header_matches_child"] = header_pid == child_pid if header_pid is not None else None
    event_data = event.findall(f"{NS}EventData")
    payload = event.findall(f"{NS}EventData/{NS}Data")
    user_data = event.findall(f"{NS}UserData")
    names = {"ProcessId", "ParentId", "ImageFileName", "FileName", "ImageBase", "UniqueProcessKey", "CommandLine"}
    structure.update(
        event_data_count=len(event_data), data_count=len(payload),
        other_event_data_element_count=sum(len(node) for node in event_data) - len(payload),
        user_data_count=len(user_data), user_data_child_count=sum(len(node) for node in user_data),
        other_payload_field_count=sum(node.get("Name") not in names for node in payload),
        other_payload_attribute_count=sum(len(node.attrib.keys() - {"Name"}) for node in payload),
        payload_matches_child=None,
    )
    for name in sorted(names):
        nodes = [node for node in payload if node.get("Name") == name]
        if nodes:
            info, shape, value = field(nodes)
            structure["payload_fields"][name] = info
            sample["payload_fields"][name] = shape
            if name == "ProcessId" and len(event_data) == 1:
                payload_pid = number(value)
                structure["payload_matches_child"] = payload_pid == child_pid if payload_pid is not None else None
    return structure, sample


def _private_bytes(path: Path) -> bytes:
    record = _fingerprint(path)
    with path.open("rb") as stream:
        data = stream.read(FILE_LIMIT + 1)
    if len(data) > FILE_LIMIT:
        raise ProbeError("decoder_file_limit")
    if len(data) != record["size"] or hashlib.sha256(data).hexdigest() != record["sha256"]:
        raise ProbeError("input_changed_during_read")
    return data


def _xml_root(data: bytes) -> ET.Element:
    # A bounded local tool output, not a remotely supplied document. No DTD/entity
    # expansion is needed, even if a future decoder changes its output format.
    declaration_scan = data.replace(b"\0", b"").upper()
    if b"<!DOCTYPE" in declaration_scan or b"<!ENTITY" in declaration_scan:
        raise ProbeError("xml_declaration_rejected")
    return ET.fromstring(data)


def _stop_statistics(path: Path) -> dict:
    lines = _private_bytes(path).split(b"\n")
    result = {"value_format": "ascii_decimal_string_max_64_digits_not_sdk_type", "counters": {}}
    for name, label in (
        ("msvc_events", b"Dropped MSVC events: "),
        ("msvc_buffers", b"Dropped MSVC buffers: "),
        ("system_events", b"Dropped system events: "),
        ("system_buffers", b"Dropped system buffers: "),
    ):
        values = [line.removesuffix(b"\r")[len(label):] for line in lines if line.startswith(label)]
        value = values[0] if len(values) == 1 else None
        valid = value is not None and re.fullmatch(rb"[0-9]{1,64}", value) is not None
        result["counters"][name] = {
            "label_occurrences": len(values), "value": value.decode("ascii") if valid else None,
        }
    return result


def _decoder_document(path: Path, *, xml: bool) -> dict:
    data = _private_bytes(path)
    result = {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
              "text_encoding": "unknown", "known_guid_literal_occurrences_not_events": None}
    encoding = "utf-16" if data.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
    try:
        if data.startswith((b"\xff\xfe\0\0", b"\0\0\xfe\xff")) or (encoding == "utf-8-sig" and b"\0" in data):
            raise UnicodeError("unsupported_text_encoding")
        text = data.decode(encoding)
    except UnicodeError:
        pass
    else:
        result["text_encoding"] = encoding
        # Literal appearances may describe schemas, not collected events. Never
        # feed these counts into provider decoding or process lifetime matching.
        counts = {guid: 0 for guid in (BI_PROVIDER, PROCESS_PROVIDER, IMAGE_PROVIDER)}
        for match in re.finditer(r"(?i)(?<![0-9a-f-])[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}(?![0-9a-f-])", text):
            guid = match.group().lower()
            if guid in counts:
                counts[guid] += 1
        result["known_guid_literal_occurrences_not_events"] = counts
    if xml:
        try:
            root = _xml_root(data)
        except ET.ParseError:
            result["xml_status"] = "unknown_format"
        else:
            result.update(xml_status="well_formed", root_name_shape=_value_shape(root.tag),
                          element_count=sum(1 for _ in root.iter()),
                          attribute_count=sum(len(node.attrib) for node in root.iter()))
    return result


def _strict_guid(value: str | None) -> str | None:
    if value is None or len(value) not in (36, 38):
        return None
    if len(value) == 38:
        if not (value.startswith("{") and value.endswith("}")):
            return None
        value = value[1:-1]
    return value.lower() if re.fullmatch(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", value) else None


def _image_payload_unknown_reason(event: ET.Element, event_data: list, payload: list) -> str:
    # Explain only a failed existing PID check; never supply a replacement PID.
    if not event_data:
        return "foreign_event_data" if any(
            node.tag.rsplit("}", 1)[-1] == "EventData" for node in event
        ) else "missing_event_data"
    if len(event_data) != 1:
        return "multiple_event_data"
    if not payload:
        return "foreign_process_id" if any(
            node.tag.rsplit("}", 1)[-1] == "Data" and node.get("Name") == "ProcessId" for node in event_data[0]
        ) else "missing_process_id"
    if len(payload) != 1:
        return "multiple_process_id"
    if len(payload[0]):
        return "nested_process_id"
    value = payload[0].text
    if not value:
        return "empty_text"
    if len(value) > 64:
        return "text_limit"
    if value != value.strip():
        return "surrounding_whitespace"
    if not re.fullmatch(r"(?:0x[0-9a-fA-F]+|[0-9]+)", value):
        return "invalid_syntax"
    return "out_of_range"


def _candidate_child_metadata(system: ET.Element, event_data: ET.Element, compiler: Path, row: dict) -> None:
    # Fixed shapes for the B-side candidate only: no path IO or strict event selection.
    classes = {}
    for name, known in (
        ("Version", {0: "v0", 1: "v1", 2: "v2"}),
        ("Opcode", {10: "load", 2: "unload", 3: "dc_start", 4: "dc_end"}),
    ):
        nodes = system.findall(f"{NS}{name}")
        if not nodes:
            category = "missing"
        elif len(nodes) != 1:
            category = "multiple"
        elif len(nodes[0]):
            category = "nested"
        elif not re.fullmatch(r"[0-9]{1,64}", nodes[0].text or "") or int(nodes[0].text) > 255:
            category = "invalid_text"
        else:
            category = known.get(int(nodes[0].text), "other_uint8")
        classes[name.lower()] = category

    times = system.findall(f"{NS}TimeCreated")
    if not times:
        category = "missing"
    elif len(times) != 1:
        category = "multiple"
    elif len(times[0]):
        category = "nested"
    else:
        system_time, raw_time = times[0].get("SystemTime"), times[0].get("RawTime")
        if system_time is None and raw_time is None:
            category = "no_known_attributes"
        elif (system_time is not None and not re.fullmatch(r"[0-9:.TZ+\-]{1,64}", system_time)) or (
            raw_time is not None and (
                not re.fullmatch(r"[0-9]{1,64}", raw_time) or int(raw_time) > (1 << 64) - 1
            )
        ):
            category = "invalid_known_text"
        else:
            category = "both" if system_time is not None and raw_time is not None else (
                "system_only" if system_time is not None else "raw_only"
            )
    classes["time_created"] = category

    names = [node for node in event_data.findall(f"{NS}Data") if node.get("Name") == "FileName"]
    if not names:
        category = "foreign_namespace" if any(
            node.tag.rsplit("}", 1)[-1] == "Data" and node.get("Name") == "FileName" for node in event_data
        ) else "missing"
    elif len(names) != 1:
        category = "multiple"
    elif len(names[0]):
        category = "nested"
    elif not names[0].text:
        category = "empty"
    elif len(names[0].text) > 4096:
        category = "text_limit"
    else:
        value = names[0].text
        leaf = ntpath.basename(value).lower()
        if leaf in {"c1xx.dll", "c2.dll"}:
            location = "expected_path" if _same_path(value, str(compiler.parent / leaf)) else "other_path"
            category = f"{leaf[:-4]}_{location}"
        else:
            category = "other_basename"
    classes["file_name"] = category

    counts = row.setdefault("candidate_child_metadata", {
        "version": {}, "opcode": {}, "time_created": {}, "file_name": {},
        "c1xx_v1_v2_load_time_path": 0, "c2_v1_v2_load_time_path": 0,
    })
    for name, category in classes.items():
        counts[name][category] = counts[name].get(category, 0) + 1
    if (classes["version"] in {"v1", "v2"} and classes["opcode"] == "load"
            and classes["time_created"] in {"system_only", "raw_only"}):
        joint = {"c1xx_expected_path": "c1xx_v1_v2_load_time_path",
                 "c2_expected_path": "c2_v1_v2_load_time_path"}.get(classes["file_name"])
        if joint is not None:
            counts[joint] += 1


def _trace_identity_observation(event: ET.Element, child_pid: int, observation: dict, compiler: Path) -> None:
    def increment(counts, name):
        counts[name] = counts.get(name, 0) + 1

    systems = event.findall(f"{NS}System")
    providers = systems[0].findall(f"{NS}Provider") if len(systems) == 1 else []
    if len(systems) > 1 or len(providers) > 1:
        increment(observation, "ambiguous_structure_events")
        return
    if not providers:
        return
    raw_guid = providers[0].get("Guid")
    if raw_guid is None:
        source = observation["missing_provider_guid"]
    elif _strict_guid(raw_guid) == SYSTEM_TRACE_PROVIDER:
        source = observation["system_trace"]
    else:
        return
    increment(source, "events")
    # Observe exact direct paths only. These classifications never select an
    # event for the strict decoder, nor identify its process lifetime.
    extensions = [node for node in event if node.tag.rsplit("}", 1)[-1] == "ExtendedTracingInfo"]
    if not extensions:
        increment(source, "missing_extension")
        return
    if len(extensions) != 1:
        increment(source, "multiple_extensions")
        return
    extension = extensions[0]
    scheme = next((name for name, namespace in TRACE_NAMESPACES.items()
                   if extension.tag == f"{namespace}ExtendedTracingInfo"), None)
    if scheme is None:
        increment(source, "unsupported_extension_namespace")
        return
    counts = source.setdefault(scheme, {})
    nodes = [node for node in extension if node.tag.rsplit("}", 1)[-1] == "EventGuid"]
    if not nodes:
        increment(counts, "missing_guid")
        return
    if len(nodes) != 1:
        increment(counts, "multiple_guids")
        return
    node = nodes[0]
    if node.tag != f"{TRACE_NAMESPACES[scheme]}EventGuid":
        increment(counts, "foreign_guid_namespace")
        return
    if len(node):
        increment(counts, "nested_guid")
        return
    guid = _strict_guid(node.text)
    if guid is None:
        increment(counts, "malformed_guid")
        return
    category = {PROCESS_PROVIDER: "process", IMAGE_PROVIDER: "image", BI_PROVIDER: "build_insights"}.get(guid, "other_guid")
    row = counts.setdefault(category, dict.fromkeys((
        "events", "header_match", "header_unknown", "payload_match", "payload_unknown",
    ), 0))
    row["events"] += 1
    executions = systems[0].findall(f"{NS}Execution")
    header = executions[0].get("ProcessID") if len(executions) == 1 and not len(executions[0]) else None
    event_data = event.findall(f"{NS}EventData")
    payload = [node for node in event.findall(f"{NS}EventData/{NS}Data") if node.get("Name") == "ProcessId"]
    value = payload[0].text if len(event_data) == len(payload) == 1 and not len(payload[0]) else None
    for name, text in (("header", header), ("payload", value)):
        pid = _integer(text) if text is not None and len(text) <= 64 else None
        if pid is None:
            row[f"{name}_unknown"] += 1
            if name == "payload" and guid == IMAGE_PROVIDER and source is observation["system_trace"]:
                reason = _image_payload_unknown_reason(event, event_data, payload)
                increment(row.setdefault("payload_unknown_reasons", {}), reason)
                if scheme == "http" and reason == "surrounding_whitespace":
                    # Bounded diagnostic candidate only; strict decoding keeps the original text.
                    candidate = _integer(value.strip(" \t\r\n\f\v"))
                    row["ascii_valid"] = row.get("ascii_valid", 0) + (candidate is not None)
                    row["ascii_match"] = row.get("ascii_match", 0) + (candidate == child_pid)
                    if candidate == child_pid:
                        _candidate_child_metadata(systems[0], event_data[0], compiler, row)
        elif pid == child_pid:
            row[f"{name}_match"] += 1


def _events(path: Path, child_pid: int, schema: dict, *, trace_identity: dict | None = None,
            compiler: Path | None = None):
    if trace_identity is not None and compiler is None:
        raise ValueError("trace_identity_requires_compiler")
    root = _xml_root(_private_bytes(path))
    schema.update(event_count=0, known_provider_counts={}, provider_guid_counts={},
                  missing_system_events=0, missing_provider_events=0,
                  missing_provider_guid_events=0, malformed_provider_guid_events=0,
                  target_pid_events=0, unsupported_namespace_events=0, target_field_shapes={})
    missing_guid = {"groups": [], "overflow_events": 0, "truncated": False,
                    "header_child_matches": 0, "payload_child_matches": 0}
    schema["missing_guid_schema"] = missing_guid
    groups = {}
    for event in root.iter():
        if event.tag.rsplit("}", 1)[-1] != "Event":
            continue
        schema["event_count"] += 1
        if event.tag != f"{NS}Event":
            schema["unsupported_namespace_events"] += 1
            continue
        if trace_identity is not None:
            _trace_identity_observation(event, child_pid, trace_identity, compiler)
        system = event.find(f"{NS}System")
        if system is None:
            schema["missing_system_events"] += 1
            continue
        provider = system.find(f"{NS}Provider")
        if provider is None:
            schema["missing_provider_events"] += 1
            continue
        raw_guid = provider.get("Guid")
        if raw_guid is None:
            schema["missing_provider_guid_events"] += 1
            structure, sample = _missing_guid_observation(event, system, child_pid)
            missing_guid["header_child_matches"] += structure["header_matches_child"] is True
            missing_guid["payload_child_matches"] += structure["payload_matches_child"] is True
            # Group only by structure and fixed System numbers, never payload hashes/values.
            key = json.dumps(structure, sort_keys=True, separators=(",", ":"))
            if key not in groups and len(groups) == SCHEMA_GROUP_LIMIT and structure["header_matches_child"] is True:
                displaced = next((name for name in reversed(groups)
                                  if groups[name]["structure"]["header_matches_child"] is not True), None)
                if displaced is not None:
                    # Preserve the earliest groups at each priority and account for every discarded event.
                    group = groups.pop(displaced)
                    missing_guid["groups"].remove(group)
                    missing_guid["overflow_events"] += group["count"]
                    missing_guid["truncated"] = True
            if key in groups:
                groups[key]["count"] += 1
            elif len(groups) < SCHEMA_GROUP_LIMIT:
                group = {"count": 1, "structure": structure, "first_sample": sample}
                groups[key] = group
                missing_guid["groups"].append(group)
            else:
                missing_guid["overflow_events"] += 1
                missing_guid["truncated"] = True
            continue
        unwrapped = raw_guid[1:-1] if raw_guid.startswith("{") and raw_guid.endswith("}") else raw_guid
        if re.fullmatch(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", unwrapped):
            histogram = schema["provider_guid_counts"]
            key = unwrapped.lower()
            if key not in histogram and len(histogram) >= EVENT_LIMIT:
                raise ProbeError("provider_guid_limit")
            histogram[key] = histogram.get(key, 0) + 1
        else:
            schema["malformed_provider_guid_events"] += 1
        execution = system.find(f"{NS}Execution")
        timestamp = system.find(f"{NS}TimeCreated")
        guid = raw_guid.strip("{}").lower()
        if guid not in {BI_PROVIDER, PROCESS_PROVIDER, IMAGE_PROVIDER}:
            continue
        counts = schema["known_provider_counts"]
        counts[guid] = counts.get(guid, 0) + 1
        header_pid = _integer(execution.get("ProcessID")) if execution is not None else None
        payload = event.findall(f"{NS}EventData/{NS}Data")
        if guid == BI_PROVIDER:
            selected = header_pid == child_pid
        else:
            selected = any(item.get("Name") == "ProcessId" and _integer(item.text) == child_pid for item in payload)
        if not selected:
            continue
        schema["target_pid_events"] += 1
        fields = {}
        for item in payload:
            name = item.get("Name")
            if name in fields:
                raise ProbeError("duplicate_xml_field")
            fields[name] = item.text or ""
            if name in {"ProcessId", "ParentId", "FileName", "ImageBase", "Tool", "InvocationId", "Name", "Value", "UniqueProcessKey"}:
                shapes = schema["target_field_shapes"]
                shape = shapes.setdefault(name, {"xml_value_type": "text", "occurrences": 0})
                shape["occurrences"] += 1
        system_time = timestamp.get("SystemTime") if timestamp is not None else None
        raw_time = timestamp.get("RawTime") if timestamp is not None else None
        yield {
            "provider": guid, "header_pid": header_pid,
            "timestamp": system_time if system_time and re.fullmatch(r"[0-9:.TZ+\-]{1,64}", system_time) else None,
            "raw_time": raw_time if raw_time and re.fullmatch(r"[0-9]{1,64}", raw_time) else None,
            "time_created_shape": _time_created_shape(timestamp),
            "opcode": _integer(system.findtext(f"{NS}Opcode")),
            "fields": fields,
        }


def _same_path(left: str, right: str) -> bool:
    return ntpath.normcase(ntpath.normpath(left)) == ntpath.normcase(ntpath.normpath(right))


def _fixture_unquoted_path_comparison(private: Path, expected: str, commands: list) -> dict:
    tokens = (f'/Fo"{private / "probe.obj"}"', f'"{private / "probe.cpp"}"')
    result = {"candidate_available": False, "segment_sha256_matches": [], "matches_in_xml_order": False}
    if expected.count('"') != 4 or any(expected.count(token) != 1 for token in tokens):
        return result
    # Only the four quotes around our two known fixture paths; no general
    # quote/whitespace normalization and no publication of observed text.
    candidate = expected.replace(tokens[0], f'/Fo{private / "probe.obj"}').replace(tokens[1], str(private / "probe.cpp"))
    segments = [candidate[index:index + 1000] for index in range(0, len(candidate), 1000)]
    matches = [
        index < len(segments) and entry["value_sha256"] == hashlib.sha256(segments[index].encode("utf-8")).hexdigest()
        for index, (entry, _) in enumerate(commands)
    ]
    result.update(candidate_available=True, candidate_length=len(candidate),
                  expected_segment_count=len(segments), observed_segment_count=len(commands),
                  segment_sha256_matches=matches,
                  matches_in_xml_order=bool(commands) and len(commands) == len(segments) and all(matches))
    return result


def _decode(private: Path, compiler: Path, child_pid: int, expected_command: str) -> dict:
    result = {"cl_properties": [], "process_events": [], "backend_events": [],
              "raw_time_semantics": {"unit": "unknown", "clock": "unknown"}}
    schema = {"raw": {}, "relogged": {}}
    commands = []
    for event in _events(private / "relogged.xml", child_pid, schema["relogged"]):
        fields = event["fields"]
        if event["provider"] != BI_PROVIDER or event["header_pid"] != child_pid or fields.get("Tool") != "CL":
            continue
        name = fields.get("Name")
        if name not in {"ToolPath", "WorkingDirectory", "CommandLine"}:
            continue
        value = fields.get("Value", "")
        invocation = fields.get("InvocationId")
        # Observed tracerpt padding is accepted only at the InvocationId boundary.
        trimmed_invocation = invocation.strip() if invocation is not None else None
        entry = {
            "name": name, "observed_header_pid": event["header_pid"],
            "invocation_id": _integer(trimmed_invocation),
            "invocation_id_trimmed": invocation != trimmed_invocation,
            "invocation_id_shape": _value_shape(invocation),
            "time_created_shape": event["time_created_shape"],
            "timestamp_as_rendered": event["timestamp"], "length": len(value),
            "raw_time_as_rendered": event["raw_time"],
            "value_sha256": hashlib.sha256(value.encode("utf-8")).hexdigest(),
        }
        if name in {"ToolPath", "WorkingDirectory"}:
            expected = str(compiler if name == "ToolPath" else private)
            entry["matches_probe_input"] = _same_path(value, expected)
            if entry["matches_probe_input"]:
                entry["value"] = expected
        else:
            commands.append((entry, value))
            entry["fixture_macro_indices"] = [
                int(index) for index in re.findall(r"ISSUE139_FRAGMENT_([0-9]{2})=", value)
                if int(index) < 24
            ]
        result["cl_properties"].append(entry)
        if len(result["cl_properties"]) > EVENT_LIMIT:
            raise ProbeError("selected_event_limit")

    # Publish command values only if the whole observed sequence is exactly the
    # public synthetic fixture (optionally prefixed by the known compiler path).
    # This checks privacy, not ETW segment ordering or event-loss guarantees.
    def compact(value):
        return "".join(value.split()).casefold()

    combined = "".join(value for _, value in commands)
    allowed = {compact(expected_command), compact(f'"{compiler}" {expected_command}')}
    if commands and compact(combined) in allowed:
        for entry, value in commands:
            entry["value"] = value
    result["command_matches_fixture_in_xml_order"] = bool(commands) and compact(combined) in allowed
    result["fixture_unquoted_path_comparison"] = _fixture_unquoted_path_comparison(private, expected_command, commands)

    for event in _events(private / "raw.xml", child_pid, schema["raw"]):
        fields = event["fields"]
        payload_pid = _integer(fields.get("ProcessId"))
        if payload_pid != child_pid:
            continue
        entry = {"observed_payload_pid": payload_pid, "opcode": event["opcode"],
                 "timestamp_as_rendered": event["timestamp"]}
        if event["provider"] == PROCESS_PROVIDER:
            entry["parent_pid"] = _integer(fields.get("ParentId"))
            result["process_events"].append(entry)
        elif event["provider"] == IMAGE_PROVIDER:
            filename = fields.get("FileName", "")
            leaf = ntpath.basename(filename).lower()
            if leaf not in {"c1xx.dll", "c2.dll"}:
                continue
            entry["name"] = leaf
            entry["path_length"] = len(filename)
            entry["path_sha256"] = hashlib.sha256(filename.encode("utf-8")).hexdigest()
            entry["path_status"] = "unresolved_or_outside_tool_directory"
            expected = compiler.parent / leaf
            if _same_path(filename, str(expected)):
                entry["matched_probe_path"] = str(expected)
                entry["disk_file_after_probe"] = _fingerprint(expected)
                entry["path_status"] = "observed_dos_path"
            result["backend_events"].append(entry)
        if len(result["process_events"]) + len(result["backend_events"]) > EVENT_LIMIT:
            raise ProbeError("selected_event_limit")
    result["schema_observation"] = schema
    return result


def _error(error: Exception) -> dict:
    result = {"type": type(error).__name__, "code": str(error) if isinstance(error, ProbeError) else "operation_failed"}
    if isinstance(error, ProbeError) and str(error) == "private_file_limit" and error.private_file_check is not None:
        result["private_file_check"] = error.private_file_check
    return result


def _write_report(temp: Path, report: dict) -> None:
    output = temp / "LoLReplayTool-binary-cache/w/b/evidence/cl-decode-probe.json"
    _no_redirect(output)
    content = (json.dumps(report, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    if len(content) > JSON_LIMIT:
        raise ProbeError("public_json_limit")
    output.parent.mkdir(parents=True, exist_ok=True)
    _no_redirect(output)
    with output.open("xb") as stream:
        stream.write(content)


def _capture(private: Path, tools: dict, rsp: Path, report: dict, *, cpu_sampling: bool) -> Path:
    prefix = "sampling_" if cpu_sampling else ""
    session = "issue139-cl-probe-" + uuid.uuid4().hex
    report["session"] = session
    raw = private / f"{prefix}raw.etl"
    start = [str(tools["vcperf.exe"]), "/start"]
    if not cpu_sampling:
        start.append("/nocpusampling")
    primary = None
    try:
        _command([*start, "/level1", session], private, f"{prefix}start", 15, report)
        child = _command([str(tools["cl.exe"]), "@" + str(rsp)], private, f"{prefix}compile", 30, report)
        report["independent_child_pid"] = child["pid"]
    except Exception as error:
        primary = error
    finally:
        # Also stop our unique session after an ambiguous start failure.
        try:
            _command([str(tools["vcperf.exe"]), "/stopnoanalyze", session, str(raw)], private, f"{prefix}stop", 30, report)
        except Exception as error:
            report["cleanup_error"] = _error(error)
            if primary is None:
                primary = error
    if primary is not None:
        raise primary
    report["stop_statistics"] = _stop_statistics(private / f"{prefix}stop.log")
    return raw


def _sampling_summary(path: Path, child_pid: int, compiler: Path) -> dict:
    schema = {}
    trace_identity = {name: {"events": 0} for name in ("missing_provider_guid", "system_trace")}
    matched = {guid: 0 for guid in (BI_PROVIDER, PROCESS_PROVIDER, IMAGE_PROVIDER)}
    for event in _events(path, child_pid, schema, trace_identity=trace_identity, compiler=compiler):
        matched[event["provider"]] += 1
    missing_guid = schema["missing_guid_schema"]
    return {
        "scope": "strict_decoded_xml_only_not_provider_absence_or_trace_completeness",
        **{key: schema[key] for key in (
            "event_count", "missing_system_events", "missing_provider_events",
            "missing_provider_guid_events", "malformed_provider_guid_events",
            "unsupported_namespace_events", "target_pid_events",
        )},
        "provider_guid_counts": schema["provider_guid_counts"],
        "trace_identity_observation": trace_identity,
        "missing_guid_schema": {
            **{key: missing_guid[key] for key in (
                "overflow_events", "truncated", "header_child_matches", "payload_child_matches",
            )},
            "groups": [{key: group[key] for key in ("count", "structure")} for group in missing_guid["groups"]],
        },
        "known_provider_counts": {guid: schema["known_provider_counts"].get(guid, 0) for guid in matched},
        "child_matches_by_provider": matched,
    }


def run_probe(temp: Path, tool_dir: Path, tracerpt: Path) -> dict:
    report = {
        "schema_version": 1, "purpose": "synthetic_one_tu_decoder_probe_only",
        "product_build_evidence": False, "status": "incomplete", "commands": [],
        "github_sha": os.environ.get("GITHUB_SHA") if re.fullmatch(r"[0-9a-f]{40}", os.environ.get("GITHUB_SHA", "")) else None,
        "checkout_sha": None,
        "input_origin": "explicit_probe_paths_not_observed_image_loads",
        "inputs": [], "cleanup_error": None,
        "unverified": ["decoder_schema", "event_loss", "timebase", "segment_order", "process_lifetime_join"],
    }
    inputs = []
    try:
        _no_redirect(temp)
        private = Path(tempfile.mkdtemp(prefix="issue139-cl-probe-", dir=temp))
        if any(os.environ.get(name) for name in ("CL", "_CL_")):
            raise ProbeError("inherited_compiler_flags")
        tools = {name: tool_dir / name for name in (
            "cl.exe", "c1xx.dll", "c2.dll", "vcperf.exe", "CppBuildInsights.dll", "KernelTraceControl.dll", "CppBuildInsightsEtw.xml",
        )}
        source, rsp = private / "probe.cpp", private / "probe.rsp"
        source.write_text(SOURCE, encoding="ascii", newline="\n")
        flags = ["/nologo", "/c", "/O2", "/Z7", "/TP", f'/Fo"{private / "probe.obj"}"']
        flags.extend(f'/DISSUE139_FRAGMENT_{index:02d}={"7" * 40}' for index in range(24))
        flags.append(f'"{source}"')
        expected_command = " ".join(flags)
        rsp.write_text("\n".join(flags) + "\n", encoding="ascii", newline="\n")
        inputs = [*tools.values(), tracerpt, Path(__file__), source, rsp]
        report["inputs"] = [_fingerprint(path) for path in inputs]
        _command(["git", "-C", str(Path(__file__).resolve().parents[1]), "rev-parse", "--verify", "HEAD"], private, "checkout", 10, report)
        checkout_sha = (private / "checkout.log").read_text(encoding="ascii").strip()
        if not re.fullmatch(r"[0-9a-f]{40}", checkout_sha):
            raise ProbeError("checkout_sha_unavailable")
        report["checkout_sha"] = checkout_sha
        report["build_cwd"] = str(private)
        _capture(private, tools, rsp, report, cpu_sampling=False)
        _command([str(tools["vcperf.exe"]), "/analyze", str(private / "raw.etl"), str(private / "relogged.etl")], private, "relog", 60, report)
        _command([str(tracerpt), str(private / "raw.etl"), "-of", "XML", "-rts", "-o", str(private / "raw.xml")], private, "decode_raw", 30, report)
        _command([str(tracerpt), str(private / "relogged.etl"), "-import", str(tools["CppBuildInsightsEtw.xml"]), "-of", "XML", "-rts", "-o", str(private / "relogged.xml")], private, "decode_relogged", 30, report)
        report["observations"] = _decode(private, tools["cl.exe"], report["independent_child_pid"], expected_command)
        # Reuse the stopped raw trace. -rts is incompatible with -summary; its
        # separate output is private and is never joined to existing RawTime.
        _command([str(tracerpt), str(private / "raw.etl"), "-of", "XML",
                  "-o", str(private / "inspection.xml"), "-summary", str(private / "summary.txt"),
                  "-int", str(private / "interpreted.xml")], private, "inspect_raw", 30, report)
        report["inspection_schema_observation"] = {}
        for _ in _events(private / "inspection.xml", report["independent_child_pid"], report["inspection_schema_observation"]):
            pass  # Schema counts only; do not mix its time representation into the existing observations.
        report["decoder_documents"] = {
            "summary": _decoder_document(private / "summary.txt", xml=False),
            "interpreted": _decoder_document(private / "interpreted.xml", xml=True),
        }
        report["inputs_unchanged_before_cpu_sampling"] = False
        report["inputs_unchanged_before_cpu_sampling"] = report["inputs"] == [_fingerprint(path) for path in inputs]
        if not report["inputs_unchanged_before_cpu_sampling"]:
            raise ProbeError("input_changed_before_cpu_sampling")
        _check_private_size(private)
        comparison = {"condition": "cpu_sampling_enabled", "status": "incomplete", "commands": [], "cleanup_error": None}
        report["cpu_sampling_comparison"] = comparison
        try:
            raw = _capture(private, tools, rsp, comparison, cpu_sampling=True)
            xml = private / "sampling_raw.xml"
            _command([str(tracerpt), str(raw), "-of", "XML", "-rts", "-o", str(xml)], private, "sampling_decode_raw", 30, comparison)
            comparison["raw_summary"] = _sampling_summary(xml, comparison["independent_child_pid"], tools["cl.exe"])
        except Exception as error:
            comparison["status"] = "failed"
            comparison["error"] = _error(error)
            raise
    except Exception as error:
        report["status"] = "failed"
        report["error"] = _error(error)
    finally:
        if report["inputs"]:
            try:
                report["inputs_unchanged"] = report["inputs"] == [_fingerprint(path) for path in inputs]
            except Exception:
                report["inputs_unchanged"] = False
            if not report["inputs_unchanged"]:
                report["status"] = "failed"
                if "cpu_sampling_comparison" in report:
                    comparison["status"] = "failed"
                    comparison.setdefault("error", _error(ProbeError("input_changed_during_comparison")))
        _write_report(temp, report)
    return report


def main() -> int:
    if os.name != "nt" or os.environ.get("GITHUB_ACTIONS") != "true" or os.environ.get("GITHUB_EVENT_NAME") != "pull_request":
        print("CL decode probe requires a Windows pull-request CI runner.")
        return 1
    temp = Path(os.environ["RUNNER_TEMP"])
    tool_dir = Path(os.environ["ProgramFiles"]) / "Microsoft Visual Studio/2022/Enterprise/VC/Tools/MSVC" / REQUIRED_TOOLSET_VERSION / "bin/Hostx64/x64"
    tracerpt = Path(os.environ["SystemRoot"]) / "System32/tracerpt.exe"
    try:
        report = run_probe(temp, tool_dir, tracerpt)
    except Exception:
        print("CL decode probe could not preserve its bounded status report.")
        return 1
    print(f'CL decode probe: {report["status"]}; synthetic TU only; raw evidence stays private.')
    return 1 if report["status"] == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
