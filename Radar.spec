# -*- mode: python ; coding: utf-8 -*-
# Construir con ./build.sh. Solo entra código y lo que sirve la app: index.html y
# el config de ejemplo. Los datos (jobs.db, config.json, cv.md…) NO van aquí:
# viven en ~/Library/Application Support/Radar (db._carpeta_datos).
a = Analysis(
    ['app.py'],
    pathex=[],
    binaries=[],
    datas=[('index.html', '.'), ('config.example.json', '.')],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='Radar', debug=False,
          bootloader_ignore_signals=False, strip=False, upx=False, console=False,
          disable_windowed_traceback=False, argv_emulation=False, target_arch='universal2',
          codesign_identity=None, entitlements_file=None, icon=['build/icon.icns'])
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, upx_exclude=[], name='Radar')
app = BUNDLE(coll, name='Radar.app', icon='build/icon.icns',
             bundle_identifier='com.nachosanbenito.radar',
             info_plist={'CFBundleShortVersionString': '0.1.0', 'NSHighResolutionCapable': True,
                         'LSMinimumSystemVersion': '11.0', 'NSHumanReadableCopyright': 'Radar · MIT'})
