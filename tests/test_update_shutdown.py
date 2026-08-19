from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src import app as app_module
from src.app import MainWindow, RecorderWorker


def test_update_obs_gate_uses_graceful_owned_cleanup_and_keeps_unmanaged(
    monkeypatch,
):
    manager = SimpleNamespace(
        kill_stale_owned_processes=Mock(return_value=[]),
        query_managed_processes_strict=Mock(return_value=()),
    )
    monkeypatch.setattr(
        app_module.recordtest,
        "OBSProcessManager",
        Mock(return_value=manager),
    )

    app_module._stop_owned_obs_for_update()

    manager.kill_stale_owned_processes.assert_called_once_with(allow_force=False)
    manager.query_managed_processes_strict.assert_called_once_with()


def test_update_obs_gate_fails_when_unowned_managed_obs_remains(monkeypatch):
    manager = SimpleNamespace(
        kill_stale_owned_processes=Mock(return_value=[]),
        query_managed_processes_strict=Mock(return_value=(object(),)),
    )
    monkeypatch.setattr(
        app_module.recordtest,
        "OBSProcessManager",
        Mock(return_value=manager),
    )

    with pytest.raises(app_module.recordtest.RecorderError, match="所有情報"):
        app_module._stop_owned_obs_for_update()


def test_update_request_during_startup_setup_is_blocked_immediately(monkeypatch):
    guard = SimpleNamespace(
        consume_update_shutdown_request=Mock(return_value=True),
    )
    window = SimpleNamespace(
        _instance_guard=guard,
        _closing=False,
        _startup_setup_active=True,
        _notify_installer_shutdown_blocked=Mock(),
        _handle_update_shutdown_request=Mock(),
    )

    MainWindow._poll_update_shutdown_request(window)

    window._notify_installer_shutdown_blocked.assert_called_once_with()
    window._handle_update_shutdown_request.assert_not_called()


def test_recorder_worker_rejects_update_after_recording_transition():
    supervisor = SimpleNamespace(reserve_update_shutdown=Mock(return_value=False))
    worker = SimpleNamespace(
        supervisor=supervisor,
        _update_shutdown_requested=False,
        stop=Mock(),
    )

    assert RecorderWorker.request_update_shutdown(worker) is False
    assert worker._update_shutdown_requested is False
    worker.stop.assert_not_called()


def test_recorder_worker_reserves_update_and_requests_stop():
    supervisor = SimpleNamespace(reserve_update_shutdown=Mock(return_value=True))
    worker = SimpleNamespace(
        supervisor=supervisor,
        _update_shutdown_requested=False,
        stop=Mock(),
    )

    assert RecorderWorker.request_update_shutdown(worker) is True
    assert worker._update_shutdown_requested is True
    worker.stop.assert_called_once_with()


def test_update_request_during_recording_notifies_installer_and_keeps_app_open(
    monkeypatch,
):
    worker = SimpleNamespace(
        isRunning=lambda: True,
        request_update_shutdown=Mock(return_value=False),
    )
    window = SimpleNamespace(
        _last_recorder_shutdown_failed=False,
        bg_recorder_worker=worker,
        _notify_installer_shutdown_blocked=Mock(),
        restore_from_tray=Mock(),
        close=Mock(),
    )
    warning = Mock()
    monkeypatch.setattr(app_module.QMessageBox, "warning", warning)

    MainWindow._handle_update_shutdown_request(window)

    window._notify_installer_shutdown_blocked.assert_called_once_with()
    window.restore_from_tray.assert_called_once_with()
    window.close.assert_not_called()
    assert "試合終了後" in warning.call_args.args[2]


def test_update_request_while_waiting_starts_full_shutdown():
    worker = SimpleNamespace(
        isRunning=lambda: True,
        request_update_shutdown=Mock(return_value=True),
    )
    window = SimpleNamespace(
        _last_recorder_shutdown_failed=False,
        bg_recorder_worker=worker,
        _update_shutdown_requested=False,
        _is_quitting=False,
        close=Mock(),
    )

    MainWindow._handle_update_shutdown_request(window)

    assert window._update_shutdown_requested is True
    assert window._is_quitting is True
    window.close.assert_called_once_with()


def test_update_shutdown_never_uses_worker_force_and_aborts_after_timeout():
    force_values = []
    event = SimpleNamespace(ignore=Mock(), accept=Mock())
    window = SimpleNamespace(
        _is_quitting=True,
        _closing=False,
        _update_shutdown_requested=True,
        _shutdown_attempts=1,
        _shutdown_max_attempts=3,
        _update_shutdown_max_attempts=1,
        _last_recorder_shutdown_failed=False,
        bg_recorder_worker=None,
        _stop_player=Mock(return_value=True),
        _stop_all_background_work=lambda force: force_values.append(force) or False,
        home_page=SimpleNamespace(set_recorder_status=Mock()),
        _abort_update_shutdown=Mock(),
    )

    MainWindow.closeEvent(window, event)

    assert force_values == [False]
    event.ignore.assert_called_once_with()
    window._abort_update_shutdown.assert_called_once_with()
    event.accept.assert_not_called()


def test_update_shutdown_aborts_when_final_managed_obs_check_fails(monkeypatch):
    failure = app_module.recordtest.RecorderError("managed OBS remains")
    monkeypatch.setattr(
        app_module,
        "_stop_owned_obs_for_update",
        Mock(side_effect=failure),
    )
    event = SimpleNamespace(ignore=Mock(), accept=Mock())
    window = SimpleNamespace(
        _is_quitting=True,
        _closing=False,
        _update_shutdown_requested=True,
        _update_shutdown_completed=False,
        _shutdown_attempts=0,
        _shutdown_max_attempts=3,
        _update_shutdown_max_attempts=1,
        _last_recorder_shutdown_failed=False,
        bg_recorder_worker=None,
        _stop_player=Mock(return_value=True),
        _stop_all_background_work=Mock(return_value=True),
        _abort_update_shutdown=Mock(),
        _tray_icon=None,
    )

    MainWindow.closeEvent(window, event)

    assert window._last_recorder_shutdown_failed is True
    event.ignore.assert_called_once_with()
    window._abort_update_shutdown.assert_called_once_with()
    event.accept.assert_not_called()
    assert window._update_shutdown_completed is False


def test_update_shutdown_keeps_failed_recorder_for_fail_closed_result():
    worker = SimpleNamespace(
        isRunning=lambda: False,
        update_shutdown_failed=lambda: True,
    )
    window = SimpleNamespace(
        bg_recorder_worker=worker,
        _update_shutdown_requested=True,
        _last_recorder_shutdown_failed=False,
        worker_registry=SimpleNamespace(unregister=Mock()),
    )

    assert MainWindow.stop_background_recorder(window) is False
    assert window.bg_recorder_worker is worker
    window.worker_registry.unregister.assert_not_called()
