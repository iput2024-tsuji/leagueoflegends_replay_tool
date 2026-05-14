from __future__ import annotations

from typing import Any


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


def _is_running(worker: Any) -> bool:
    is_running = getattr(worker, "isRunning", None)
    if callable(is_running):
        return bool(is_running())
    return False
