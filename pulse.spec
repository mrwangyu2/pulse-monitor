# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置: 把 Pulse 连同 Python 解释器和全部依赖打成一个 exe。

用法 (在 pulse 目录下):
    pyinstaller pulse.spec --noconfirm        # 产物在 dist/Pulse.exe

关于下面几处非默认设置的原因:
- console=False: 双击不弹黑框。程序是 GUI + 托盘, 没有控制台。
- upx=False: UPX 压过的 .NET 程序集 (Python.Runtime.dll / WebView2 的托管控件) 有概率
  加载失败, 省下的那点体积不值这个风险。
- hiddenimports: 只写静态分析抓不到的。pywebview 的后端是运行时才 import 的
  (guilib.initialize 里挑 winforms), etw 是函数里才 import 的。
- excludes: 排除明显用不到的大件, 免得白塞几十兆。
"""

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs

HERE = Path(SPECPATH)

datas = [
    (str(HERE / 'pulse.html'), '.'),
    (str(HERE / 'pulse.ico'), '.'),
]

# pywebview 自带一份收集规则 (webview/__pyinstaller), 但那是装在 site-packages 里的,
# 打包时用的是这份 spec —— 这里显式收一遍, 不依赖 hook 的注册顺序。
# webview/lib 里是 WebView2 的托管控件和 WebView2Loader.dll, js 里是注入页面的桥接脚本,
# 少了任何一样窗口都起不来 (interop_dll_path / get_js_dir 都按包内相对路径找)。
datas += collect_data_files('webview', subdir='lib')
datas += collect_data_files('webview', subdir='js')
binaries = collect_dynamic_libs('webview')

hiddenimports = [
    'webview.platforms.winforms',    # guilib 运行时才 import 的后端
    'webview.platforms.edgechromium',  # ↑ 再往里一层 (WebView2 渲染器)
    'clr',                           # pythonnet 的入口, 只在 winforms.py 里 import
    'pythonnet',
    'etw', 'etw.etw', 'etw.evntrace', 'etw.evntcons', 'etw.evntprov', 'etw.tdh',
    'etw.wmistr', 'etw.common', 'etw.GUID', 'etw.in6addr',
    'pystray._win32',                # pystray 按平台挑后端, 静态分析看不出来
    'psutil._pswindows',
]

excludes = [
    'tkinter', 'unittest', 'pydoc', 'doctest',
    'numpy', 'pandas', 'matplotlib', 'scipy',
    'PyQt5', 'PyQt6', 'PySide2', 'PySide6', 'gi',
    'boto3', 'botocore', 'azure', 'google', 'pytest',
]

a = Analysis(
    ['pulse.py'],
    pathex=[str(HERE)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='Pulse',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=False,          # GUI 程序, 不要控制台窗口
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(HERE / 'pulse.ico'),
)
