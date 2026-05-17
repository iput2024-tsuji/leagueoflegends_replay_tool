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
