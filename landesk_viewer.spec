# landesk_viewer.spec
# PyInstaller spec file for LANDesk Viewer
# Build with:  pyinstaller landesk_viewer.spec

import sys
import os

block_cipher = None

a = Analysis(
    ['viewer/viewer.py'],
    pathex=['.'],
    binaries=[],
    datas=[
        ('shared/protocol.py', 'shared'),
        ('shared/auth.py',     'shared'),
        ('shared/tlsutil.py',  'shared'),
        ('assets/icon.ico',    'assets'),
        ('assets/icon.png',    'assets'),
    ],
    hiddenimports=[
        'PIL',
        'PIL.Image',
        'PIL.ImageTk',
        'PIL._tkinter_finder',
        'pyperclip',
        'pyperclip.backends',
        'tkinter',
        'tkinter.ttk',
        'tkinter.messagebox',
        'tkinter.simpledialog',
        'asyncio',
        'json',
        'struct',
        'hashlib',
        'hmac',
        'io',
        'ssl',
        'threading',
        'logging',
        'argparse',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['mss', 'pynput', 'matplotlib', 'numpy', 'scipy'],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='LANDesk-Viewer',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,         # No console window for the viewer (it has a GUI)
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='assets/icon.ico',
)
