"""Pin the local OBS connection used for one recording's status recovery."""

from __future__ import annotations

import ipaddress
import json
import os
from dataclasses import dataclass
from typing import Any

try:
    from .obs_process import OBSProcessInfo, OBSProcessManager, _obs_process_identities_equal
except ImportError:
    from obs_process import OBSProcessInfo, OBSProcessManager, _obs_process_identities_equal


_NATIVE_WINDOWS = os.name == "nt"


def _loopback_peer(client: Any) -> tuple[str, int]:
    peer = client.base_client.ws.sock.getpeername()
    if not isinstance(peer, tuple) or len(peer) not in (2, 4):
        raise ValueError("OBS connection peer is unavailable")
    host, port = peer[:2]
    if not isinstance(host, str) or type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("OBS connection peer is malformed")
    address = ipaddress.ip_address(host)
    if not address.is_loopback:
        raise ValueError("OBS recording recovery requires a loopback peer")
    return str(address), port


def _verify_listener(manager: OBSProcessManager, peer: tuple[str, int], pid: int) -> None:
    host, port = peer
    if not _NATIVE_WINDOWS or type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("OBS listener cannot be verified on this platform or port")
    # Only a validated integer enters this otherwise constant PowerShell command.
    script = (
        "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
        "$OutputEncoding = [System.Text.Encoding]::UTF8; "
        "$ErrorActionPreference = 'Stop'; "
        f"Get-NetTCPConnection -State Listen -LocalPort {port} -ErrorAction Stop "
        "| Select-Object LocalAddress,LocalPort,OwningProcess | ConvertTo-Json -Compress"
    )
    completed = manager._run_hidden(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="strict",
        timeout=5.0,  # Bound shell startup plus listener enumeration; two seconds expired during real-game startup.
    )
    if completed.returncode != 0 or completed.stderr.strip():
        raise ValueError("OBS listener query failed")
    if len(completed.stdout) > 65536:
        raise ValueError("OBS listener query result is too large")
    rows = json.loads(completed.stdout)
    if isinstance(rows, dict):
        rows = [rows]
    if not isinstance(rows, list) or not rows:
        raise ValueError("OBS listener query is empty or malformed")
    matched = False
    peer_address = ipaddress.ip_address(host)
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("OBS listener row is malformed")
        if type(row.get("LocalPort")) is not int or row["LocalPort"] != port:
            raise ValueError("OBS listener port changed")
        if type(row.get("OwningProcess")) is not int or row["OwningProcess"] != pid:
            raise ValueError("OBS listener is not owned by the recording process")
        address_text = row.get("LocalAddress")
        if not isinstance(address_text, str):
            raise ValueError("OBS listener address is malformed")
        address = ipaddress.ip_address(address_text)
        if address == peer_address or address.is_unspecified:
            matched = True
    if not matched:
        raise ValueError("OBS listener does not cover the connected peer")


@dataclass(frozen=True)
class RecordingConnection:
    manager: OBSProcessManager
    process: OBSProcessInfo
    peer: tuple[str, int]

    def verify(self, client: Any | None = None) -> None:
        if client is not None and _loopback_peer(client) != self.peer:
            raise ValueError("OBS recording connection peer changed")
        for check_listener in (True, False):
            current = self.manager.find_owned_process()
            if current is None or not _obs_process_identities_equal(self.process, current):
                raise ValueError("OBS recording process identity changed")
            if check_listener:
                _verify_listener(self.manager, self.peer, self.process.pid)


def capture_recording_connection(client: Any, config: Any) -> RecordingConnection:
    host = config.host
    if not isinstance(host, str) or (
        host != "localhost" and not ipaddress.ip_address(host).is_loopback
    ):
        raise ValueError("OBS recording recovery requires a local endpoint")
    peer = _loopback_peer(client)
    if type(config.port) is not int or peer[1] != config.port:
        raise ValueError("OBS recording connection port differs from configuration")
    manager = OBSProcessManager(config.obs_dir)
    process = manager.find_owned_process()
    if (
        type(process) is not OBSProcessInfo
        or type(process.pid) is not int
        or process.pid <= 0
        or type(process.creation_time_filetime) is not int
        or process.creation_time_filetime <= 0
    ):
        raise ValueError("OBS recording process identity is unavailable")
    context = RecordingConnection(manager, process, peer)
    context.verify(client)
    return context
