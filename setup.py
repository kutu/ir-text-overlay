#!python3

import os
import sys
import zipfile
from cx_Freeze import setup, Executable
from stream import VERSION

# Dependencies are automatically detected, but it might need
# fine tuning.
buildOptions = dict(
    packages = [],
    excludes = [],
    path = sys.path + ['D:/projects/py/pyirsdk'],
    include_files = ['settings.tmpl'],
    silent = True,
)

base = 'Console'

executables = [
    Executable('stream.py', base=base)
]

setup(name='ir-text-overlay',
      version = VERSION,
      description = 'iRacing Text Overlay',
      options = dict(build_exe = buildOptions),
      executables = executables)

zf = zipfile.ZipFile('ir-text-overlay-%s.zip' % VERSION, 'w', zipfile.ZIP_LZMA)
for dirname, _, files in os.walk('build/exe.win32-3.3'):
    for filename in files:
        zf.write(os.path.join(dirname, filename), filename)
zf.close()
