import json
import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from obsws_python.error import OBSSDKRequestError, OBSSDKTimeoutError
from websocket import WebSocketConnectionClosedException, WebSocketTimeoutException

from src import obs_recording_connection as connection, obs_websocket_client as module
from src.obs_process import OBSProcessInfo


class RawClient:
    def __init__(self, name, calls, *, peer=("127.0.0.1", 4455), status=True):
        self.name = name
        self.calls = calls
        self.status = status
        self.errors = {}
        self.base_client = SimpleNamespace(
            ws=SimpleNamespace(sock=SimpleNamespace(getpeername=lambda: peer))
        )

    def operation(self, name):
        self.calls.append((self.name, name))
        if name in self.errors:
            raise self.errors[name]

    def get_version(self):
        self.operation("get_version")
        return SimpleNamespace(obs_version="32.1.2")

    def get_record_status(self):
        self.operation("get_record_status")
        return SimpleNamespace(output_active=self.status)

    def disconnect(self):
        self.operation("disconnect")

    def stop_record(self):
        self.operation("stop_record")
        return SimpleNamespace(output_path="C:/recordings/current.mkv")

    def send(self, name, *_args, **_kwargs):
        self.operation(name)
        return {}


@pytest.fixture
def recovery(monkeypatch):
    calls = []
    owned = OBSProcessInfo(
        pid=123,
        executable_path=Path("C:/portable/bin/64bit/obs64.exe"),
        creation_time=1000.0,
        creation_time_filetime=10000000000,
    )
    listener_rows = [{"LocalAddress": "0.0.0.0", "LocalPort": 4455, "OwningProcess": 123}]
    manager = SimpleNamespace(process=owned, rows=listener_rows, query_error=None, commands=[])

    def find_owned():
        if manager.query_error is not None:
            raise manager.query_error
        return manager.process

    def run_hidden(command, **kwargs):
        manager.commands.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, json.dumps(manager.rows), "")

    manager.find_owned_process = find_owned
    manager._run_hidden = run_hidden
    monkeypatch.setattr(connection, "OBSProcessManager", lambda _directory: manager)
    monkeypatch.setattr(connection, "_NATIVE_WINDOWS", True)
    config = SimpleNamespace(
        obs=SimpleNamespace(host="localhost", port=4455, password="test-password", obs_dir="C:/portable")
    )
    process_handle = object()
    wrapper = module.ObsWebSocketClient(config=config, obs_process=process_handle)
    old = RawClient("old", calls)
    candidate = RawClient("candidate", calls)
    wrapper.client = old
    monkeypatch.setattr(wrapper, "_apply_record_output_basics", lambda: calls.append("output basics"))
    monkeypatch.setattr(wrapper, "_apply_recording_quality_settings", lambda: calls.append("quality"))
    created = []

    def construct(**kwargs):
        created.append(kwargs)
        return candidate

    monkeypatch.setattr(module.obs, "ReqClient", construct)
    return SimpleNamespace(
        wrapper=wrapper,
        old=old,
        candidate=candidate,
        manager=manager,
        config=config,
        calls=calls,
        created=created,
        process_handle=process_handle,
    )


@pytest.mark.parametrize(
    "error",
    [
        TimeoutError("timeout"),
        ConnectionResetError("reset"),
        OBSSDKTimeoutError("SDK timeout"),
        WebSocketTimeoutException("websocket timeout"),
        WebSocketConnectionClosedException("closed"),
    ],
)
def test_status_recovers_once_without_mutating_recording(recovery, error):
    r = recovery
    r.wrapper.prepare_recording_start()
    assert r.calls == ["output basics", "quality"]
    r.calls.clear()
    r.old.errors["get_record_status"] = error

    assert r.wrapper.is_recording_active() is True
    assert r.wrapper.raw_client is r.candidate
    assert r.wrapper.obs_process is r.process_handle
    assert r.created == [{"host": "127.0.0.1", "port": 4455, "password": "test-password", "timeout": 2.5}]
    assert r.calls == [
        ("old", "get_record_status"),
        ("old", "disconnect"),
        ("candidate", "get_version"),
        ("candidate", "get_record_status"),
    ]
    second = OBSSDKTimeoutError("second timeout")
    r.candidate.errors["get_record_status"] = second
    with pytest.raises(OBSSDKTimeoutError) as caught:
        r.wrapper.get_record_status_details()
    assert caught.value is second
    assert r.wrapper.raw_client is None
    assert len(r.created) == 1
    assert r.calls[-1] == ("candidate", "disconnect")


