import hashlib
import json
import stat
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import probe_opencv_cl_decode as target
from scripts.probe_opencv_cl_decode import _command as run_command


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


def _trace_identity_event(provider, scheme="http", guid=None):
    event = ET.fromstring(_xml([(provider or target.BI_PROVIDER, 42, {"ProcessId": 42})]))[0]
    if provider is None:
        event.find(f"{target.NS}System/{target.NS}Provider").attrib.clear()
    namespace = f"{{{scheme}://schemas.microsoft.com/win/2004/08/events/trace}}"
    extension = ET.SubElement(event, namespace + "ExtendedTracingInfo")
    ET.SubElement(extension, namespace + "EventGuid").text = guid or target.PROCESS_PROVIDER
    return event


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
        entry = {"stage": stage, "status": "success", "pid": 43 if stage.startswith("sampling_") else 42, "returncode": 0}
        report["commands"].append(entry)
        if stage == "checkout":
            (private / "checkout.log").write_text("b" * 40 + "\n", encoding="ascii")
        if stage in {"stop", "sampling_stop"}:
            prefix = "sampling_" if stage == "sampling_stop" else ""
            (private / f"{prefix}raw.etl").write_bytes(b"private trace")
            (private / f"{prefix}stop.log").write_bytes(
                b"Dropped MSVC events: 0\r\nDropped MSVC buffers: 0\r\n"
                b"Dropped system events: 0\r\nDropped system buffers: 0\r\n"
            )
        if stage == "relog":
            (private / "relogged.etl").write_bytes(b"private processed trace")
        if stage == "decode_raw":
            (private / "raw.xml").write_bytes(_xml([
                (target.PROCESS_PROVIDER, 999, {"ProcessId": 42, "ParentId": 10}),
            ]))
        if stage == "sampling_decode_raw":
            (private / "sampling_raw.xml").write_bytes(_xml([
                (target.PROCESS_PROVIDER, 999, {"ProcessId": 43, "ParentId": 10, "CommandLine": "sampling-private-command"}),
                (target.IMAGE_PROVIDER, 43, {"ProcessId": 666, "FileName": "sampling-private-path"}),
            ]))
        if stage == "decode_relogged":
            expected = " ".join((private / "probe.rsp").read_text().splitlines())
            rows = [(target.BI_PROVIDER, 42, {"Tool": "CL", "InvocationId": 7, "Name": name, "Value": value})
                    for name, value in (("ToolPath", str(tool_dir / "cl.exe")),
                                        ("WorkingDirectory", str(private)),
                                        ("CommandLine", expected[:1000]), ("CommandLine", expected[1000:]))]
            rows.append((target.BI_PROVIDER, 666, {"Tool": "CL", "Name": "CommandLine", "Value": "other-process-secret"}))
            (private / "relogged.xml").write_bytes(_xml(rows))
        if stage == "inspect_raw":
            assert args[1] == str(private / "raw.etl") and "-rts" not in args
            assert args[args.index("-o") + 1] == str(private / "inspection.xml")
            assert args[args.index("-summary") + 1] == str(private / "summary.txt")
            assert args[args.index("-int") + 1] == str(private / "interpreted.xml")
            (private / "inspection.xml").write_bytes(_xml([
                (target.PROCESS_PROVIDER, 999, {"ProcessId": 42, "ParentId": 10}),
            ]))
            (private / "summary.txt").write_text("private summary text", encoding="utf-8")
            (private / "interpreted.xml").write_bytes(b'<private-root secret="private-value"/>')
        return entry

    monkeypatch.setattr(target, "_command", command)
    return tmp_path, tool_dir, tracerpt, calls, command


def test_probe_collects_fixture_fields_without_claiming_verified_trace(probe):
    temp, tools, tracerpt, calls, _ = probe
    report = target.run_probe(temp, tools, tracerpt)
    assert calls == ["checkout", "start", "compile", "stop", "relog", "decode_raw", "decode_relogged", "inspect_raw",
                     "sampling_start", "sampling_compile", "sampling_stop", "sampling_decode_raw"]
    assert len(report["commands"]) == 8
    assert report["status"] == "incomplete"
    assert report["product_build_evidence"] is False
    assert report["inputs_unchanged"] is True
    assert report["github_sha"] == "a" * 40 and report["checkout_sha"] == "b" * 40
    assert {Path(item["path"]).name for item in report["inputs"]} >= {"cl.exe", "c1xx.dll", "c2.dll"}
    assert report["observations"]["command_matches_fixture_in_xml_order"] is True
    assert report["observations"]["process_events"][0]["observed_payload_pid"] == 42
    assert "event_loss" in report["unverified"]
    assert all(row["value"] == "0" for row in report["stop_statistics"]["counters"].values())
    assert report["decoder_documents"]["interpreted"]["xml_status"] == "well_formed"
    assert report["inspection_schema_observation"]["known_provider_counts"] == {target.PROCESS_PROVIDER: 1}
    assert report["inspection_schema_observation"]["target_pid_events"] == 1
    comparison = report["cpu_sampling_comparison"]
    assert len(report["inputs"]) == 11 and report["inputs_unchanged_before_cpu_sampling"] is True
    assert len(comparison["commands"]) == 4 and comparison["status"] == "incomplete"
    assert comparison["session"] != report["session"]
    assert comparison["independent_child_pid"] == 43
    assert comparison["raw_summary"]["known_provider_counts"][target.IMAGE_PROVIDER] == 1
    assert comparison["raw_summary"]["child_matches_by_provider"] == {
        target.BI_PROVIDER: 0, target.PROCESS_PROVIDER: 1, target.IMAGE_PROVIDER: 0,
    }
    assert "not_provider_absence" in comparison["raw_summary"]["scope"]
    assert not ({"inputs", "observations", "build_cwd", "schema_observation"} & comparison.keys())
    public = temp / "LoLReplayTool-binary-cache/w/b/evidence"
    assert [path.name for path in public.iterdir()] == ["cl-decode-probe.json"]
    text = (public / "cl-decode-probe.json").read_text()
    assert "other-process-secret" not in text
    assert "raw.etl" not in text and "relogged.xml" not in text
    assert not any(value in text for value in ("private summary text", "private-root", "private-value"))
    assert "sampling-private" not in text


@pytest.mark.parametrize("failure", ["checkout", "start", "compile", "stop", "relog", "decode_raw", "decode_relogged", "inspect_raw"])
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
    assert not any(stage.startswith("sampling_") for stage in calls)
    assert "cpu_sampling_comparison" not in report
    assert calls.count("start") == calls.count("stop") == (0 if failure == "checkout" else 1)
    if failure in {"checkout", "start"}:
        assert "compile" not in calls
    if failure in {"checkout", "start", "compile", "stop"}:
        assert "relog" not in calls
    if failure == "inspect_raw":
        assert report["observations"]["cl_properties"]
        assert "stop_statistics" in report


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
    temp, tools, tracerpt, calls, original = probe

    def change(args, private, stage, timeout, report):
        result = original(args, private, stage, timeout, report)
        if stage == "decode_relogged":
            (tools / "cl.exe").write_bytes(b"changed input")
        return result

    monkeypatch.setattr(target, "_command", change)
    report = target.run_probe(temp, tools, tracerpt)
    assert report["status"] == "failed" and report["inputs_unchanged"] is False
    assert report["inputs_unchanged_before_cpu_sampling"] is False
    assert "sampling_start" not in calls and "cpu_sampling_comparison" not in report


