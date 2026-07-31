from pathlib import Path

from src.license_info import (
    REQUIRED_DISTRIBUTION_DOCUMENTS,
    build_about_html,
    read_app_version,
    validate_distribution_documents,
)


def test_read_app_version_uses_packaged_version_file(tmp_path):
    root = tmp_path / "distribution"
    root.mkdir()
    (root / "VERSION").write_text("1.2.3\n", encoding="utf-8")

    assert read_app_version(root) == "1.2.3"


def test_build_about_html_explains_license_source_and_warranty():
    html = build_about_html("1<&")

    assert "1&lt;&amp;" in html
    assert "GPL-3.0-only" in html
    assert "対応ソースコード" in html
    assert "無保証" in html


def test_build_about_html_prefers_bundled_documents(tmp_path):
    root = tmp_path / "distribution with spaces"
    root.mkdir()
    for name in (
        "LICENSE",
        "THIRD_PARTY_NOTICES.md",
        "SOURCE_OFFER.md",
        "QT_RELINKING.md",
    ):
        (root / name).write_text("test", encoding="utf-8")

    html = build_about_html("1.2.3", document_root=root)

    assert (root / "LICENSE").resolve().as_uri() in html
    assert (root / "THIRD_PARTY_NOTICES.md").resolve().as_uri() in html
    assert (root / "SOURCE_OFFER.md").resolve().as_uri() in html
    assert (root / "QT_RELINKING.md").resolve().as_uri() in html
    assert "/blob/v1.2.3/LICENSE" not in html
    assert "/tree/v1.2.3" in html


def test_build_about_html_uses_release_tag_for_online_fallback(tmp_path):
    html = build_about_html("1.2.3", document_root=tmp_path)

    assert "/blob/v1.2.3/LICENSE" in html
    assert "/blob/v1.2.3/THIRD_PARTY_NOTICES.md" in html
    assert "/blob/v1.2.3/QT_RELINKING.md" in html
    assert "/tree/v1.2.3" in html
    assert "/blob/main/" not in html


def test_build_about_html_uses_main_only_for_development(tmp_path):
    html = build_about_html("development", document_root=tmp_path)

    assert "/blob/main/LICENSE" in html
    assert "/tree/main" in html


def test_validate_distribution_documents_reports_only_missing_files(tmp_path):
    root = tmp_path / "distribution"
    for relative_path in REQUIRED_DISTRIBUTION_DOCUMENTS:
        path = root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("test", encoding="utf-8")

    assert validate_distribution_documents(root) == []

    (root / "SOURCE_OFFER.md").unlink()

    assert validate_distribution_documents(root) == ["SOURCE_OFFER.md"]


def test_installer_uses_plain_text_third_party_notice():
    installer_script = Path("installer/LoLReplayTool.iss").read_text(encoding="utf-8")
    notice_path = Path("installer/THIRD_PARTY_NOTICES.txt")

    assert "InfoBeforeFile=THIRD_PARTY_NOTICES.txt" in installer_script
    assert "InfoBeforeFile=..\\THIRD_PARTY_NOTICES.md" not in installer_script
    assert notice_path.is_file()
    assert "GPL-3.0-only" in notice_path.read_text(encoding="utf-8")
