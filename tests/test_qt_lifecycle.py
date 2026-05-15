from src.qt_lifecycle import WorkerRegistry, request_worker_stop


class FakeWorker:
    def __init__(self, running=True, wait_result=True):
        self.running = running
        self.wait_result = wait_result
        self.cancel_called = 0
        self.terminate_called = 0
        self.terminated = False
        self.wait_calls = []

    def isRunning(self):
        return self.running

    def cancel(self):
        self.cancel_called += 1

    def stop(self):
        self.cancel_called += 1

    def terminate(self):
        self.terminate_called += 1
        self.terminated = True
        self.running = False

    def wait(self, timeout_ms):
        self.wait_calls.append(timeout_ms)
        self.running = False if self.terminated else not self.wait_result
        return self.wait_result


def test_request_worker_stop_calls_cancel_and_waits():
    worker = FakeWorker(running=True, wait_result=True)

    stopped = request_worker_stop(worker, 1234)

    assert stopped is True
    assert worker.cancel_called == 1
    assert worker.wait_calls == [1234]


def test_request_worker_stop_reports_timeout():
    worker = FakeWorker(running=True, wait_result=False)

    stopped = request_worker_stop(worker, 50, cancel_method="stop")

    assert stopped is False
    assert worker.cancel_called == 1
    assert worker.wait_calls == [50]


def test_request_worker_stop_ignores_already_stopped_worker():
    worker = FakeWorker(running=False)

    stopped = request_worker_stop(worker, 50)

    assert stopped is True
    assert worker.cancel_called == 0
    assert worker.wait_calls == []


def test_worker_registry_stops_registered_workers():
    registry = WorkerRegistry()
    first = registry.register(FakeWorker(running=True, wait_result=True))
    second = registry.register(FakeWorker(running=True, wait_result=False), cancel_method="stop")

    stopped = registry.stop_all(250)

    assert stopped is False
    assert first.cancel_called == 1
    assert second.cancel_called == 1
    assert first.wait_calls == [250]
    assert second.wait_calls == [250]
    assert registry.running_workers() == [second]


def test_worker_registry_force_stops_after_timeout():
    registry = WorkerRegistry()
    worker = registry.register(FakeWorker(running=True, wait_result=False))

    stopped = registry.force_stop_all(100)

    assert stopped is True
    assert worker.cancel_called == 1
    assert worker.terminate_called == 1
    assert registry.running_workers() == []
