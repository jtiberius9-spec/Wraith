# PyInstaller spec for the macOS build — produces Wraith.app.
# RUN ON A MAC (PyInstaller cannot cross-build from Windows):
#   pip install pyinstaller pillow -r requirements.txt
#   ./build_mac.sh        (stages bin/ with mac adb+ffmpeg+scrcpy-server, then this)
from PyInstaller.utils.hooks import collect_all

datas = [('bin', 'bin'), ('keymaps', 'keymaps'),
         ('wraith_icon_preview.png', '.')]
binaries = []
hiddenimports = [
    'wraith.mirror', 'wraith.launcher', 'wraith.editor', 'wraith.injector',
    'wraith.control', 'wraith.keymap', 'wraith.runtime', 'wraith.toolbar',
    'wraith.updater', 'wraith.perf',
]
for pkg in ('av', 'cv2', 'sounddevice', 'pygame'):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

a = Analysis(['wraith_app.py'], pathex=['.'], binaries=binaries, datas=datas,
             hiddenimports=hiddenimports, excludes=['test', 'tkinter.test'])
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='Wraith',
          console=False, icon='wraith.icns')
coll = COLLECT(exe, a.binaries, a.datas, name='Wraith')
app = BUNDLE(
    coll, name='Wraith.app', icon='wraith.icns',
    bundle_identifier='com.wraith.app',
    info_plist={
        'CFBundleShortVersionString': '0.3.0',
        'NSHighResolutionCapable': True,
        # macOS REQUIRES these or the app is denied mic/network access:
        'NSMicrophoneUsageDescription': 'Wraith records your microphone into gameplay clips.',
        'NSLocalNetworkUsageDescription': 'Wraith connects to your phone over adb.',
    },
)
