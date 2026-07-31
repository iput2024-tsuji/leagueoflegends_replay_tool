from pathlib import Path

from src.license_info import (
    REQUIRED_DISTRIBUTION_DOCUMENTS,
    build_about_html,
    read_app_version,
    validate_distribution_documents,
)


def runtime_dir(name: str) -> Path:
    path = Path("tests") / "_tmp" / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def test_read_app_version_uses_packaged_version_file():
    root = runtime_dir("license_info_version")
    (root / "VERSION").write_text("1.2.3\n", encoding="utf-8")

    assert read_app_version(root) == "1.2.3"


def test_build_about_html_explains_license_source_and_warranty():
    html = build_about_html("1<&")

    assert "1&lt;&amp;" in html
    assert "GPL-3.0-only" in html
    assert "対応ソースコード" in html
    assert "無保証" in html


def test_validate_distribution_documents_reports_only_missing_files():
    root = runtime_dir("license_info_distribution")
    for relative_path in REQUIRED_DISTRIBUTION_DOCUMENTS:
        path = root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("test", encoding="utf-8")

    assert validate_distribution_documents(root) == []

    (root / "SOURCE_OFFER.md").unlink()

    assert validate_distribution_documents(root) == ["SOURCE_OFFER.md"]
