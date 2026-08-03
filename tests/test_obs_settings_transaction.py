import hashlib
import json
import multiprocessing
import os
from contextlib import contextmanager
from pathlib import Path

import pytest

from src import obs_bootstrap


def _hold_settings_guard(base_dir: str, entered, release, result_queue) -> None:
    try:
        with obs_bootstrap.obs_config_mutation_guard(base_dir):
            entered.set()
            if not release.wait(15):
                raise TimeoutError("test did not release settings guard")
        result_queue.put("ok")
    except Exception as exc:  # pragma: no cover - reported to the parent process
        result_queue.put(f"{type(exc).__name__}: {exc}")


def _make_plan(
    base_dir: Path,
    specifications: tuple[tuple[str, bytes | None, bytes], ...],
) -> tuple[obs_bootstrap.OBSConfigTransactionPlan, tuple[Path, ...]]:
    base_dir.mkdir(parents=True, exist_ok=True)
    writes = []
    paths = []
    directories = []
    for relative, original, desired in specifications:
        target = (base_dir / relative).absolute()
        target.parent.mkdir(parents=True, exist_ok=True)
        if original is not None:
            target.write_bytes(original)
        snapshot = obs_bootstrap.preflight_obs_config_file(
            target,
            label=relative,
        )
        writes.append(obs_bootstrap.OBSConfigPlannedWrite(snapshot, desired))
        paths.append(target)
        directories.append(target.parent)
    return (
        obs_bootstrap.OBSConfigTransactionPlan(
            base_dir=base_dir.absolute(),
            directories=tuple(directories),
            writes=tuple(writes),
        ),
        tuple(paths),
    )


def _journal(base_dir: Path) -> dict[str, object]:
    marker = obs_bootstrap.get_obs_settings_transaction_marker(base_dir)
    return json.loads(marker.read_text(encoding="utf-8"))


def _transaction_temporaries(base_dir: Path) -> tuple[Path, ...]:
    return tuple(
        path
        for path in base_dir.rglob("*.tmp")
        if obs_bootstrap._parse_transaction_temporary(path) is not None
    )


def _assert_transaction_clean(base_dir: Path) -> None:
    assert not obs_bootstrap.get_obs_settings_transaction_marker(base_dir).exists()
    assert _transaction_temporaries(base_dir) == ()


def _leave_stale_settings_transaction(monkeypatch, base_dir: Path, phase: str) -> Path:
    plan, (target,) = _make_plan(
        base_dir,
        (("config/obs-studio/global.ini", b"original", b"desired"),),
    )
    if phase == "preparing":
        real_write = obs_bootstrap._write_settings_temporary
        write_calls = 0

        def crash_after_temporaries(path, payload):
            nonlocal write_calls
            write_calls += 1
            result = real_write(path, payload)
            if write_calls == 2:
                raise SystemExit("stale preparing transaction")
            return result

        with monkeypatch.context() as patcher:
            patcher.setattr(
                obs_bootstrap,
                "_write_settings_temporary",
                crash_after_temporaries,
            )
            with pytest.raises(SystemExit, match="stale preparing"):
                obs_bootstrap.execute_obs_config_transaction(plan)
    elif phase == "committed":
        real_journal = obs_bootstrap._write_settings_journal

        def crash_after_committed(base, owner, current_phase, writes):
            result = real_journal(base, owner, current_phase, writes)
            if current_phase == "committed":
                raise SystemExit("stale committed transaction")
            return result

        with monkeypatch.context() as patcher:
            patcher.setattr(
                obs_bootstrap,
                "_write_settings_journal",
                crash_after_committed,
            )
            with pytest.raises(SystemExit, match="stale committed"):
                obs_bootstrap.execute_obs_config_transaction(plan)
    elif phase == "orphan":
        real_journal = obs_bootstrap._write_settings_journal

        def crash_before_initial_journal(base, owner, current_phase, writes):
            if current_phase == "preparing":
                marker = obs_bootstrap.get_obs_settings_transaction_marker(base)
                temporary = obs_bootstrap._transaction_write_temporary_path(
                    marker,
                    owner,
                )
                obs_bootstrap._write_settings_temporary(temporary, b"partial-journal")
                raise SystemExit("orphan journal temporary")
            return real_journal(base, owner, current_phase, writes)

        with monkeypatch.context() as patcher:
            patcher.setattr(
                obs_bootstrap,
                "_write_settings_journal",
                crash_before_initial_journal,
            )
            with pytest.raises(SystemExit, match="orphan journal"):
                obs_bootstrap.execute_obs_config_transaction(plan)
    else:
        raise AssertionError(f"unsupported stale phase: {phase}")
    return target


@contextmanager
def _planned_outside_write(root: Path, target: Path):
    with obs_bootstrap.obs_config_mutation_guard(root):
        token = obs_bootstrap._ACTIVE_OBS_SETTINGS_TRANSACTION_TARGETS.set(
            frozenset({obs_bootstrap._filesystem_path_key(target.absolute())})
        )
        try:
            yield
        finally:
            obs_bootstrap._ACTIVE_OBS_SETTINGS_TRANSACTION_TARGETS.reset(token)


def test_settings_transaction_prepares_durable_files_without_password_in_journal(tmp_path):
    base_dir = tmp_path / "obs-portable"
    old_secret = b"old-secret-value"
    new_secret = b"new-secret-value"
    original = b'{"server_password":"' + old_secret + b'"}'
    desired = b'{"server_password":"' + new_secret + b'"}'
    plan, (target,) = _make_plan(
        base_dir,
        (("config/obs-studio/plugin_config/obs-websocket/config.json", original, desired),),
    )
    callback_calls = 0

    def inspect_before_stop() -> None:
        nonlocal callback_calls
        callback_calls += 1
        marker = obs_bootstrap.get_obs_settings_transaction_marker(base_dir)
        marker_payload = marker.read_bytes()
        journal = json.loads(marker_payload)
        assert journal["phase"] == "preparing"
        assert old_secret not in marker_payload
        assert new_secret not in marker_payload
        owner = journal["owner_token"]
        backup, temporary = obs_bootstrap._settings_temp_paths(target, owner)
        assert backup.read_bytes() == original
        assert temporary.read_bytes() == desired
        assert target.read_bytes() == original

    changed = obs_bootstrap.execute_obs_config_transaction(
        plan,
        before_commit=inspect_before_stop,
    )

    assert changed == (target,)
    assert callback_calls == 1
    assert target.read_bytes() == desired
    _assert_transaction_clean(base_dir)