def test_sampling_capture_reuses_inputs_arguments_and_cwd_after_a_stops(probe, monkeypatch):
    temp, tools, tracerpt, _, original = probe
    recorded = {}
    fingerprints = []
    fingerprint = target._fingerprint

    def observe_fingerprint(path):
        fingerprints.append(path.name)
        return fingerprint(path)

    def observe(args, private, stage, timeout, report):
        recorded[stage] = (args, private, timeout)
        if stage == "sampling_start":
            assert "inspect_raw" in recorded and "stop" in recorded
            assert set(fingerprints[-11:]) == {
                "cl.exe", "c1xx.dll", "c2.dll", "vcperf.exe", "CppBuildInsights.dll",
                "KernelTraceControl.dll", "CppBuildInsightsEtw.xml", "tracerpt.exe",
                "probe_opencv_cl_decode.py", "probe.cpp", "probe.rsp",
            }
        return original(args, private, stage, timeout, report)

    monkeypatch.setattr(target, "_fingerprint", observe_fingerprint)
    monkeypatch.setattr(target, "_command", observe)
    report = target.run_probe(temp, tools, tracerpt)
    assert report["status"] == "incomplete"
    assert recorded["compile"] == recorded["sampling_compile"]
    a, b = recorded["start"][0], recorded["sampling_start"][0]
    assert [arg for arg in a[:-1] if arg != "/nocpusampling"] == b[:-1]
    assert a[-1] != b[-1]
    assert recorded["stop"][0][2] == a[-1]
    assert recorded["sampling_stop"][0][2] == b[-1]
    assert recorded["stop"][0][3] != recorded["sampling_stop"][0][3]
    assert all(item[1] == recorded["start"][1] for item in recorded.values())
    # 340 seconds of command budgets + at most 5 seconds per owned-child cleanup.
    assert sum(item[2] for item in recorded.values()) == 340
    workflow = Path(".github/workflows/build-opencv.yml").read_text(encoding="utf-8")
    diagnostic_step = workflow.split("- name: Probe one CL translation unit decoder", 1)[1].split("\n      - name:", 1)[0]
    assert "timeout-minutes: 8" in diagnostic_step
    assert "continue-on-error: true" in diagnostic_step and "pull_request" in diagnostic_step
    assert sum(item[2] for item in recorded.values()) + 5 * len(recorded) < 8 * 60


@pytest.mark.parametrize("name", [
    "cl.exe", "c1xx.dll", "c2.dll", "vcperf.exe", "CppBuildInsights.dll",
    "KernelTraceControl.dll", "CppBuildInsightsEtw.xml", "tracerpt.exe",
    "probe_opencv_cl_decode.py", "probe.cpp", "probe.rsp",
])
def test_each_changed_input_prevents_sampling_capture(probe, monkeypatch, name):
    temp, tools, tracerpt, calls, original = probe
    fingerprint = target._fingerprint
    changed = False

    def observe(args, private, stage, timeout, report):
        nonlocal changed
        entry = original(args, private, stage, timeout, report)
        if stage == "inspect_raw":
            changed = True
        return entry

    def changed_fingerprint(path):
        record = fingerprint(path)
        if changed and path.name == name:
            record["sha256"] = "0" * 64
        return record

    monkeypatch.setattr(target, "_command", observe)
    monkeypatch.setattr(target, "_fingerprint", changed_fingerprint)
    report = target.run_probe(temp, tools, tracerpt)
    assert report["status"] == "failed"
    assert report["error"]["code"] == "input_changed_before_cpu_sampling"
    assert report["inputs_unchanged_before_cpu_sampling"] is False
    assert "sampling_start" not in calls
    assert report["observations"]["cl_properties"]


@pytest.mark.parametrize("failure", ["sampling_start", "sampling_compile", "sampling_stop", "sampling_decode_raw"])
def test_sampling_failure_preserves_a_and_stops_only_b_once(probe, monkeypatch, failure):
    temp, tools, tracerpt, calls, original = probe

    def fail(args, private, stage, timeout, report):
        if stage == failure:
            calls.append(stage)
            raise target.ProbeError("sampling_stage_failure")
        return original(args, private, stage, timeout, report)

    monkeypatch.setattr(target, "_command", fail)
    report = target.run_probe(temp, tools, tracerpt)
    comparison = report["cpu_sampling_comparison"]
    assert report["status"] == comparison["status"] == "failed"
    assert report["error"] == comparison["error"] == {"type": "ProbeError", "code": "sampling_stage_failure"}
    assert report["observations"]["cl_properties"] and report["decoder_documents"]
    assert report["cleanup_error"] is None
    assert calls.count("stop") == calls.count("sampling_stop") == 1
    if failure == "sampling_start":
        assert "sampling_compile" not in calls
    if failure != "sampling_decode_raw":
        assert "sampling_decode_raw" not in calls


def test_sampling_stop_failure_retains_primary_and_private_errors(probe, monkeypatch):
    temp, tools, tracerpt, calls, original = probe

    def fail(args, private, stage, timeout, report):
        if stage in {"sampling_compile", "sampling_stop"}:
            calls.append(stage)
            if stage == "sampling_compile":
                raise target.ProbeError("sampling_compile_failure")
            raise RuntimeError("private-cleanup-error")
        return original(args, private, stage, timeout, report)

    monkeypatch.setattr(target, "_command", fail)
    report = target.run_probe(temp, tools, tracerpt)
    comparison = report["cpu_sampling_comparison"]
    assert report["error"]["code"] == comparison["error"]["code"] == "sampling_compile_failure"
    assert comparison["cleanup_error"] == {"type": "RuntimeError", "code": "operation_failed"}
    assert report["observations"]["cl_properties"] and calls.count("sampling_stop") == 1
    assert "private-cleanup-error" not in json.dumps(report)


@pytest.mark.parametrize("prefix", ["", "sampling_"])
def test_private_file_details_preserve_distinct_primary_and_cleanup_failures(probe, monkeypatch, prefix):
    temp, tools, tracerpt, calls, original = probe
    primary = target.ProbeError("private_file_limit", private_file_check={
        "category": "probe.obj", "regular_file": True, "exceeds_file_limit": True,
    })
    cleanup = target.ProbeError("private_file_limit", private_file_check={
        "category": prefix + "raw.etl", "regular_file": False, "exceeds_file_limit": False,
    })

    def fail(args, private, stage, timeout, report):
        if stage in {prefix + "compile", prefix + "stop"}:
            calls.append(stage)
            raise primary if stage == prefix + "compile" else cleanup
        return original(args, private, stage, timeout, report)

    monkeypatch.setattr(target, "_command", fail)
    report = target.run_probe(temp, tools, tracerpt)
    capture = report["cpu_sampling_comparison"] if prefix else report
    assert report["status"] == capture["status"] == "failed"
    assert report["error"] == capture["error"] == target._error(primary)
    assert capture["cleanup_error"] == target._error(cleanup)
    assert calls.count(prefix + "stop") == 1 and prefix + "decode_raw" not in calls
    if prefix:
        assert report["observations"]["cl_properties"] and report["cleanup_error"] is None


