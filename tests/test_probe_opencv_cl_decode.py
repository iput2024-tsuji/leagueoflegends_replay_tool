import hashlib
import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from scripts import probe_opencv_cl_decode as target


def _xml(events):
    root = ET.Element(f"{target.NS}Events")
    for provider, pid, fields in events:
        event = ET.SubElement(root, f"{target.NS}Event")
        system = ET.SubElement(event, f"{target.NS}System")
        ET.SubElement(system, f"{target.NS}Provider", Guid="{" + provider + "}")
        ET.SubElement(system, f"{target.NS}Execution", ProcessID=str(pid))
        ET.SubElement(system, f"{target.NS}TimeCreated", SystemTime="123456")
        ET.SubElement(system, f"{target.NS}Opcode").text = "1"
        payload = ET.SubElement(event, f"{target.NS}EventData")
        for name, value in fields.items():
            ET.SubElement(payload, f"{target.NS}Data", Name=name).text = str(value)
    return ET.tostring(root)


@pytest.fixture
def probe(tmp_path, monkeypatch):
    tool_dir = tmp_path / "tools"
    tool_dir.mkdir()
    for name in ("cl.exe", "c1xx.dll", "c2.dll", "vcperf.exe", "CppBuildInsights.dll", "KernelTraceControl.dll", "CppBuildInsightsEtw.xml"):
        (tool_dir / name).write_bytes(b"fixture input")
    tracerpt = tmp_path / "tracerpt.exe"
    tracerpt.write_bytes(b"fixture decoder")
    monkeypatch.delenv("CL", raising=False)
    monkeypatch.delenv("_CL_", raising=False)
    monkeypatch.setenv("GITHUB_SHA", "a" * 40)
    calls = []

    def command(args, private, stage, timeout, report):
        calls.append(stage)
        entry = {"stage": stage, "status": "success", "pid": 42, "returncode": 0}
        report["commands"].append(entry)
        if stage == "checkout":
            (private / "checkout.log").write_text("b" * 40 + "\n", encoding="ascii")
        if stage == "stop":
            (private / "raw.etl").write_bytes(b"private trace")
        if stage == "relog":
            (private / "relogged.etl").write_bytes(b"private processed trace")
        if stage == "decode_raw":
            (private / "raw.xml").write_bytes(_xml([
                (target.PROCESS_PROVIDER, 999, {"ProcessId": 42, "ParentId": 10}),
            ]))
        if stage == "decode_relogged":
            expected = " ".join((private / "probe.rsp").read_text().splitlines())
            rows = [(target.BI_PROVIDER, 42, {"Tool": "CL", "InvocationId": 7, "Name": name, "Value": value})
                    for name, value in (("ToolPath", str(tool_dir / "cl.exe")),
                                        ("WorkingDirectory", str(private)),
                                        ("CommandLine", expected[:1000]), ("CommandLine", expected[1000:]))]
            rows.append((target.BI_PROVIDER, 666, {"Tool": "CL", "Name": "CommandLine", "Value": "other-process-secret"}))
            (private / "relogged.xml").write_bytes(_xml(rows))
        return entry

    monkeypatch.setattr(target, "_command", command)
    return tmp_path, tool_dir, tracerpt, calls, command


def test_probe_collects_fixture_fields_without_claiming_verified_trace(probe):
    temp, tools, tracerpt, calls, _ = probe
    report = target.run_probe(temp, tools, tracerpt)
    assert calls == ["checkout", "start", "compile", "stop", "relog", "decode_raw", "decode_relogged"]
    assert report["status"] == "incomplete"
    assert report["product_build_evidence"] is False
    assert report["inputs_unchanged"] is True
    assert report["github_sha"] == "a" * 40 and report["checkout_sha"] == "b" * 40
    assert {Path(item["path"]).name for item in report["inputs"]} >= {"cl.exe", "c1xx.dll", "c2.dll"}
    assert report["observations"]["command_matches_fixture_in_xml_order"] is True
    assert report["observations"]["process_events"][0]["observed_payload_pid"] == 42
    assert "event_loss" in report["unverified"]
    public = temp / "LoLReplayTool-binary-cache/w/b/evidence"
    assert [path.name for path in public.iterdir()] == ["cl-decode-probe.json"]
    text = (public / "cl-decode-probe.json").read_text()
    assert "other-process-secret" not in text
    assert "raw.etl" not in text and "relogged.xml" not in text


