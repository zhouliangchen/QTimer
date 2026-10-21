"""Build with a deterministic DLL search path, isolated from unrelated tools."""
import os
from pathlib import Path
import sys

if sys.platform != 'win32':
    raise SystemExit('This build script requires Windows.')

import PySide6
import shiboken6
from PyInstaller.__main__ import run

root = Path(__file__).resolve().parents[1]
windows = Path(os.environ.get('WINDIR', r'C:\Windows'))
# Poppler/other tools can put incompatible ICU/UCRT DLLs on PATH. Qt must
# resolve Windows' own system ICU, rather than bundle a tool's DLL by name.
os.environ['PATH'] = os.pathsep.join(str(path) for path in (
    Path(PySide6.__file__).parent, Path(shiboken6.__file__).parent,
    Path(sys.base_prefix), Path(sys.base_prefix) / 'DLLs',
    windows / 'System32', windows,
))
os.chdir(root)
run([
    '--onefile', '--windowed', '--clean', '--noconfirm',
    '--name', '超智能计时器', '--icon', 'icon.ico', '--version-file', 'version.txt',
    '--add-data', 'assets;assets', '--add-data', 'icon.ico;.', 'main.py',
])