def test_input_change_during_sampling_invalidates_comparison(probe, monkeypatch):
    temp, tools, tracerpt, _, original = probe

    def change(args, private, stage, timeout, report):
        entry = original(args, private, stage, timeout, report)
        if stage == "sampling_decode_raw":
            (tools / "cl.exe").write_bytes(b"changed after second capture")
        return entry

    monkeypatch.setattr(target, "_command", change)
    report = target.run_probe(temp, tools, tracerpt)
    assert report["inputs_unchanged_before_cpu_sampling"] is True
    assert report["inputs_unchanged"] is False and report["status"] == "failed"
    assert report["cpu_sampling_comparison"]["status"] == "failed"
    assert report["observations"]["cl_properties"]


def test_sampling_summary_does_not_infer_unknown_schema_or_publish_values(tmp_path):
    path = tmp_path / "sampling.xml"
    path.write_bytes(b'<private-root><Event secret="private-value"/></private-root>')
    result = target._sampling_summary(path, 43)
    assert result["unsupported_namespace_events"] == 1
    assert all(value == 0 for value in result["known_provider_counts"].values())
    assert all(value == 0 for value in result["child_matches_by_provider"].values())
    assert "not_provider_absence" in result["scope"]
    assert "private" not in json.dumps(result)


@pytest.mark.parametrize("source", ["missing_provider_guid", "system_trace"])
@pytest.mark.parametrize("scheme", ["http", "https"])
@pytest.mark.parametrize(("guid", "kind"), [
    ("3d6fa8d0-fe05-11d0-9dda-00c04fd7ba7c", "process"),
    ("2cb15d1d-5fc1-11d2-abe1-00a0c911f518", "image"),
    ("f78a07b0-796a-5da4-5c20-61aa526e77af", "build_insights"),
    ("11111111-aaaa-bbbb-cccc-222222222222", "other_guid"),
])
def test_trace_identity_classifies_fixed_scopes_namespaces_and_guids(tmp_path, source, scheme, guid, kind):
    provider = None if source == "missing_provider_guid" else "9e814aad-3204-11d2-9a82-006008a86939"
    root = ET.Element("Events")
    root.append(_trace_identity_event(provider, scheme, guid))
    root.append(_trace_identity_event(provider, scheme, "{" + guid.upper() + "}"))
    path = tmp_path / "sampling.xml"
    path.write_bytes(ET.tostring(root))
    result = target._sampling_summary(path, 42)
    observation = result["trace_identity_observation"]
    assert observation[source] == {"events": 2, scheme: {kind: {
        "events": 2, "header_match": 2, "header_unknown": 0, "payload_match": 2, "payload_unknown": 0,
    }}}
    other = "system_trace" if source == "missing_provider_guid" else "missing_provider_guid"
    assert observation[other] == {"events": 0}
    assert set(observation) == {source, other}
    assert result["target_pid_events"] == 0
    assert all(count == 0 for count in result["child_matches_by_provider"].values())
    assert guid not in json.dumps(observation) and hashlib.sha256(guid.encode()).hexdigest() not in json.dumps(observation)


@pytest.mark.parametrize(("value", "valid"), [
    (None, False), ("", False), ("3d6fa8d0-fe05-11d0-9dda-00c04fd7ba7c", True),
    ("{3D6FA8D0-FE05-11D0-9DDA-00C04FD7BA7C}", True),
    (" 3d6fa8d0-fe05-11d0-9dda-00c04fd7ba7c", False),
    ("3d6fa8d0-fe05-11d0-9dda-00c04fd7ba7c\n", False),
    ("{{3d6fa8d0-fe05-11d0-9dda-00c04fd7ba7c}}", False),
    ("{3d6fa8d0-fe05-11d0-9dda-00c04fd7ba7c", False),
    ("3d6fa8d0fe0511d09dda00c04fd7ba7c", False), ("private-guid" * 1000, False),
])
def test_trace_identity_guid_parser_does_not_trim_or_relax_syntax(value, valid):
    assert target._strict_guid(value) == (target.PROCESS_PROVIDER if valid else None)


@pytest.mark.parametrize(("change", "expected", "within_namespace"), [
    ("missing_extension", "missing_extension", False),
    ("nested_extension", "missing_extension", False),
    ("multiple_extensions", "multiple_extensions", False),
    ("foreign_duplicate_extension", "multiple_extensions", False),
    ("unsupported_extension", "unsupported_extension_namespace", False),
    ("unqualified_extension", "unsupported_extension_namespace", False),
    ("missing_guid", "missing_guid", True),
    ("nested_only_guid", "missing_guid", True),
    ("multiple_guids", "multiple_guids", True),
    ("foreign_duplicate_guid", "multiple_guids", True),
    ("foreign_guid", "foreign_guid_namespace", True),
    ("unqualified_guid", "foreign_guid_namespace", True),
    ("nested_guid", "nested_guid", True),
    ("malformed_guid", "malformed_guid", True),
])
def test_trace_identity_reports_first_structural_error_without_private_names(
    tmp_path, change, expected, within_namespace,
):
    event = _trace_identity_event(None)
    extension, guid = event[-1], event[-1][0]
    if change == "missing_extension":
        event.remove(extension)
    elif change == "nested_extension":
        event.remove(extension)
        ET.SubElement(event, f"{target.NS}RenderingInfo").append(extension)
    elif change in {"multiple_extensions", "foreign_duplicate_extension"}:
        ET.SubElement(event, extension.tag if change == "multiple_extensions" else "{private-namespace}ExtendedTracingInfo")
    elif change in {"unsupported_extension", "unqualified_extension"}:
        extension.tag = "{private-namespace}ExtendedTracingInfo" if change == "unsupported_extension" else "ExtendedTracingInfo"
    elif change in {"missing_guid", "nested_only_guid"}:
        extension.remove(guid)
        if change == "nested_only_guid":
            ET.SubElement(extension, "private-wrapper").append(guid)
    elif change in {"multiple_guids", "foreign_duplicate_guid"}:
        ET.SubElement(extension, guid.tag if change == "multiple_guids" else "{private-namespace}EventGuid")
    elif change in {"foreign_guid", "unqualified_guid"}:
        guid.tag = "{private-namespace}EventGuid" if change == "foreign_guid" else "EventGuid"
        ET.SubElement(guid, "private-child")  # Namespace rejection takes priority over nested content.
    elif change == "nested_guid":
        ET.SubElement(guid, "private-child")
        guid.text = "private-malformed-guid"  # Nested content takes priority over text parsing.
    else:
        guid.text = "private-malformed-guid"
    path = tmp_path / "sampling.xml"
    path.write_bytes(ET.tostring(event))
    result = target._sampling_summary(path, 42)
    details = {"http": {expected: 1}} if within_namespace else {expected: 1}
    assert result["trace_identity_observation"] == {
        "missing_provider_guid": {"events": 1, **details}, "system_trace": {"events": 0},
    }
    text = json.dumps(result)
    for secret in ("private-namespace", "private-wrapper", "private-child", "private-malformed-guid"):
        assert secret not in text and hashlib.sha256(secret.encode()).hexdigest() not in text


