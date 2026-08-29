"""Resolve the exact wheel bytes allowed in a verified build environment."""

from __future__ import annotations

import re
from pathlib import PurePath
from typing import Any

POLICY_KEY = "external_vc_runtime_policy"
_SHA256 = re.compile(r"[0-9a-f]{64}")
_VERSION = re.compile(r"\d+\.\d+\.\d+\.\d+")
_EXPECTED_POLICY_KEYS = {
    "schema_version",
    "minimum_redistributable_version",
    "official_information_url",
    "recipe",
    "required_components",
    "tool_artifacts",
    "wheels",
}
_EXPECTED_ARCHIVE_KEYS = {"filename", "sha256", "size"}
_EXPECTED_TOOL_KEYS = {"filename", "url", "sha256", "size"}
_WINDOWS_RESERVED_NAMES = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{index}" for index in range(1, 10)),
    *(f"lpt{index}" for index in range(1, 10)),
}


class BinaryInstallPolicyError(ValueError):
    """The component lock does not define a safe binary-install policy."""


def _validate_filename(value: Any, *, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or PurePath(value).name != value
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
        or ":" in value
        or "\x00" in value
        or value.endswith((" ", "."))
        or value.split(".", 1)[0].casefold() in _WINDOWS_RESERVED_NAMES
    ):
        raise BinaryInstallPolicyError(f"{label} filename is invalid")
    return value