@pytest.mark.parametrize("failure", ["checkout", "start", "compile", "stop", "relog", "decode_raw", "decode_relogged"])
def test_stage_failure_stops_only_own_session_and_records_failure(probe, monkeypatch, failure):
    temp, tools, tracerpt, calls, original = probe

    def fail(args, private, stage, timeout, report):
        if stage == failure:
            calls.append(stage)
            raise target.ProbeError("injected_stage_failure")
        return original(args, private, stage, timeout, report)

    monkeypatch.setattr(target, "_command", fail)
    report = target.run_probe(temp, tools, tracerpt)
    assert report["status"] == "failed"
    assert report["error"]["code"] == "injected_stage_failure"
    assert calls.count("start") == calls.count("stop") == (0 if failure == "checkout" else 1)
    if failure in {"checkout", "start"}:
        assert "compile" not in calls
    if failure in {"checkout", "start", "compile", "stop"}:
        assert "relog" not in calls


def test_stop_failure_preserves_primary_and_does_not_publish_exception_text(probe, monkeypatch):
    temp, tools, tracerpt, _, original = probe

    def fail(args, private, stage, timeout, report):
        if stage == "compile":
            raise target.ProbeError("primary_compile_failure")
        if stage == "stop":
            raise RuntimeError("credential-in-raw-error")
        return original(args, private, stage, timeout, report)

    monkeypatch.setattr(target, "_command", fail)
    report = target.run_probe(temp, tools, tracerpt)
    assert report["error"]["code"] == "primary_compile_failure"
    assert report["cleanup_error"] == {"type": "RuntimeError", "code": "operation_failed"}
    assert "credential-in-raw-error" not in json.dumps(report)


def test_inherited_flags_refuse_before_start(probe, monkeypatch):
    temp, tools, tracerpt, calls, _ = probe
    monkeypatch.setenv("_CL_", "private-secret")
    report = target.run_probe(temp, tools, tracerpt)
    assert report["status"] == "failed" and not calls
    assert "private-secret" not in json.dumps(report)


def test_missing_actual_checkout_sha_is_not_replaced_with_context_sha(probe, monkeypatch):
    temp, tools, tracerpt, calls, original = probe

    def invalid_checkout(args, private, stage, timeout, report):
        entry = original(args, private, stage, timeout, report)
        if stage == "checkout":
            (private / "checkout.log").write_text("not-a-commit\n")
        return entry

    monkeypatch.setattr(target, "_command", invalid_checkout)
    report = target.run_probe(temp, tools, tracerpt)
    assert report["checkout_sha"] is None
    assert report["github_sha"] == "a" * 40
    assert report["status"] == "failed" and calls == ["checkout"]


def test_changed_input_invalidates_probe(probe, monkeypatch):
    temp, tools, tracerpt, _, original = probe

    def change(args, private, stage, timeout, report):
        result = original(args, private, stage, timeout, report)
        if stage == "decode_relogged":
            (tools / "cl.exe").write_bytes(b"changed input")
        return result

    monkeypatch.setattr(target, "_command", change)
    report = target.run_probe(temp, tools, tracerpt)
    assert report["status"] == "failed" and report["inputs_unchanged"] is False


def test_unknown_schema_and_unexpected_command_are_not_invented_or_exposed(tmp_path):
    (tmp_path / "raw.xml").write_bytes(b'<Events><Data Name="ProcessId">42</Data></Events>')
    (tmp_path / "relogged.xml").write_bytes(_xml([
        (target.BI_PROVIDER, 42, {"Tool": "CL", "InvocationId": 1, "Name": "CommandLine", "Value": "private-secret"}),
        (target.BI_PROVIDER, 42, {"Tool": "CL", "InvocationId": 1, "Name": "WorkingDirectory", "Value": "private-location"}),
    ]))
    result = target._decode(tmp_path, tmp_path / "cl.exe", 42, "public fixture")
    assert result["process_events"] == []
    assert result["schema_observation"]["raw"]["target_field_shapes"] == {}
    assert result["schema_observation"]["relogged"]["target_pid_events"] == 2
    assert result["schema_observation"]["relogged"]["target_field_shapes"]["Value"]["xml_value_type"] == "text"
    assert result["command_matches_fixture_in_xml_order"] is False
    assert all("value" not in row for row in result["cl_properties"])
    assert "private-secret" not in json.dumps(result)
    assert "private-location" not in json.dumps(result)


