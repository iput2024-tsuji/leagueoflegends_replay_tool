from __future__ import annotations

import os
import re
import subprocess
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class LCUConnectionInfo:
    port: int
    password: str
    protocol: str = "https"

    @property
    def base_url(self) -> str:
        return f"{self.protocol}://127.0.0.1:{self.port}"


def parse_lcu_command_line(command_line: str | None) -> LCUConnectionInfo | None:
    if not command_line:
        return None
    port = _command_line_argument(command_line, "app-port")
    password = _command_line_argument(command_line, "remoting-auth-token")
    if not port or not password:
        return None
    try:
        port_number = int(port)
    except (TypeError, ValueError):
        return None
    if port_number <= 0:
        return None
    return LCUConnectionInfo(port=port_number, password=password)


def parse_lcu_lockfile(text: str | None) -> LCUConnectionInfo | None:
    if not text:
        return None
    parts = text.strip().split(":", 4)
    if len(parts) != 5:
        return None
    _name, _pid, port, password, protocol = parts
    try:
        port_number = int(port)
    except (TypeError, ValueError):
        return None
    if port_number <= 0 or not password:
        return None
    return LCUConnectionInfo(
        port=port_number,
        password=password,
        protocol=(protocol or "https").strip().lower(),
    )


class LCUConnectionProvider:
    """Discover and cache the local League Client connection credentials."""

    def __init__(
        self,
        command_line_reader: Callable[[], str | None] | None = None,
        environ: Mapping[str, str] | None = None,
        retry_interval_sec: float = 5.0,
    ) -> None:
        self.command_line_reader = command_line_reader or read_league_client_command_line
        self.environ = environ if environ is not None else os.environ
        self.retry_interval_sec = max(0.0, float(retry_interval_sec))
        self._cached: LCUConnectionInfo | None = None
        self._last_attempt = 0.0

    def get_connection_info(self) -> LCUConnectionInfo | None:
        if self._cached is not None:
            return self._cached

        now = time.monotonic()
        if self._last_attempt and now - self._last_attempt < self.retry_interval_sec:
            return None
        self._last_attempt = now

        command_line = self.command_line_reader()
        info = parse_lcu_command_line(command_line)
        if info is not None:
            self._cached = info
            return info

        for lockfile in self._lockfile_candidates(command_line):
            try:
                info = parse_lcu_lockfile(lockfile.read_text(encoding="utf-8"))
            except (OSError, UnicodeError):
                continue
            if info is not None:
                self._cached = info
                return info
        return None

    def invalidate(self) -> None:
        self._cached = None
        self._last_attempt = 0.0

    def _lockfile_candidates(self, command_line: str | None) -> tuple[Path, ...]:
        candidates: list[Path] = []
        override = self.environ.get("LOL_REPLAY_TOOL_LCU_LOCKFILE")
        if override:
            candidates.append(Path(override).expanduser())

        executable = _command_line_executable(command_line)
        if executable is not None:
            candidates.append(executable.parent / "lockfile")

        candidates.append(Path("C:/Riot Games/League of Legends/lockfile"))
        for variable in ("ProgramFiles", "ProgramFiles(x86)"):
            root = self.environ.get(variable)
            if root:
                candidates.append(Path(root) / "Riot Games" / "League of Legends" / "lockfile")

        unique: list[Path] = []
        seen: set[str] = set()
        for candidate in candidates:
            key = str(candidate).casefold()
            if key not in seen:
                seen.add(key)
                unique.append(candidate)
        return tuple(unique)


def read_league_client_command_line() -> str | None:
    if os.name != "nt":
        return None
    command = (
        "Get-CimInstance Win32_Process -Filter \"Name='LeagueClientUx.exe'\" | "
        "Select-Object -First 1 -ExpandProperty CommandLine"
    )
    kwargs: dict[str, object] = {
        "capture_output": True,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "timeout": 3,
        "check": False,
    }
    create_no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if create_no_window:
        kwargs["creationflags"] = create_no_window
    try:
        completed = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-WindowStyle",
                "Hidden",
                "-Command",
                command,
            ],
            **kwargs,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    output = str(completed.stdout or "").strip()
    return output or None


def _command_line_argument(command_line: str, name: str) -> str | None:
    pattern = rf"(?:^|\s)--{re.escape(name)}(?:=|\s+)(?:\"([^\"]+)\"|(\S+))"
    match = re.search(pattern, command_line)
    if not match:
        return None
    return match.group(1) or match.group(2)


def _command_line_executable(command_line: str | None) -> Path | None:
    if not command_line:
        return None
    match = re.match(r'\s*(?:"([^"]+)"|(\S+))', command_line)
    if not match:
        return None
    value = match.group(1) or match.group(2)
    if not value:
        return None
    return Path(value)
