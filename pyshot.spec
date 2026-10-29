# -*- mode: python ; coding: utf-8 -*-
#
# Build a self-contained PyShot folder (no Python installation needed):
#
#     pip install pyinstaller
#     pyinstaller pyshot.spec
#     → dist\PyShot\PyShot.exe + dist\PyShot\_internal
#
# One-folder, not one-file: a one-file exe unpacks ~20 MB into %TEMP% on every
# launch (and Defender rescans it), which delayed the hotkeys by several
# seconds after logon. UPX is off for the same reason.
#
# console=False is the .exe equivalent of launching with pythonw: no black
# console window ever appears. Requires PyInstaller 6 or newer.
#
# win32com.client is imported lazily inside the startup manager, so the analyser
# cannot see it — it has to be named here or "add to startup" fails in the .exe.

a = Analysis(
    ['pyshot.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=['win32com.client'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['numpy', 'pytest'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='PyShot',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='PyShot')