def test_provider_histogram_distinguishes_unknown_guids_and_missing_schema(tmp_path):
    unknown = "11111111-aaaa-bbbb-cccc-222222222222"
    root = ET.fromstring(_xml([(unknown, 42, {}), (unknown.upper(), 42, {})]))
    ET.SubElement(root, f"{target.NS}Event")  # No System.
    event = ET.SubElement(root, f"{target.NS}Event")
    ET.SubElement(event, f"{target.NS}System")  # No Provider.
    for attributes in ({}, {"Guid": ""}, {"Guid": "private-malformed-guid"}):
        event = ET.SubElement(root, f"{target.NS}Event")
        system = ET.SubElement(event, f"{target.NS}System")
        ET.SubElement(system, f"{target.NS}Provider", attributes)
    ET.SubElement(root, "Event")  # Unsupported namespace remains distinct.
    path = tmp_path / "raw.xml"
    path.write_bytes(ET.tostring(root))
    schema = {}
    assert list(target._events(path, 42, schema)) == []
    assert schema["event_count"] == 8
    assert schema["provider_guid_counts"] == {unknown: 2}
    assert schema["known_provider_counts"] == {}
    assert schema["missing_system_events"] == 1
    assert schema["missing_provider_events"] == 1
    assert schema["missing_provider_guid_events"] == 1
    assert schema["malformed_provider_guid_events"] == 2
    assert schema["unsupported_namespace_events"] == 1
    assert "private-malformed-guid" not in json.dumps(schema)


def test_provider_histogram_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(target, "EVENT_LIMIT", 1)
    path = tmp_path / "raw.xml"
    path.write_bytes(_xml([(target.BI_PROVIDER, 666, {}), (target.PROCESS_PROVIDER, 666, {})]))
    with pytest.raises(target.ProbeError, match="provider_guid_limit"):
        list(target._events(path, 42, {}))


def test_missing_guid_schema_groups_structure_without_exposing_dynamic_values(tmp_path):
    secrets = ("private-provider", "private-source", "private-attribute", "private-field",
               "private-path", "private-command", "private-sid", "private-element")
    root = ET.fromstring(_xml([
        (target.BI_PROVIDER, 999, {"ProcessId": 42, "FileName": "private-path" + suffix,
                                  "CommandLine": "private-command" + suffix, "UserSID": "private-sid",
                                  "private-field": "private-value"})
        for suffix in ("-one", "-different-two")
    ]))
    for index, event in enumerate(root):
        system = event.find(f"{target.NS}System")
        provider = system.find(f"{target.NS}Provider")
        provider.attrib.clear()
        provider.attrib.update(Name="private-provider" + str(index), EventSourceName="private-source",
                               **{"private-attribute": "private-value"})
        ET.SubElement(system, f"{target.NS}EventID").text = "1"
        ET.SubElement(system, f"{target.NS}private-element").text = "private-value"
        user_data = ET.SubElement(event, f"{target.NS}UserData")
        ET.SubElement(user_data, "private-element").text = "private-sid"
    (tmp_path / "raw.xml").write_bytes(ET.tostring(root))
    (tmp_path / "relogged.xml").write_bytes(_xml([]))
    result = target._decode(tmp_path, tmp_path / "cl.exe", 42, "fixture")
    assert result["process_events"] == result["backend_events"] == []
    schema = result["schema_observation"]["raw"]["missing_guid_schema"]
    assert len(schema["groups"]) == 1 and schema["groups"][0]["count"] == 2
    assert schema["header_child_matches"] == 0 and schema["payload_child_matches"] == 2
    structure = schema["groups"][0]["structure"]
    sample = schema["groups"][0]["first_sample"]
    assert structure["system_fields"]["EventID"]["uint32"] == 1
    assert structure["other_provider_attribute_count"] == structure["other_system_element_count"] == 1
    assert structure["other_payload_field_count"] == 2
    assert structure["user_data_count"] == structure["user_data_child_count"] == 1
    assert sample["payload_fields"]["FileName"]["sha256"] == hashlib.sha256(b"private-path-one").hexdigest()
    public = json.dumps(result)
    assert not any(secret in public for secret in secrets)
    assert hashlib.sha256(b"private-sid").hexdigest() not in public