@pytest.mark.parametrize(
    ("run_on_noop", "expected_calls"),
    [(False, 0), (True, 1)],
)
def test_noop_stop_policy_is_explicit(tmp_path, run_on_noop, expected_calls):
    base_dir = tmp_path / "obs-portable"
    plan, (target,) = _make_plan(
        base_dir,
        (("config/obs-studio/global.ini", b"ready", b"ready"),),
    )
    calls = 0

    def before_commit() -> None:
        nonlocal calls
        calls += 1

    assert (
        obs_bootstrap.execute_obs_config_transaction(
            plan,
            before_commit=before_commit,
            run_before_commit_on_noop=run_on_noop,
        )
        == ()
    )
    assert calls == expected_calls
    assert target.read_bytes() == b"ready"
    _assert_transaction_clean(base_dir)


def test_safe_write_rejects_planned_target_outside_managed_root(tmp_path):
    base_dir = (tmp_path / "obs-portable").absolute()
    base_dir.mkdir()
    outside = (tmp_path / "outside.ini").absolute()
    outside.write_bytes(b"sentinel")
    snapshot = obs_bootstrap.preflight_obs_config_file(outside, label="outside.ini")

    with _planned_outside_write(base_dir, outside):
        with pytest.raises(obs_bootstrap.OBSPathSafetyError, match="管理OBS root外"):
            obs_bootstrap.write_preflighted_obs_config_file(snapshot, b"replaced")

    assert outside.read_bytes() == b"sentinel"


def test_guarded_delete_rejects_unplanned_managed_file(tmp_path):
    base_dir = (tmp_path / "obs-portable").absolute()
    target = base_dir / "config" / "obs-studio" / "sentinel.ini"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"keep")
    snapshot = obs_bootstrap.preflight_obs_config_file(target, label="sentinel.ini")

    with obs_bootstrap.obs_config_mutation_guard(base_dir):
        with pytest.raises(obs_bootstrap.OBSPathSafetyError, match="削除を計画"):
            obs_bootstrap.delete_preflighted_obs_config_file(snapshot)

    assert target.read_bytes() == b"keep"


def test_reserved_transaction_path_cannot_be_planned_as_directory(tmp_path):
    base_dir = (tmp_path / "obs-portable").absolute()
    plan, _targets = _make_plan(
        base_dir,
        (("config/obs-studio/global.ini", b"old", b"new"),),
    )
    marker = obs_bootstrap.get_obs_settings_transaction_marker(base_dir)
    unsafe_plan = obs_bootstrap.OBSConfigTransactionPlan(
        base_dir=plan.base_dir,
        directories=(marker,),
        writes=plan.writes,
    )

    with pytest.raises(obs_bootstrap.OBSPathSafetyError, match="管理path"):
        obs_bootstrap.execute_obs_config_transaction(unsafe_plan)

    assert not marker.exists()


@pytest.mark.parametrize(
    "relative",
    [
        ".lol_replay_obs_lease.json",
        "temp_appdata/runtime.ini",
    ],
)
def test_reserved_control_namespace_cannot_be_planned_as_target(tmp_path, relative):
    base_dir = (tmp_path / "obs-portable").absolute()
    plan, (target,) = _make_plan(
        base_dir,
        ((relative, b"control-original", b"control-replacement"),),
    )

    with pytest.raises(obs_bootstrap.OBSPathSafetyError, match="transaction管理"):
        obs_bootstrap.execute_obs_config_transaction(plan)

    assert target.read_bytes() == b"control-original"
    _assert_transaction_clean(base_dir)


@pytest.mark.parametrize(
    "relative",
    [
        f"config/.custom.{'a' * 32}.write.tmp",
        "config/.custom.invalid-owner.write.tmp",
    ],
)
def test_transaction_temporary_name_cannot_be_planned_as_target(tmp_path, relative):
    base_dir = (tmp_path / "obs-portable").absolute()
    plan, (target,) = _make_plan(
        base_dir,
        ((relative, None, b"desired"),),
    )

    with pytest.raises(obs_bootstrap.OBSPathSafetyError, match="transaction一時file形式"):
        obs_bootstrap.execute_obs_config_transaction(plan)

    assert not target.exists()
    assert not obs_bootstrap.get_obs_settings_transaction_marker(base_dir).exists()
    assert tuple(
        path for path in base_dir.rglob("*.tmp") if path != target
    ) == ()


def test_transaction_temporary_name_cannot_be_planned_as_extra_directory(tmp_path):
    base_dir = (tmp_path / "obs-portable").absolute()
    plan, (target,) = _make_plan(
        base_dir,
        (("config/obs-studio/global.ini", b"original", b"desired"),),
    )
    temporary_directory = (
        base_dir / "config" / f".custom.{'a' * 32}.write.tmp"
    )
    unsafe_plan = obs_bootstrap.OBSConfigTransactionPlan(
        base_dir=plan.base_dir,
        directories=(*plan.directories, temporary_directory),
        writes=plan.writes,
    )

    with pytest.raises(obs_bootstrap.OBSPathSafetyError, match="transaction一時file形式"):
        obs_bootstrap.execute_obs_config_transaction(unsafe_plan)

    assert not temporary_directory.exists()
    assert target.read_bytes() == b"original"
    _assert_transaction_clean(base_dir)


def test_oversized_desired_payload_is_rejected_before_journal_or_stop(tmp_path):
    base_dir = tmp_path / "obs-portable"
    plan, (target,) = _make_plan(
        base_dir,
        (
            (
                "config/obs-studio/global.ini",
                b"original",
                b"x" * (obs_bootstrap.OBS_BOOTSTRAP_CONFIG_MAX_BYTES + 1),
            ),
        ),
    )
    stop_calls = 0

    def before_commit() -> None:
        nonlocal stop_calls
        stop_calls += 1

    with pytest.raises(obs_bootstrap.OBSPathSafetyError, match="payloadが上限"):
        obs_bootstrap.execute_obs_config_transaction(
            plan,
            before_commit=before_commit,
        )

    assert stop_calls == 0
    assert target.read_bytes() == b"original"
    _assert_transaction_clean(base_dir)


