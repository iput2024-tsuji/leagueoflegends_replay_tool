from __future__ import annotations

import subprocess
import threading
from pathlib import Path
from typing import Any

import pytest

from src.player import ClipExportWorker


class FakeClipProcess:
    def __init__(
        self,
        output_path: Path,
        *,
        lines: list[str] | None = None,
        return_code: int = 0,
        initial_output: bytes | None = None,
        output_on_wait: bytes | None = None,
        stdout_error: Exception | None = None,
        wait_side_effects: list[int | BaseException] | None = None,
        terminate_error: Exception | None = None,
    ) -> None:
        self.output_path = output_path
        self.stdout = self._iter_stdout(lines or [], stdout_error)
        self.return_code = return_code
        self.output_on_wait = output_on_wait
        self.wait_side_effects = list(wait_side_effects or [])
        self.terminate_error = terminate_error
        self.terminate_calls = 0
        self.wait_calls = 0
        self.wait_timeouts: list[float | None] = []
        self._terminated = False
        self._waited = False
        if initial_output is not None:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(initial_output)

    @staticmethod
    def _iter_stdout(lines: list[str], error: Exception | None):
        yield from lines
        if error is not None:
            raise error

    def poll(self) -> int | None:
        if self._terminated:
            return -15
        if self._waited:
            return self.return_code
        return None

    def terminate(self) -> None:
        self.terminate_calls += 1
        if self.terminate_error is not None:
            raise self.terminate_error
        self._terminated = True

    def wait(self, timeout: float | None = None) -> int:
        self.wait_calls += 1
        self.wait_timeouts.append(timeout)
        if self.wait_side_effects:
            result = self.wait_side_effects.pop(0)
            if isinstance(result, BaseException):
                raise result
            self._waited = True
            return result
        self._waited = True
        if self._terminated:
            return -15
        if self.output_on_wait is not None:
            self.output_path.parent.mkdir(parents=True, exist_ok=True)
            self.output_path.write_bytes(self.output_on_wait)
        return self.return_code


class BlockingStdout:
    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.exited = threading.Event()
        self.reader_is_daemon: bool | None = None

    def __iter__(self):
        return self

    def __next__(self):
        self.reader_is_daemon = threading.current_thread().daemon
        self.entered.set()
        self.release.wait(5.0)
        self.exited.set()
        raise StopIteration


class BarrierWaitProcess(FakeClipProcess):
    def __init__(self, output_path: Path) -> None:
        super().__init__(output_path, terminate_error=OSError("terminate failed"))
        self.wait_entered = threading.Event()
        self.wait_release = threading.Event()

    def wait(self, timeout: float | None = None) -> int:
        self.wait_calls += 1
        self.wait_timeouts.append(timeout)
        self.wait_entered.set()
        self.wait_release.wait(5.0)
        raise subprocess.TimeoutExpired("unused-ffmpeg.exe", timeout)


class FakeClipProcessFactory:
    def __init__(self, output_path: Path, scenarios: list[dict[str, Any] | BaseException]) -> None:
        self.output_path = output_path
        self.scenarios = list(scenarios)
        self.commands: list[list[str]] = []
        self.processes: list[FakeClipProcess] = []

    def __call__(self, command: list[str]) -> FakeClipProcess:
        self.commands.append(list(command))
        if not self.scenarios:
            raise AssertionError("unexpected FFmpeg process attempt")
        scenario = self.scenarios.pop(0)
        if isinstance(scenario, BaseException):
            raise scenario
        if scenario.pop("require_output_absent", False):
            assert not self.output_path.exists()
        process = FakeClipProcess(self.output_path, **scenario)
        self.processes.append(process)
        return process


def _make_worker(
    tmp_path: Path,
    scenarios: list[dict[str, Any] | BaseException],
    *,
    output_path: Path | None = None,
    start_sec: float = 10.0,
    end_sec: float = 20.0,
) -> tuple[ClipExportWorker, FakeClipProcessFactory, Path]:
    output = output_path or tmp_path / "clips" / "result.mp4"
    factory = FakeClipProcessFactory(output, scenarios)
    worker = ClipExportWorker(
        "unused-ffmpeg.exe",
        tmp_path / "input.mp4",
        output,
        start_sec=start_sec,
        end_sec=end_sec,
        process_factory=factory,
    )
    return worker, factory, output


