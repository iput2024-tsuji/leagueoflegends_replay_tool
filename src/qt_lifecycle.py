from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class WorkerHandle:
    worker: Any
    cancel_method: str = "cancel"


class WorkerRegistry:
    """QThread系ワーカーの停止を一箇所で扱う。"""

    def __init__(self) -> None:
        self._handles: dict[int, WorkerHandle] = {}

    def register(self, worker: Any, cancel_method: str = "cancel") -> Any:
        if worker is None:
            return worker
        self._handles[id(worker)] = WorkerHandle(worker=worker, cancel_method=cancel_method)
        finished = getattr(worker, "finished", None)
        connect = getattr(finished, "connect", None)
        if callable(connect):
            connect(lambda _worker=worker: self.unregister(_worker))
        return worker

    def unregister(self, worker: Any) -> None:
        if worker is not None:
            self._handles.pop(id(worker), None)

    def stop_all(self, timeout_ms: int) -> bool:
        stopped = True
        for handle in list(self._handles.values()):
            if request_worker_stop(handle.worker, timeout_ms, cancel_method=handle.cancel_method):
                self.unregister(handle.worker)
            else:
                stopped = False
        return stopped

    def force_stop_all(self, timeout_ms: int) -> bool:
        stopped = True
        for handle in list(self._handles.values()):
            if force_worker_stop(handle.worker, timeout_ms, cancel_method=handle.cancel_method):
                self.unregister(handle.worker)
            else:
                stopped = False
        return stopped

    def running_workers(self) -> list[Any]:
        return [handle.worker for handle in self._handles.values() if _is_running(handle.worker)]


def request_worker_stop(worker: Any, timeout_ms: int, cancel_method: str = "cancel") -> bool:
    if worker is None:
        return True
    if not _is_running(worker):
        return True

    cancel = getattr(worker, cancel_method, None)
    if callable(cancel):
        cancel()

    wait = getattr(worker, "wait", None)
    if not callable(wait):
        return not _is_running(worker)
    result = wait(max(0, int(timeout_ms)))
    if isinstance(result, bool):
        return result
    return not _is_running(worker)


def force_worker_stop(worker: Any, timeout_ms: int, cancel_method: str = "cancel") -> bool:
    if request_worker_stop(worker, timeout_ms, cancel_method=cancel_method):
        return True
    terminate = getattr(worker, "terminate", None)
    if callable(terminate):
        terminate()
    wait = getattr(worker, "wait", None)
    if callable(wait):
        result = wait(max(0, int(timeout_ms)))
        if isinstance(result, bool) and result:
            return result
    return not _is_running(worker)


def _is_running(worker: Any) -> bool:
    is_running = getattr(worker, "isRunning", None)
    if callable(is_running):
        return bool(is_running())
    return False