@pytest.mark.parametrize("label", [" ", 123])
def test_invalid_plan_label_is_rejected_before_journal_or_stop(tmp_path, label):
    base_dir = tmp_path / "obs-portable"
    plan, (target,) = _make_plan(
        base_dir,
        (("config/obs-studio/global.ini", b"original", b"desired"),),
    )
    original_write = plan.writes[0]
    invalid_snapshot = obs_bootstrap.OBSConfigFileSnapshot(
        path=original_write.snapshot.path,
        payload=original_write.snapshot.payload,
        identity=original_write.snapshot.identity,
        label=label,
    )
    invalid_plan = obs_bootstrap.OBSConfigTransactionPlan(
        base_dir=plan.base_dir,
        directories=plan.directories,
        writes=(obs_bootstrap.OBSConfigPlannedWrite(invalid_snapshot, b"desired"),),
    )
    stop_calls = 0

    def before_commit() -> None:
        nonlocal stop_calls
        stop_calls += 1

    with pytest.raises(obs_bootstrap.OBSPathSafetyError, match="labelが不正"):
        obs_bootstrap.execute_obs_config_transaction(
            invalid_plan,
            before_commit=before_commit,
        )

    assert stop_calls == 0
    assert target.read_bytes() == b"original"
    _assert_transaction_clean(base_dir)


def test_changed_transaction_revalidates_unchanged_target_after_stop(tmp_path):
    base_dir = tmp_path / "obs-portable"
    plan, targets = _make_plan(
        base_dir,
        (
            ("config/obs-studio/global.ini", b"global-old", b"global-new"),
            ("config/obs-studio/user.ini", b"user-stable", b"user-stable"),
        ),
    )

    def simulate_unchanged_target_flush() -> None:
        targets[1].write_bytes(b"user-external")

    with pytest.raises(obs_bootstrap.OBSPathSafetyError, match="内容が変化"):
        obs_bootstrap.execute_obs_config_transaction(
            plan,
            before_commit=simulate_unchanged_target_flush,
        )

    assert targets[0].read_bytes() == b"global-old"
    assert targets[1].read_bytes() == b"user-external"
    _assert_transaction_clean(base_dir)


@pytest.mark.parametrize("failure_call", [1, 2, 3, 4])
def test_preparing_temporary_failure_does_not_stop_or_change_targets(
    monkeypatch,
    tmp_path,
    failure_call,
):
    base_dir = tmp_path / "obs-portable"
    specifications = (
        ("config/obs-studio/global.ini", b"global-original", b"global-desired"),
        ("config/obs-studio/user.ini", b"user-original", b"user-desired"),
    )
    plan, targets = _make_plan(base_dir, specifications)
    originals = tuple(specification[1] for specification in specifications)
    real_write = obs_bootstrap._write_settings_temporary
    write_calls = 0
    stop_calls = 0

    def fail_selected_write(path, payload):
        nonlocal write_calls
        write_calls += 1
        if write_calls == failure_call:
            raise OSError(f"simulated temporary failure {failure_call}")
        return real_write(path, payload)

    def before_commit() -> None:
        nonlocal stop_calls
        stop_calls += 1

    monkeypatch.setattr(obs_bootstrap, "_write_settings_temporary", fail_selected_write)

    with pytest.raises(OSError, match="temporary failure"):
        obs_bootstrap.execute_obs_config_transaction(
            plan,
            before_commit=before_commit,
        )

    assert stop_calls == 0
    assert tuple(target.read_bytes() for target in targets) == originals
    _assert_transaction_clean(base_dir)


