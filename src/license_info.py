from __future__ import annotations

from html import escape
from pathlib import Path

from .app_paths import get_app_root, get_resource_root

PROJECT_NAME = "LoL Replay Tool"
PROJECT_LICENSE = "GPL-3.0-only"
PROJECT_SOURCE_URL = "https://github.com/iput2024-tsuji/leagueoflegends_replay_tool"
PROJECT_LICENSE_URL = f"{PROJECT_SOURCE_URL}/blob/main/LICENSE"
THIRD_PARTY_NOTICES_URL = f"{PROJECT_SOURCE_URL}/blob/main/THIRD_PARTY_NOTICES.md"

REQUIRED_DISTRIBUTION_DOCUMENTS = (
    "LICENSE",
    "THIRD_PARTY_NOTICES.md",
    "SOURCE_OFFER.md",
    "VERSION",
    "licenses/python-packages.json",
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
            version = version_file.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if version:
            return version
    return "development"


def build_about_html(version: str | None = None) -> str:
    display_version = escape(version or read_app_version())
    return (
        f"<h3>{PROJECT_NAME}</h3>"
        f"<p>Version {display_version}</p>"
        f"<p>Copyright © {PROJECT_NAME} Contributors</p>"
        f"<p>このプログラムは <b>{PROJECT_LICENSE}</b> で提供される"
        f"フリーソフトウェアです。法律で認められる範囲で無保証です。</p>"
        f'<p><a href="{PROJECT_SOURCE_URL}">対応ソースコード</a><br>'
        f'<a href="{PROJECT_LICENSE_URL}">GNU GPL version 3 本文</a><br>'
        f'<a href="{THIRD_PARTY_NOTICES_URL}">第三者ソフトウェア通知</a></p>'
        "<p>オフラインのライセンス原文はインストール先の "
        "<code>LICENSE</code> と <code>licenses</code> フォルダーにあります。</p>"
    )


def validate_distribution_documents(distribution_root: Path) -> list[str]:
    return [
        relative_path
        for relative_path in REQUIRED_DISTRIBUTION_DOCUMENTS
        if not (distribution_root / Path(relative_path)).is_file()
    ]
