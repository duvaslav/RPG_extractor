# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build.

The console backends (UberWolfCli, WolfTL) and their MIT licenses are bundled
into ``tools/`` and ``licenses/`` inside the executable when the files exist.
They are collected conditionally: a missing binary prints a warning and leaves
the build working for every engine that does not need it, instead of failing
the build outright.

Put the binaries in ``tools/`` before building — ``tools/README.md`` lists the
versions and SHA-256 hashes this project is pinned to.
"""

import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

SPEC_DIR = Path(os.path.abspath(SPECPATH))

hiddenimports = []
hiddenimports += collect_submodules('UnityPy.classes')
hiddenimports += collect_submodules('UnityPy.enums')

# --- console backends -------------------------------------------------------
binaries = []
for name in ['UberWolfCli.exe', 'WolfTL.exe']:
    candidate = SPEC_DIR / 'tools' / name
    if candidate.is_file():
        binaries.append((str(candidate), 'tools'))
        print(f'[spec] bundling tools/{name}')
    else:
        print(f'[spec] tools/{name} not found — building without it; the app will '
              f'report it as missing instead of crashing.')

# fmod is only needed for some Unity audio, so it is optional too.
FMOD_DLL = Path(
    os.environ.get(
        'FMOD_DLL',
        r'C:\Users\Duvakiller\AppData\Local\Programs\Python\Python312\Lib'
        r'\site-packages\fmod_toolkit\libfmod\Windows\x64\fmod.dll',
    )
)
if FMOD_DLL.is_file():
    binaries.append((str(FMOD_DLL), r'fmod_toolkit\libfmod\Windows\x64'))
else:
    print(f'[spec] fmod.dll not found at {FMOD_DLL} — building without it.')

# --- licenses ---------------------------------------------------------------
# MIT requires the copyright notice and license text to travel with the binary.
datas = []
for license_name in ['UberWolf-LICENSE.txt', 'WolfTL-LICENSE.txt']:
    candidate = SPEC_DIR / 'licenses' / license_name
    if candidate.is_file():
        datas.append((str(candidate), 'licenses'))
    else:
        print(f'[spec] licenses/{license_name} is missing — required when '
              f'shipping the matching binary.')


a = Analysis(
    ['rpg_maker_gui.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
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