def _capture_signals(worker: ClipExportWorker) -> dict[str, list[Any]]:
    captured: dict[str, list[Any]] = {
        "progress": [],
        "warning": [],
        "finished": [],
        "failed": [],
    }
    worker.progress.connect(lambda percent, message: captured["progress"].append((percent, message)))
    worker.warning.connect(captured["warning"].append)
    worker.export_finished.connect(captured["finished"].append)
    worker.export_failed.connect(captured["failed"].append)
    return captured


def _selected_encoder(command: list[str]) -> str:
    return command[command.index("-c:v") + 1]


def test_clip_export_success_reports_progress_and_keeps_completed_file(tmp_path):
    worker, factory, output = _make_worker(
        tmp_path,
        [
            {
                "lines": [
                    "out_time_us=2500000\n",
                    "progress=continue\n",
                    "out_time=00:00:09.500\n",
                    "out_time_ms=11000000\n",
                ],
                "output_on_wait": b"completed clip",
            }
        ],
    )
    signals = _capture_signals(worker)

    worker.run()

    assert [_selected_encoder(command) for command in factory.commands] == ["h264_nvenc"]
    assert signals["warning"] == []
    assert signals["failed"] == []
    assert signals["finished"] == [str(output)]
    assert (25, "出力中... 25% (h264_nvenc)") in signals["progress"]
    assert (95, "出力中... 95% (h264_nvenc)") in signals["progress"]
    assert (100, "出力中... 100% (h264_nvenc)") in signals["progress"]
    assert signals["progress"][-1] == (100, "クリップ出力が完了しました。")
    assert output.read_bytes() == b"completed clip"
    assert factory.processes[0].terminate_calls == 0
    assert factory.processes[0].wait_calls == 1
    assert worker.process is None


@pytest.mark.parametrize(
    "line",
    [
        "",
        "not-a-progress-line",
        "progress=end",
        "out_time_us=not-a-number",
        "out_time_us=nan",
        "out_time_us=inf",
        "out_time_us=-inf",
        "out_time=broken",
        "out_time=00:00:nan",
        "out_time=00:00:inf",
        "out_time=01:02",
        "unknown_time=1000000",
    ],
)
def test_clip_export_ignores_invalid_progress_lines(tmp_path, line):
    worker, _factory, _output = _make_worker(tmp_path, [])
    signals = _capture_signals(worker)

    worker._handle_progress_line(line, "h264_nvenc")

    assert signals["progress"] == []


def test_clip_export_falls_back_to_libx264_after_nvenc_failure(tmp_path):
    worker, factory, output = _make_worker(
        tmp_path,
        [
            {
                "lines": ["NVENC initialization failed\n"],
                "return_code": 1,
                "initial_output": b"partial nvenc output",
            },
            {
                "require_output_absent": True,
                "lines": ["out_time_us=5000000\n"],
                "output_on_wait": b"completed cpu clip",
            },
        ],
    )
    signals = _capture_signals(worker)

    worker.run()

    assert [_selected_encoder(command) for command in factory.commands] == ["h264_nvenc", "libx264"]
    assert signals["warning"] == ["H.264 NVENC が使えないため、CPUエンコード(libx264)へ切り替えます。"]
    assert any("CPUエンコードへ切り替えます" in message for _percent, message in signals["progress"])
    assert (50, "出力中... 50% (libx264)") in signals["progress"]
    assert signals["failed"] == []
    assert signals["finished"] == [str(output)]
    assert output.read_bytes() == b"completed cpu clip"


def test_clip_export_cancel_terminates_once_and_deletes_partial_output(tmp_path):
    worker, factory, output = _make_worker(
        tmp_path,
        [
            {
                "lines": ["out_time_us=1000000\n"],
                "stdout_error": OSError("stdout closed during cancel"),
                "initial_output": b"partial output",
                "output_on_wait": b"must not be written after termination",
            }
        ],
    )
    signals = _capture_signals(worker)

    def cancel_repeatedly(percent: int, _message: str) -> None:
        if percent == 10:
            worker.cancel()
            worker.cancel()

    worker.progress.connect(cancel_repeatedly)

    worker.run()
    worker.cancel()

    process = factory.processes[0]
    assert process.terminate_calls == 1
    assert process.wait_calls == 1
    assert signals["finished"] == []
    assert signals["failed"] == ["クリップ出力をキャンセルしました。"]
    assert not output.exists()
    assert worker.process is None


