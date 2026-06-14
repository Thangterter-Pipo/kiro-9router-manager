# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['scripts\\kiro_9router_gui.py'],
    pathex=[],
    binaries=[],
    datas=[('scripts\\ninerouter_kiro_idc_auto_import.mjs', 'scripts')],
    hiddenimports=['scripts.kiro_9router_app', 'scripts.ninerouter_kiro_login', 'scripts.kiro_account_store', 'scripts.kiro_ide_login', 'scripts.kiro_json_login', 'scripts.kiro_device_login'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
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
    name='Kiro9RouterImporter',
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