@pytest.mark.parametrize("change", ["duplicate_system", "duplicate_provider", "missing_system", "missing_provider",
                                    "empty_provider_guid", "malformed_provider_guid", "other_provider"])
def test_trace_identity_excludes_ambiguous_or_out_of_scope_headers(tmp_path, change):
    event = _trace_identity_event(None)
    system = event.find(f"{target.NS}System")
    provider = system.find(f"{target.NS}Provider")
    if change == "duplicate_system":
        event.append(ET.fromstring(ET.tostring(system)))
    elif change == "duplicate_provider":
        ET.SubElement(system, provider.tag, Guid=target.BI_PROVIDER)
    elif change == "missing_system":
        event.remove(system)
    elif change == "missing_provider":
        system.remove(provider)
    else:
        provider.set("Guid", {"empty_provider_guid": "", "malformed_provider_guid": "private-provider",
                              "other_provider": "11111111-aaaa-bbbb-cccc-222222222222"}[change])
    path = tmp_path / "sampling.xml"
    path.write_bytes(ET.tostring(event))
    observed = target._sampling_summary(path, 42)["trace_identity_observation"]
    expected = {"missing_provider_guid": {"events": 0}, "system_trace": {"events": 0}}
    if change.startswith("duplicate"):
        expected["ambiguous_structure_events"] = 1
    assert observed == expected


@pytest.mark.parametrize(("value", "match", "unknown"), [
    ("42", 1, 0), ("0x2a", 1, 0), ("0" * 62 + "42", 1, 0), ("43", 0, 0),
    ("4294967295", 0, 0), ("4294967296", 0, 1), ("0" * 63 + "42", 0, 1),
    (" 42 ", 0, 1), ("0X2A", 0, 1), ("１２", 0, 1), (None, 0, 1),
])
def test_trace_identity_pid_comparison_keeps_strict_bounded_numbers(tmp_path, value, match, unknown):
    event = _trace_identity_event(None)
    execution = event.find(f"{target.NS}System/{target.NS}Execution")
    if value is None:
        execution.attrib.clear()
    else:
        execution.set("ProcessID", value)
    event.find(f"{target.NS}EventData/{target.NS}Data").text = value
    path = tmp_path / "sampling.xml"
    path.write_bytes(ET.tostring(event))
    group = target._sampling_summary(path, 42)["trace_identity_observation"]["missing_provider_guid"]["http"]["process"]
    assert group == {"events": 1, "header_match": match, "header_unknown": unknown,
                     "payload_match": match, "payload_unknown": unknown}


@pytest.mark.parametrize("change", ["duplicate_execution", "nested_execution", "duplicate_event_data",
                                    "duplicate_pid", "nested_pid", "missing_pid"])
def test_trace_identity_pid_requires_unique_leaf_fields(tmp_path, change):
    event = _trace_identity_event(None)
    system, payload = event.find(f"{target.NS}System"), event.find(f"{target.NS}EventData")
    if change == "duplicate_execution":
        ET.SubElement(system, f"{target.NS}Execution", ProcessID="42")
    elif change == "nested_execution":
        ET.SubElement(system.find(f"{target.NS}Execution"), "private-child")
    elif change == "duplicate_event_data":
        ET.SubElement(event, payload.tag)
    elif change == "duplicate_pid":
        ET.SubElement(payload, f"{target.NS}Data", Name="ProcessId").text = "42"
    elif change == "nested_pid":
        ET.SubElement(payload[0], "private-child")
    else:
        payload.remove(payload[0])
    path = tmp_path / "sampling.xml"
    path.write_bytes(ET.tostring(event))
    group = target._sampling_summary(path, 42)["trace_identity_observation"]["missing_provider_guid"]["http"]["process"]
    header_unknown = change.endswith("execution")
    assert group == {"events": 1, "header_match": int(not header_unknown), "header_unknown": int(header_unknown),
                     "payload_match": int(header_unknown), "payload_unknown": int(not header_unknown)}


def test_trace_identity_keeps_parent_and_child_pid_matches_independent(tmp_path):
    root = ET.Element("Events")
    for header, payload in ((99, 42), (42, 99)):
        event = _trace_identity_event(None)
        event.find(f"{target.NS}System/{target.NS}Execution").set("ProcessID", str(header))
        event.find(f"{target.NS}EventData/{target.NS}Data").text = str(payload)
        path = tmp_path / f"pid-{header}.xml"
        path.write_bytes(ET.tostring(event))
        group = target._sampling_summary(path, 42)["trace_identity_observation"]["missing_provider_guid"]["http"]["process"]
        assert group == {"events": 1, "header_match": int(header == 42), "header_unknown": 0,
                         "payload_match": int(payload == 42), "payload_unknown": 0}
        root.append(event)
    path = tmp_path / "both.xml"
    path.write_bytes(ET.tostring(root))
    group = target._sampling_summary(path, 42)["trace_identity_observation"]["missing_provider_guid"]["http"]["process"]
    assert group == {"events": 2, "header_match": 1, "header_unknown": 0, "payload_match": 1, "payload_unknown": 0}


def test_trace_identity_ignores_private_value_changes_in_the_same_structure(tmp_path):
    observations = []
    for suffix, guid in (("one", "11111111-aaaa-bbbb-cccc-222222222222"),
                         ("two", "33333333-dddd-eeee-ffff-444444444444")):
        secret = f"private-{suffix}"
        root = ET.Element("Events")
        event = _trace_identity_event(None, guid=guid)
        event[-1].set(secret + "-attribute", secret + "-value")
        event[-1].text = secret + "-body"
        root.append(event)
        unsupported = _trace_identity_event(None)
        unsupported[-1].tag = "{" + secret + "-namespace}ExtendedTracingInfo"
        root.append(unsupported)
        malformed = _trace_identity_event(None)
        malformed[-1][0].text = secret + "-guid"
        root.append(malformed)
        path = tmp_path / f"{suffix}.xml"
        path.write_bytes(ET.tostring(root))
        result = target._sampling_summary(path, 42)
        observations.append(result["trace_identity_observation"])
        for value in (guid, *(secret + ending for ending in ("-attribute", "-value", "-body", "-namespace", "-guid"))):
            assert value not in json.dumps(result) and hashlib.sha256(value.encode()).hexdigest() not in json.dumps(result)
    assert observations[0] == observations[1] == {
        "missing_provider_guid": {
            "events": 3, "unsupported_extension_namespace": 1,
            "http": {"malformed_guid": 1, "other_guid": {
                "events": 1, "header_match": 1, "header_unknown": 0, "payload_match": 1, "payload_unknown": 0,
            }},
        },
        "system_trace": {"events": 0},
    }


