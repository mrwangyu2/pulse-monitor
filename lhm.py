"""LibreHardwareMonitor (LHM) 的检测、安装与启动。

Pulse 的 CPU 温度和 CPU 封装功耗读的是 LHM 的 Remote Web Server
(http://localhost:8085/data.json)。LHM 是个独立的 .NET 程序, 不跟着 exe 走,
所以这里负责: 看它装没装 / 在没在跑, 没装就从旁边那份拷贝一份装到本地, 然后拉起来。

**它是便携版, 没有安装程序** —— 所谓"安装"就是把同目录下 LibreHardwareMonitor\\ 整个
拷到 %LOCALAPPDATA%\\Pulse\\LibreHardwareMonitor。写文件不需要管理员; 但 LHM 自己的
manifest 是 requireAdministrator, 所以**每次启动都会弹 UAC**, 这是没法绕的,
autostart 走 HKCU Run 就意味着开机弹一次。

LHM 需要的 PawnIO 内核驱动它自己会管 (exe 里嵌了安装器, 首启会问), 这里不重复。
"""

from __future__ import annotations

import ctypes
import os
import shutil
import sys
import urllib.error
import urllib.request
import winreg
from pathlib import Path

# LHM 的 Web 服务端口, 和 pulse.py 的 LHM_URL 对齐。config 里也是这个值。
PORT = 8085
URL = f"http://localhost:{PORT}/data.json"

EXE_NAME = "LibreHardwareMonitor.exe"

# HKCU Run 键下的值名。不能叫 "Pulse" —— 那是 Pulse 自己的自启项。
AUTOSTART_NAME = "Pulse 温度传感器"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

# ShellExecuteW 的返回值: <=32 是错误码。1223 是用户在意 UAC 上点了"否" ——
# 而 1223 > 32 也成立, 所以判成功必须显式排除它, 光写 >32 会把"取消"当成成功。
# (同一个坑见 tray.restart_elevated)
ERROR_CANCELLED = 1223

_shell32 = ctypes.windll.shell32
_user32 = ctypes.windll.user32

# 系统配了代理, 连 localhost 都得绕开 —— 不绕的话 urllib 会走代理然后连不上。
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# 这几种状态对外说给 main() / 托盘用
RUNNING = "running"            # 端口通, 在跑
STOPPED = "stopped"            # 装好了, 但没在跑
NOT_INSTALLED = "not_installed"  # 没装, 手边有安装源
NO_SOURCE = "no_source"        # 没装, 而且旁边没有安装源 (用户只拷了 exe)
START_FAILED = "start_failed"  # 试过拉起来, 失败或用户取消了 UAC


def source_dir() -> Path:
    """随 Pulse 一起分发的那份 LHM 在哪。

    注意是 **exe 旁边**, 不是 sys._MEIPASS —— LHM 是并排文件夹, 没打进 exe。
    开发时就是脚本目录下的 LibreHardwareMonitor\\。
    """
    env = os.environ.get("PULSE_LHM_SOURCE")
    if env:
        return Path(env)
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent / "LibreHardwareMonitor"
    return Path(__file__).resolve().parent / "LibreHardwareMonitor"


def install_dir() -> Path:
    """装到哪。默认 %LOCALAPPDATA%\\Pulse\\LibreHardwareMonitor。

    写这里不需要管理员权限 (LHM 运行时才要)。PULSE_LHM_HOME 可以改根目录,
    方便测试和便携部署。
    """
    root = os.environ.get("PULSE_LHM_HOME")
    base = Path(root) if root else Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Pulse"
    return base / "LibreHardwareMonitor"


def exe_path() -> Path:
    return install_dir() / EXE_NAME


def can_install() -> bool:
    """手边有没有可装的源。用户只拷了 Pulse.exe 没拷文件夹时是 False。"""
    return (source_dir() / EXE_NAME).is_file()


def is_installed() -> bool:
    return exe_path().is_file()


def is_running(timeout: float = 0.5) -> bool:
    """LHM 的 Web 服务通不通。

    超时给得比采样用的 0.8s 短 —— 这是在启动路径上同步阻塞的, 慢一点用户就等在那里。
    """
    try:
        with _opener.open(URL, timeout=timeout) as resp:
            return resp.status == 200
    except (OSError, urllib.error.URLError, ValueError):
        return False


