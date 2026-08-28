# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_submodules

hiddenimports = []
hiddenimports += collect_submodules('UnityPy.classes')
hiddenimports += collect_submodules('UnityPy.enums')


a = Analysis(
    ['rpg_maker_gui.py'],
    pathex=[],
    binaries=[('C:\\Users\\Duvakiller\\AppData\\Local\\Programs\\Python\\Python312\\Lib\\site-packages\\fmod_toolkit\\libfmod\\Windows\\x64\\fmod.dll', 'fmod_toolkit\\libfmod\\Windows\\x64')],
    datas=[],
    hiddenimports=hiddenimports,
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
    name='RPGMakerExtractor',
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