@pytest.mark.parametrize("crash_call", [1, 2, 3, 4])
def test_partial_preparing_temporary_is_recovered_after_hard_crash(
    monkeypatch,
    tmp_path,
    crash_call,
):
    base_dir = tmp_path / "obs-portable"
    specifications = (
        ("config/obs-studio/global.ini", b"global-original", b"global-desired"),
        ("config/obs-studio/user.ini", b"user-original", b"user-desired"),
    )
    plan, targets = _make_plan(base_dir, specifications)
    originals = tuple(specification[1] for specification in specifications)
    real_write = obs_bootstrap._write_settings_temporary
    write_calls = 0

    def crash_after_partial_write(path, payload):
        nonlocal write_calls
        write_calls += 1
        if write_calls == crash_call:
            real_write(path, payload[: max(1, len(payload) // 2)])
            raise SystemExit(f"simulated crash {crash_call}")
        return real_write(path, payload)

    monkeypatch.setattr(
        obs_bootstrap,
        "_write_settings_temporary",
        crash_after_partial_write,
    )
    with pytest.raises(SystemExit, match="simulated crash"):
        obs_bootstrap.execute_obs_config_transaction(plan)

    assert _journal(base_dir)["phase"] == "preparing"
    assert tuple(target.read_bytes() for target in targets) == originals
    monkeypatch.setattr(obs_bootstrap, "_write_settings_temporary", real_write)

    with obs_bootstrap.obs_config_mutation_guard(base_dir):
        pass

    assert tuple(target.read_bytes() for target in targets) == originals
    _assert_transaction_clean(base_dir)


def test_preparing_recovery_does_not_depend_on_target_after_preflight(
    monkeypatch,
    tmp_path,
):
    base_dir = tmp_path / "obs-portable"
    plan, (target,) = _make_plan(
        base_dir,
        (("config/obs-studio/global.ini", b"original", b"desired"),),
    )
    real_write = obs_bootstrap._write_settings_temporary
    write_calls = 0

    def crash_after_preparing_temporary(path, payload):
        nonlocal write_calls
        write_calls += 1
        result = real_write(path, payload)
        if write_calls == 2:
            raise SystemExit("preparing-crash")
        return result

    monkeypatch.setattr(
        obs_bootstrap,
        "_write_settings_temporary",
        crash_after_preparing_temporary,
    )
    with pytest.raises(SystemExit, match="preparing-crash"):
        obs_bootstrap.execute_obs_config_transaction(plan)
    monkeypatch.setattr(obs_bootstrap, "_write_settings_temporary", real_write)

    real_revalidate = obs_bootstrap.revalidate_obs_config_file
    target_changed = False

    def change_target_after_recovery_preflight(snapshot):
        nonlocal target_changed
        if not target_changed:
            target.write_bytes(b"external-update")
            target_changed = True
        return real_revalidate(snapshot)

    monkeypatch.setattr(
        obs_bootstrap,
        "revalidate_obs_config_file",
        change_target_after_recovery_preflight,
    )
    with obs_bootstrap.obs_config_mutation_guard(base_dir):
        pass

    assert target.read_bytes() == b"external-update"
    _assert_transaction_clean(base_dir)


def test_stop_flush_conflict_discards_preparing_state_without_overwrite(tmp_path):
    base_dir = tmp_path / "obs-portable"
    plan, (target,) = _make_plan(
        base_dir,
        (("config/obs-studio/global.ini", b"original", b"desired"),),
    )

    def simulate_obs_exit_flush() -> None:
        assert _journal(base_dir)["phase"] == "preparing"
        target.write_bytes(b"obs-exit-flush")

    with pytest.raises(obs_bootstrap.OBSPathSafetyError, match="内容が変化"):
        obs_bootstrap.execute_obs_config_transaction(
            plan,
            before_commit=simulate_obs_exit_flush,
        )

    assert target.read_bytes() == b"obs-exit-flush"
    _assert_transaction_clean(base_dir)


def test_stop_callback_cannot_swap_desired_temporary_with_same_bytes(tmp_path):
    base_dir = tmp_path / "obs-portable"
    plan, (target,) = _make_plan(
        base_dir,
        (("config/obs-studio/global.ini", b"original", b"desired"),),
    )

    def swap_desired_temporary() -> None:
        owner = str(_journal(base_dir)["owner_token"])
        _backup, desired = obs_bootstrap._settings_temp_paths(target, owner)
        replacement = desired.with_name("same-bytes-replacement.tmp")
        replacement.write_bytes(b"desired")
        replacement.replace(desired)

    with pytest.raises(obs_bootstrap.OBSPathSafetyError, match="identity"):
        obs_bootstrap.execute_obs_config_transaction(
            plan,
            before_commit=swap_desired_temporary,
        )

    assert target.read_bytes() == b"original"
    _assert_transaction_clean(base_dir)


@pytest.mark.parametrize(
    ("phase", "position", "marker_phase", "expected"),
    [
        ("committing", "before", "preparing", b"original"),
        ("committing", "after", "committing", b"original"),
        ("committed", "before", "committing", b"original"),
        ("committed", "after", "committed", b"desired"),
    ],
)
def test_journal_phase_crash_resumes_safely(
    monkeypatch,
    tmp_path,
    phase,
    position,
    marker_phase,
    expected,
):
    base_dir = tmp_path / "obs-portable"
    plan, (target,) = _make_plan(
        base_dir,
        (("config/obs-studio/global.ini", b"original", b"desired"),),
    )
    real_write_journal = obs_bootstrap._write_settings_journal

    def crash_at_phase(base, owner, current_phase, writes):
        if current_phase == phase and position == "before":
            raise SystemExit(f"{phase}-before")
        result = real_write_journal(base, owner, current_phase, writes)
        if current_phase == phase and position == "after":
            raise SystemExit(f"{phase}-after")
        return result

    monkeypatch.setattr(obs_bootstrap, "_write_settings_journal", crash_at_phase)
    with pytest.raises(SystemExit, match=phase):
        obs_bootstrap.execute_obs_config_transaction(plan)

    assert _journal(base_dir)["phase"] == marker_phase
    monkeypatch.setattr(obs_bootstrap, "_write_settings_journal", real_write_journal)

    with obs_bootstrap.obs_config_mutation_guard(base_dir):
        pass

    assert target.read_bytes() == expected
    _assert_transaction_clean(base_dir)


def test_partial_replace_crash_rolls_back_every_target(monkeypatch, tmp_path):
    base_dir = tmp_path / "obs-portable"
    plan, targets = _make_plan(
        base_dir,
        (
            ("config/obs-studio/global.ini", b"global-old", b"global-new"),
            ("config/obs-studio/user.ini", b"user-old", b"user-new"),
        ),
    )
    real_replace = obs_bootstrap._replace_settings_temporary
    replace_calls = 0

    def crash_after_first_replace(*args, **kwargs):
        nonlocal replace_calls
        replace_calls += 1
        result = real_replace(*args, **kwargs)
        if replace_calls == 1:
            raise SystemExit("after-first-replace")
        return result

    monkeypatch.setattr(
        obs_bootstrap,
        "_replace_settings_temporary",
        crash_after_first_replace,
    )
    with pytest.raises(SystemExit, match="first-replace"):
        obs_bootstrap.execute_obs_config_transaction(plan)

    assert _journal(base_dir)["phase"] == "committing"
    assert targets[0].read_bytes() == b"global-new"
    assert targets[1].read_bytes() == b"user-old"
    monkeypatch.setattr(obs_bootstrap, "_replace_settings_temporary", real_replace)

    with obs_bootstrap.obs_config_mutation_guard(base_dir):
        pass

    assert targets[0].read_bytes() == b"global-old"
    assert targets[1].read_bytes() == b"user-old"
    _assert_transaction_clean(base_dir)


def test_new_target_is_removed_when_committing_recovery_rolls_back(
    monkeypatch,
    tmp_path,
):
    base_dir = tmp_path / "obs-portable"
    plan, (target,) = _make_plan(
        base_dir,
        (("config/obs-studio/new.ini", None, b"desired"),),
    )
    real_replace = obs_bootstrap._replace_settings_temporary

    def crash_after_new_target_replace(*args, **kwargs):
        real_replace(*args, **kwargs)
        raise SystemExit("new target replaced")

    monkeypatch.setattr(
        obs_bootstrap,
        "_replace_settings_temporary",
        crash_after_new_target_replace,
    )
    with pytest.raises(SystemExit, match="new target replaced"):
        obs_bootstrap.execute_obs_config_transaction(plan)
    monkeypatch.setattr(obs_bootstrap, "_replace_settings_temporary", real_replace)

    assert target.read_bytes() == b"desired"
    assert _journal(base_dir)["phase"] == "committing"
    with obs_bootstrap.obs_config_mutation_guard(base_dir):
        pass

    assert not target.exists()
    _assert_transaction_clean(base_dir)


def test_recovery_rejects_backup_identity_change_after_inventory(monkeypatch, tmp_path):
    base_dir = tmp_path / "obs-portable"
    plan, (target,) = _make_plan(
        base_dir,
        (("config/obs-studio/global.ini", b"original", b"desired"),),
    )
    real_replace = obs_bootstrap._replace_settings_temporary

    def crash_after_target_replace(*args, **kwargs):
        real_replace(*args, **kwargs)
        raise SystemExit("target replaced")

    monkeypatch.setattr(
        obs_bootstrap,
        "_replace_settings_temporary",
        crash_after_target_replace,
    )
    with pytest.raises(SystemExit, match="target replaced"):
        obs_bootstrap.execute_obs_config_transaction(plan)
    monkeypatch.setattr(obs_bootstrap, "_replace_settings_temporary", real_replace)
    replaced_backup = False

    def replace_backup_after_inventory(*args, **kwargs):
        nonlocal replaced_backup
        temporary = Path(args[0])
        if kwargs.get("expected_temporary_identity") is not None and not replaced_backup:
            replacement = temporary.with_name("replacement-backup.tmp")
            replacement.write_bytes(temporary.read_bytes())
            replacement.replace(temporary)
            replaced_backup = True
        return real_replace(*args, **kwargs)

    monkeypatch.setattr(
        obs_bootstrap,
        "_replace_settings_temporary",
        replace_backup_after_inventory,
    )

    with pytest.raises(
        obs_bootstrap.OBSSettingsRecoveryRequiredError,
        match="identity",
    ):
        with obs_bootstrap.obs_config_mutation_guard(base_dir):
            pass

    assert replaced_backup is True
    assert target.read_bytes() == b"desired"
    assert _journal(base_dir)["phase"] == "committing"


@pytest.mark.parametrize("race", ["payload", "identity"])
def test_all_replaced_targets_are_reopened_before_committed(
    monkeypatch,
    tmp_path,
    race,
):
    base_dir = tmp_path / "obs-portable"
    plan, targets = _make_plan(
        base_dir,
        (
            ("config/obs-studio/global.ini", b"global-old", b"global-new"),
            ("config/obs-studio/user.ini", b"user-old", b"user-new"),
        ),
    )
    real_replace = obs_bootstrap._replace_settings_temporary
    replace_calls = 0

    def race_after_all_replacements(*args, **kwargs):
        nonlocal replace_calls
        replace_calls += 1
        identity = real_replace(*args, **kwargs)
        if replace_calls == 2:
            if race == "payload":
                targets[0].write_bytes(b"global-old")
            else:
                replacement = targets[0].with_name("replacement.ini")
                replacement.write_bytes(b"global-new")
                replacement.replace(targets[0])
        return identity

    monkeypatch.setattr(
        obs_bootstrap,
        "_replace_settings_temporary",
        race_after_all_replacements,
    )

    with pytest.raises(
        obs_bootstrap.OBSPathSafetyError,
        match="desired payload／identity",
    ):
        obs_bootstrap.execute_obs_config_transaction(plan)

    assert tuple(target.read_bytes() for target in targets) == (
        b"global-old",
        b"user-old",
    )
    _assert_transaction_clean(base_dir)


@pytest.mark.parametrize(
    ("failure", "visible_phase", "expected_after_retry"),
    [
        ("before-update", "committing", b"original"),
        ("parent-fsync", "committed", b"desired"),
    ],
)
def test_committed_phase_failure_defers_cleanup_to_next_durable_recovery(
    monkeypatch,
    tmp_path,
    failure,
    visible_phase,
    expected_after_retry,
):
    base_dir = tmp_path / "obs-portable"
    plan, (target,) = _make_plan(
        base_dir,
        (("config/obs-studio/global.ini", b"original", b"desired"),),
    )
    if failure == "before-update":
        real_journal = obs_bootstrap._write_settings_journal

        def fail_before_committed(base, owner, phase, writes):
            if phase == "committed":
                raise OSError("committed journal update failed")
            return real_journal(base, owner, phase, writes)

        monkeypatch.setattr(
            obs_bootstrap,
            "_write_settings_journal",
            fail_before_committed,
        )
    else:
        real_journal = obs_bootstrap._write_settings_journal
        real_flush = obs_bootstrap._OBSDirectoryLease.flush_metadata
        active_journal_phase = None

        def write_journal_with_phase(base, owner, phase, writes):
            nonlocal active_journal_phase
            active_journal_phase = phase
            try:
                return real_journal(base, owner, phase, writes)
            finally:
                active_journal_phase = None

        def fail_committed_parent_flush(directory):
            if (
                active_journal_phase == "committed"
                and directory.path == base_dir.absolute()
            ):
                raise OSError("committed parent fsync failed")
            return real_flush(directory)

        monkeypatch.setattr(
            obs_bootstrap,
            "_write_settings_journal",
            write_journal_with_phase,
        )
        monkeypatch.setattr(
            obs_bootstrap._OBSDirectoryLease,
            "flush_metadata",
            fail_committed_parent_flush,
        )

    with pytest.raises(
        obs_bootstrap.OBSSettingsRecoveryRequiredError,
        match="次回復旧",
    ):
        obs_bootstrap.execute_obs_config_transaction(plan)

    assert _journal(base_dir)["phase"] == visible_phase
    assert target.read_bytes() == b"desired"
    assert _transaction_temporaries(base_dir)

    with obs_bootstrap.obs_config_mutation_guard(base_dir):
        pass

    assert target.read_bytes() == expected_after_retry
    _assert_transaction_clean(base_dir)


def test_committed_cleanup_resumes_after_hard_crash(monkeypatch, tmp_path):
    base_dir = tmp_path / "obs-portable"
    plan, (target,) = _make_plan(
        base_dir,
        (("config/obs-studio/global.ini", b"old", b"new"),),
    )
    real_delete = obs_bootstrap.delete_preflighted_obs_config_file
    crashed = False

    def crash_during_cleanup(snapshot):
        nonlocal crashed
        result = real_delete(snapshot)
        if not crashed and snapshot.path.name.endswith(".copy.tmp"):
            crashed = True
            raise SystemExit("cleanup-crash")
        return result

    monkeypatch.setattr(
        obs_bootstrap,
        "delete_preflighted_obs_config_file",
        crash_during_cleanup,
    )
    with pytest.raises(SystemExit, match="cleanup-crash"):
        obs_bootstrap.execute_obs_config_transaction(plan)

    assert target.read_bytes() == b"new"
    assert _journal(base_dir)["phase"] == "committed"
    monkeypatch.setattr(
        obs_bootstrap,
        "delete_preflighted_obs_config_file",
        real_delete,
    )

    with obs_bootstrap.obs_config_mutation_guard(base_dir):
        pass

    assert target.read_bytes() == b"new"
    _assert_transaction_clean(base_dir)


def test_partial_initial_journal_temporary_is_removed_on_retry(monkeypatch, tmp_path):
    base_dir = tmp_path / "obs-portable"
    plan, (target,) = _make_plan(
        base_dir,
        (("config/obs-studio/global.ini", b"old", b"new"),),
    )
    real_write_journal = obs_bootstrap._write_settings_journal

    def crash_before_initial_journal(base, owner, phase, writes):
        if phase == "preparing":
            marker = obs_bootstrap.get_obs_settings_transaction_marker(base)
            temporary = obs_bootstrap._transaction_write_temporary_path(marker, owner)
            obs_bootstrap._write_settings_temporary(temporary, b"partial-journal")
            raise SystemExit("journal-create-crash")
        return real_write_journal(base, owner, phase, writes)

    monkeypatch.setattr(
        obs_bootstrap,
        "_write_settings_journal",
        crash_before_initial_journal,
    )
    with pytest.raises(SystemExit, match="journal-create-crash"):
        obs_bootstrap.execute_obs_config_transaction(plan)

    assert not obs_bootstrap.get_obs_settings_transaction_marker(base_dir).exists()
    assert len(_transaction_temporaries(base_dir)) == 1
    monkeypatch.setattr(obs_bootstrap, "_write_settings_journal", real_write_journal)

    with obs_bootstrap.obs_config_mutation_guard(base_dir):
        pass

    assert target.read_bytes() == b"old"
    _assert_transaction_clean(base_dir)


def test_markerless_nested_data_temporary_requires_manual_recovery(tmp_path):
    base_dir = (tmp_path / "obs-portable").absolute()
    target = base_dir / "config" / "obs-studio" / "global.ini"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"original")
    temporary = obs_bootstrap._transaction_copy_temporary_path(
        target,
        "a" * 32,
    )
    temporary.write_bytes(b"orphan-backup")

    with pytest.raises(
        obs_bootstrap.OBSSettingsRecoveryRequiredError,
        match="markerなしでdata transaction",
    ):
        with obs_bootstrap.obs_config_mutation_guard(base_dir):
            pass

    assert target.read_bytes() == b"original"
    assert temporary.read_bytes() == b"orphan-backup"
    assert not obs_bootstrap.get_obs_settings_transaction_marker(base_dir).exists()


@pytest.mark.parametrize(
    "payload",
    [
        b"{not-json",
        json.dumps(
            {
                "schema_version": 1,
                "owner_token": "a" * 32,
                "phase": "committing",
                "entries": [
                    {
                        "path": "../outside.ini",
                        "label": "unsafe",
                        "original_exists": True,
                        "original_size": 3,
                        "original_sha256": "0" * 64,
                        "desired_size": 3,
                        "desired_sha256": "1" * 64,
                    }
                ],
            }
        ).encode("utf-8"),
        json.dumps(
            {
                "schema_version": 1,
                "owner_token": "a" * 32,
                "phase": "committing",
                "entries": [
                    {
                        "path": f"config/.custom.{'b' * 32}.write.tmp",
                        "label": "self-colliding target",
                        "original_exists": True,
                        "original_size": 3,
                        "original_sha256": "0" * 64,
                        "desired_size": 3,
                        "desired_sha256": "1" * 64,
                    }
                ],
            }
        ).encode("utf-8"),
    ],
)
def test_recovery_rejects_invalid_journal_before_stop(tmp_path, payload):
    base_dir = tmp_path / "obs-portable"
    base_dir.mkdir()
    obs_bootstrap.get_obs_settings_transaction_marker(base_dir).write_bytes(payload)
    stop_calls = 0

    def before_recovery() -> None:
        nonlocal stop_calls
        stop_calls += 1

    with pytest.raises(obs_bootstrap.OBSSettingsRecoveryRequiredError):
        with obs_bootstrap.obs_config_mutation_guard(
            base_dir,
            before_settings_recovery=before_recovery,
        ):
            pass

    assert stop_calls == 0


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("original_size", True),
        ("desired_size", obs_bootstrap.OBS_BOOTSTRAP_CONFIG_MAX_BYTES + 1),
        ("original_sha256", "g" * 64),
        ("desired_sha256", "A" * 64),
        ("label", " "),
        ("entries", []),
    ],
)
def test_recovery_rejects_noncanonical_journal_metadata_before_stop(
    tmp_path,
    field,
    value,
):
    base_dir = tmp_path / "obs-portable"
    base_dir.mkdir()
    entry = {
        "path": "config/obs-studio/new.ini",
        "label": "new.ini",
        "original_exists": False,
        "original_size": 0,
        "original_sha256": hashlib.sha256(b"").hexdigest(),
        "desired_size": 3,
        "desired_sha256": hashlib.sha256(b"new").hexdigest(),
    }
    journal = {
        "schema_version": 1,
        "owner_token": "a" * 32,
        "phase": "preparing",
        "entries": [entry],
    }
    if field == "entries":
        journal[field] = value
    else:
        entry[field] = value
    obs_bootstrap.get_obs_settings_transaction_marker(base_dir).write_text(
        json.dumps(journal),
        encoding="utf-8",
    )
    stop_calls = 0

    def before_recovery() -> None:
        nonlocal stop_calls
        stop_calls += 1

    with pytest.raises(obs_bootstrap.OBSSettingsRecoveryRequiredError):
        with obs_bootstrap.obs_config_mutation_guard(
            base_dir,
            before_settings_recovery=before_recovery,
        ):
            pass

    assert stop_calls == 0


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", True),
        ("schema_version", 1.0),
        ("owner_token", int("1" * 32)),
    ],
)
def test_recovery_rejects_noncanonical_journal_header_before_stop(
    tmp_path,
    field,
    value,
):
    base_dir = tmp_path / "obs-portable"
    base_dir.mkdir()
    journal = {
        "schema_version": 1,
        "owner_token": "a" * 32,
        "phase": "preparing",
        "entries": [
            {
                "path": "config/obs-studio/new.ini",
                "label": "new.ini",
                "original_exists": False,
                "original_size": 0,
                "original_sha256": hashlib.sha256(b"").hexdigest(),
                "desired_size": 3,
                "desired_sha256": hashlib.sha256(b"new").hexdigest(),
            }
        ],
    }
    journal[field] = value
    obs_bootstrap.get_obs_settings_transaction_marker(base_dir).write_text(
        json.dumps(journal),
        encoding="utf-8",
    )
    stop_calls = 0

    def before_recovery() -> None:
        nonlocal stop_calls
        stop_calls += 1

    with pytest.raises(obs_bootstrap.OBSSettingsRecoveryRequiredError):
        with obs_bootstrap.obs_config_mutation_guard(
            base_dir,
            before_settings_recovery=before_recovery,
        ):
            pass

    assert stop_calls == 0


