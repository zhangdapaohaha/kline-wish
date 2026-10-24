# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['arrow_pet.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['unittest', 'pydoc', 'doctest', 'lib2to3', 'sqlite3', 'tkinter.test'],
    noarchive=False,
    optimize=0,
)
# ---- 精简 Tcl/Tk 资源：砍掉时区库、多语言消息、示例、图片（本程序全用不到）----
def _junk(name):
    n = name.replace('/', '\\').lower()
    if '\\tzdata\\' in n or n.endswith('\\tzdata'):
        return True
    if '\\demos\\' in n or '\\images\\' in n:
        return True
    if '\\msgs\\' in n and not n.endswith('\\en.msg') and not n.endswith('\\en_gb.msg'):
        return True
    return False


_before = len(a.datas)
a.datas = [d for d in a.datas if not _junk(d[0])]
print(f'[spec] datas trimmed: {_before} -> {len(a.datas)}')

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='KLineWish',
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
    icon=['icon.ico'],
)