def test_normal_status_does_not_probe_processes_or_reconnect(recovery):
    r = recovery
    r.wrapper.prepare_recording_start()
    r.manager.query_error = AssertionError("normal status must not query processes")
    assert r.wrapper.is_recording_active() is True
    assert r.created == []


@pytest.mark.parametrize("active", [False, None])
def test_recovery_reports_inactive_or_unknown_without_starting_or_stopping(recovery, active):
    r = recovery
    r.wrapper.prepare_recording_start()
    r.old.errors["get_record_status"] = TimeoutError("lost")
    r.candidate.status = active
    assert r.wrapper.is_recording_active() is active
    assert not any(isinstance(call, tuple) and call[1] in {"StartRecord", "ToggleRecord", "stop_record"} for call in r.calls)


@pytest.mark.parametrize("phase", ["get_version", "get_record_status", "post_identity", "post_peer"])
def test_failed_candidate_is_disconnected_and_original_error_is_preserved(recovery, phase):
    r = recovery
    r.wrapper.prepare_recording_start()
    original = OBSSDKTimeoutError("original status timeout")
    r.old.errors["get_record_status"] = original
    if phase in {"get_version", "get_record_status"}:
        r.candidate.errors[phase] = RuntimeError("candidate failure")
    elif phase == "post_identity":
        def changed():
            r.candidate.operation("get_record_status")
            r.manager.process = replace(r.manager.process, creation_time_filetime=20000000000)
            return SimpleNamespace(output_active=True)
        r.candidate.get_record_status = changed
    else:
        r.candidate.base_client.ws.sock.getpeername = lambda: ("127.0.0.2", 4455)
    with pytest.raises(OBSSDKTimeoutError) as caught:
        r.wrapper.is_recording_active()
    assert caught.value is original
    assert r.wrapper.raw_client is None
    assert r.wrapper.obs_process is r.process_handle
    assert r.calls[-1] == ("candidate", "disconnect")


@pytest.mark.parametrize("change", ["pid", "filetime", "path", "absent", "query", "listener"])
def test_changed_process_or_listener_is_rejected_before_candidate_creation(recovery, change):
    r = recovery
    r.wrapper.prepare_recording_start()
    r.old.errors["get_record_status"] = TimeoutError("lost")
    if change == "pid":
        r.manager.process = replace(r.manager.process, pid=456)
    elif change == "filetime":
        r.manager.process = replace(r.manager.process, creation_time_filetime=20000000000)
    elif change == "path":
        r.manager.process = replace(r.manager.process, executable_path=Path("C:/other/obs64.exe"))
    elif change == "absent":
        r.manager.process = None
    elif change == "query":
        r.manager.query_error = RuntimeError("query denied")
    else:
        r.manager.rows.append({"LocalAddress": "::", "LocalPort": 4455, "OwningProcess": 456})
    with pytest.raises(TimeoutError):
        r.wrapper.is_recording_active()
    assert r.wrapper.raw_client is None
    assert r.created == []


@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_stop_response_loss_discards_socket_and_never_resends(recovery, cleanup_fails):
    r = recovery
    r.wrapper.prepare_recording_start()
    error = OBSSDKTimeoutError("StopRecord response lost")
    r.old.errors["stop_record"] = error
    if cleanup_fails:
        r.old.errors["disconnect"] = RuntimeError("disconnect failed")
    with pytest.raises(OBSSDKTimeoutError) as caught:
        r.wrapper.stop_recording()
    assert caught.value is error
    assert r.wrapper.raw_client is None
    assert r.wrapper._recording_connection is None
    assert r.wrapper.obs_process is r.process_handle
    with pytest.raises(AttributeError):
        r.wrapper.stop_recording()
    assert r.calls.count(("old", "stop_record")) == 1
    assert r.created == []
    if cleanup_fails:
        assert "RuntimeError" in error.__notes__[0]


def test_candidate_cleanup_failure_does_not_mask_initial_transport_error(recovery):
    r = recovery
    r.wrapper.prepare_recording_start()
    initial = TimeoutError("initial")
    r.old.errors["get_record_status"] = initial
    r.old.errors["disconnect"] = RuntimeError("old close failure")
    r.candidate.errors["get_version"] = ValueError("bad response")
    r.candidate.errors["disconnect"] = RuntimeError("candidate close failure")
    with pytest.raises(TimeoutError) as caught:
        r.wrapper.is_recording_active()
    assert caught.value is initial
    assert r.wrapper.raw_client is None
    assert len(initial.__notes__) == 3