def test_trace_identity_observation_does_not_change_existing_yields_or_schema(tmp_path):
    root = ET.fromstring(_xml([(target.PROCESS_PROVIDER, 999, {"ProcessId": 42})]))
    root.append(_trace_identity_event(None))
    root.append(_trace_identity_event("9e814aad-3204-11d2-9a82-006008a86939"))
    path = tmp_path / "sampling.xml"
    path.write_bytes(ET.tostring(root))
    before, after = {}, {}
    expected = list(target._events(path, 42, before))
    observation = {"missing_provider_guid": {"events": 0}, "system_trace": {"events": 0}}
    assert list(target._events(path, 42, after, trace_identity=observation)) == expected
    assert before == after and after["target_pid_events"] == 1
    assert observation["missing_provider_guid"]["events"] == observation["system_trace"]["events"] == 1


def test_trace_identity_is_b_only_single_read_bounded_and_preserves_all_counts(probe, monkeypatch):
    temp, tools, tracerpt, _, original = probe
    reads = []
    private_bytes = target._private_bytes

    def read(path):
        reads.append(path.name)
        return private_bytes(path)

    def command(args, private, stage, timeout, report):
        entry = original(args, private, stage, timeout, report)
        if stage == "sampling_decode_raw":
            root = ET.Element("Events")
            for provider in (None, "9e814aad-3204-11d2-9a82-006008a86939"):
                for scheme in ("http", "https"):
                    for guid in (target.PROCESS_PROVIDER, target.IMAGE_PROVIDER, target.BI_PROVIDER,
                                 "11111111-aaaa-bbbb-cccc-222222222222"):
                        for _ in range(3):
                            event = _trace_identity_event(provider, scheme, guid)
                            event[-1].set("private-attribute", "private-value")
                            root.append(event)
            (private / "sampling_raw.xml").write_bytes(ET.tostring(root))
        return entry

    monkeypatch.setattr(target, "_private_bytes", read)
    monkeypatch.setattr(target, "_command", command)
    report = target.run_probe(temp, tools, tracerpt)
    summary = report["cpu_sampling_comparison"]["raw_summary"]
    observed = summary["trace_identity_observation"]
    assert reads.count("sampling_raw.xml") == 1
    assert report["status"] == report["cpu_sampling_comparison"]["status"] == "incomplete"
    assert report["product_build_evidence"] is False and summary["target_pid_events"] == 0
    for source in observed.values():
        assert source["events"] == 24
        assert sum(group["events"] for scheme in ("http", "https") for group in source[scheme].values()) == 24
    for value in (report["observations"], report["inspection_schema_observation"], report["decoder_documents"]):
        assert "trace_identity_observation" not in json.dumps(value)
    assert "private-attribute" not in json.dumps(report) and "private-value" not in json.dumps(report)
    for value in ("private-attribute", "private-value", "11111111-aaaa-bbbb-cccc-222222222222"):
        assert hashlib.sha256(value.encode()).hexdigest() not in json.dumps(report)
    # Bound the whole fixed wire vocabulary even when every counter needs ten digits.
    maximum = {source: {"events": 9999999999,
                       **dict.fromkeys(("missing_extension", "multiple_extensions", "unsupported_extension_namespace"), 9999999999),
                       **{scheme: {**dict.fromkeys(("missing_guid", "multiple_guids", "foreign_guid_namespace", "nested_guid", "malformed_guid"), 9999999999),
                                   **{kind: dict.fromkeys(("events", "header_match", "header_unknown", "payload_match", "payload_unknown"), 9999999999)
                                      for kind in ("process", "image", "build_insights", "other_guid")}}
                          for scheme in ("http", "https")}}
               for source in ("missing_provider_guid", "system_trace")}
    maximum["ambiguous_structure_events"] = 9999999999
    assert len(json.dumps(maximum, separators=(",", ":")).encode()) < 4096
    summary["trace_identity_observation"] = maximum
    target._write_report(temp / "maximum", report)
    public = temp / "maximum/LoLReplayTool-binary-cache/w/b/evidence/cl-decode-probe.json"
    assert public.stat().st_size <= target.JSON_LIMIT == 64 * 1024


@pytest.mark.parametrize("guid_count", [32, 33])
def test_sampling_report_retains_bounded_schema_without_samples_or_raw_values(probe, monkeypatch, guid_count):
    temp, tools, tracerpt, _, original = probe
    guids = [f"{index:08x}-aaaa-bbbb-cccc-222222222222" for index in range(guid_count)]

    def sampling_schema(args, private, stage, timeout, report):
        entry = original(args, private, stage, timeout, report)
        if stage == "sampling_decode_raw":
            root = ET.fromstring(_xml([(guid, 43, {}) for guid in guids]))
            missing = ET.fromstring(_xml([
                (target.BI_PROVIDER, 43, {"ProcessId": 43, "FileName": "private-path",
                                          "CommandLine": "private-command", "private-field": "private-value"})
                for _ in range(11)
            ]))
            for event, event_id in zip(missing, [*range(9), 0, 8], strict=True):
                system = event.find(f"{target.NS}System")
                provider = system.find(f"{target.NS}Provider")
                provider.attrib.clear()
                provider.attrib.update(Name="private-provider", EventSourceName="private-source")
                ET.SubElement(system, f"{target.NS}EventID").text = str(event_id)
                root.append(event)
            (private / "sampling_raw.xml").write_bytes(ET.tostring(root))
        return entry

    monkeypatch.setattr(target, "_command", sampling_schema)
    report = target.run_probe(temp, tools, tracerpt)
    comparison = report["cpu_sampling_comparison"]
    assert report["product_build_evidence"] is False
    assert report["observations"]["process_events"][0]["observed_payload_pid"] == 42
    if guid_count > 32:
        assert report["status"] == comparison["status"] == "failed"
        assert report["error"]["code"] == comparison["error"]["code"] == "provider_guid_limit"
        assert "raw_summary" not in comparison
    else:
        assert report["status"] == comparison["status"] == "incomplete"
        summary = comparison["raw_summary"]
        assert summary["provider_guid_counts"] == dict.fromkeys(guids, 1)
        assert summary["event_count"] == 43 and summary["missing_provider_guid_events"] == 11
        assert all(value == 0 for value in summary["known_provider_counts"].values())
        assert all(value == 0 for value in summary["child_matches_by_provider"].values())
        assert summary["target_pid_events"] == 0 and "not_provider_absence" in summary["scope"]
        missing = summary["missing_guid_schema"]
        assert len(missing["groups"]) == 8 and missing["groups"][0]["count"] == 2
        assert missing["overflow_events"] == 2 and missing["truncated"] is True
        assert missing["header_child_matches"] == missing["payload_child_matches"] == 11
        assert all(set(group) == {"count", "structure"} for group in missing["groups"])
        structure = missing["groups"][0]["structure"]
        assert structure["system_fields"]["EventID"]["uint32"] == 0
        assert structure["other_payload_field_count"] == 1
        assert structure["payload_fields"]["FileName"]["form"] == "other"
    public = temp / "LoLReplayTool-binary-cache/w/b/evidence/cl-decode-probe.json"
    assert public.stat().st_size <= target.JSON_LIMIT == 64 * 1024
    text = public.read_text(encoding="utf-8")
    assert json.loads(text) == report
    assert "first_sample" not in text and "private-" not in text
    assert hashlib.sha256(b"private-path").hexdigest() not in text
    assert hashlib.sha256(b"private-command").hexdigest() not in text



