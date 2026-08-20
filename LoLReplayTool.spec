# -*- mode: python ; coding: utf-8 -*-

from scripts.pyinstaller_runtime_policy import apply_windows_runtime_policy

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=[],
    datas=[
        ("config\\setting.sample.json", "config"),
        ("config\\champion_aliases.json", "config"),
        ("assets\\app\\app.ico", "assets\\app"),
    ],
    hiddenimports=["mpv"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["PIL"],
    noarchive=False,
    optimize=0,
)
a.binaries = apply_windows_runtime_policy(a.binaries)
# Analysis writes its cache before the spec can apply the runtime policy. Keep
# the provenance TOC aligned with the exact set later handed to COLLECT.
a._save_guts()
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='LoLReplayTool',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=["assets\\app\\app.ico"],
    contents_directory="_internal",
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="LoLReplayTool",
)