@pytest.mark.parametrize(
    "stdout_error",
    [OSError("stdout iteration failed"), FileNotFoundError("stdout pipe disappeared")],
    ids=["os-error", "file-not-found-after-launch"],
)
def test_clip_export_stops_and_reaps_process_before_fallback_after_stdout_error(tmp_path, stdout_error):
    worker, factory, output = _make_worker(
        tmp_path,
        [
            {
                "lines": ["out_time_us=1000000\n"],
                "stdout_error": stdout_error,
                "initial_output": b"partial output",
            },
            {
                "require_output_absent": True,
                "output_on_wait": b"completed fallback clip",
            },
        ],
    )
    signals = _capture_signals(worker)

    worker.run()

    failed_process = factory.processes[0]
    assert failed_process.terminate_calls == 1
    assert failed_process.wait_calls == 1
    assert failed_process.wait_timeouts == [worker.PROCESS_EXIT_TIMEOUT_SEC]
    assert [_selected_encoder(command) for command in factory.commands] == ["h264_nvenc", "libx264"]
    assert signals["failed"] == []
    assert signals["finished"] == [str(output)]
    assert output.read_bytes() == b"completed fallback clip"


def test_clip_export_cancel_fails_closed_when_cached_terminate_failed(tmp_path):
    worker, factory, output = _make_worker(
        tmp_path,
        [
            {
                "lines": ["out_time_us=1000000\n", "still_running=yes\n"],
                "terminate_error": OSError("terminate failed"),
                "initial_output": b"possibly active partial output",
            }
        ],
    )
    signals = _capture_signals(worker)
    worker.progress.connect(lambda percent, _message: worker.cancel() if percent == 10 else None)

    worker.run()

    process = factory.processes[0]
    assert process.terminate_calls == 1
    assert process.wait_calls == 0
    assert [_selected_encoder(command) for command in factory.commands] == ["h264_nvenc"]
    assert signals["finished"] == []
    assert len(signals["failed"]) == 1
    assert "キャンセル後に安全な終了を確認できませんでした" in signals["failed"][0]
    assert "FFmpegプロセスが残っている可能性があります" in signals["failed"][0]
    assert output.read_bytes() == b"possibly active partial output"


def test_clip_export_cancel_fails_closed_when_exit_confirmation_times_out(tmp_path):
    worker, factory, output = _make_worker(
        tmp_path,
        [
            {
                "lines": ["out_time_us=1000000\n", "still_running=yes\n"],
                "wait_side_effects": [subprocess.TimeoutExpired("unused-ffmpeg.exe", 5.0)],
                "initial_output": b"possibly active partial output",
            }
        ],
    )
    signals = _capture_signals(worker)
    worker.progress.connect(lambda percent, _message: worker.cancel() if percent == 10 else None)

    worker.run()

    process = factory.processes[0]
    assert process.terminate_calls == 1
    assert process.wait_calls == 1
    assert process.wait_timeouts == [worker.PROCESS_EXIT_TIMEOUT_SEC]
    assert [_selected_encoder(command) for command in factory.commands] == ["h264_nvenc"]
    assert signals["finished"] == []
    assert len(signals["failed"]) == 1
    assert "キャンセル後に安全な終了を確認できませんでした" in signals["failed"][0]
    assert "FFmpegプロセスが残っている可能性があります" in signals["failed"][0]
    assert output.read_bytes() == b"possibly active partial output"


def test_async_cancel_returns_while_stdout_reader_is_blocked_and_terminate_fails(qtbot, tmp_path):
    output = tmp_path / "clips" / "result.mp4"
    blocking_stdout = BlockingStdout()
    process = FakeClipProcess(output, terminate_error=OSError("terminate failed"))
    process.stdout = blocking_stdout
    commands: list[list[str]] = []

    def process_factory(command: list[str]) -> FakeClipProcess:
        commands.append(list(command))
        output.write_bytes(b"possibly active partial output")
        return process

    worker = ClipExportWorker(
        "unused-ffmpeg.exe",
        tmp_path / "input.mp4",
        output,
        start_sec=10.0,
        end_sec=20.0,
        process_factory=process_factory,
    )
    signals = _capture_signals(worker)

    try:
        with qtbot.waitSignal(worker.finished, timeout=3000):
            worker.start()
            assert blocking_stdout.entered.wait(1.0)
            worker.cancel()
    finally:
        blocking_stdout.release.set()

    assert blocking_stdout.exited.wait(1.0)
    qtbot.waitUntil(lambda: len(signals["failed"]) == 1, timeout=1000)
    assert blocking_stdout.reader_is_daemon is True
    assert process.terminate_calls == 1
    assert process.wait_calls == 0
    assert [_selected_encoder(command) for command in commands] == ["h264_nvenc"]
    assert signals["finished"] == []
    assert "キャンセル後に安全な終了を確認できませんでした" in signals["failed"][0]
    assert "FFmpegプロセスが残っている可能性があります" in signals["failed"][0]
    assert output.read_bytes() == b"possibly active partial output"