@pytest.mark.parametrize("matching_group_count", [1, 9])
def test_sampling_prioritizes_late_child_headers_with_exact_bounded_public_counts(
    probe, monkeypatch, matching_group_count,
):
    temp, tools, tracerpt, _, original = probe
    # Fill the first eight slots, including three occurrences of the last group.
    rows = [(event_id, 999) for event_id in range(8)] + [(7, 999), (7, 999)]
    rows += [(event_id, 43) for event_id in range(8, 8 + matching_group_count) for _ in range(2)]
    rows += [(7, 999), (6, 999)]
    child_names = ("System", "EventData", "UserData", "RenderingInfo", "ProcessingErrorData",
                   "BinaryEventData", "DebugData", "other")

    def late_child_headers(args, private, stage, timeout, report):
        entry = original(args, private, stage, timeout, report)
        if stage == "sampling_decode_raw":
            root = ET.fromstring(_xml([
                (target.BI_PROVIDER, header, {"ProcessId": 999, "FileName": "private-priority-path",
                                            "CommandLine": "private-priority-command"})
                for _, header in rows
            ]))
            for event, (event_id, _) in zip(root, rows, strict=True):
                system = event.find(f"{target.NS}System")
                provider = system.find(f"{target.NS}Provider")
                provider.attrib.clear()
                provider.attrib.update(Name="private-priority-provider")
                ET.SubElement(system, f"{target.NS}EventID").text = str(event_id)
                for namespace in (target.NS, "", "{private-priority-namespace}"):
                    for name in child_names:
                        if namespace == target.NS and name in {"System", "EventData"}:
                            continue
                        local_name = "private-priority-element" if name == "other" else name
                        ET.SubElement(event, namespace + local_name)
            # A real provider GUID is still decoded normally, independent of diagnostic prioritization.
            root.append(ET.fromstring(_xml([(target.BI_PROVIDER, 43, {"Tool": "CL"})]))[0])
            (private / "sampling_raw.xml").write_bytes(ET.tostring(root))
        return entry

    monkeypatch.setattr(target, "_command", late_child_headers)
    report = target.run_probe(temp, tools, tracerpt)
    summary = report["cpu_sampling_comparison"]["raw_summary"]
    missing = summary["missing_guid_schema"]
    groups = missing["groups"]
    retained_ids = [group["structure"]["system_fields"]["EventID"]["uint32"] for group in groups]
    assert retained_ids == ([*range(7), 8] if matching_group_count == 1 else list(range(8, 16)))
    assert len(groups) == target.SCHEMA_GROUP_LIMIT == 8
    assert sum(group["count"] for group in groups) + missing["overflow_events"] == len(rows)
    assert missing["overflow_events"] == (4 if matching_group_count == 1 else 14)
    assert missing["header_child_matches"] == matching_group_count * 2
    assert missing["payload_child_matches"] == 0
    for group in groups:
        structure = group["structure"]
        assert structure["event_child_count"] == 24
        assert structure["event_direct_children"] == {
            f"{namespace}:{name}": 1 for namespace in ("event", "none", "other") for name in child_names
        }
    assert missing["truncated"] is True
    assert summary["missing_provider_guid_events"] == len(rows)
    assert summary["known_provider_counts"][target.BI_PROVIDER] == summary["target_pid_events"] == 1
    assert summary["child_matches_by_provider"] == {
        target.BI_PROVIDER: 1, target.PROCESS_PROVIDER: 0, target.IMAGE_PROVIDER: 0,
    }
    assert report["status"] == report["cpu_sampling_comparison"]["status"] == "incomplete"
    assert report["product_build_evidence"] is False
    public = temp / "LoLReplayTool-binary-cache/w/b/evidence/cl-decode-probe.json"
    assert public.stat().st_size <= target.JSON_LIMIT == 64 * 1024
    text = public.read_text(encoding="utf-8")
    assert json.loads(text) == report
    assert "first_sample" not in text and "private-priority" not in text
    for value in ("private-priority-path", "private-priority-command", "private-priority-provider",
                  "private-priority-namespace", "private-priority-element"):
        assert hashlib.sha256(value.encode()).hexdigest() not in text


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


def test_event_direct_children_group_only_fixed_namespaces_and_names_without_private_values(tmp_path):
    root = ET.fromstring(_xml([(target.BI_PROVIDER, 42, {})] * 2))
    secrets = []
    for suffix, event in zip(("one", "different-two"), root, strict=True):
        event.find(f"{target.NS}System/{target.NS}Provider").attrib.clear()
        namespace, name, attribute, value = (f"private-{field}-{suffix}" for field in (
            "namespace", "element", "attribute", "value",
        ))
        secrets.extend((namespace, name, attribute, value))
        rendering = ET.SubElement(event, f"{target.NS}RenderingInfo", {attribute: value})
        rendering.text = value
        ET.SubElement(rendering, f"{target.NS}DebugData").text = value
        for tag in (f"{{{namespace}}}EventData", "UserData"):
            payload = ET.SubElement(event, tag)
            ET.SubElement(payload, f"{target.NS}Data", Name="ProcessId").text = "42"
        for prefix in (target.NS, target.NS, "", f"{{{namespace}}}"):
            ET.SubElement(event, prefix + name, {attribute: value}).text = value
    path = tmp_path / "sampling.xml"
    path.write_bytes(ET.tostring(root))
    result = target._sampling_summary(path, 42)
    missing = result["missing_guid_schema"]
    assert len(missing["groups"]) == 1 and missing["groups"][0]["count"] == 2
    structure = missing["groups"][0]["structure"]
    assert structure["event_child_count"] == 9
    assert structure["event_direct_children"] == {
        "event:System": 1, "event:EventData": 1, "event:RenderingInfo": 1,
        "other:EventData": 1, "none:UserData": 1, "event:other": 2, "none:other": 1, "other:other": 1,
    }
    assert sum(structure["event_direct_children"].values()) == structure["event_child_count"]
    assert missing["header_child_matches"] == 2 and missing["payload_child_matches"] == 0
    assert result["target_pid_events"] == 0
    assert all(count == 0 for count in result["child_matches_by_provider"].values())
    public = json.dumps(result)
    assert "first_sample" not in public
    for secret in secrets:
        assert secret not in public and hashlib.sha256(secret.encode()).hexdigest() not in public