@pytest.mark.parametrize("damage", ["missing", "mutated"])
def test_recovery_preflights_required_backup_before_stop(monkeypatch, tmp_path, damage):
    base_dir = tmp_path / "obs-portable"
    plan, (target,) = _make_plan(
        base_dir,
        (("config/obs-studio/global.ini", b"original", b"desired"),),
    )
    real_replace = obs_bootstrap._replace_settings_temporary

    def crash_after_replace(*args, **kwargs):
        real_replace(*args, **kwargs)
        raise SystemExit("stale-committing")

    monkeypatch.setattr(obs_bootstrap, "_replace_settings_temporary", crash_after_replace)
    with pytest.raises(SystemExit, match="stale-committing"):
        obs_bootstrap.execute_obs_config_transaction(plan)
    monkeypatch.setattr(obs_bootstrap, "_replace_settings_temporary", real_replace)

    owner = str(_journal(base_dir)["owner_token"])
    backup, _desired = obs_bootstrap._settings_temp_paths(target, owner)
    if damage == "missing":
        backup.unlink()
    else:
        backup.write_bytes(b"corrupt-backup")
    stop_calls = 0

    def before_recovery() -> None:
        nonlocal stop_calls
        stop_calls += 1

    with pytest.raises(obs_bootstrap.OBSSettingsRecoveryRequiredError):
        with obs_bootstrap.obs_config_mutation_guard(
            base_dir,
            before_settings_recovery=before_recovery,
        ):
            pass

    assert stop_calls == 0
    assert target.read_bytes() == b"desired"