def test_async_cancel_returns_while_stdout_reader_is_blocked_and_exit_confirmation_times_out(qtbot, tmp_path):
    output = tmp_path / "clips" / "result.mp4"
    blocking_stdout = BlockingStdout()
    process = FakeClipProcess(
        output,
        wait_side_effects=[subprocess.TimeoutExpired("unused-ffmpeg.exe", 5.0)],
    )
    process.stdout = blocking_stdout
    commands: list[list[str]] = []

    def process_factory(command: list[str]) -> FakeClipProcess:
        commands.append(list(command))
        output.write_bytes(b"possibly active partial output")
        return process

    worker = ClipExportWorker(
        "unused-ffmpeg.exe",
        tmp_path / "input.mp4",
        output,
        start_sec=10.0,
        end_sec=20.0,
        process_factory=process_factory,
    )
    signals = _capture_signals(worker)

    try:
        with qtbot.waitSignal(worker.finished, timeout=3000):
            worker.start()
            assert blocking_stdout.entered.wait(1.0)
            worker.cancel()
    finally:
        blocking_stdout.release.set()

    assert blocking_stdout.exited.wait(1.0)
    qtbot.waitUntil(lambda: len(signals["failed"]) == 1, timeout=1000)
    assert blocking_stdout.reader_is_daemon is True
    assert process.terminate_calls == 1
    assert process.wait_calls == 1
    assert process.wait_timeouts == [worker.PROCESS_EXIT_TIMEOUT_SEC]
    assert [_selected_encoder(command) for command in commands] == ["h264_nvenc"]
    assert signals["finished"] == []
    assert "キャンセル後に安全な終了を確認できませんでした" in signals["failed"][0]
    assert "FFmpegプロセスが残っている可能性があります" in signals["failed"][0]
    assert output.read_bytes() == b"possibly active partial output"


def test_async_cancel_between_check_and_bounded_wait_fails_closed(qtbot, tmp_path):
    output = tmp_path / "clips" / "result.mp4"
    process = BarrierWaitProcess(output)
    commands: list[list[str]] = []

    def process_factory(command: list[str]) -> BarrierWaitProcess:
        commands.append(list(command))
        output.write_bytes(b"possibly active partial output")
        return process

    worker = ClipExportWorker(
        "unused-ffmpeg.exe",
        tmp_path / "input.mp4",
        output,
        start_sec=10.0,
        end_sec=20.0,
        process_factory=process_factory,
    )
    signals = _capture_signals(worker)

    try:
        with qtbot.waitSignal(worker.finished, timeout=3000):
            worker.start()
            assert process.wait_entered.wait(1.0)
            worker.cancel()
            process.wait_release.set()
    finally:
        process.wait_release.set()

    qtbot.waitUntil(lambda: len(signals["failed"]) == 1, timeout=1000)
    assert process.terminate_calls == 1
    assert process.wait_calls == 1
    assert process.wait_timeouts == [worker.PROCESS_POLL_INTERVAL_SEC]
    assert [_selected_encoder(command) for command in commands] == ["h264_nvenc"]
    assert signals["finished"] == []
    assert "キャンセル後に安全な終了を確認できませんでした" in signals["failed"][0]
    assert "FFmpegプロセスが残っている可能性があります" in signals["failed"][0]
    assert output.read_bytes() == b"possibly active partial output"


def test_clip_export_stops_and_reaps_process_before_fallback_after_wait_error(tmp_path):
    worker, factory, output = _make_worker(
        tmp_path,
        [
            {
                "wait_side_effects": [OSError("wait failed"), -15],
                "initial_output": b"partial output",
            },
            {
                "require_output_absent": True,
                "output_on_wait": b"completed fallback clip",
            },
        ],
    )
    signals = _capture_signals(worker)

    worker.run()

    failed_process = factory.processes[0]
    assert failed_process.terminate_calls == 1
    assert failed_process.wait_calls == 2
    assert failed_process.wait_timeouts == [worker.PROCESS_POLL_INTERVAL_SEC, worker.PROCESS_EXIT_TIMEOUT_SEC]
    assert [_selected_encoder(command) for command in factory.commands] == ["h264_nvenc", "libx264"]
    assert signals["failed"] == []
    assert signals["finished"] == [str(output)]
    assert output.read_bytes() == b"completed fallback clip"