def test_missing_guid_child_matches_require_unique_leaf_fields_and_strict_numbers(tmp_path):
    pids = [(" 42 ", "0x2a"), ("0x2a", " 42 "), (43, 43), (42, 42),
            (42, 42), (42, 42), ("9" * 5000, "9" * 5000), (42, 42)]
    root = ET.fromstring(_xml([(target.BI_PROVIDER, header, {"ProcessId": payload}) for header, payload in pids]))
    for event in root:
        event.find(f"{target.NS}System/{target.NS}Provider").attrib.clear()
        event.find(f"{target.NS}System/{target.NS}Opcode").text = " 1 "
    ET.SubElement(root[3].find(f"{target.NS}System"), f"{target.NS}Execution", ProcessID="42")
    ET.SubElement(root[4].find(f"{target.NS}EventData"), f"{target.NS}Data", Name="ProcessId").text = "42"
    ET.SubElement(root[5].find(f"{target.NS}EventData/{target.NS}Data"), "private-nested").text = "private-value"
    ET.SubElement(root[6].find(f"{target.NS}System"), f"{target.NS}EventID").text = "9" * 5000
    root[7].append(ET.fromstring(ET.tostring(root[7].find(f"{target.NS}System"))))
    path = tmp_path / "raw.xml"
    path.write_bytes(ET.tostring(root))
    schema = {}
    assert list(target._events(path, 42, schema)) == []
    groups = schema["missing_guid_schema"]["groups"]
    assert [(group["structure"]["header_matches_child"], group["structure"]["payload_matches_child"])
            for group in groups] == [(None, True), (True, None), (False, False), (None, True),
                                     (True, None), (True, None), (None, None), (None, True)]
    assert all(group["structure"]["system_fields"]["Opcode"]["uint32"] is None for group in groups)
    assert groups[6]["structure"]["system_fields"]["EventID"]["uint32"] is None
    assert groups[6]["first_sample"]["payload_fields"]["ProcessId"]["length"] == 5000
    assert "private-nested" not in json.dumps(schema) and "9" * 65 not in json.dumps(schema)


def test_missing_guid_schema_caps_groups_but_counts_overflow_and_keeps_normal_decode(tmp_path):
    root = ET.fromstring(_xml([
        (target.BI_PROVIDER, 42, {"ProcessId": 42, "FileName": "private-path"}) for _ in range(11)
    ]))
    for event, event_id in zip(root, [*range(9), 0, 8], strict=True):
        system = event.find(f"{target.NS}System")
        system.find(f"{target.NS}Provider").attrib.clear()
        ET.SubElement(system, f"{target.NS}EventID").text = str(event_id)
    root.append(ET.fromstring(_xml([
        (target.BI_PROVIDER, 42, {"Tool": "CL", "InvocationId": " 7 ", "Name": "CommandLine", "Value": "fixture"}),
    ]))[0])
    (tmp_path / "relogged.xml").write_bytes(ET.tostring(root))
    (tmp_path / "raw.xml").write_bytes(_xml([]))
    result = target._decode(tmp_path, tmp_path / "cl.exe", 42, "fixture")
    observed = result["schema_observation"]["relogged"]
    schema = observed["missing_guid_schema"]
    assert observed["missing_provider_guid_events"] == 11 and observed["target_pid_events"] == 1
    assert len(schema["groups"]) == 8 and schema["groups"][0]["count"] == 2
    assert schema["overflow_events"] == 2 and schema["truncated"] is True
    assert schema["header_child_matches"] == schema["payload_child_matches"] == 11
    assert result["command_matches_fixture_in_xml_order"] is True
    assert result["cl_properties"][0]["invocation_id"] == 7
    target._write_report(tmp_path, result)
    public = tmp_path / "LoLReplayTool-binary-cache/w/b/evidence/cl-decode-probe.json"
    assert public.stat().st_size <= target.JSON_LIMIT
    assert "private-path" not in public.read_text()