def test_settings_transactions_are_excluded_across_processes(tmp_path):
    base_dir = tmp_path / "obs-portable"
    plan, (target,) = _make_plan(
        base_dir,
        (("config/obs-studio/global.ini", b"old", b"new"),),
    )
    context = multiprocessing.get_context("spawn")
    entered = context.Event()
    release = context.Event()
    result_queue = context.Queue()
    process = context.Process(
        target=_hold_settings_guard,
        args=(str(base_dir), entered, release, result_queue),
    )
    process.start()
    try:
        assert entered.wait(15), "child did not acquire settings guard"
        assert obs_bootstrap.has_pending_obs_copy_transaction(base_dir) is False
        with pytest.raises(obs_bootstrap.OBSMigrationInProgressError):
            obs_bootstrap.execute_obs_config_transaction(plan)
        assert target.read_bytes() == b"old"
    finally:
        release.set()
        process.join(15)
        if process.is_alive():
            process.terminate()
            process.join(5)
        assert process.exitcode == 0
        assert result_queue.get(timeout=5) == "ok"
        process.close()
        result_queue.close()


def test_copy_pending_probe_only_scans_root_journal_temporaries(monkeypatch, tmp_path):
    base_dir = (tmp_path / "obs-portable").absolute()
    unrelated = base_dir / "large" / "unrelated" / "tree"
    unrelated.mkdir(parents=True)
    (unrelated / "ordinary.txt").write_bytes(b"keep")

    def fail_recursive_scan(*_args, **_kwargs):
        raise AssertionError("copy pending probe must not recursively scan OBS contents")

    monkeypatch.setattr(
        obs_bootstrap,
        "_list_root_transaction_temporaries",
        fail_recursive_scan,
    )

    assert obs_bootstrap.has_pending_obs_copy_transaction(base_dir) is False

    marker = obs_bootstrap.get_obs_copy_in_progress_marker(base_dir)
    temporary = obs_bootstrap._transaction_journal_temporary_path(
        marker,
        "a" * 32,
    )
    temporary.write_bytes(b"partial-copy-journal")

    assert obs_bootstrap.has_pending_obs_copy_transaction(base_dir) is True