def test_clip_export_fails_closed_when_process_exit_confirmation_times_out(tmp_path):
    worker, factory, output = _make_worker(
        tmp_path,
        [
            {
                "stdout_error": OSError("stdout iteration failed"),
                "wait_side_effects": [subprocess.TimeoutExpired("unused-ffmpeg.exe", 5.0)],
                "initial_output": b"possibly active partial output",
            }
        ],
    )
    signals = _capture_signals(worker)

    worker.run()

    process = factory.processes[0]
    assert process.terminate_calls == 1
    assert process.wait_calls == 1
    assert process.wait_timeouts == [worker.PROCESS_EXIT_TIMEOUT_SEC]
    assert [_selected_encoder(command) for command in factory.commands] == ["h264_nvenc"]
    assert signals["finished"] == []
    assert len(signals["failed"]) == 1
    assert "fallbackを中止" in signals["failed"][0]
    assert "FFmpegプロセスが残っている可能性があります" in signals["failed"][0]
    assert output.read_bytes() == b"possibly active partial output"


def test_clip_export_fails_closed_when_process_exit_confirmation_raises(tmp_path):
    worker, factory, output = _make_worker(
        tmp_path,
        [
            {
                "stdout_error": OSError("stdout iteration failed"),
                "wait_side_effects": [OSError("exit confirmation failed")],
                "initial_output": b"possibly active partial output",
            }
        ],
    )
    signals = _capture_signals(worker)

    worker.run()

    process = factory.processes[0]
    assert process.terminate_calls == 1
    assert process.wait_calls == 1
    assert process.wait_timeouts == [worker.PROCESS_EXIT_TIMEOUT_SEC]
    assert [_selected_encoder(command) for command in factory.commands] == ["h264_nvenc"]
    assert signals["finished"] == []
    assert len(signals["failed"]) == 1
    assert "fallbackを中止" in signals["failed"][0]
    assert "終了を確認できませんでした: exit confirmation failed" in signals["failed"][0]
    assert "FFmpegプロセスが残っている可能性があります" in signals["failed"][0]
    assert output.read_bytes() == b"possibly active partial output"


def test_clip_export_fails_closed_when_process_terminate_raises(tmp_path):
    worker, factory, output = _make_worker(
        tmp_path,
        [
            {
                "stdout_error": OSError("stdout iteration failed"),
                "terminate_error": OSError("terminate failed"),
                "initial_output": b"possibly active partial output",
            }
        ],
    )
    signals = _capture_signals(worker)

    worker.run()

    process = factory.processes[0]
    assert process.terminate_calls == 1
    assert process.wait_calls == 0
    assert [_selected_encoder(command) for command in factory.commands] == ["h264_nvenc"]
    assert signals["finished"] == []
    assert len(signals["failed"]) == 1
    assert "fallbackを中止" in signals["failed"][0]
    assert "FFmpegプロセスが残っている可能性があります" in signals["failed"][0]
    assert output.read_bytes() == b"possibly active partial output"


def test_clip_export_cancel_reports_cleanup_failure_once_without_retry(tmp_path, monkeypatch):
    worker, factory, _output = _make_worker(
        tmp_path,
        [{"lines": ["out_time_us=1000000\n", "still_running=yes\n"]}],
    )
    signals = _capture_signals(worker)
    cleanup_calls = 0

    def fake_cleanup() -> str | None:
        nonlocal cleanup_calls
        cleanup_calls += 1
        if cleanup_calls == 1:
            return None
        return "部分出力ファイルを削除できませんでした: locked"

    monkeypatch.setattr(worker, "_remove_partial_output", fake_cleanup)
    worker.progress.connect(lambda percent, _message: worker.cancel() if percent == 10 else None)

    worker.run()

    assert factory.processes[0].terminate_calls == 1
    assert cleanup_calls == 2
    assert len(signals["failed"]) == 1
    assert signals["failed"][0].count("部分出力ファイルを削除できませんでした") == 1