def test_target_value_shapes_preserve_raw_metadata_after_invocation_trim(tmp_path):
    invocations = [None, "", " 17 ", "0x00000007", "invocation-secret", " \t "]
    root = ET.fromstring(_xml([
        (target.BI_PROVIDER, 42, {"Tool": "CL", "Name": "ToolPath", "Value": "private-tool",
                                  **({"InvocationId": value} if value is not None else {})})
        for value in invocations
    ]))
    time_attributes = [None, {}, {"SystemTime": " 123 "}, {"RawTime": "0x123"},
                       {"unknown-attribute-name": "unknown-attribute-secret"}, {"SystemTime": ""}]
    for event, attributes in zip(root, time_attributes, strict=True):
        system = event.find(f"{target.NS}System")
        timestamp = system.find(f"{target.NS}TimeCreated")
        if attributes is None:
            system.remove(timestamp)
        else:
            timestamp.attrib.clear()
            timestamp.attrib.update(attributes)
    (tmp_path / "relogged.xml").write_bytes(ET.tostring(root))
    (tmp_path / "raw.xml").write_bytes(_xml([]))
    result = target._decode(tmp_path, tmp_path / "cl.exe", 42, "fixture")
    rows = result["cl_properties"]
    assert [row["invocation_id"] for row in rows] == [None, None, 17, 7, None, None]
    assert [row["invocation_id_trimmed"] for row in rows] == [False, False, True, False, False, True]
    assert all(row["timestamp_as_rendered"] is None for row in rows)
    assert all(row["raw_time_as_rendered"] is None for row in rows)
    assert [row["invocation_id_shape"]["form"] for row in rows] == [
        "missing", "empty", "decimal", "hex", "other", "whitespace",
    ]
    for row, value in zip(rows, invocations, strict=True):
        shape = row["invocation_id_shape"]
        assert shape["present"] is (value is not None)
        assert shape["length"] == (len(value) if value is not None else None)
        assert shape["sha256"] == (hashlib.sha256(value.encode()).hexdigest() if value is not None else None)
    assert rows[2]["invocation_id_shape"]["surrounding_whitespace"] is True
    assert rows[0]["time_created_shape"]["present"] is False
    assert rows[1]["time_created_shape"]["present"] is True
    assert rows[1]["time_created_shape"]["attributes"]["SystemTime"]["present"] is False
    assert rows[2]["time_created_shape"]["attributes"]["SystemTime"]["form"] == "decimal"
    assert rows[2]["time_created_shape"]["attributes"]["SystemTime"]["surrounding_whitespace"] is True
    assert rows[3]["time_created_shape"]["attributes"]["RawTime"]["form"] == "hex"
    assert rows[4]["time_created_shape"]["unknown_attribute_count"] == 1
    assert rows[5]["time_created_shape"]["attributes"]["SystemTime"]["form"] == "empty"
    assert not any(secret in json.dumps(result) for secret in (
        "invocation-secret", "unknown-attribute-name", "unknown-attribute-secret", "private-tool",
    ))


def test_invocation_trim_keeps_uint32_limits_and_does_not_relax_other_fields(tmp_path):
    values = ["\t4294967295\n", " 4294967296 ", " -1 ", " +7 ", " 1 7 ", " １２ ", "\u200317\u2003"]
    root = ET.fromstring(_xml([
        (target.BI_PROVIDER, 42, {"Tool": "CL", "InvocationId": value, "Name": "ToolPath", "Value": "private-tool"})
        for value in values
    ] + [(target.BI_PROVIDER, " 42 ", {"Tool": "CL", "InvocationId": " 7 ", "Name": "ToolPath", "Value": "excluded"})]))
    (tmp_path / "relogged.xml").write_bytes(ET.tostring(root))
    raw = ET.fromstring(_xml([
        (target.PROCESS_PROVIDER, 42, {"ProcessId": " 42 ", "ParentId": 1}),
        (target.PROCESS_PROVIDER, 999, {"ProcessId": 42, "ParentId": " 1 "}),
    ]))
    raw[1].find(f"{target.NS}System/{target.NS}Opcode").text = " 1 "
    (tmp_path / "raw.xml").write_bytes(ET.tostring(raw))
    result = target._decode(tmp_path, tmp_path / "cl.exe", 42, "fixture")
    assert [row["invocation_id"] for row in result["cl_properties"]] == [0xFFFFFFFF, None, None, None, None, None, 17]
    assert all(row["invocation_id_trimmed"] for row in result["cl_properties"])
    assert len(result["process_events"]) == 1
    assert result["process_events"][0]["parent_pid"] is None
    assert result["process_events"][0]["opcode"] is None
    assert "private-tool" not in json.dumps(result) and "excluded" not in json.dumps(result)


