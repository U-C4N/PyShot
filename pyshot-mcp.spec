# Console entry point: stdout is reserved for MCP protocol messages.
from PyInstaller.utils.hooks import collect_all, copy_metadata

mcp_data, mcp_bins, mcp_imports = collect_all(
    'mcp', filter_submodules=lambda name: not name.startswith('mcp.cli'))
a = Analysis(
    ['pyshot_mcp.py'], pathex=[], binaries=mcp_bins,
    datas=mcp_data + copy_metadata('mcp') + copy_metadata('mcp-types'),
    hiddenimports=mcp_imports, hookspath=[], hooksconfig={}, runtime_hooks=[],
    excludes=['pytest', 'tkinter', 'numpy'], noarchive=False, optimize=0,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, a.binaries, a.datas, [], name='pyshot-mcp',
    debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
    console=True, disable_windowed_traceback=False,
)