def install() -> bool:
    """把 source_dir() 整个拷到 install_dir()。

    已存在的**配置文件不覆盖** —— LHM 会往 config 里记它学到的传感器参数,
    重装不该把人家的数据清掉。其余文件一律覆盖, 保证版本一致。
    """
    src, dst = source_dir(), install_dir()
    if not (src / EXE_NAME).is_file():
        return False
    keep = dst / "LibreHardwareMonitor.config"
    saved = keep.read_bytes() if keep.is_file() else None
    dst.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dst, dirs_exist_ok=True)
    if saved is not None:
        keep.write_bytes(saved)
    return True


def launch() -> bool:
    """以管理员身份启动 LHM。

    成功返回 True。用户在 UAC 上点"否"返回 False (1223), 这个必须显式排除。
    工作目录设成 LHM 自己的目录 —— 它的 config 和驱动资源都是相对路径找的。
    """
    exe = exe_path()
    if not exe.is_file():
        return False
    try:
        rc = _shell32.ShellExecuteW(None, "runas", str(exe), None, str(exe.parent), 1)
    except Exception:
        return False
    return rc > 32 and rc != ERROR_CANCELLED


def autostart_command() -> str:
    """Run 键里的命令行。给 tray 菜单用来展示, 也方便排查。"""
    return f'"{exe_path()}"'


def is_autostart() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, AUTOSTART_NAME)
        return True
    except OSError:
        return False


def set_autostart(on: bool) -> None:
    """写/删 HKCU Run 里的自启项。

    不需要管理员。命令不带参数: config 里 runWebServerMenuItem=true 已经能让
    LHM 启动时自动开 Web 服务, 不需要命令行开关 (实测过, 全程没点菜单, 8085 就通了)。
    """
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if on:
            winreg.SetValueEx(key, AUTOSTART_NAME, 0, winreg.REG_SZ, autostart_command())
        else:
            try:
                winreg.DeleteValue(key, AUTOSTART_NAME)
            except OSError:
                pass  # 本来就没写


def install_and_start() -> bool:
    """安装 (已经装过就跳过) 并拉起来, 顺带补上自启。托盘菜单点的那条路走这个。"""
    if not is_installed() and not install():
        return False
    set_autostart(True)
    return launch()


# ---------------------------------------------------------------- 启动时的引导

def _ask_install() -> bool:
    """弹个是/否框问用户要不要装。MB_YESNO | MB_ICONQUESTION。"""
    MB_YESNO, MB_ICONQUESTION = 0x4, 0x20
    MB_SETFOREGROUND, MB_TOPMOST = 0x10000, 0x40000
    text = (
        "Pulse 需要 LibreHardwareMonitor 才能显示 CPU 温度和封装功耗。\n\n"
        f"没有检测到它。是否现在安装到:\n{install_dir()}\n\n"
        "安装后会自动启动并加入开机自启 (每次开机需要确认一次管理员权限)。"
    )
    rc = _user32.MessageBoxW(
        None, text, "Pulse",
        MB_YESNO | MB_ICONQUESTION | MB_SETFOREGROUND | MB_TOPMOST,
    )
    return rc == 6  # IDYES


def ensure(interactive: bool = True) -> str:
    """启动时调用一次, 返回最终状态 (上面那组常量之一)。

    interactive=False (也就是 --tray, 开机自启那条路) **绝不弹模态框** ——
    开机时没人看着, 一个没人点的框会把整个进程挂住。
    """
    if is_running():
        return RUNNING

    if is_installed():
        # 装了但没跑 (用户手动退了, 或者开机自启那会还没起来)。
        # 每次启动只试一次, 失败就算了 —— 不反复弹 UAC 骚扰人。
        return RUNNING if launch() else START_FAILED

    if not can_install():
        return NO_SOURCE  # 面板会显示"需运行 LibreHardwareMonitor", 没什么可做的

    if not interactive or not _ask_install():
        return NOT_INSTALLED

    if not install():
        return NOT_INSTALLED
    set_autostart(True)
    return RUNNING if launch() else START_FAILED
