import json
from pathlib import Path

from scripts import collect_licenses as license_collector
from scripts.check_license_compliance import (
    REQUIRED_PACKAGE_LICENSES,
    canonicalize_distribution_name,
    validate_distribution,
)
from scripts.collect_licenses import is_license_file, parse_requirement_names, safe_component_name
from src.license_info import REQUIRED_DISTRIBUTION_DOCUMENTS


def runtime_dir(name: str) -> Path:
    path = Path("tests") / "_tmp" / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def test_parse_requirement_names_ignores_comments_and_include_lines():
    root = runtime_dir("license_scripts_requirements")
    requirements = root / "requirements.txt"
    requirements.write_text(
        "# generated\n-r base.txt\nPyQt6==6.10.2\nobsws-python>=1.8\n",
        encoding="utf-8",
    )

    assert parse_requirement_names(requirements) == ["PyQt6", "obsws-python"]


def test_is_license_file_recognizes_metadata_license_paths():
    assert is_license_file(Path("package.dist-info/licenses/LICENSE.txt"))
    assert is_license_file(Path("package/COPYING"))
    assert not is_license_file(Path("packaging/licenses/_spdx.py"))
    assert not is_license_file(Path("package/module.py"))


def test_safe_component_name_rejects_parent_directory_components():
    assert safe_component_name("..") == "unknown"
    assert safe_component_name("PyQt6 sip") == "PyQt6-sip"


def test_canonicalize_distribution_name_treats_separators_equally():
    assert canonicalize_distribution_name("PyQt6_sip") == "pyqt6-sip"
    assert canonicalize_distribution_name("PyQt6.sip") == "pyqt6-sip"


def test_collect_distribution_licenses_is_idempotent(monkeypatch):
    root = runtime_dir("license_scripts_idempotent")
    metadata_license = root / "demo.dist-info" / "LICENSE"
    metadata_license.parent.mkdir(parents=True, exist_ok=True)
    metadata_license.write_text("MIT license", encoding="utf-8")

    class FakeDistribution:
        metadata = {"Name": "demo", "License-Expression": "MIT"}
        version = "1.0"
        files = [Path("demo.dist-info/LICENSE")]

        @staticmethod
        def locate_file(relative_path):
            return root / relative_path

    monkeypatch.setattr(
        license_collector.metadata,
        "distribution",
        lambda _distribution_name: FakeDistribution(),
    )
    destination = root / "licenses" / "python-packages"

    first = license_collector.collect_distribution_licenses("demo", destination)
    second = license_collector.collect_distribution_licenses("demo", destination)

    assert first["license_files"] == second["license_files"]
    assert second["license_files"] == [
        "python-packages/demo/demo.dist-info/LICENSE"
    ]


def test_validate_distribution_checks_manifest_and_copied_files():
    root = runtime_dir("license_scripts_distribution")
    for relative_path in REQUIRED_DISTRIBUTION_DOCUMENTS:
        path = root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("test", encoding="utf-8")

    packages = []
    for package_name in REQUIRED_PACKAGE_LICENSES:
        relative_license = f"python-packages/{package_name}/LICENSE"
        license_path = root / "licenses" / relative_license
        license_path.parent.mkdir(parents=True, exist_ok=True)
        license_path.write_text("license", encoding="utf-8")
        packages.append({"name": package_name, "license_files": [relative_license]})

    manifest_path = root / "licenses" / "python-packages.json"
    manifest_path.write_text(json.dumps({"packages": packages}), encoding="utf-8")

    assert validate_distribution(root) == []

    missing = root / "licenses" / "python-packages" / "PyQt6" / "LICENSE"
    missing.unlink()

    assert validate_distribution(root) == ["Collected license file is missing for PyQt6: python-packages/PyQt6/LICENSE"]