def test_raw_time_is_bounded_ascii_text_separate_from_system_time(tmp_path):
    values = ["12345678901", "000123", "9" * 64, "9" * 65, " 123 ", "0x123", "１２３", "raw-time-secret", ""]
    root = ET.fromstring(_xml([
        (target.BI_PROVIDER, 42, {"Tool": "CL", "InvocationId": 7, "Name": "ToolPath", "Value": "private-tool"})
        for _ in values
    ]))
    for index, (event, value) in enumerate(zip(root, values, strict=True)):
        timestamp = event.find(f"{target.NS}System/{target.NS}TimeCreated")
        if index != 1:
            timestamp.attrib.clear()
        timestamp.set("RawTime", value)
    (tmp_path / "relogged.xml").write_bytes(ET.tostring(root))
    (tmp_path / "raw.xml").write_bytes(_xml([]))
    result = target._decode(tmp_path, tmp_path / "cl.exe", 42, "fixture")
    rows = result["cl_properties"]
    assert [row["raw_time_as_rendered"] for row in rows] == values[:3] + [None] * 6
    assert [row["timestamp_as_rendered"] for row in rows] == [None, "123456"] + [None] * 7
    assert result["raw_time_semantics"] == {"unit": "unknown", "clock": "unknown"}
    assert rows[3]["time_created_shape"]["attributes"]["RawTime"]["length"] == 65
    assert rows[7]["time_created_shape"]["attributes"]["RawTime"]["sha256"] == hashlib.sha256(b"raw-time-secret").hexdigest()
    assert "raw-time-secret" not in json.dumps(result) and "private-tool" not in json.dumps(result)


@pytest.mark.parametrize("change", ["none", "value", "order", "omitted"])
def test_known_fixture_path_quote_candidate_keeps_old_comparison_and_status(probe, monkeypatch, change):
    temp, tools, tracerpt, _, original = probe

    def decoded_without_path_quotes(args, private, stage, timeout, report):
        entry = original(args, private, stage, timeout, report)
        if stage == "decode_relogged":
            expected = " ".join((private / "probe.rsp").read_text().splitlines())
            candidate = expected.replace(f'/Fo"{private / "probe.obj"}"', f'/Fo{private / "probe.obj"}')
            candidate = candidate.replace(f'"{private / "probe.cpp"}"', str(private / "probe.cpp"))
            segments = [candidate[index:index + 1000] for index in range(0, len(candidate), 1000)]
            if change == "value":
                segments[-1] += "private-extra-argument"
            elif change == "order":
                segments.reverse()
            elif change == "omitted":
                segments.pop()
            (private / "relogged.xml").write_bytes(_xml([
                (target.BI_PROVIDER, 42, {"Tool": "CL", "InvocationId": 7, "Name": "CommandLine", "Value": value})
                for value in segments
            ]))
        return entry

    monkeypatch.setattr(target, "_command", decoded_without_path_quotes)
    report = target.run_probe(temp, tools, tracerpt)
    assert report["status"] == "incomplete" and report["product_build_evidence"] is False
    observations = report["observations"]
    assert observations["command_matches_fixture_in_xml_order"] is False
    comparison = observations["fixture_unquoted_path_comparison"]
    assert comparison["candidate_available"] is True
    assert comparison["matches_in_xml_order"] is (change == "none")
    if change == "none":
        assert comparison["segment_sha256_matches"] == [True, True]
    assert all("value" not in row for row in observations["cl_properties"])
    assert "private-extra-argument" not in json.dumps(report)


