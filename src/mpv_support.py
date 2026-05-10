from __future__ import annotations

from pathlib import Path

MPV_DLL_NAMES = ("mpv-1.dll", "libmpv-1.dll", "mpv-2.dll", "libmpv-2.dll")


def find_mpv_dll(bin_dir: str | Path | None, root_dir: str | Path | None = None) -> Path | None:
    bases = []
    if bin_dir:
        bases.append(Path(bin_dir))
    if root_dir:
        bases.append(Path(root_dir))

    for base in bases:
        if not base.exists():
            continue
        for name in MPV_DLL_NAMES:
            candidate = base / name
            if candidate.exists():
                return candidate
    return None


def has_mpv_dll(bin_dir: str | Path | None, root_dir: str | Path | None = None) -> bool:
    return find_mpv_dll(bin_dir, root_dir) is not None
