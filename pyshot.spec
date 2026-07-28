# -*- mode: python ; coding: utf-8 -*-
#
# Build one self-contained PyShot.exe (no Python installation needed):
#
#     pip install pyinstaller
#     pyinstaller pyshot.spec
#     → dist\PyShot.exe
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
    a.binaries,
    a.datas,
    [],
    name='PyShot',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