def test_obs_request_error_is_not_transport_recovery(recovery):
    r = recovery
    r.wrapper.prepare_recording_start()
    error = OBSSDKRequestError("GetRecordStatus", 500, "request refused")
    r.old.errors["get_record_status"] = error
    with pytest.raises(OBSSDKRequestError) as caught:
        r.wrapper.is_recording_active()
    assert caught.value is error
    assert r.wrapper.raw_client is r.old
    assert r.created == []


@pytest.mark.parametrize("peer", [None, ("192.0.2.1", 4455), ("127.0.0.1", 4456), ("127.0.0.1", True)])
def test_unknown_or_nonlocal_peer_disables_only_recovery(recovery, peer):
    r = recovery
    r.old.base_client.ws.sock.getpeername = lambda: peer
    r.wrapper.prepare_recording_start()
    assert r.wrapper._recording_connection is None
    r.wrapper.start_recording()
    assert r.wrapper.is_recording_active() is True
    r.old.errors["get_record_status"] = TimeoutError("lost")
    with pytest.raises(TimeoutError):
        r.wrapper.is_recording_active()
    assert r.created == []
    assert r.wrapper.raw_client is None


@pytest.mark.parametrize("host", [None, "192.0.2.1", "remote.invalid"])
def test_unknown_or_remote_config_cannot_authorize_loopback_recovery(recovery, host):
    r = recovery
    r.config.obs.host = host
    r.wrapper.prepare_recording_start()
    assert r.wrapper._recording_connection is None
    assert r.wrapper.is_recording_active() is True


def test_ipv6_peer_is_pinned_without_hostname_fallback(recovery):
    r = recovery
    for raw in (r.old, r.candidate):
        raw.base_client.ws.sock.getpeername = lambda: ("::1", 4455, 0, 0)
    r.manager.rows = [{"LocalAddress": "::", "LocalPort": 4455, "OwningProcess": 123}]
    r.wrapper.prepare_recording_start()
    r.old.errors["get_record_status"] = TimeoutError("lost")
    assert r.wrapper.is_recording_active() is True
    # The SDK interpolates host into ws://host:port, so IPv6 needs brackets.
    assert r.created[0]["host"] == "[::1]"


@pytest.mark.parametrize(
    "rows",
    [None, [], "invalid", [True], [{"LocalAddress": "0.0.0.0", "LocalPort": "4455", "OwningProcess": 123}],
     [{"LocalAddress": "0.0.0.0", "LocalPort": 4455, "OwningProcess": True}],
     [{"LocalAddress": "192.0.2.1", "LocalPort": 4455, "OwningProcess": 123}],
     [{"LocalAddress": 0, "LocalPort": 4455, "OwningProcess": 123}]],
)
def test_malformed_or_unmatched_listener_disables_recovery(recovery, rows):
    r = recovery
    r.manager.rows = rows
    r.wrapper.prepare_recording_start()
    assert r.wrapper._recording_connection is None
    assert r.wrapper.raw_client is r.old


@pytest.mark.parametrize("result", ["stderr", "exit", "json", "oversized", "timeout"])
def test_listener_query_failure_is_fail_closed(recovery, result):
    r = recovery
    def query(_command, **_kwargs):
        if result == "timeout":
            raise subprocess.TimeoutExpired("powershell", 5)
        return subprocess.CompletedProcess(
            [], 1 if result == "exit" else 0,
            "x" * 65537 if result == "oversized" else "{" if result == "json" else json.dumps(r.manager.rows),
            "denied" if result == "stderr" else "",
        )
    r.manager._run_hidden = query
    r.wrapper.prepare_recording_start()
    assert r.wrapper._recording_connection is None
    r.wrapper.start_recording()
    assert r.wrapper.is_recording_active() is True
    assert r.wrapper.raw_client is r.old
    error = OBSSDKTimeoutError("later status failure")
    r.old.errors["get_record_status"] = error
    with pytest.raises(OBSSDKTimeoutError) as caught:
        r.wrapper.is_recording_active()
    assert caught.value is error
    assert r.created == []
    assert r.wrapper.raw_client is None


