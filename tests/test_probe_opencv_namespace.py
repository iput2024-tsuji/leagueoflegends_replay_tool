import hashlib
import io
import json
import struct
import sys
import threading
import time
import zlib
from pathlib import Path

import pytest

from scripts import probe_opencv_namespace as target


def _coff(*names, auxiliary=False):
    strings = bytearray(b"\0" * 4)
    symbols = bytearray()
    for name in names:
        symbols.extend(struct.pack("<IIIhHBB", 0, len(strings), 0, 1, 0, 2, int(auxiliary)))
        strings.extend(name.encode("ascii") + b"\0")
        if auxiliary:
            symbols.extend(b"\0" * 18)
    struct.pack_into("<I", strings, 0, len(strings))
    header = struct.pack("<HHIIIHH", 0x8664, 1, 0, 60, len(symbols) // 18, 0, 0)
    return header + b"\0" * 40 + symbols + strings


def test_coff_primary_symbols_allow_repeated_id_but_not_two_distinct_ids():
    data = _coff("?read@Local@?A0xabcdef12@@", "??_7Local@?A0xabcdef12@@", auxiliary=True)
    assert target.inspect_coff(data)["namespace_id"] == "?a0xabcdef12"
    with pytest.raises(target.ProbeError, match="coff_namespace_not_unique"):
        target.inspect_coff(_coff("?read@?A0xabcdef12@@", "?read@?A0x12345678@@"))


@pytest.mark.parametrize("change,code", [
    (lambda value: value[:19], "coff_size"),
    (lambda value: value + b"\0", "coff_string_bounds"),
    (lambda value: value[:60] + b"\0\0\0\0\xff\xff\xff\xff" + value[68:], "coff_name_bounds"),
    (lambda value: value[:77] + b"\xff" + value[78:], "coff_auxiliary_bounds"),
])
def test_coff_rejects_truncated_or_out_of_bounds_tables(change, code):
    with pytest.raises(target.ProbeError, match=code):
        target.inspect_coff(change(_coff("?read@?A0xabcdef12@@")))


def test_coff_does_not_accept_ids_only_in_unreferenced_raw_bytes():
    data = bytearray(_coff("ordinary"))
    data.extend(b"?A0xabcdef12\0")
    struct.pack_into("<I", data, 78, len(data) - 78)
    with pytest.raises(target.ProbeError, match="coff_namespace_not_unique"):
        target.inspect_coff(bytes(data))


def test_coff_input_is_bounded_before_parsing():
    with pytest.raises(target.ProbeError, match="coff_size"):
        target.inspect_coff(b"\0" * (target.COFF_LIMIT + 1))


def test_json_policy_input_rejects_duplicate_keys():
    with pytest.raises(target.ProbeError, match="duplicate_json_key"):
        json.loads('{"msvc_tools":{},"msvc_tools":{}}', object_pairs_hook=target._unique_json)


@pytest.mark.parametrize("pch,suffix", [(False, b"\0"), (True, b"\1")])
@pytest.mark.parametrize("seed", [0, 0x1EEB6040])
def test_forward_model_uses_lower_absolute_utf8_and_one_pch_byte(tmp_path, pch, suffix, seed):
    source = tmp_path / "MiXeD" / "Probe.cpp"
    payload = str(source.absolute()).lower().encode("utf-8") + suffix
    expected = f"?a0x{zlib.crc32(payload, seed) ^ 0xFFFFFFFF:08x}"
    assert target.predict_namespace(source, pch=pch, seed=seed) == expected
    assert expected != target.predict_namespace(source, pch=not pch, seed=seed)


@pytest.fixture
def probe(tmp_path, monkeypatch):
    host = "1eeb6040"
    pins = {str(index): {"size": index + 1, "sha256": "a" * 64} for index in range(8)}
    monkeypatch.setenv("GITHUB_SHA", "a" * 40)
    for key in target.COMPILER_FLAG_ENVIRONMENT:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(target, "_select_msvc_instance", lambda policy: (tmp_path, tmp_path / "cl.exe", pins))
    monkeypatch.setattr(target, "_capture_msvc_tools", lambda compiler: pins.copy())
    monkeypatch.setattr(target, "_computer_name_crc32", lambda: host)
    calls = []

    def command(args, cwd, **kwargs):
        calls.append(args)
        if args[0] == "git":
            return {"returncode": 0}, b"a" * 40 + b"\n"
        assert all(flag in args for flag in ("/c", "/Od", "/Z7", "/TP", "/X"))
        assert not any(flag.lower() == "/link" for flag in args)
        mode = "deterministic" if "/experimental:deterministic" in args else "control"
        obj = Path(next(flag[3:] for flag in args if flag.startswith("/Fo")))
        source = Path(args[-1])
        if "/Ycprobe.h" in args:
            assert source.read_bytes() == target.CREATOR
            Path(next(flag[3:] for flag in args if flag.startswith("/Fp"))).write_bytes(b"fixture PCH")
            obj.write_bytes(_coff("creator"))
        else:
            assert source.read_bytes() == target.SOURCE
            namespace = target.predict_namespace(source, pch="/Yuprobe.h" in args,
                                                 seed=0 if mode == "deterministic" else int(host, 16))
            obj.write_bytes(_coff(f"?read@Local@{namespace}@@", "<lambda_" + "a" * 32 + ">"))
        return {"returncode": 0, "diagnostic_codes": [], "unknown_option_D9002": False,
                "output_size": 0, "output_sha256": hashlib.sha256(b"").hexdigest()}, b""

    monkeypatch.setattr(target, "_command", command)
    return tmp_path, calls, command


def test_probe_compares_all_six_consumers_per_mode_and_keeps_private_data_out(probe, monkeypatch):
    temp, calls, _ = probe
    monkeypatch.setenv("COMPUTERNAME", "PRIVATE-NAME-NOT-AN-INPUT")
    monkeypatch.setenv("UNRELATED_SECRET", "private-env-value")
    report = target.run_probe(temp)
    assert report["status"] == "completed"
    assert len(calls) == 19 and len(report["commands"]) == 18
    assert report["summary"] == {"consumer_count": 12, "control_all_host_seed": True,
                                  "deterministic_all_zero_seed": True,
                                  "deterministic_accepted_without_D9002": True,
                                  "lambda_ids_equal_by_path_and_pch": True}
    assert report["inputs_unchanged"] and report["sources_unchanged"] and report["tools_unchanged"]
    assert len(report["tools_before"]) == len(report["tools_after"]) == 8
    assert report["computer_name_crc32"]["crc32"] == "1eeb6040"
    assert report["actual_commit_sha"] == report["github_sha"] == "a" * 40
    assert {row["pch"] for row in report["observations"]} == {True, False}
    assert len({row["source_path_lower_utf8_sha256"] for row in report["observations"]}) == 3
    assert all(row["source"]["sha256"] == hashlib.sha256(target.SOURCE).hexdigest() for row in report["observations"])
    assert not report["product_build_evidence"] and not report["linked"]
    root = temp / target.OUTPUT_NAME
    saved = (root / "report.json").read_text()
    assert "PRIVATE-NAME" not in saved and "private-env-value" not in saved
    assert all(not Path(item["path"]).is_absolute() for item in report["files"])
    assert all((root / item["path"]).stat().st_size == item["size"] for item in report["files"])
    assert all(item["artifact_scope"] == "private_hash_only" for item in report["files"] if item["path"].endswith(".pch"))
    assert not list(root.rglob("*.exe"))


def test_D9002_is_observed_as_an_unaccepted_option_even_when_compiler_returns_zero(probe, monkeypatch):
    temp, _, command = probe

    def ignored(args, cwd, **kwargs):
        entry, output = command(args, cwd, **kwargs)
        if "/experimental:deterministic" in args:
            entry.update(diagnostic_codes=["D9002"], unknown_option_D9002=True)
        return entry, output

    monkeypatch.setattr(target, "_command", ignored)
    report = target.run_probe(temp)
    assert report["status"] == "completed"
    assert report["summary"]["deterministic_accepted_without_D9002"] is False
    assert all(not row["option_accepted_without_D9002"] for row in report["observations"] if row["mode"] == "deterministic")


def test_command_exception_is_sanitized_and_after_receipts_are_preserved(probe, monkeypatch):
    temp, _, command = probe

    def failed(args, cwd, **kwargs):
        if args[0] != "git":
            raise OSError("private-hostname-private-output")
        return command(args, cwd, **kwargs)

    monkeypatch.setattr(target, "_command", failed)
    report = target.run_probe(temp)
    assert report["status"] == "failed"
    assert report["errors"] == [{"code": "probe_failed", "exception_type": "OSError"}]
    assert report["tools_unchanged"] and report["sources_unchanged"]
    assert "private-hostname" not in json.dumps(report)


def test_checkout_mismatch_refuses_compilation(probe, monkeypatch):
    temp, calls, _ = probe
    monkeypatch.setenv("GITHUB_SHA", "b" * 40)
    report = target.run_probe(temp)
    assert report["status"] == "failed" and len(calls) == 1
    assert report["errors"][0]["code"] == "checkout_sha_mismatch"


def test_compile_nonzero_preserves_original_code_and_bounded_command_receipt(probe, monkeypatch):
    temp, _, command = probe

    def failed(args, cwd, **kwargs):
        entry, output = command(args, cwd, **kwargs)
        if args[0] != "git":
            entry["returncode"] = 2
        return entry, output

    monkeypatch.setattr(target, "_command", failed)
    report = target.run_probe(temp)
    assert report["status"] == "failed" and report["commands"][0]["returncode"] == 2
    assert report["errors"][0]["code"] == "compile_nonzero"
    assert report["tools_unchanged"] and report["sources_unchanged"]


def test_inherited_flags_refuse_probe_before_any_process(probe, monkeypatch):
    temp, calls, _ = probe
    monkeypatch.setenv("CL", "/link private-env-value")
    report = target.run_probe(temp)
    assert report["status"] == "failed" and not calls
    assert report["errors"][0]["code"] == "inherited_compiler_flags"
    assert "private-env-value" not in json.dumps(report)


def test_os_crc_failure_is_sanitized_and_compilation_is_not_attempted(probe, monkeypatch):
    temp, calls, _ = probe

    def failed():
        raise OSError("private-hostname")

    monkeypatch.setattr(target, "_computer_name_crc32", failed)
    report = target.run_probe(temp)
    assert report["status"] == "failed" and len(calls) == 1
    assert report["computer_name_crc32"]["status"] == "error"
    assert "private-hostname" not in json.dumps(report)


def test_changed_tool_receipt_fails_after_comparison(probe, monkeypatch):
    temp, _, _ = probe
    monkeypatch.setattr(target, "_capture_msvc_tools", lambda compiler: {})
    report = target.run_probe(temp)
    assert report["status"] == "failed" and report["tools_unchanged"] is False


def test_fresh_output_refuses_existing_evidence(probe):
    temp, _, _ = probe
    root = temp / target.OUTPUT_NAME
    root.mkdir()
    (root / "report.json").write_bytes(b"preserve")
    with pytest.raises(FileExistsError):
        target.run_probe(temp)
    assert (root / "report.json").read_bytes() == b"preserve"


def test_read_and_output_limits_reject_oversized_files(tmp_path, monkeypatch):
    source = tmp_path / "bounded"
    source.write_bytes(b"12345")
    with pytest.raises(target.ProbeError, match="input_file_limit"):
        target._read(source, 4)
    monkeypatch.setattr(target, "FILE_LIMIT", 4)
    with pytest.raises(target.ProbeError, match="output_file_limit"):
        target._check_sizes(tmp_path)


@pytest.mark.parametrize("data,limit,code", [(b"D9002 private compiler text", 64, None),
                                             (b"123456789", 8, "command_output_limit")])
def test_command_reads_bounded_pipe_and_retains_only_codes_and_hash(tmp_path, monkeypatch, data, limit, code):
    class Process:
        stdout = io.BytesIO(data)
        returncode = 0

        def poll(self):
            return 0

    calls = []

    def spawn(args, **kwargs):
        calls.append(kwargs)
        return Process()

    monkeypatch.setattr(target.subprocess, "Popen", spawn)
    monkeypatch.setattr(target, "LOG_LIMIT", limit)
    if code:
        with pytest.raises(target.ProbeError, match=code):
            target._command(["cl.exe"], tmp_path)
    else:
        entry, output = target._command(["cl.exe"], tmp_path)
        assert output == data and entry["unknown_option_D9002"] is True
        assert entry["diagnostic_codes"] == ["D9002"]
        assert "private compiler" not in json.dumps(entry)
    assert not list(tmp_path.iterdir())
    assert calls[0]["stdout"] == target.subprocess.PIPE
    assert calls[0]["bufsize"] == 0


def test_owned_child_timeout_does_not_close_pipe_concurrently_with_blocked_reader(tmp_path, monkeypatch):
    released = threading.Event()
    started = threading.Event()
    closed = threading.Event()
    real_thread = threading.Thread
    readers = []

    class ShortJoinThread(real_thread):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            readers.append(self)

        def join(self, timeout=None):
            return super().join(timeout=0.01)

    class Pipe:
        def read(self, size):
            started.set()
            assert released.wait(2)
            return b""

        def close(self):
            assert released.is_set()
            closed.set()

    class Process:
        stdout = Pipe()
        stopped = False

        def poll(self):
            return 0 if self.stopped else None

        def kill(self):
            self.stopped = True

        def wait(self, **kwargs):
            return 0

    child = Process()
    monkeypatch.setattr(target.threading, "Thread", ShortJoinThread)
    monkeypatch.setattr(target.subprocess, "Popen", lambda args, **kwargs: child)
    beginning = time.monotonic()
    try:
        with pytest.raises(target.ProbeError, match="command_timeout"):
            target._command(["cl.exe"], tmp_path, timeout=0)
        assert started.is_set() and child.stopped and not closed.is_set()
        assert time.monotonic() - beginning < 1
    finally:
        released.set()
        for reader in readers:
            real_thread.join(reader, timeout=2)
    assert closed.is_set()


def test_exited_child_with_still_open_pipe_reports_sanitized_reader_failure(tmp_path, monkeypatch):
    released = threading.Event()
    closed = threading.Event()
    real_thread = threading.Thread
    readers = []

    class ShortJoinThread(real_thread):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            readers.append(self)

        def join(self, timeout=None):
            return super().join(timeout=0.01)

    class Pipe:
        def read(self, size):
            assert released.wait(2)
            return b""

        def close(self):
            assert released.is_set()
            closed.set()

    class Process:
        stdout = Pipe()

        def poll(self):
            return 0

    monkeypatch.setattr(target.threading, "Thread", ShortJoinThread)
    monkeypatch.setattr(target.subprocess, "Popen", lambda args, **kwargs: Process())
    try:
        with pytest.raises(target.ProbeError, match="command_output_read"):
            target._command(["cl.exe"], tmp_path)
        assert not closed.is_set()
    finally:
        released.set()
        for reader in readers:
            real_thread.join(reader, timeout=2)
    assert closed.is_set()


def test_nonwindows_main_has_no_native_or_filesystem_side_effects(monkeypatch, capsys):
    monkeypatch.setattr(target, "os", type("OS", (), {"name": "posix"})())
    monkeypatch.setattr(sys, "argv", ["probe"])
    monkeypatch.setattr(target, "run_probe", lambda temp: pytest.fail("must not run"))
    assert target.main() == 1
    assert "Windows CI" in capsys.readouterr().out


@pytest.mark.parametrize("event", ["pull_request", "workflow_dispatch"])
def test_main_accepts_only_explicit_diagnostic_ci_job(probe, monkeypatch, event):
    temp, _, _ = probe
    monkeypatch.setattr(sys, "argv", ["probe"])
    monkeypatch.setattr(target, "os", type("OS", (), {"name": "nt", "environ": {
        "GITHUB_ACTIONS": "true", "GITHUB_EVENT_NAME": event, "OPENCV_NAMESPACE_PROBE_ONLY": "1",
        "RUNNER_TEMP": str(temp),
    }})())
    monkeypatch.setattr(target, "run_probe", lambda path: {"status": "completed"})
    assert target.main() == 0