@pytest.mark.skipif(
    os.name == "nt" or not obs_bootstrap._supports_posix_handle_relative_migration(),
    reason="POSIX handle-relative root swap regression",
)
def test_settings_transaction_rejects_physical_root_swap_without_touching_new_root(
    tmp_path,
):
    base_dir = (tmp_path / "obs-portable").absolute()
    detached_root = (tmp_path / "detached-obs-portable").absolute()
    plan, (target,) = _make_plan(
        base_dir,
        (("config/obs-studio/global.ini", b"original", b"desired"),),
    )
    sentinel = base_dir / "new-root-sentinel.txt"

    def swap_managed_root() -> None:
        base_dir.rename(detached_root)
        base_dir.mkdir()
        sentinel.write_bytes(b"keep-new-root")
        (detached_root / "config").rename(base_dir / "config")
        old_marker = obs_bootstrap.get_obs_settings_transaction_marker(detached_root)
        old_marker.rename(obs_bootstrap.get_obs_settings_transaction_marker(base_dir))

    with pytest.raises(obs_bootstrap.OBSSettingsRecoveryRequiredError):
        obs_bootstrap.execute_obs_config_transaction(
            plan,
            before_commit=swap_managed_root,
        )

    assert target.read_bytes() == b"original"
    assert sentinel.read_bytes() == b"keep-new-root"
    assert obs_bootstrap.get_obs_settings_transaction_marker(base_dir).exists()
    assert _transaction_temporaries(base_dir)
    second_lock = obs_bootstrap._OBSInterProcessLock(
        obs_bootstrap.get_obs_copy_lock_path(base_dir)
    )
    assert second_lock.acquire() is True
    second_lock.release()


