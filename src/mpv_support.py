from __future__ import annotations

from pathlib import Path

# Keep this order aligned with python-mpv's Windows loader preference.
MPV_DLL_NAMES = ("mpv-2.dll", "libmpv-2.dll", "mpv-1.dll", "libmpv-1.dll")


def iter_mpv_search_dirs(bin_dir: str | Path | None, root_dir: str | Path | None = None) -> tuple[Path, ...]:
    candidates = []
    if bin_dir:
        candidates.append(Path(bin_dir))
    if root_dir:
        root = Path(root_dir)
        candidates.append(root / "bin")
        candidates.append(root)

    result = []
    seen = set()
    for candidate in candidates:
        try:
            key = str(candidate.resolve()).casefold()
        except Exception:
            key = str(candidate).casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(candidate)
    return tuple(result)


def find_mpv_dll(bin_dir: str | Path | None, root_dir: str | Path | None = None) -> Path | None:
    for base in iter_mpv_search_dirs(bin_dir, root_dir):
        if not base.exists():
            continue
        for name in MPV_DLL_NAMES:
            candidate = base / name
            if candidate.exists():
                return candidate
    return None


def has_mpv_dll(bin_dir: str | Path | None, root_dir: str | Path | None = None) -> bool:
    return find_mpv_dll(bin_dir, root_dir) is not None
