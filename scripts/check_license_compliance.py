"""Validate the license material included in a packaged application."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from src.license_info import validate_distribution_documents

REQUIRED_PACKAGE_LICENSES = {
    "PyQt6",
    "PyQt6-Qt6",
    "PyQt6-sip",
    "PyInstaller",
    "Python",
    "aiohttp",
    "numpy",
    "obsws-python",
    "opencv-python",
    "pandas",
    "python-mpv",
    "scikit-learn",
    "scipy",
}


def canonicalize_distribution_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).casefold()


def validate_package_manifest(manifest_path: Path) -> list[str]:
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"Cannot read license manifest: {exc}"]

    packages = payload.get("packages")
    if not isinstance(packages, list):
        return ["License manifest does not contain a package list."]

    by_name = {
        canonicalize_distribution_name(str(package.get("name", ""))): package
        for package in packages
        if isinstance(package, dict)
    }
    errors = []
    for package in packages:
        if not isinstance(package, dict):
            errors.append("License manifest contains an invalid package entry.")
            continue
        package_name = str(package.get("name", "")).strip() or "<unknown>"
        license_files = package.get("license_files")
        if not isinstance(license_files, list) or not license_files:
            errors.append(f"No license file was collected for package: {package_name}")
            continue
        for relative_path in license_files:
            if not isinstance(relative_path, str) or not (manifest_path.parent / relative_path).is_file():
                errors.append(f"Collected license file is missing for {package_name}: {relative_path}")

    for required_name in sorted(REQUIRED_PACKAGE_LICENSES):
        if canonicalize_distribution_name(required_name) not in by_name:
            errors.append(f"Required package is missing from license manifest: {required_name}")
    return errors


def validate_distribution(distribution_root: Path) -> list[str]:
    errors = [
        f"Required distribution document is missing: {relative_path}"
        for relative_path in validate_distribution_documents(distribution_root)
    ]
    manifest_path = distribution_root / "licenses" / "python-packages.json"
    if manifest_path.is_file():
        errors.extend(validate_package_manifest(manifest_path))
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("distribution_root", type=Path)
    args = parser.parse_args()
    errors = validate_distribution(args.distribution_root)
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1
    print(f"License compliance check passed: {args.distribution_root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
