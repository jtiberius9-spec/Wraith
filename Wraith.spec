# PyInstaller spec for Wraith — single exe (launcher + mirror via argv dispatch).
# Bundles adb + scrcpy-server + ffmpeg under bin/, seeds keymaps/, and collects
# the native libs PyAV / OpenCV / sounddevice / pygame ship.
from PyInstaller.utils.hooks import collect_all

datas = [('bin', 'bin'), ('keymaps', 'keymaps'),
         ('wraith.ico', '.'), ('wraith_icon_preview.png', '.')]
binaries = []
hiddenimports = [
    'wraith.mirror', 'wraith.launcher', 'wraith.editor',
    'wraith.injector', 'wraith.control', 'wraith.keymap', 'wraith.runtime',
    'wraith.toolbar', 'wraith.updater',
]

# pull in each native package's data files + DLLs + submodules
for pkg in ('av', 'cv2', 'sounddevice', 'pygame'):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

a = Analysis(
    ['wraith_app.py'],
    pathex=['.'],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=['test', 'tkinter.test', 'unittest', 'pytest'],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name='Wraith',
    debug=False,
    strip=False,
    upx=False,
    console=False,         # finished product: no console window (logs -> wraith.log)
    disable_windowed_traceback=False,
    icon='wraith.ico',
)
coll = COLLECT(
    exe, a.binaries, a.datas,
    strip=False, upx=False, name='Wraith',
)