def test_quote_candidate_requires_exactly_the_two_known_fixture_paths(tmp_path):
    expected = f'/Fo"{tmp_path / "probe.obj"}" "{tmp_path / "other.cpp"}"'
    assert target._fixture_unquoted_path_comparison(tmp_path, expected, [({}, "anything")]) == {
        "candidate_available": False, "segment_sha256_matches": [], "matches_in_xml_order": False,
    }


def test_backend_uses_payload_pid_and_hashes_only_observed_matching_path(tmp_path):
    backend = tmp_path / "c1xx.dll"
    backend.write_bytes(b"observed backend, never executed")
    (tmp_path / "relogged.xml").write_bytes(_xml([]))
    (tmp_path / "raw.xml").write_bytes(_xml([
        (target.IMAGE_PROVIDER, 999, {"ProcessId": 42, "FileName": str(backend)}),
        (target.IMAGE_PROVIDER, 42, {"ProcessId": 666, "FileName": str(backend)}),
        (target.IMAGE_PROVIDER, 999, {"ProcessId": 42, "FileName": "C:/private-location/c2.dll"}),
    ]))
    result = target._decode(tmp_path, tmp_path / "cl.exe", 42, "fixture")
    assert len(result["backend_events"]) == 2
    assert result["backend_events"][0]["disk_file_after_probe"] == target._fingerprint(backend)
    assert result["backend_events"][0]["matched_probe_path"] == str(backend)
    assert result["backend_events"][1]["path_status"] == "unresolved_or_outside_tool_directory"
    assert "private-location" not in json.dumps(result)


def test_matching_paths_publish_only_known_inputs_and_never_open_observed_segments(tmp_path):
    tools, private = tmp_path / "tools", tmp_path / "private"
    tools.mkdir()
    private.mkdir()
    compiler, backend = tools / "cl.exe", tools / "c1xx.dll"
    backend.write_bytes(b"known backend, never executed")
    observed_tool = str(tools / "private-tool-name" / ".." / compiler.name)
    observed_cwd = str(private / "private-cwd-name" / "..")
    observed_backend = str(tools / "private-backend-name" / ".." / backend.name)
    (private / "relogged.xml").write_bytes(_xml([
        (target.BI_PROVIDER, 42, {"Tool": "CL", "Name": name, "Value": value})
        for name, value in (("ToolPath", observed_tool), ("WorkingDirectory", observed_cwd))
    ]))
    (private / "raw.xml").write_bytes(_xml([
        (target.IMAGE_PROVIDER, 999, {"ProcessId": 42, "FileName": observed_backend}),
    ]))
    result = target._decode(private, compiler, 42, "fixture")
    for row, observed, expected in zip(
        result["cl_properties"], (observed_tool, observed_cwd), (compiler, private), strict=True,
    ):
        assert row["matches_probe_input"] is True and row["value"] == str(expected)
        assert row["length"] == len(observed)
        assert row["value_sha256"] == hashlib.sha256(observed.encode("utf-8")).hexdigest()
    row = result["backend_events"][0]
    assert row["matched_probe_path"] == str(backend)
    assert row["disk_file_after_probe"] == target._fingerprint(backend)
    assert row["path_length"] == len(observed_backend)
    assert row["path_sha256"] == hashlib.sha256(observed_backend.encode("utf-8")).hexdigest()
    assert not any(secret in json.dumps(result) for secret in (
        "private-tool-name", "private-cwd-name", "private-backend-name",
    ))


def test_selected_events_are_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(target, "EVENT_LIMIT", 1)
    (tmp_path / "raw.xml").write_bytes(_xml([]))
    (tmp_path / "relogged.xml").write_bytes(_xml([
        (target.BI_PROVIDER, 42, {"Tool": "CL", "Name": "CommandLine", "Value": "fixture"}),
    ] * 2))
    with pytest.raises(target.ProbeError, match="selected_event_limit"):
        target._decode(tmp_path, tmp_path / "cl.exe", 42, "fixture")


