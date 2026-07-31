from __future__ import annotations

import re
import stat
from html import escape
from pathlib import Path

from .app_paths import get_app_root, get_resource_root

PROJECT_NAME = "LoL Replay Tool"
PROJECT_LICENSE = "GPL-3.0-only"
PROJECT_SOURCE_URL = "https://github.com/iput2024-tsuji/leagueoflegends_replay_tool"
_RELEASE_VERSION_PATTERN = re.compile(
    r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?\Z"
)

REQUIRED_DISTRIBUTION_DOCUMENTS = (
    "LICENSE",
    "THIRD_PARTY_NOTICES.md",
    "SOURCE_OFFER.md",
    "QT_RELINKING.md",
    "VERSION",
    "licenses/python-packages.json",
)
FILE_ATTRIBUTE_REPARSE_POINT = 0x400


def _is_nonempty_regular_file(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return False
    return (
        stat.S_ISREG(metadata.st_mode)
        and not stat.S_ISLNK(metadata.st_mode)
        and not (
            getattr(metadata, "st_file_attributes", 0)
            & FILE_ATTRIBUTE_REPARSE_POINT
        )
        and metadata.st_size > 0
    )


def read_app_version(resource_root: Path | None = None) -> str:
    roots = [resource_root or get_resource_root(), get_app_root()]
    seen: set[Path] = set()
    for root in roots:
        resolved = root.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        version_file = resolved / "VERSION"
        try:
            if not _is_nonempty_regular_file(version_file):
                continue
            version = version_file.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if version:
            return version
    return "development"


def _source_ref(version: str) -> str:
    if _RELEASE_VERSION_PATTERN.fullmatch(version):
        return f"v{version}"
    return "main"


def _document_roots(document_root: Path | None) -> tuple[Path, ...]:
    if document_root is not None:
        return (document_root,)
    return (get_app_root(), get_resource_root())


def _document_href(
    relative_path: str,
    *,
    source_ref: str,
    document_root: Path | None,
) -> str:
    seen: set[Path] = set()
    for root in _document_roots(document_root):
        resolved_root = root.resolve()
        if resolved_root in seen:
            continue
        seen.add(resolved_root)
        document_path = resolved_root / relative_path
        if _is_nonempty_regular_file(document_path):
            return document_path.resolve().as_uri()
    return f"{PROJECT_SOURCE_URL}/blob/{source_ref}/{relative_path}"


def build_about_html(
    version: str | None = None,
    *,
    document_root: Path | None = None,
) -> str:
    raw_version = version or read_app_version()
    display_version = escape(raw_version)
    source_ref = _source_ref(raw_version)
    source_url = f"{PROJECT_SOURCE_URL}/tree/{source_ref}"
    license_url = _document_href(
        "LICENSE",
        source_ref=source_ref,
        document_root=document_root,
    )
    notices_url = _document_href(
        "THIRD_PARTY_NOTICES.md",
        source_ref=source_ref,
        document_root=document_root,
    )
    source_offer_url = _document_href(
        "SOURCE_OFFER.md",
        source_ref=source_ref,
        document_root=document_root,
    )
    qt_relinking_url = _document_href(
        "QT_RELINKING.md",
        source_ref=source_ref,
        document_root=document_root,
    )
    return (
        f"<h3>{PROJECT_NAME}</h3>"
        f"<p>Version {display_version}</p>"
        f"<p>Copyright © {PROJECT_NAME} Contributors</p>"
        f"<p>このプログラムは <b>{PROJECT_LICENSE}</b> で提供される"
        f"フリーソフトウェアです。法律で認められる範囲で無保証です。</p>"
        f'<p><a href="{escape(source_offer_url, quote=True)}">'
        "対応ソースの案内</a><br>"
        f'<a href="{escape(license_url, quote=True)}">GNU GPL version 3 本文</a><br>'
        f'<a href="{escape(notices_url, quote=True)}">'
        "第三者ソフトウェア通知</a><br>"
        f'<a href="{escape(qt_relinking_url, quote=True)}">'
        "Qtライブラリの交換手順</a><br>"
        f'<a href="{escape(source_url, quote=True)}">'
        "対応ソースコード（オンライン）</a></p>"
        "<p>同梱資料がある場合はインストール先のファイルを開きます。"
        "Python依存関係のライセンス原文とビルドinventoryは "
        "<code>licenses</code> フォルダーにあります。</p>"
    )


def validate_distribution_documents(distribution_root: Path) -> list[str]:
    return [
        relative_path
        for relative_path in REQUIRED_DISTRIBUTION_DOCUMENTS
        if not _is_nonempty_regular_file(
            distribution_root / Path(relative_path)
        )
    ]
