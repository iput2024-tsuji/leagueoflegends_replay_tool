from __future__ import annotations

import csv
import json
import logging
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger("lol_replay.obs_process")


@dataclass(frozen=True)
class OBSProcessInfo:
    pid: int
    executable_path: Path | None
    creation_time: float | None = None


@dataclass(frozen=True)
class OBSProcessLease:
    pid: int
    executable_path: Path
    created_at: float
    process_creation_time: float | None = None


class OBSProcessManager:
    """アプリ管理OBSだけを対象に起動・終了する安全境界。"""

    def __init__(self, obs_dir: str | Path, logger: logging.Logger | None = None) -> None:
        self.obs_dir = Path(obs_dir).resolve()
        self.obs_exe = (self.obs_dir / "bin" / "64bit" / "obs64.exe").resolve()
        self.working_dir = self.obs_exe.parent
        self.logger = logger or LOGGER
        self.lease_path = self.obs_dir / ".lol_replay_obs_lease.json"

    def list_obs_processes(self) -> list[OBSProcessInfo]:
        if os.name != "nt":
            return []
        return self._list_obs_processes_windows()

    def is_managed_process(self, process: OBSProcessInfo) -> bool:
        if process.executable_path is None:
            return False
        try:
            return process.executable_path.resolve() == self.obs_exe
        except Exception:
            return str(process.executable_path).casefold() == str(self.obs_exe).casefold()

    def managed_processes(self) -> list[OBSProcessInfo]:
        return [process for process in self.list_obs_processes() if self.is_managed_process(process)]

    def has_managed_process(self) -> bool:
        return bool(self.managed_processes())

    def unmanaged_processes(self) -> list[OBSProcessInfo]:
        return [process for process in self.list_obs_processes() if not self.is_managed_process(process)]

    def has_unmanaged_process(self) -> bool:
        return bool(self.unmanaged_processes())

    def find_owned_process(self) -> OBSProcessInfo | None:
        lease = self.read_process_lease()
        if lease is None:
            return None
        process = self._find_process_by_pid(lease.pid)
        if process is None:
            self.clear_process_lease()
            return None
        if not self.is_owned_process(process, lease):
            self.clear_process_lease()
            return None
        return process

    def has_owned_process(self) -> bool:
        return self.find_owned_process() is not None

    def kill_stale_managed_processes(self, timeout_sec: float = 3.0) -> list[int]:
        """管理OBSに一致するプロセスだけを終了する。通常版OBSは触らない。"""
        targets = self.managed_processes()
        if not targets:
            return []

        killed = []
        for process in targets:
            if self._terminate_pid(process.pid, force=False):
                killed.append(process.pid)

        deadline = time.monotonic() + timeout_sec
        while time.monotonic() < deadline:
            remaining = [process for process in self.list_obs_processes() if self.is_managed_process(process)]
            if not remaining:
                return killed
            time.sleep(0.1)

        for process in self.list_obs_processes():
            if self.is_managed_process(process):
                self._terminate_pid(process.pid, force=True)
                if process.pid not in killed:
                    killed.append(process.pid)
        self.wait_until_no_managed_processes(timeout_sec=timeout_sec)
        return killed

    def kill_stale_owned_processes(self, timeout_sec: float = 3.0) -> list[int]:
        """前回このアプリが起動したOBSだけをleaseから特定して終了する。"""
        process = self.find_owned_process()
        if process is None:
            return []

        killed = []
        if self._terminate_pid(process.pid, force=False):
            killed.append(process.pid)

        deadline = time.monotonic() + timeout_sec
        while time.monotonic() < deadline:
            if self._find_process_by_pid(process.pid) is None:
                self.clear_process_lease()
                return killed
            time.sleep(0.1)

        if self._find_process_by_pid(process.pid) is not None:
            self._terminate_pid(process.pid, force=True)
            if process.pid not in killed:
                killed.append(process.pid)
        if self._find_process_by_pid(process.pid) is None:
            self.clear_process_lease()
        return killed

    def wait_until_no_managed_processes(self, timeout_sec: float = 5.0, poll_interval: float = 0.2) -> bool:
        """管理OBSプロセスが完全に消えるまでブロッキング待機する。"""
        deadline = time.monotonic() + max(0.0, timeout_sec)
        while True:
            remaining = [process for process in self.list_obs_processes() if self.is_managed_process(process)]
            if not remaining:
                return True
            if time.monotonic() >= deadline:
                self.logger.warning(
                    "Managed OBS processes are still running: %s",
                    ", ".join(str(process.pid) for process in remaining),
                )
                return False
            time.sleep(max(0.05, poll_interval))

    def start_obs(
        self,
        env: dict[str, str] | None = None,
        hidden: bool = True,
        extra_args: list[str] | None = None,
    ) -> subprocess.Popen[Any]:
        if not self.obs_exe.exists():
            raise FileNotFoundError(f"obs64.exe was not found: {self.obs_exe}")

        cmd = [
            str(self.obs_exe),
            "--portable",
            "--multi",
            "--disable-shutdown-check",
            "--disable-updater",
            *(extra_args or []),
        ]
        popen_kwargs: dict[str, Any] = {"cwd": str(self.working_dir), "env": env or os.environ.copy()}
        if hidden:
            popen_kwargs.update(self._hidden_subprocess_kwargs())
        process = subprocess.Popen(cmd, **popen_kwargs)
        self.write_process_lease(process)
        return process

    def latest_log_path(self, since: float | None = None) -> Path | None:
        logs_dir = self.obs_dir / "config" / "obs-studio" / "logs"
        try:
            candidates = [path for path in logs_dir.glob("*.txt") if path.is_file()]
        except Exception:
            return None
        if since is not None:
            candidates = [path for path in candidates if path.stat().st_mtime >= since]
        if not candidates:
            return None
        return max(candidates, key=lambda path: path.stat().st_mtime)

    def latest_log_portable_mode(self, since: float | None = None) -> bool | None:
        log_path = self.latest_log_path(since=since)
        if log_path is None:
            return None
        try:
            text = log_path.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            self.logger.debug("Failed to read OBS log for portable mode check: %s", e)
            return None
        if "Portable mode: true" in text:
            return True
        if "Portable mode: false" in text:
            return False
        return None

    def hide_main_windows(
        self,
        process: subprocess.Popen[Any] | int,
        timeout_sec: float = 3.0,
        poll_interval: float = 0.1,
    ) -> int:
        if os.name != "nt":
            return 0
        pid = int(process.pid if hasattr(process, "pid") else process)
        deadline = time.monotonic() + max(0.0, timeout_sec)
        hidden = 0
        while True:
            hidden += self._hide_windows_by_pid_windows(pid)
            if hidden > 0 or time.monotonic() >= deadline:
                return hidden
            time.sleep(max(0.05, poll_interval))

    def _hide_windows_by_pid_windows(self, pid: int) -> int:
        try:
            import ctypes
            from ctypes import wintypes
        except Exception:
            return 0

        user32 = ctypes.windll.user32
        target_pid = int(pid)
        hidden_count = 0
        enum_windows_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def callback(hwnd: int, _lparam: int) -> bool:
            nonlocal hidden_count
            window_pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(window_pid))
            if int(window_pid.value) == target_pid and user32.IsWindowVisible(hwnd):
                user32.ShowWindow(hwnd, 0)  # SW_HIDE
                hidden_count += 1
            return True

        try:
            user32.EnumWindows(enum_windows_proc(callback), 0)
        except Exception as e:
            self.logger.debug("Failed to hide OBS windows for pid=%s: %s", pid, e)
            return hidden_count
        return hidden_count

    def is_owned_process(self, process: OBSProcessInfo, lease: OBSProcessLease) -> bool:
        if process.pid != lease.pid:
            return False
        if not self.is_managed_process(process):
            return False
        try:
            if lease.executable_path.resolve() != self.obs_exe:
                return False
        except Exception:
            if str(lease.executable_path).casefold() != str(self.obs_exe).casefold():
                return False
        if lease.process_creation_time is None or process.creation_time is None:
            return True
        return abs(float(process.creation_time) - float(lease.process_creation_time)) <= 2.0

    def terminate_process(self, process: subprocess.Popen[Any] | None, timeout_sec: float = 3.0) -> None:
        if process is None:
            return
        if process.poll() is not None:
            self.clear_process_lease(process)
            return
        try:
            process.terminate()
            process.wait(timeout=timeout_sec)
            self.clear_process_lease(process)
            return
        except Exception:
            pass
        try:
            process.kill()
            process.wait(timeout=2)
            self.clear_process_lease(process)
        except Exception as e:
            self.logger.warning("Failed to kill managed OBS process: %s", e, exc_info=True)

    def write_process_lease(self, process: subprocess.Popen[Any]) -> None:
        try:
            self.obs_dir.mkdir(parents=True, exist_ok=True)
            process_info = self._find_process_by_pid(int(process.pid))
            payload = {
                "pid": int(process.pid),
                "executable_path": str(self.obs_exe),
                "created_at": time.time(),
                "process_creation_time": process_info.creation_time if process_info else None,
            }
            with open(self.lease_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
        except Exception as e:
            self.logger.warning("Failed to write OBS process lease: %s", e, exc_info=True)

    def read_process_lease(self) -> OBSProcessLease | None:
        try:
            with open(self.lease_path, encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return None
            return OBSProcessLease(
                pid=int(data["pid"]),
                executable_path=Path(str(data["executable_path"])).resolve(),
                created_at=float(data.get("created_at") or 0.0),
                process_creation_time=float(data["process_creation_time"])
                if data.get("process_creation_time") is not None
                else None,
            )
        except FileNotFoundError:
            return None
        except Exception:
            return None

    def clear_process_lease(self, process: subprocess.Popen[Any] | None = None) -> None:
        lease = self.read_process_lease()
        if process is not None and lease is not None and lease.pid != int(process.pid):
            return
        try:
            self.lease_path.unlink(missing_ok=True)
        except Exception:
            pass

    def isolated_env(self) -> dict[str, str]:
        isolated_root = self.obs_dir / "temp_appdata"
        isolated_roaming = isolated_root / "Roaming"
        isolated_local = isolated_root / "Local"
        isolated_profile = isolated_root / "UserProfile"
        for path in (isolated_root, isolated_roaming, isolated_local, isolated_profile):
            path.mkdir(parents=True, exist_ok=True)

        env = os.environ.copy()
        env["APPDATA"] = str(isolated_roaming)
        env["LOCALAPPDATA"] = str(isolated_local)
        env["USERPROFILE"] = str(isolated_profile)
        return env

    def _list_obs_processes_windows(self) -> list[OBSProcessInfo]:
        command = [
            "wmic",
            "process",
            "where",
            "name='obs64.exe'",
            "get",
            "ProcessId,ExecutablePath,CreationDate",
            "/format:csv",
        ]
        try:
            completed = self._run_hidden(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            if completed.returncode == 0 and completed.stdout.strip():
                return self._parse_wmic_csv(completed.stdout)
        except Exception as e:
            self.logger.debug("WMIC process query failed: %s", e)
        return self._list_obs_processes_powershell()

    def _list_obs_processes_powershell(self) -> list[OBSProcessInfo]:
        script = (
            "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
            "$OutputEncoding = [System.Text.Encoding]::UTF8; "
            "Get-CimInstance Win32_Process -Filter \"Name='obs64.exe'\" "
            "| Select-Object ProcessId,ExecutablePath,CreationDate | ConvertTo-Json -Compress"
        )
        try:
            completed = self._run_hidden(
                ["powershell", "-NoProfile", "-Command", script],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            if completed.returncode != 0 or not completed.stdout.strip():
                return []
            data = json.loads(completed.stdout)
        except Exception as e:
            self.logger.debug("PowerShell process query failed: %s", e)
            return []

        rows = data if isinstance(data, list) else [data]
        result = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            pid = row.get("ProcessId")
            try:
                pid_int = int(pid)
            except Exception:
                continue
            exe_text = str(row.get("ExecutablePath") or "").strip()
            result.append(
                OBSProcessInfo(
                    pid=pid_int,
                    executable_path=Path(exe_text) if exe_text else None,
                    creation_time=_parse_windows_process_creation_time(row.get("CreationDate")),
                )
            )
        return result

    def _parse_wmic_csv(self, text: str) -> list[OBSProcessInfo]:
        result = []
        for row in csv.DictReader(line for line in text.splitlines() if line.strip()):
            pid_text = (row.get("ProcessId") or "").strip()
            if not pid_text.isdigit():
                continue
            exe_text = (row.get("ExecutablePath") or "").strip()
            result.append(
                OBSProcessInfo(
                    pid=int(pid_text),
                    executable_path=Path(exe_text) if exe_text else None,
                    creation_time=_parse_windows_process_creation_time(row.get("CreationDate")),
                )
            )
        return result

    def _find_process_by_pid(self, pid: int) -> OBSProcessInfo | None:
        for process in self.list_obs_processes():
            if process.pid == int(pid):
                return process
        return None

    def _terminate_pid(self, pid: int, force: bool) -> bool:
        command = ["taskkill", "/pid", str(int(pid))]
        if force:
            command.append("/f")
        try:
            self._run_hidden(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
            return True
        except Exception as e:
            self.logger.warning("Failed to terminate OBS pid=%s: %s", pid, e, exc_info=True)
            return False

    def _run_hidden(self, command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[Any]:
        run_kwargs = self._hidden_subprocess_kwargs()
        run_kwargs.update(kwargs)
        return subprocess.run(command, **run_kwargs)

    def _hidden_subprocess_kwargs(self) -> dict[str, Any]:
        if os.name != "nt":
            return {}
        kwargs: dict[str, Any] = {}
        startupinfo = self._startupinfo_hidden()
        if startupinfo is not None:
            kwargs["startupinfo"] = startupinfo
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        if creationflags:
            kwargs["creationflags"] = creationflags
        return kwargs

    def _startupinfo_hidden(self) -> subprocess.STARTUPINFO | None:
        if os.name != "nt" or not hasattr(subprocess, "STARTUPINFO"):
            return None
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= getattr(subprocess, "STARTF_USESHOWWINDOW", 0)
        startupinfo.wShowWindow = 0  # SW_HIDE
        return startupinfo


def _parse_windows_process_creation_time(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.startswith("/Date(") and text.endswith(")/"):
        try:
            return int(text[6:-2].split("+", 1)[0].split("-", 1)[0]) / 1000.0
        except Exception:
            return None
    try:
        return float(text)
    except Exception:
        pass
    try:
        base = text[:14]
        offset_text = text[21:] if len(text) > 21 else ""
        parsed = time.strptime(base, "%Y%m%d%H%M%S")
        timestamp = time.mktime(parsed)
        if offset_text:
            sign = 1 if offset_text.startswith("+") else -1
            minutes = int(offset_text[1:])
            timestamp -= sign * minutes * 60
            if time.localtime().tm_isdst > 0:
                timestamp -= 3600
        return timestamp
    except Exception:
        return None