def test_duplicate_fields_only_reject_a_selected_target_event(tmp_path):
    root = ET.fromstring(_xml([
        (target.BI_PROVIDER, 666, {"Tool": "CL", "Name": "CommandLine", "Value": "unrelated"}),
    ]))
    payload = root.find(f"{target.NS}Event/{target.NS}EventData")
    ET.SubElement(payload, f"{target.NS}Data", Name="Value").text = "unrelated-duplicate"
    path = tmp_path / "events.xml"
    path.write_bytes(ET.tostring(root))
    schema = {}
    assert list(target._events(path, 42, schema)) == []
    assert schema["known_provider_counts"][target.BI_PROVIDER] == 1
    assert schema["target_field_shapes"] == {}
    root.find(f"{target.NS}Event/{target.NS}System/{target.NS}Execution").set("ProcessID", "42")
    path.write_bytes(ET.tostring(root))
    with pytest.raises(target.ProbeError, match="duplicate_xml_field"):
        list(target._events(path, 42, {}))


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16", "utf-32"])
def test_decoder_rejects_entity_declarations(tmp_path, encoding):
    path = tmp_path / "decoder.xml"
    path.write_bytes('<!DOCTYPE test [<!ENTITY x "unexpected">]><test>&x;</test>'.encode(encoding))
    with pytest.raises(target.ProbeError, match="xml_declaration_rejected"):
        list(target._events(path, 42, {}))


def test_public_destination_redirect_is_rejected(tmp_path, monkeypatch):
    original = Path.is_junction
    blocked = tmp_path / "LoLReplayTool-binary-cache"
    monkeypatch.setattr(Path, "is_junction", lambda self: self == blocked or original(self))
    with pytest.raises(target.ProbeError, match="redirected_path"):
        target._write_report(tmp_path, {})
    assert not blocked.exists()


def test_public_output_is_bounded_and_never_overwrites(tmp_path, monkeypatch):
    monkeypatch.setattr(target, "JSON_LIMIT", 32)
    with pytest.raises(target.ProbeError, match="public_json_limit"):
        target._write_report(tmp_path, {"value": "x" * 40})
    target._write_report(tmp_path, {"status": "first"})
    with pytest.raises(FileExistsError):
        target._write_report(tmp_path, {"status": "second"})


def test_command_timeout_kills_owned_handle_without_real_process(tmp_path, monkeypatch):
    class Process:
        pid = 123
        returncode = None
        killed = False

        def poll(self):
            return self.returncode

        def kill(self):
            self.killed = True
            self.returncode = 1

        def wait(self, timeout):
            return self.returncode

    process = Process()
    monkeypatch.setattr(target.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(target.time, "monotonic", iter([0, 1]).__next__)
    report = {"commands": []}
    with pytest.raises(target.ProbeError, match="command_timeout"):
        target._command(["must-not-execute"], tmp_path, "compile", 0.1, report)
    assert process.killed
    assert report["commands"][0]["pid"] == 123


def test_command_size_limit_stops_owned_child_without_masking_cleanup_failure(tmp_path, monkeypatch):
    class Process:
        pid = 123
        returncode = None

        def poll(self):
            return None

        def kill(self):
            raise RuntimeError("private process cleanup failure")

    monkeypatch.setattr(target.subprocess, "Popen", lambda *args, **kwargs: Process())
    monkeypatch.setattr(target, "FILE_LIMIT", 1)
    (tmp_path / "oversized.xml").write_bytes(b"oversize")
    report = {"commands": []}
    with pytest.raises(target.ProbeError, match="private_file_limit"):
        target._command(["must-not-execute"], tmp_path, "relog", 0.1, report)
    assert report["commands"][0]["child_cleanup"] == "failed"


def test_stop_is_attempted_even_if_private_data_is_already_oversized(tmp_path, monkeypatch):
    class Process:
        pid = 123
        returncode = 0
        polls = 0

        def poll(self):
            self.polls += 1
            return None if self.polls == 1 else 0

    process = Process()
    monkeypatch.setattr(target.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(target.time, "sleep", lambda *_: None)
    monkeypatch.setattr(target, "FILE_LIMIT", 1)
    (tmp_path / "oversized.etl").write_bytes(b"oversize")
    report = {"commands": []}
    with pytest.raises(target.ProbeError, match="private_file_limit"):
        target._command(["must-not-execute"], tmp_path, "stop", 5, report)
    assert report["commands"][0]["returncode"] == 0


def test_cli_refuses_local_trace_before_any_command(monkeypatch):
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setattr(target, "run_probe", lambda *args: pytest.fail("must not start local trace"))
    assert target.main() == 1
