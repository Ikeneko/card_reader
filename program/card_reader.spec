# -*- mode: python ; coding: utf-8 -*-
#
# card_reader.spec
# PyInstaller build configuration
#
# How to build:
#   pyinstaller card_reader.spec
#
# Output: dist/card_reader/card_reader.exe

import os

# -------------------------------------------------------
# Data files to bundle (placed in the same folder as .exe)
# -------------------------------------------------------
added_files = []

# Sound files (only added if they exist)
for wav in ["\u732b\u306e\u9cf3\u304d\u58f01.wav", "\u305a\u3093\u3060_\u304a\u75b2\u308c\u69d8.wav"]:
    if os.path.exists(wav):
        added_files.append((wav, "."))

# .env file (only added if it exists)
if os.path.exists(".env"):
    added_files.append((".env", "."))

# student_map.json (only added if it exists)
if os.path.exists("student_map.json"):
    added_files.append(("student_map.json", "."))

# -------------------------------------------------------
# Analysis
# -------------------------------------------------------
a = Analysis(
    ["card_reader.py"],
    pathex=[],
    binaries=[],
    datas=added_files,
    hiddenimports=[
		# nfcpy
		"nfc.clf.usb",
		"nfc.clf.transport",
		"nfc.clf.rcs380",
		"nfc.tag.tt3",
		# python-dotenv
		"dotenv",
		# requests
		"requests",
		"urllib3",
		"charset_normalizer",
		"certifi",
		# tkinter
		"tkinter",
		"tkinter.messagebox",
	],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="card_reader",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,   # GUI app - no console window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # icon="icon.ico",
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="card_reader",
)
