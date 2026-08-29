from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from scripts import binary_install_policy as target

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def _lock() -> dict:
    return json.loads(
        (REPOSITORY_ROOT / "compliance" / "components.json").read_text(
            encoding="utf-8"
        )
    )


def _component(lock: dict, name: str) -> dict:
    return next(
        item
        for section in ("runtime_components", "build_components")
        for item in lock[section]
        if item.get("component") == name
    )


def test_repository_policy_selects_only_fixed_external_runtime_wheels() -> None:
    lock = _lock()
    policy = target.external_vc_runtime_policy(lock)

    assert policy is not None
    assert set(policy["wheels"]) == {"numpy", "pandas", "qt", "scikit-learn"}
    source, archive = target.expected_install_archive(
        lock, _component(lock, "numpy")
    )
    assert source == "external-vc-runtime-wheel"
    assert archive == policy["wheels"]["numpy"]

    source, archive = target.expected_install_archive(
        lock, _component(lock, "opencv-python")
    )
    assert source == "locked-wheel"
    assert archive["sha256"] == _component(lock, "opencv-python")[
        "binary_archive"
    ]["sha256"]


def test_policy_removal_restores_upstream_wheel_selection() -> None:
    lock = _lock()
    lock.pop(target.POLICY_KEY)
    component = _component(lock, "numpy")

    source, archive = target.expected_install_archive(lock, component)

    assert source == "locked-wheel"
    assert archive["sha256"] == component["binary_archive"]["sha256"]


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("minimum_redistributable_version", "14.44.65536.0", "out of range"),
        ("minimum_redistributable_version", "14.44.35211", "minimum version"),
    ],
)
def test_policy_rejects_invalid_runtime_version(
    field: str, value: str, message: str
) -> None:
    lock = _lock()
    lock[target.POLICY_KEY][field] = value

    with pytest.raises(target.BinaryInstallPolicyError, match=message):
        target.external_vc_runtime_policy(lock)


def test_policy_rejects_duplicate_component_definition() -> None:
    lock = _lock()
    lock["runtime_components"].append(
        deepcopy(_component(lock, "numpy"))
    )

    with pytest.raises(target.BinaryInstallPolicyError, match="duplicated"):
        target.external_vc_runtime_policy(lock)


def test_policy_rejects_windows_unsafe_archive_name() -> None:
    lock = _lock()
    lock[target.POLICY_KEY]["wheels"][0]["filename"] = "C:numpy.whl"

    with pytest.raises(target.BinaryInstallPolicyError, match="filename"):
        target.external_vc_runtime_policy(lock)


def test_policy_rejects_changed_custom_wheel_hash() -> None:
    lock = _lock()
    lock[target.POLICY_KEY]["wheels"][0]["sha256"] = "not-a-sha256"

    with pytest.raises(target.BinaryInstallPolicyError, match="SHA256"):
        target.external_vc_runtime_policy(lock)
