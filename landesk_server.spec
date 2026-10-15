# landesk_server.spec  (updated to use GUI launcher)
block_cipher = None

a = Analysis(
    ['server/server_gui.py'],
    pathex=['.'],
    binaries=[],
    datas=[
        ('shared/protocol.py', 'shared'),
        ('shared/auth.py',     'shared'),
        ('shared/tlsutil.py',  'shared'),
        ('server/server.py',   '.'),      # bundled so server_gui.py can import it
        ('server/ratelimit.py','.'),      # imported by server.py
        ('assets/icon.ico',    'assets'),
        ('assets/icon.png',    'assets'),
    ],
    hiddenimports=[
        'mss', 'mss.windows', 'mss.linux', 'mss.darwin',
        'PIL', 'PIL.Image', 'PIL.ImageTk', 'PIL._tkinter_finder',
        'pynput', 'pynput.mouse', 'pynput.keyboard',
        'pynput.mouse._win32', 'pynput.keyboard._win32',
        'pyperclip', 'pyperclip.backends',
        'tkinter', 'tkinter.ttk', 'tkinter.messagebox', 'tkinter.simpledialog',
        'asyncio', 'json', 'struct', 'hashlib', 'hmac', 'io', 'threading',
        'logging', 'argparse', 'importlib.util', 'socket', 'ssl', 'getpass',
        # The certificate the server generates on each start comes from these.
        'cryptography', 'cryptography.hazmat.primitives.asymmetric.ec',
        'cryptography.hazmat.primitives.serialization', 'cryptography.x509',
    ],
    hookspath=[], hooksconfig={}, runtime_hooks=[],
    excludes=['matplotlib', 'numpy', 'scipy'],
    cipher=block_cipher, noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz, a.scripts, a.binaries, a.zipfiles, a.datas, [],
    name='LANDesk-Server',
    debug=False, bootloader_ignore_signals=False,
    strip=False, upx=True, upx_exclude=[],
    runtime_tmpdir=None,
    console=False,   # GUI window, no black console box
    target_arch=None, codesign_identity=None, entitlements_file=None,
    icon='assets/icon.ico',
)