def _validate_archive(value: Any, *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != _EXPECTED_ARCHIVE_KEYS:
        raise BinaryInstallPolicyError(f"{label} archive fields are invalid")
    filename = _validate_filename(value.get("filename"), label=label)
    digest = value.get("sha256")
    size = value.get("size")
    if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
        raise BinaryInstallPolicyError(f"{label} SHA256 is invalid")
    if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
        raise BinaryInstallPolicyError(f"{label} size is invalid")
    return {"filename": filename, "sha256": digest, "size": size}


def external_vc_runtime_policy(
    lock: dict[str, Any],
) -> dict[str, Any] | None:
    """Validate and normalize the optional external VC++ Runtime wheel policy."""

    raw = lock.get(POLICY_KEY)
    if raw is None:
        return None
    if not isinstance(raw, dict) or set(raw) != _EXPECTED_POLICY_KEYS:
        raise BinaryInstallPolicyError(
            "external VC++ Runtime policy fields are invalid"
        )
    if raw.get("schema_version") != 1:
        raise BinaryInstallPolicyError(
            "external VC++ Runtime policy schema is unsupported"
        )
    minimum = raw.get("minimum_redistributable_version")
    if not isinstance(minimum, str) or _VERSION.fullmatch(minimum) is None:
        raise BinaryInstallPolicyError(
            "external VC++ Runtime minimum version is invalid"
        )
    if any(int(part) > 65_535 for part in minimum.split(".")):
        raise BinaryInstallPolicyError(
            "external VC++ Runtime minimum version component is out of range"
        )
    official_url = raw.get("official_information_url")
    if (
        not isinstance(official_url, str)
        or not official_url.startswith("https://learn.microsoft.com/")
    ):
        raise BinaryInstallPolicyError(
            "external VC++ Runtime information URL is not Microsoft Learn"
        )
    if raw.get("recipe") != "scripts/prepare_external_vc_runtime_wheels.py":
        raise BinaryInstallPolicyError("external VC++ Runtime recipe differs")

    required = raw.get("required_components")
    if (
        not isinstance(required, list)
        or not required
        or not all(isinstance(item, str) and item for item in required)
        or len(required) != len(set(required))
    ):
        raise BinaryInstallPolicyError(
            "external VC++ Runtime component set is invalid"
        )

    components: dict[str, dict[str, Any]] = {}
    for section in ("runtime_components", "build_components"):
        entries = lock.get(section, [])
        if not isinstance(entries, list):
            raise BinaryInstallPolicyError(
                f"external VC++ Runtime component section is invalid: {section}"
            )
        for item in entries:
            if not isinstance(item, dict):
                raise BinaryInstallPolicyError(
                    f"external VC++ Runtime component entry is invalid: {section}"
                )
            component_name = item.get("component")
            if not isinstance(component_name, str) or not component_name:
                raise BinaryInstallPolicyError(
                    f"external VC++ Runtime component name is invalid: {section}"
                )
            if component_name in components:
                raise BinaryInstallPolicyError(
                    f"external VC++ Runtime component is duplicated: {component_name}"
                )
            components[component_name] = item
    raw_wheels = raw.get("wheels")
    if not isinstance(raw_wheels, list) or len(raw_wheels) != len(required):
        raise BinaryInstallPolicyError(
            "external VC++ Runtime wheel set differs from required components"
        )
    wheels: dict[str, dict[str, Any]] = {}
    for item in raw_wheels:
        if not isinstance(item, dict) or set(item) != {
            "component",
            *_EXPECTED_ARCHIVE_KEYS,
        }:
            raise BinaryInstallPolicyError(
                "external VC++ Runtime wheel record is invalid"
            )
        component_name = item.get("component")
        if not isinstance(component_name, str) or component_name in wheels:
            raise BinaryInstallPolicyError(
                "external VC++ Runtime wheel component is duplicated or invalid"
            )
        component = components.get(component_name)
        original = component.get("binary_archive") if component else None
        archive = _validate_archive(
            {key: item.get(key) for key in _EXPECTED_ARCHIVE_KEYS},
            label=f"external VC++ Runtime wheel {component_name}",
        )
        if (
            not isinstance(original, dict)
            or archive["filename"] != original.get("filename")
        ):
            raise BinaryInstallPolicyError(
                f"external VC++ Runtime wheel does not match its source: "
                f"{component_name}"
            )
        wheels[component_name] = archive
    if set(wheels) != set(required):
        raise BinaryInstallPolicyError(
            "external VC++ Runtime wheel component set differs"
        )

    raw_tools = raw.get("tool_artifacts")
    if not isinstance(raw_tools, list) or not raw_tools:
        raise BinaryInstallPolicyError(
            "external VC++ Runtime tool artifacts are missing"
        )
    tools: list[dict[str, Any]] = []
    seen_tools: set[str] = set()
    for item in raw_tools:
        if not isinstance(item, dict) or set(item) != _EXPECTED_TOOL_KEYS:
            raise BinaryInstallPolicyError(
                "external VC++ Runtime tool artifact fields are invalid"
            )
        archive = _validate_archive(
            {key: item.get(key) for key in _EXPECTED_ARCHIVE_KEYS},
            label="external VC++ Runtime tool",
        )
        url = item.get("url")
        if not isinstance(url, str) or not url.startswith(
            "https://files.pythonhosted.org/"
        ):
            raise BinaryInstallPolicyError(
                "external VC++ Runtime tool URL is not files.pythonhosted.org"
            )
        collision_key = archive["filename"].casefold()
        if collision_key in seen_tools:
            raise BinaryInstallPolicyError(
                "external VC++ Runtime tool artifact is duplicated"
            )
        seen_tools.add(collision_key)
        tools.append({**archive, "url": url})

    return {
        "schema_version": 1,
        "minimum_redistributable_version": minimum,
        "official_information_url": official_url,
        "recipe": raw["recipe"],
        "required_components": list(required),
        "tool_artifacts": tools,
        "wheels": wheels,
    }


def expected_install_archive(
    lock: dict[str, Any],
    component: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    """Return the source kind and exact wheel expected to be installed."""

    original = component.get("binary_archive")
    if not isinstance(original, dict):
        raise BinaryInstallPolicyError(
            f"component has no binary archive: {component.get('component')}"
        )
    validated_original = _validate_archive(
        {key: original.get(key) for key in _EXPECTED_ARCHIVE_KEYS},
        label=f"component {component.get('component')}",
    )
    policy = external_vc_runtime_policy(lock)
    if policy is None:
        return "locked-wheel", validated_original
    replacement = policy["wheels"].get(str(component.get("component")))
    if replacement is None:
        return "locked-wheel", validated_original
    return "external-vc-runtime-wheel", dict(replacement)
