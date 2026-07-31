"""Collect license files for pinned Python runtime dependencies."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from importlib import metadata
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_REQUIREMENTS = REPO_ROOT / "requirements.txt"
PROJECT_DOCUMENTS = ("LICENSE", "THIRD_PARTY_NOTICES.md", "SOURCE_OFFER.md", "VERSION")
PACKAGING_DISTRIBUTIONS = ("PyInstaller",)
LICENSE_FILE_PREFIXES = (
    "license",
    "copying",
    "notice",
    "copyright",
    "authors",
)


def parse_requirement_names(requirements_file: Path) -> list[str]:
    names = []
    for raw_line in requirements_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("#", "-", "--")):
            continue
        name = re.split(r"\s*(?:===|==|~=|!=|<=|>=|<|>|@)\s*", line, maxsplit=1)[0]
        if name:
            names.append(name)
    return names


def is_license_file(relative_path: Path) -> bool:
    name = relative_path.name.casefold()
    if any(name.startswith(prefix) for prefix in LICENSE_FILE_PREFIXES):
        return True
    return any(part.casefold() == "license_files" for part in relative_path.parts)


def safe_component_name(value: str) -> str:
    result = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-") or "unknown"
    return "unknown" if result in {".", ".."} else result


def collect_distribution_licenses(
    distribution_name: str,
    destination_root: Path,
) -> dict[str, object]:
    distribution = metadata.distribution(distribution_name)
    canonical_name = distribution.metadata.get("Name") or distribution_name
    package_dir = destination_root / safe_component_name(canonical_name)
    copied_files = []

    for relative_path in distribution.files or ():
        relative = Path(str(relative_path))
        if not is_license_file(relative):
            continue
        source = Path(distribution.locate_file(relative))
        if not source.is_file():
            continue
        safe_relative = Path(*(safe_component_name(part) for part in relative.parts))
        target = package_dir / safe_relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and target.read_bytes() == source.read_bytes():
            copied_files.append(target.relative_to(destination_root.parent).as_posix())
            continue
        shutil.copy2(source, target)
        copied_files.append(target.relative_to(destination_root.parent).as_posix())

    return {
        "name": canonical_name,
        "version": distribution.version,
        "license_expression": distribution.metadata.get("License-Expression")
        or distribution.metadata.get("License")
        or "UNKNOWN",
        "homepage": distribution.metadata.get("Home-page") or "",
        "license_files": sorted(copied_files),
    }


def collect_python_runtime_license(destination_root: Path) -> dict[str, object]:
    license_candidates = [
        Path(sys.base_prefix) / "LICENSE.txt",
        Path(sys.prefix) / "LICENSE.txt",
    ]
    license_source = next((path for path in license_candidates if path.is_file()), None)
    if license_source is None:
        raise RuntimeError("Python runtime LICENSE.txt was not found.")

    notices_candidates = [
        Path(sys.base_prefix) / "Doc" / "html" / "license.html",
        Path(sys.prefix) / "Doc" / "html" / "license.html",
    ]
    notices_source = next((path for path in notices_candidates if path.is_file()), None)
    if notices_source is None:
        raise RuntimeError("Python runtime third-party license page was not found.")

    package_dir = destination_root / "Python"
    package_dir.mkdir(parents=True, exist_ok=True)
    license_target = package_dir / "LICENSE.txt"
    notices_target = package_dir / "third-party-licenses.html"
    shutil.copy2(license_source, license_target)
    shutil.copy2(notices_source, notices_target)
    return {
        "name": "Python",
        "version": sys.version.split()[0],
        "license_expression": "PSF-2.0",
        "homepage": "https://www.python.org/",
        "license_files": [
            license_target.relative_to(destination_root.parent).as_posix(),
            notices_target.relative_to(destination_root.parent).as_posix(),
        ],
    }


def collect_licenses(destination: Path, requirements_file: Path = RUNTIME_REQUIREMENTS) -> Path:
    destination.mkdir(parents=True, exist_ok=True)
    for document in PROJECT_DOCUMENTS:
        source = REPO_ROOT / document
        if not source.is_file():
            raise RuntimeError(f"Required project document is missing: {source}")
        shutil.copy2(source, destination.parent / document)

    package_root = destination / "python-packages"
    package_root.mkdir(parents=True, exist_ok=True)
    manifest = [collect_python_runtime_license(package_root)]
    distribution_names = dict.fromkeys([*parse_requirement_names(requirements_file), *PACKAGING_DISTRIBUTIONS])
    for requirement_name in distribution_names:
        manifest.append(collect_distribution_licenses(requirement_name, package_root))

    manifest_path = destination / "python-packages.json"
    manifest_path.write_text(
        json.dumps(
            {
                "generated_from": requirements_file.name,
                "packages": manifest,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return manifest_path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--destination", required=True, type=Path)
    parser.add_argument("--requirements", type=Path, default=RUNTIME_REQUIREMENTS)
    args = parser.parse_args()
    manifest_path = collect_licenses(args.destination, args.requirements)
    print(f"License manifest created: {manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
