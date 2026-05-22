from pathlib import Path

from src.obs_bootstrap import OBSBootstrapper


class FakeProcessManager:
    def __init__(self) -> None:
        self.kill_calls = 0

    def kill_stale_managed_processes(self, timeout_sec: float = 3.0) -> list[int]:
        self.kill_calls += 1
        return []


def test_apply_stops_managed_obs_once(tmp_path):
    process_manager = FakeProcessManager()
    bootstrapper = OBSBootstrapper(tmp_path / "obs-portable", process_manager=process_manager)

    result = bootstrapper.apply()

    assert process_manager.kill_calls == 1
    assert Path(result["global_ini_path"]).exists()
    assert Path(result["user_ini_path"]).exists()


def test_standalone_ini_repairs_still_stop_managed_obs(tmp_path):
    process_manager = FakeProcessManager()
    bootstrapper = OBSBootstrapper(tmp_path / "obs-portable", process_manager=process_manager)

    bootstrapper.ensure_global_ini()
    bootstrapper.ensure_user_ini()

    assert process_manager.kill_calls == 2


def test_websocket_config_requires_password_authentication(tmp_path):
    bootstrapper = OBSBootstrapper(tmp_path / "obs-portable", process_manager=FakeProcessManager())

    changed, config_path = bootstrapper.ensure_websocket_config(4455, "secret-password")

    text = config_path.read_text(encoding="utf-8")
    assert changed is True
    assert '"server_enabled": true' in text
    assert '"auth_required": true' in text
    assert '"server_password": "secret-password"' in text


def test_websocket_config_rejects_empty_password(tmp_path):
    bootstrapper = OBSBootstrapper(tmp_path / "obs-portable", process_manager=FakeProcessManager())

    try:
        bootstrapper.ensure_websocket_config(4455, "")
    except ValueError as exc:
        assert "password" in str(exc)
    else:
        raise AssertionError("empty obs-websocket password should be rejected")