def test_event_child_observation_does_not_decode_payloads_outside_exact_event_data(tmp_path):
    root = ET.fromstring(_xml([(target.PROCESS_PROVIDER, 42, {})] * 4))
    for event, tag in zip(root, ("EventData", "{private-namespace}EventData",
                                 f"{target.NS}UserData", f"{target.NS}BinaryEventData"), strict=True):
        event.remove(event.find(f"{target.NS}EventData"))
        payload = ET.SubElement(event, tag)
        ET.SubElement(payload, f"{target.NS}Data", Name="ProcessId").text = "42"
    root.append(ET.fromstring(_xml([(target.PROCESS_PROVIDER, 999, {"ProcessId": 42})]))[0])
    path = tmp_path / "sampling.xml"
    path.write_bytes(ET.tostring(root))
    result = target._sampling_summary(path, 42)
    assert result["known_provider_counts"][target.PROCESS_PROVIDER] == 5
    assert result["target_pid_events"] == 1
    assert result["child_matches_by_provider"] == {
        target.BI_PROVIDER: 0, target.PROCESS_PROVIDER: 1, target.IMAGE_PROVIDER: 0,
    }
    assert result["missing_guid_schema"]["groups"] == []
    assert "private-namespace" not in json.dumps(result)


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


def test_stop_statistics_keep_decimal_strings_and_do_not_infer_missing_or_duplicate_values(tmp_path):
    path = tmp_path / "stop.log"
    path.write_bytes(
        b"private path and command\r\nDropped MSVC events: 000\r\n"
        b"Dropped MSVC buffers: " + b"9" * 64 + b"\r\n"
        b"Dropped system events: 0\r\nDropped system events: invalid\r\n"
    )
    result = target._stop_statistics(path)
    assert result["counters"] == {
        "msvc_events": {"label_occurrences": 1, "value": "000"},
        "msvc_buffers": {"label_occurrences": 1, "value": "9" * 64},
        "system_events": {"label_occurrences": 2, "value": None},
        "system_buffers": {"label_occurrences": 0, "value": None},
    }
    assert "not_sdk_type" in result["value_format"]
    assert "private" not in json.dumps(result)


@pytest.mark.parametrize("value", [b"", b"-1", b"+1", b"0x10", b"1,000", b" 1", b"1 ", b"9" * 65, "１２".encode()])
def test_stop_statistics_reject_unobserved_numeric_formats(tmp_path, value):
    path = tmp_path / "stop.log"
    path.write_bytes(b"Dropped MSVC events: " + value + b"\n")
    assert target._stop_statistics(path)["counters"]["msvc_events"] == {"label_occurrences": 1, "value": None}


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig", "utf-16"])
def test_decoder_document_counts_literals_without_interpreting_them_as_events_or_publishing_text(tmp_path, encoding):
    path = tmp_path / "interpreted.xml"
    content = (
        f'<secret-root private-attribute="{target.PROCESS_PROVIDER.upper()}">'
        f'<secret-node>{{{target.PROCESS_PROVIDER}}}</secret-node>'
        f'<secret-node>x{target.PROCESS_PROVIDER}0</secret-node>'
        '</secret-root>'
    )
    data = content.encode(encoding)
    path.write_bytes(data)
    result = target._decoder_document(path, xml=True)
    assert result["bytes"] == len(data) and result["sha256"] == hashlib.sha256(data).hexdigest()
    assert result["text_encoding"] != "unknown"
    assert result["xml_status"] == "well_formed"
    assert result["element_count"] == 3 and result["attribute_count"] == 1
    assert result["root_name_shape"] == target._value_shape("secret-root")
    assert result["known_guid_literal_occurrences_not_events"] == {
        target.BI_PROVIDER: 0, target.PROCESS_PROVIDER: 2, target.IMAGE_PROVIDER: 0,
    }
    assert not any(value in json.dumps(result) for value in ("secret-root", "private-attribute", "secret-node"))


@pytest.mark.parametrize("data", [b"\xff", "private".encode("utf-32"), "private".encode("utf-16-le")])
def test_decoder_document_unknown_encoding_does_not_claim_zero_guid_occurrences(tmp_path, data):
    path = tmp_path / "summary.txt"
    path.write_bytes(data)
    result = target._decoder_document(path, xml=False)
    assert result["text_encoding"] == "unknown"
    assert result["known_guid_literal_occurrences_not_events"] is None
    assert result["sha256"] == hashlib.sha256(data).hexdigest()


def test_decoder_document_unknown_xml_and_dtd_are_distinct(tmp_path):
    path = tmp_path / "interpreted.xml"
    path.write_text("not a known schema", encoding="utf-8")
    result = target._decoder_document(path, xml=True)
    assert result["xml_status"] == "unknown_format" and "element_count" not in result
    path.write_bytes(b'<!DOCTYPE x [<!ENTITY y "private">]><x>&y;</x>')
    with pytest.raises(target.ProbeError, match="xml_declaration_rejected"):
        target._decoder_document(path, xml=True)


def test_private_output_changed_after_fingerprinting_is_rejected(tmp_path, monkeypatch):
    path = tmp_path / "summary.txt"
    path.write_bytes(b"before")
    original = target._fingerprint

    def change(path):
        record = original(path)
        path.write_bytes(b"after!")
        return record

    monkeypatch.setattr(target, "_fingerprint", change)
    with pytest.raises(target.ProbeError, match="input_changed_during_read"):
        target._decoder_document(path, xml=False)


def test_public_report_compaction_preserves_values_within_existing_byte_limit(tmp_path, monkeypatch):
    report = {"rows": [{"name": "fixed", "value": " space  改行\n "}] * 8}
    monkeypatch.setattr(target, "JSON_LIMIT", 400)
    assert len(json.dumps(report, ensure_ascii=False, indent=2).encode()) > target.JSON_LIMIT
    target._write_report(tmp_path, report)
    output = tmp_path / "LoLReplayTool-binary-cache/w/b/evidence/cl-decode-probe.json"
    assert output.stat().st_size <= target.JSON_LIMIT
    assert json.loads(output.read_bytes()) == report


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


@pytest.mark.parametrize(("filename", "category"), [
    *((name, name) for name in (
        "probe.cpp", "probe.rsp", "probe.obj", "raw.etl", "relogged.etl", "raw.xml", "relogged.xml",
        "inspection.xml", "summary.txt", "interpreted.xml", "sampling_raw.etl", "sampling_raw.xml",
    )),
    *((stage + ".log", "command_log") for stage in (
        "checkout", "start", "compile", "stop", "relog", "decode_raw", "decode_relogged", "inspect_raw",
        "sampling_start", "sampling_compile", "sampling_stop", "sampling_decode_raw",
    )),
    ("private-unknown.log", "other"), ("raw.xml.private", "other"),
])
def test_private_file_limit_publishes_only_fixed_file_categories(tmp_path, monkeypatch, filename, category):
    (tmp_path / filename).write_bytes(b"private-content")
    monkeypatch.setattr(target, "FILE_LIMIT", 1)
    with pytest.raises(target.ProbeError, match="^private_file_limit$") as caught:
        target._check_private_size(tmp_path)
    result = target._error(caught.value)
    assert result == {"type": "ProbeError", "code": "private_file_limit", "private_file_check": {
        "category": category, "regular_file": True, "exceeds_file_limit": True,
    }}
    public = json.dumps(result)
    for secret in (str(tmp_path), "private-content", *([filename] if category == "other" else [])):
        assert secret not in public and hashlib.sha256(secret.encode()).hexdigest() not in public