def test_clip_export_cancel_before_process_start_finishes_once_without_launch(tmp_path):
    worker, factory, output = _make_worker(tmp_path, [])
    signals = _capture_signals(worker)

    worker.cancel()
    worker.cancel()
    worker.run()
    worker.cancel()

    assert factory.commands == []
    assert signals["finished"] == []
    assert signals["failed"] == ["クリップ出力をキャンセルしました。"]
    assert not output.exists()


def test_clip_export_cancel_during_process_creation_terminates_attached_process_once(tmp_path):
    output = tmp_path / "clips" / "result.mp4"
    process = FakeClipProcess(output, initial_output=b"partial output")
    worker = None

    def cancelling_factory(_command: list[str]) -> FakeClipProcess:
        assert worker is not None
        worker.cancel()
        return process

    worker = ClipExportWorker(
        "unused-ffmpeg.exe",
        tmp_path / "input.mp4",
        output,
        start_sec=10.0,
        end_sec=20.0,
        process_factory=cancelling_factory,
    )
    signals = _capture_signals(worker)

    worker.run()

    assert process.terminate_calls == 1
    assert process.wait_calls == 1
    assert signals["finished"] == []
    assert signals["failed"] == ["クリップ出力をキャンセルしました。"]
    assert not output.exists()
    assert worker.process is None


def test_clip_export_nonzero_exits_report_tail_and_delete_each_partial_output(tmp_path):
    worker, factory, output = _make_worker(
        tmp_path,
        [
            {
                "lines": ["nvenc detail\n"],
                "return_code": 23,
                "initial_output": b"first partial output",
            },
            {
                "require_output_absent": True,
                "lines": ["libx264 detail\n"],
                "return_code": 42,
                "initial_output": b"second partial output",
            },
        ],
    )
    signals = _capture_signals(worker)

    worker.run()

    assert [_selected_encoder(command) for command in factory.commands] == ["h264_nvenc", "libx264"]
    assert signals["finished"] == []
    assert len(signals["failed"]) == 1
    assert "[h264_nvenc] nvenc detail" in signals["failed"][0]
    assert "[libx264] libx264 detail" in signals["failed"][0]
    assert not output.exists()
    assert all(process.terminate_calls == 0 for process in factory.processes)


def test_clip_export_reports_missing_ffmpeg_without_calling_subprocess(tmp_path):
    worker, factory, output = _make_worker(
        tmp_path,
        [FileNotFoundError(), FileNotFoundError()],
    )
    signals = _capture_signals(worker)

    worker.run()

    assert [_selected_encoder(command) for command in factory.commands] == ["h264_nvenc", "libx264"]
    assert factory.processes == []
    assert signals["finished"] == []
    assert len(signals["failed"]) == 1
    assert "FFmpegが見つかりません" in signals["failed"][0]
    assert not output.exists()


def test_clip_export_rejects_missing_and_empty_output_even_after_zero_exit(tmp_path):
    worker, factory, output = _make_worker(
        tmp_path,
        [
            {"return_code": 0},
            {
                "require_output_absent": True,
                "return_code": 0,
                "output_on_wait": b"",
            },
        ],
    )
    signals = _capture_signals(worker)

    worker.run()

    assert [_selected_encoder(command) for command in factory.commands] == ["h264_nvenc", "libx264"]
    assert signals["finished"] == []
    assert len(signals["failed"]) == 1
    assert "出力ファイルが作成されませんでした" in signals["failed"][0]
    assert "出力ファイルが空です" in signals["failed"][0]
    assert not output.exists()


def test_clip_export_reports_output_directory_creation_failure_without_launch(tmp_path):
    blocked_parent = tmp_path / "not-a-directory"
    blocked_parent.write_text("file blocks mkdir", encoding="utf-8")
    worker, factory, output = _make_worker(
        tmp_path,
        [],
        output_path=blocked_parent / "result.mp4",
    )
    signals = _capture_signals(worker)

    worker.run()

    assert factory.commands == []
    assert signals["finished"] == []
    assert len(signals["failed"]) == 1
    assert "出力先ディレクトリを作成できません" in signals["failed"][0]
    assert not output.exists()


def test_clip_export_rejects_invalid_time_range_without_launch(tmp_path):
    worker, factory, output = _make_worker(tmp_path, [], start_sec=20.0, end_sec=20.0)
    signals = _capture_signals(worker)

    worker.run()

    assert factory.commands == []
    assert signals["finished"] == []
    assert signals["failed"] == ["クリップ範囲が不正です。終了時間は開始時間より後にしてください。"]
    assert not output.exists()