@pytest.mark.parametrize("phase", ["preparing", "committed", "orphan"])
@pytest.mark.skipif(
    os.name == "nt" or not obs_bootstrap._supports_posix_handle_relative_migration(),
    reason="POSIX handle-relative recovery root swap regression",
)
def test_settings_recovery_rejects_root_swap_during_marker_preflight(
    monkeypatch,
    tmp_path,
    phase,
):
    base_dir = (tmp_path / "obs-portable").absolute()
    detached_root = (tmp_path / f"detached-{phase}").absolute()
    replacement_root = (tmp_path / f"replacement-{phase}").absolute()
    target = _leave_stale_settings_transaction(monkeypatch, base_dir, phase)
    marker = obs_bootstrap.get_obs_settings_transaction_marker(base_dir)
    tracked = tuple(
        dict.fromkeys(
            (
                target,
                *((marker,) if marker.exists() else ()),
                *_transaction_temporaries(base_dir),
            )
        )
    )
    expected_payloads = {
        path.relative_to(base_dir): path.read_bytes() for path in tracked
    }
    real_preflight = obs_bootstrap.preflight_obs_config_file
    swapped = False

    def swap_during_marker_preflight(path, **kwargs):
        nonlocal swapped
        absolute = Path(path).absolute()
        if not swapped and absolute == marker:
            base_dir.rename(detached_root)
            base_dir.mkdir()
            sentinel = base_dir / "new-root-sentinel.txt"
            sentinel.write_bytes(b"keep-new-root")
            try:
                return real_preflight(path, **kwargs)
            finally:
                base_dir.rename(replacement_root)
                detached_root.rename(base_dir)
                swapped = True
        return real_preflight(path, **kwargs)

    monkeypatch.setattr(
        obs_bootstrap,
        "preflight_obs_config_file",
        swap_during_marker_preflight,
    )

    with pytest.raises(obs_bootstrap.OBSPathSafetyError):
        with obs_bootstrap.obs_config_mutation_guard(base_dir):
            pass

    assert swapped is True
    assert (replacement_root / "new-root-sentinel.txt").read_bytes() == b"keep-new-root"
    assert tuple(replacement_root.iterdir()) == (
        replacement_root / "new-root-sentinel.txt",
    )
    for relative, payload in expected_payloads.items():
        assert (base_dir / relative).read_bytes() == payload


@pytest.mark.skipif(
    os.name == "nt" or not obs_bootstrap._supports_posix_handle_relative_migration(),
    reason="POSIX handle-relative committed recovery root swap regression",
)
def test_committed_recovery_rejects_root_swap_after_stop_callback(
    monkeypatch,
    tmp_path,
):
    base_dir = (tmp_path / "obs-portable").absolute()
    detached_root = (tmp_path / "detached-committed").absolute()
    target = _leave_stale_settings_transaction(monkeypatch, base_dir, "committed")
    relative_target = target.relative_to(base_dir)
    marker = obs_bootstrap.get_obs_settings_transaction_marker(base_dir)

    def swap_after_stop() -> None:
        base_dir.rename(detached_root)
        base_dir.mkdir()
        (base_dir / "new-root-sentinel.txt").write_bytes(b"keep-new-root")

    with pytest.raises(obs_bootstrap.OBSSettingsRecoveryRequiredError):
        with obs_bootstrap.obs_config_mutation_guard(
            base_dir,
            before_settings_recovery=swap_after_stop,
        ):
            pass

    assert (base_dir / "new-root-sentinel.txt").read_bytes() == b"keep-new-root"
    assert tuple(base_dir.iterdir()) == (base_dir / "new-root-sentinel.txt",)
    assert (detached_root / relative_target).read_bytes() == b"desired"
    assert (detached_root / marker.name).is_file()
    assert _transaction_temporaries(detached_root)


@pytest.mark.skipif(
    os.name == "nt" or not obs_bootstrap._supports_posix_handle_relative_migration(),
    reason="POSIX copy-marker lexical bypass regression",
)
def test_settings_guard_reads_copy_marker_from_locked_root(
    monkeypatch,
    tmp_path,
):
    base_dir = (tmp_path / "obs-portable").absolute()
    detached_root = (tmp_path / "detached-obs-portable").absolute()
    base_dir.mkdir()
    marker = obs_bootstrap.get_obs_copy_in_progress_marker(base_dir)
    marker.write_text(
        json.dumps(
            {
                "schema_version": obs_bootstrap.OBS_COPY_JOURNAL_SCHEMA_VERSION,
                "source": str((tmp_path / "legacy-obs").absolute()),
                "source_fingerprint": "0" * 64,
                "phase": obs_bootstrap.OBS_MIGRATION_PHASE_COPYING,
                "owner_pid": None,
                "owner_token": "a" * 32,
                "started_at": None,
            }
        ),
        encoding="utf-8",
    )
    real_lexists = obs_bootstrap._path_lexists
    lexical_marker_checks = 0

    def hide_marker_during_lexical_check(path):
        nonlocal lexical_marker_checks
        if Path(path).absolute() == marker:
            lexical_marker_checks += 1
            base_dir.rename(detached_root)
            base_dir.mkdir()
            try:
                assert real_lexists(marker) is False
                return False
            finally:
                base_dir.rmdir()
                detached_root.rename(base_dir)
        return real_lexists(path)

    real_read_journal = obs_bootstrap._read_obs_migration_journal
    anchored_read = False

    def verify_anchored_read(path, *, directory_lease=None, expected_identity=None):
        nonlocal anchored_read
        assert directory_lease is not None
        assert directory_lease.path == base_dir
        assert expected_identity is not None
        anchored_read = True
        return real_read_journal(
            path,
            directory_lease=directory_lease,
            expected_identity=expected_identity,
        )

    monkeypatch.setattr(obs_bootstrap, "_path_lexists", hide_marker_during_lexical_check)
    monkeypatch.setattr(
        obs_bootstrap,
        "_read_obs_migration_journal",
        verify_anchored_read,
    )
    entered = False

    with pytest.raises(
        obs_bootstrap.OBSMigrationRecoveryRequiredError,
        match="コピー中marker",
    ):
        with obs_bootstrap.obs_config_mutation_guard(base_dir):
            entered = True

    assert entered is False
    assert lexical_marker_checks == 0
    assert anchored_read is True
    assert marker.is_file()