@pytest.mark.parametrize("timeout_at", [1, 2], ids=["before_connect", "after_connect"])
def test_listener_timeout_during_recovery_preserves_error_and_disposes_clients(recovery, timeout_at):
    r = recovery
    r.wrapper.prepare_recording_start()
    r.calls.clear()
    original = OBSSDKTimeoutError("original status failure")
    r.old.errors["get_record_status"] = original
    listener_error = subprocess.TimeoutExpired("powershell", 5)
    original_query = r.manager._run_hidden
    query_count = 0

    def query(command, **kwargs):
        nonlocal query_count
        query_count += 1
        assert kwargs["timeout"] == 5.0
        if query_count == timeout_at:
            raise listener_error
        return original_query(command, **kwargs)

    r.manager._run_hidden = query
    with pytest.raises(OBSSDKTimeoutError) as caught:
        r.wrapper.is_recording_active()
    assert caught.value is original
    assert caught.value.__cause__ is listener_error
    assert r.wrapper.raw_client is None
    assert r.wrapper.obs_process is r.process_handle
    assert r.wrapper._recording_recovery_attempted is True
    expected_calls = [("old", "get_record_status"), ("old", "disconnect")]
    if timeout_at == 2:
        expected_calls += [("candidate", "get_version"), ("candidate", "get_record_status"), ("candidate", "disconnect")]
    assert r.calls == expected_calls
    with pytest.raises(AttributeError):
        r.wrapper.is_recording_active()
    assert query_count == timeout_at
    assert len(r.created) == timeout_at - 1


def test_listener_command_is_hidden_bounded_and_contains_no_connection_secret(recovery):
    r = recovery
    r.wrapper.prepare_recording_start()
    command, kwargs = r.manager.commands[0]
    assert command[:4] == ["powershell", "-NoProfile", "-NonInteractive", "-Command"]
    assert "Get-NetTCPConnection -State Listen -LocalPort 4455" in command[4]
    assert "test-password" not in command[4]
    assert kwargs["timeout"] == 5.0
    assert kwargs["encoding"] == "utf-8"


def test_nonwindows_recovery_is_disabled_without_process_command(recovery, monkeypatch):
    monkeypatch.setattr(connection, "_NATIVE_WINDOWS", False)
    recovery.wrapper.prepare_recording_start()
    assert recovery.wrapper._recording_connection is None
    assert recovery.manager.commands == []


@pytest.mark.parametrize("identity", ["absent", "missing_filetime", "query_error"])
def test_unavailable_initial_identity_preserves_normal_recording(recovery, identity):
    r = recovery
    if identity == "absent":
        r.manager.process = None
    elif identity == "missing_filetime":
        r.manager.process = replace(r.manager.process, creation_time_filetime=None)
    else:
        r.manager.query_error = RuntimeError("query denied")
    r.wrapper.prepare_recording_start()
    assert r.wrapper._recording_connection is None
    r.wrapper.start_recording()
    assert r.wrapper.is_recording_active() is True


@pytest.mark.parametrize("port", [True, "4455", "4455; unexpected-command"])
def test_invalid_port_never_reaches_powershell(recovery, port):
    r = recovery
    r.config.obs.port = port
    r.wrapper.prepare_recording_start()
    assert r.wrapper._recording_connection is None
    assert r.manager.commands == []


def test_candidate_constructor_failure_leaves_old_socket_discarded(recovery, monkeypatch):
    r = recovery
    r.wrapper.prepare_recording_start()
    initial = TimeoutError("initial")
    r.old.errors["get_record_status"] = initial
    def fail_constructor(**_kwargs):
        raise RuntimeError("authentication failed")
    monkeypatch.setattr(module.obs, "ReqClient", fail_constructor)
    with pytest.raises(TimeoutError) as caught:
        r.wrapper.is_recording_active()
    assert caught.value is initial
    assert r.wrapper.raw_client is None
    assert r.wrapper.obs_process is r.process_handle
    assert r.calls[-1] == ("old", "disconnect")


def test_successful_stop_returns_response_path_and_disables_recovery(recovery):
    r = recovery
    r.wrapper.prepare_recording_start()
    assert r.wrapper.stop_recording() == "C:/recordings/current.mkv"
    assert r.wrapper._recording_connection is None
    assert r.wrapper.raw_client is r.old
    assert r.created == []