@pytest.mark.parametrize(("regular", "size"), [(True, 10), (True, 11), (False, 10), (False, 11)])
def test_private_file_limit_uses_one_stat_and_reports_only_first_failure(tmp_path, monkeypatch, regular, size):
    first, second = tmp_path / "raw.xml", tmp_path / "private-second"
    observed = []

    def snapshot(path):
        observed.append(path)
        return SimpleNamespace(st_mode=stat.S_IFREG if regular or path == second else stat.S_IFDIR,
                               st_size=size if path == first else 0)

    monkeypatch.setattr(target, "FILE_LIMIT", 10)
    monkeypatch.setattr(target, "_no_redirect", lambda _: None)
    monkeypatch.setattr(Path, "iterdir", lambda _: iter((first, second)))
    monkeypatch.setattr(Path, "stat", snapshot)
    if regular and size == 10:
        target._check_private_size(tmp_path)
        assert observed == [first, second]
    else:
        with pytest.raises(target.ProbeError, match="^private_file_limit$") as caught:
            target._check_private_size(tmp_path)
        assert observed == [first]
        assert caught.value.private_file_check == {
            "category": "raw.xml", "regular_file": regular, "exceeds_file_limit": size > 10,
        }


def test_private_file_detail_is_optional_and_never_added_to_other_errors():
    detail = {"category": "other", "regular_file": False, "exceeds_file_limit": True}
    assert target._error(target.ProbeError("private_file_limit")) == {
        "type": "ProbeError", "code": "private_file_limit",
    }
    for code in ("private_total_limit", "redirected_path", "command_nonzero"):
        assert target._error(target.ProbeError(code, private_file_check=detail)) == {"type": "ProbeError", "code": code}
    error = RuntimeError("private-error-text")
    error.private_file_check = detail
    assert target._error(error) == {"type": "RuntimeError", "code": "operation_failed"}


def test_private_redirect_rejection_precedes_file_limit_detail(tmp_path, monkeypatch):
    path = tmp_path / "raw.xml"
    path.write_bytes(b"oversize")
    original = Path.is_junction
    monkeypatch.setattr(Path, "is_junction", lambda self: self == path or original(self))
    monkeypatch.setattr(target, "FILE_LIMIT", 1)
    with pytest.raises(target.ProbeError, match="^redirected_path$") as caught:
        target._check_private_size(tmp_path)
    assert target._error(caught.value) == {"type": "ProbeError", "code": "redirected_path"}


@pytest.mark.parametrize(("failed_stage", "filename"), [
    ("inspect_raw", "summary.txt"), ("sampling_decode_raw", "sampling_raw.xml"),
])
def test_completed_decoder_postcheck_failure_keeps_prior_evidence_without_killing(
    probe, monkeypatch, failed_stage, filename,
):
    temp, tools, tracerpt, calls, original = probe
    original_stat = Path.stat
    oversized = None

    class CompletedProcess:
        pid = 123
        returncode = 0

        def poll(self):
            return 0

        def kill(self):
            pytest.fail("decoder already exited; no kill is allowed")

    def snapshot(path, *args, **kwargs):
        if path == oversized and kwargs.get("follow_symlinks", True):
            return SimpleNamespace(st_mode=stat.S_IFREG, st_size=target.FILE_LIMIT + 1)
        return original_stat(path, *args, **kwargs)

    def command(args, private, stage, timeout, report):
        nonlocal oversized
        if stage == failed_stage:
            calls.append(stage)
            oversized = private / filename
            oversized.write_bytes(b"private-decoder-output")
            return run_command(args, private, stage, timeout, report)
        return original(args, private, stage, timeout, report)

    monkeypatch.setattr(target.subprocess, "Popen", lambda *args, **kwargs: CompletedProcess())
    monkeypatch.setattr(Path, "stat", snapshot)
    monkeypatch.setattr(target, "_command", command)
    report = target.run_probe(temp, tools, tracerpt)
    assert report["status"] == "failed" and report["inputs_unchanged"] is True
    assert report["observations"]["command_matches_fixture_in_xml_order"] is True
    assert report["observations"]["process_events"][0]["observed_payload_pid"] == 42
    assert report["error"] == {"type": "ProbeError", "code": "private_file_limit", "private_file_check": {
        "category": filename, "regular_file": True, "exceeds_file_limit": True,
    }}
    capture = report
    if failed_stage == "inspect_raw":
        assert "cpu_sampling_comparison" not in report
        assert not any(stage.startswith("sampling_") for stage in calls)
    else:
        capture = report["cpu_sampling_comparison"]
        assert capture["status"] == "failed" and capture["error"] == report["error"]
        assert "raw_summary" not in capture and report["decoder_documents"]
        assert calls.count("sampling_stop") == 1
    assert capture["commands"][-1] == {
        "stage": failed_stage, "status": "failed", "pid": 123, "returncode": 0,
    }
    assert calls.count("stop") == 1 and capture["cleanup_error"] is None
    assert report["product_build_evidence"] is False
    public = temp / "LoLReplayTool-binary-cache/w/b/evidence/cl-decode-probe.json"
    assert public.stat().st_size <= target.JSON_LIMIT == 64 * 1024
    text = public.read_text(encoding="utf-8")
    assert "private-decoder-output" not in text
    assert hashlib.sha256(b"private-decoder-output").hexdigest() not in text


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


def test_sampling_command_enforces_combined_a_and_b_private_size(tmp_path, monkeypatch):
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
    monkeypatch.setattr(target, "FILE_LIMIT", 10)
    monkeypatch.setattr(target, "TOTAL_LIMIT", 15)
    (tmp_path / "raw.etl").write_bytes(b"a" * 8)
    (tmp_path / "sampling_raw.etl").write_bytes(b"b" * 8)
    report = {"commands": []}
    with pytest.raises(target.ProbeError, match="private_total_limit") as caught:
        target._command(["must-not-execute"], tmp_path, "sampling_compile", 30, report)
    assert process.killed and report["commands"][0]["status"] == "failed"
    assert target._error(caught.value) == {"type": "ProbeError", "code": "private_total_limit"}


@pytest.mark.parametrize("stage", ["stop", "sampling_stop"])
def test_stop_is_attempted_even_if_private_data_is_already_oversized(tmp_path, monkeypatch, stage):
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
    with pytest.raises(target.ProbeError, match="private_file_limit") as caught:
        target._command(["must-not-execute"], tmp_path, stage, 5, report)
    assert report["commands"][0]["returncode"] == 0
    assert report["commands"][0]["status"] == "failed"
    assert caught.value.private_file_check == {
        "category": "other", "regular_file": True, "exceeds_file_limit": True,
    }


def test_cli_refuses_local_trace_before_any_command(monkeypatch):
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setattr(target, "run_probe", lambda *args: pytest.fail("must not start local trace"))
    assert target.main() == 1
