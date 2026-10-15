"""Windows 托盘图标。

窗口关到托盘后, 采样循环照常跑, 只是窗口被隐藏; 真正退出只走托盘菜单。
开机自启用 HKCU 的 Run 键 (不需要管理员), 命令带 --tray, 启动时只有图标不弹窗。

鼠标悬停在图标上会弹出窗口, 摆到工作区右下角 (挨着托盘图标, 尺寸不变), 离开图标和
窗口就收回去。悬停判定是轮询 GetCursorPos, 不是 pystray 的事件 —— legacy 模式下
托盘图标收不到 mouse-leave, 而升级到 v4 会打断右键菜单, 详见 Tray._poll 的注释。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import sys
import threading
import time
import winreg
from pathlib import Path

import pystray
from PIL import Image, ImageDraw

import lhm

def base_dir():
    """程序自带资源 (pulse.ico) 的目录: 打包后是 PyInstaller 解出来的临时目录, 否则是脚本目录。"""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent


ICON = base_dir() / "pulse.ico"

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "Pulse"

# 设计稿的配色
PAPER = (233, 236, 227, 255)  # --card
RULE = (94, 106, 100, 255)  # --soft
USAGE = (31, 92, 82, 255)  # --usage
HEAT = (217, 72, 43, 255)  # --heat

_user32 = ctypes.windll.user32
_shell32 = ctypes.windll.shell32

# 悬停判定的节奏
POLL_INTERVAL = 0.1  # 轮询指针位置的间隔 (秒)
LEAVE_MISSES = 8  # 连续多少次判定"不在上面"才真的收起来 (≈0.8s, 防止掠过时闪)

# ShellExecuteW / ShellExecuteExW 的返回值: <=32 是错误码; 1223 是用户在意 UAC 上点了"否"
_ERROR_CANCELLED = 1223


# ---------------------------------------------------------------- 图标

def icon_image(size=256):
    """圆角方块 + 心电图波形, 配色取自设计稿。"""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    s = size / 256
    pad = max(1, round(10 * s))
    inset = pad / 2
    d.rounded_rectangle(
        (inset, inset, size - 1 - inset, size - 1 - inset),
        radius=round(56 * s), fill=PAPER, outline=RULE, width=max(1, round(9 * s)),
    )

    w = max(1, round(24 * s))
    flat = 0.56  # 基线 (y 越大越靠下)
    left = (0.17, flat)
    right = (0.83, flat)
    spike = [(0.35, flat), (0.46, 0.30), (0.55, 0.80), (0.64, flat)]

    def pt(p):
        return (p[0] * size, p[1] * size)

    d.line([pt(left), pt(spike[0])], fill=USAGE, width=w, joint="curve")
    d.line([pt(spike[-1]), pt(right)], fill=USAGE, width=w, joint="curve")
    d.line([pt(p) for p in spike], fill=HEAT, width=w, joint="curve")
    return img


def icon_pil():
    """优先用现成的 pulse.ico, 没有就现画一个, 保证托盘一定有图标。"""
    if ICON.is_file():
        try:
            with Image.open(ICON) as img:
                return img.convert("RGBA")
        except OSError:
            pass
    return icon_image(64)


# ---------------------------------------------------------------- 开机自启

def _launch(exe, script, *extra):
    """重新拉起自己时要用的 (exe, 参数, 工作目录)。

    打包成 exe 后 (PyInstaller) 入口就是 exe 本身, 不能再带脚本路径 —— 那是解包出来的
    临时目录, 下次启动早没了; 工作目录取 exe 所在目录。
    """
    exe = Path(exe)
    if getattr(sys, "frozen", False):
        return exe, list(extra), exe.parent
    if exe.name.lower() != "pythonw.exe":
        pythonw = exe.with_name("pythonw.exe")
        if pythonw.is_file():
            exe = pythonw
    return exe, [str(script), *extra], Path(script).parent


def _command(script):
    """Run 键里写的启动命令: pythonw.exe "…\\pulse.py" --tray (打包后是 Pulse.exe --tray)"""
    exe, args, _ = _launch(sys.executable, script, "--tray")
    return " ".join(f'"{a}"' for a in [exe, *args])


def is_autostart():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, RUN_NAME)
        return True
    except OSError:
        return False


def set_autostart(on, script):
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if on:
            winreg.SetValueEx(key, RUN_NAME, 0, winreg.REG_SZ, _command(script))
        else:
            try:
                winreg.DeleteValue(key, RUN_NAME)
            except OSError:
                pass  # 本来就没写


# ---------------------------------------------------------------- 屏幕与悬停坐标

class _NotifyIconId(ctypes.Structure):
    _fields_ = [
        ("cbSize", wt.DWORD),
        ("hWnd", wt.HWND),
        ("uID", wt.UINT),
        ("guidItem", ctypes.c_byte * 16),
    ]


def _work_area():
    """工作区 (排掉任务栏那块), 单位是物理像素。"""
    _user32.SetProcessDPIAware()  # 不用 DPI 虚拟化, 免得算出来的坐标跟实际差一个缩放比
    r = wt.RECT()
    if not _user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(r), 0):  # SPI_GETWORKAREA
        return 0, 0, _user32.GetSystemMetrics(0), _user32.GetSystemMetrics(1)
    return r.left, r.top, r.right, r.bottom


def _place_over_tray(w, h):
    """把窗口摆到工作区右下角, 右下角与工作区右下角对齐。

    托盘图标就在工作区右下角, 所以这样摆鼠标从图标往上扫就进窗口了。
    尺寸尽量保持原样 (1920×1040 的工作区放得下 1052×802); 只有工作区比窗口还小时
    才等比缩小, 免得伸到屏幕外面去。
    """
    x, y, right, bottom = _work_area()
    work_w, work_h = right - x, bottom - y

    width, height = min(w, work_w), min(h, work_h)
    if width != w or height != h:  # 放不下, 等比缩到装得下
        scale = min(work_w / w, work_h / h)
        width, height = round(w * scale), round(h * scale)

    return right - width, bottom - height, width, height


def start_pos(width, height):
    """窗口该从哪儿露出来: 工作区右下角 —— 跟悬停弹窗摆的是同一个位置。

    一开始就摆在那儿, 之后显示窗口 (悬停或点图标) 就不用再挪。否则窗口会先在屏幕中间
    露一下、再跳到右下角, 看着就是闪一下。
    """
    x, y, _, _ = _place_over_tray(width, height)
    return x, y


def _icon_rect():
    """托盘图标的屏幕矩形; 拿不到就返回 None。

    Shell_NotifyIconGetRect 返回的是 HRESULT —— 用 COM 的约定判成败 (负数是失败),
    别写成 ``!= 0``: 它经常返回 S_FALSE (1), 那其实是成功。
    图标被隐藏或资源管理器重启过才会真失败, 那种情况一律当成"指针不在图标上"。
    """
    hwnd = getattr(_icon_ref[0], "_hwnd", None) if _icon_ref[0] is not None else None
    if not hwnd:
        return None
    ident = _NotifyIconId(
        ctypes.sizeof(_NotifyIconId), hwnd, _icon_ref[1], (ctypes.c_byte * 16)(),
    )
    r = wt.RECT()
    if _shell32.Shell_NotifyIconGetRect(ctypes.byref(ident), ctypes.byref(r)) < 0:
        return None
    return r.left, r.top, r.right, r.bottom


def _window_rect(window):
    """窗口当前的屏幕矩形; 读不到返回 None。

    这些属性读的是 pywebview 里 BrowserView 的 Location/Size, 是纯 Python 属性,
    不经过 WinForms 控件, 所以轮询线程里读是安全的。
    """
    try:
        x, y = window.x, window.y
        w, h = window.width, window.height
    except Exception:
        return None
    if not w or not h:
        return None
    return x, y, x + w, y + h


def _inside(rect, point):
    if rect is None:
        return False
    left, top, right, bottom = rect
    return left <= point[0] < right and top <= point[1] < bottom


class _HoverState:
    """指针在不在"图标 ∪ 窗口"上的边沿判定。

    进入立刻算数; 离开要连续 miss 够次数才算 —— 鼠标从图标划向窗口的路上会短暂
    离开两者, 不迟滞的话窗口会一闪一闪。
    """

    def __init__(self, misses):
        self.limit = misses
        self.on = False
        self.n = 0

    def feed(self, on):
        if on:
            self.n = 0
            if not self.on:
                self.on = True
                return "enter"
        elif self.on:
            self.n += 1
            if self.n >= self.limit:
                self.on, self.n = False, 0
                return "leave"
        return None


# pystray 的图标对象, 和它在 Shell_NotifyIcon 里的 id。
# id 恒为 0 —— pystray 的 _message() 写的是 hID=id(self), 但结构体字段其实叫 uID,
# ctypes 把 hID 当成普通属性吞了, 真实 id 一直是 0。
_icon_ref = [None, 0]


class Tray:
    """窗口关到托盘; 菜单管显隐、退出、开机自启; 悬停图标弹出窗口。

    pystray 的回调跑在它自己的消息循环线程上, pywebview 的 show/hide/destroy
    都走 Form.Invoke, 可以跨线程调用, 所以不需要额外的调度。
    但窗口坐标属性 (window.x 等) 只有在 shown 之后读才准, 而且轮询线程不能碰
    窗口本身, 所以悬停的坐标计算全在轮询线程里做完, 只把动作丢给回调线程。
    """

    def __init__(self, window, stop, script, title="Pulse", hover=True,
                 admin=True, tray_only=False, sensors=None, visible=False):
        self.window = window
        self.stop = stop
        self.script = script
        # 窗口是隐藏着起来的 (见 pulse.main), 所以这里起始就是"没显示"。这个值只有托盘菜单、
        # 悬停和关闭按钮会改 —— 说白了它得跟窗口的真实显隐一致, 不然第一次点图标会反过来。
        self.visible = visible
        self.quitting = False
        self.hover_on = hover  # 「悬停显示窗口」开关, 菜单里可勾
        self.admin = admin  # 有没有管理员权限 (网络排行靠 ETW, 需要它)
        self.tray_only = tray_only  # --tray 起的: 静默进托盘, 不发气泡
        self.sensors = sensors  # 启动时温度传感器的状态, 见 lhm.ensure
        self._lines = [title, ""]
        self._click = 0.0
        self.icon = None
        self._hover_fired = False  # 当前这次显示是悬停弹出来的 (离开时只收这种)
        self._cursor_hidden = False
        self._poll_stop = threading.Event()

    # --- 生命周期 ---

    def start(self):
        self.icon = pystray.Icon(
            "pulse", icon_pil(), title="\n".join(self._lines), menu=self._menu(),
        )
        _icon_ref[0] = self.icon
        # 一定要通过 setup 回调来搭: pystray 的初始化顺序是「进消息循环 ->
        # _mark_ready() -> 队列 -> setup 线程」, 而 setup 线程里设图标才是安全的。
        # 传了 setup 就不会自动设 visible 了, 得自己设, 否则托盘没有图标。
        self.icon.run_detached(setup=self._setup)

    def _setup(self, icon):
        icon.visible = True
        # 气泡都放在这里而不是 start() 之后: 气泡走 NIM_MODIFY, 图标 ready 之前发是无效的
        if not self.admin and not self.tray_only:
            self.notify("网络前10进程排行不可用\n需要以管理员身份重新运行", "Pulse")
        # 温度传感器没起来。--tray (开机自启) 那条路走的是非交互的 ensure(), 用户没看过任何
        # 提示, 这里补一句气泡 —— 交互启动的话用户已经在对话框上看过了, 不重复打扰。
        if self.tray_only and (lhm.can_install() or lhm.is_installed()) and \
                self.sensors in (lhm.NOT_INSTALLED, lhm.START_FAILED):
            self.notify("温度传感器未运行\n点托盘图标装 / 启动它", "Pulse")
        threading.Thread(target=self._poll, daemon=True).start()

    def _menu(self):
        return pystray.Menu(
            pystray.MenuItem("显示窗口", self.toggle, checked=self._is_visible, default=True),
            pystray.MenuItem("悬停显示窗口", self._toggle_hover, checked=self._is_hover),
            pystray.MenuItem("以管理员身份重新启动", self.restart_elevated,
                             visible=self._can_elevate),
            pystray.MenuItem(self._sensor_label, self.install_sensors,
                             visible=self._can_install_sensors),
            pystray.MenuItem("开机自启", self._toggle_autostart, checked=self._is_autostart),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出 Pulse", self.quit),
        )

    def notify(self, message, title=None):
        """托盘气泡通知。图标还没起来时静默跳过。"""
        if self.icon is not None:
            try:
                self.icon.notify(message, title)
            except Exception:
                pass

    def _can_elevate(self, _item=None):
        return not self.admin

    # --- 温度传感器 (LibreHardwareMonitor) ---

    def _can_install_sensors(self, _item=None):
        """菜单里这一项什么时候露面: 传感器没在跑, 而且我们手上有办法让它跑起来
        (要么旁边有安装源可以装, 要么已经装过了可以直接启动)。

        用户在首次启动时点了"否"、或者 UAC 被拒了, 这里就是他的后路。
        """
        return not lhm.is_running() and (lhm.can_install() or lhm.is_installed())

    def _sensor_label(self, _item=None):
        """还没装就说"安装", 装过了没跑就说"启动" —— 同一个动作两种叫法。"""
        return "启动温度传感器 (需管理员)" if lhm.is_installed() else "安装温度传感器 (需管理员)"

    def install_sensors(self, _icon=None, _item=None):
        """菜单点"安装/启动温度传感器": 装好 (需要的话) 并拉起来。

        不走 lhm.ensure() —— 那条路在已经装过时会直接 launch(), 而这里用户是明确
        要求动手的; 失败原因用气泡说清楚 (UAC 被取消 / 拷文件失败)。
        """
        if not lhm.is_installed():
            if not lhm.can_install():
                self.notify("旁边没有 LibreHardwareMonitor 文件夹, 无法安装", "Pulse")
                return
            if not lhm.install():
                self.notify("安装失败: 拷贝文件出错", "Pulse")
                return
        lhm.set_autostart(True)
        ok = lhm.launch()
        # 菜单是在图标起来时一次性构造的 (pystray 的 win32 后端不会每次弹菜单都重建),
        # 所以这一项的文字和显隐不会自己更新, 得手动叫它重建一次。
        self._refresh_menu()
        self.notify("温度传感器已启动" if ok else
                    "温度传感器未能启动\n(管理员权限被拒绝?)", "Pulse")

    def _refresh_menu(self):
        """菜单项的文字/显隐是回调算出来的, 但 pystray 只在构造时求一次值。
        状态变了 (装上了、起来了) 就得让它重建。从 pystray 自己的线程调, 安全。"""
        if self.icon is not None:
            try:
                self.icon.update_menu()
            except Exception:
                pass

    def restart_elevated(self, _icon=None, _item=None):
        """以管理员身份重新启动, 起来了再退出自己。

        ShellExecuteW 的返回值必须看, 而且不能只判 >32: 用户在意 UAC 上点"否"时
        返回 ERROR_CANCELLED (1223), 而 1223 > 32 也成立 —— 只判 >32 会把"取消"
        当成成功然后退出自己, 等于用户点个取消反而把程序关掉了。所以要显式排除。
        """
        if self.admin:
            return
        exe, args, cwd = _launch(sys.executable, self.script,
                                 *(["--tray"] if self.tray_only else []))
        try:
            rc = _shell32.ShellExecuteW(
                None, "runas", str(exe), " ".join(f'"{a}"' for a in args), str(cwd), 1,
            )
        except Exception:
            return
        if rc > 32 and rc != _ERROR_CANCELLED:
            self.quit()

    def on_closing(self):
        """点窗口 × : 藏着不关。

        返回值是反的 —— pywebview 的 Event.set() 收集处理器的返回值, 只要有人返回
        False 就把这次关闭取消掉 (winforms.py 里那句 should_cancel 名字没起错)。
        所以这里: 想拦下就返回 False, 想放行就返回 True。
        """
        if self.quitting:
            return True  # 放行, 让它真的关
        self.hide()
        return False  # 拦下这次关闭, 窗口只是藏起来

    def on_closed(self):
        """窗口真的没了 (退出路径走完) —— 收掉托盘图标。"""
        self._poll_stop.set()
        if self.icon is not None:
            self.icon.stop()

    # --- 显隐 ---

    def _is_visible(self, _item=None):
        return self.visible

    def toggle(self, _icon=None, _item=None):
        # 左键单击/双击都会走到这 (pystray 的 win32 后端把每次 WM_LBUTTONUP
        # 都派给默认菜单项), 双击太快会让状态来回抵消, 这里做个去抖。
        now = time.monotonic()
        if now - self._click < 0.4:
            return
        self._click = now
        if self.visible:
            self.hide()
        else:
            self.show()

    def show(self):
        self._hover_fired = False  # 手动显示, 鼠标离开不该把它收走
        self.window.restore()  # 从任务栏最小化状态也要能回来
        self.window.show()
        self.visible = True

    def hide(self):
        self.window.hide()
        self.visible = False
        self._hover_fired = False

    def _show_hover(self):
        """悬停显示: 摆到工作区右下角, 挨着托盘图标, 尺寸不动。

        从轮询线程调; window.width/show/move 都是 pywebview 那层的方法 (自己会
        Form.Invoke marshal 到 UI 线程), 只读的 width/height 更是纯 Python 属性,
        所以这里安全。别在这里直接碰 winforms 控件。
        """
        if self.visible or not self.hover_on:
            return
        try:
            w, h = self.window.width, self.window.height
        except Exception:
            return  # 窗口还不让读 (没 shown 或正在关), 下一次轮询再说
        if not w or not h:
            return
        x, y, w, h = _place_over_tray(w, h)
        try:
            self.window.restore()  # 窗口是 minimized 起的, 不还原的话 show() 出来还是最小化状态
            # 窗口本来就摆在这儿 (start_pos 摆的) 就别再动它 —— 位置没变也发一次移动消息,
            # 显示的一瞬间照样会闪。
            if _window_rect(self.window) != (x, y, x + w, y + h):
                self.window.resize(w, h)
                self.window.move(x, y)
        except Exception:
            return
        self.window.show()
        self.visible = True
        self._hover_fired = True

    def _hide_hover(self):
        """悬停离开: 只收悬停弹出来的那个窗口。

        菜单手动显示的窗口不算 (上面 show() 会把 _hover_fired 清掉), 否则鼠标随便
        一划就把用户要看的窗口收走了。
        """
        if not self._hover_fired:
            return
        self.hide()

    # --- 悬停判定 ---

    def _poll(self):
        """轮询指针位置, 边沿触发显示/隐藏。

        为什么轮询而不是收托盘消息: legacy 模式的托盘图标只有左键/右键两个回调,
        鼠标在图标上移动、滚轮、以及 mouse-leave 都收不到; 升级到 v4 能拿到
        POPUPOPEN/CLOSE, 但 v4 会改 lParam 的布局, pystray 比的是整个 lParam,
        右键菜单会直接失灵 —— 不值得。
        """
        state = _HoverState(LEAVE_MISSES)
        while not self._poll_stop.wait(POLL_INTERVAL):
            if self.quitting:
                return
            point = wt.POINT()
            if not _user32.GetCursorPos(ctypes.byref(point)):
                continue
            here = (point.x, point.y)
            on = _inside(_icon_rect(), here) or (
                self.visible and _inside(_window_rect(self.window), here)
            )
            edge = state.feed(on)
            if edge == "enter" and self.hover_on:
                self._invoke(self._show_hover)
            elif edge == "leave":
                self._invoke(self._hide_hover)

    def _invoke(self, action):
        """执行一个悬停动作。

        不用 pystray 的 _handler 包一层 —— 它是给「菜单项回调」用的, 会额外塞一个
        icon 参数进来 (它按 callback(icon) 调), 而这里传的是已经绑好的方法, 多一个
        参数就 TypeError。也不需要换线程: window.show/hide/resize/move 内部都走
        Form.Invoke 自己会 marshaling 到 UI 线程, 从哪个线程调都一样。
        """
        try:
            action()
        except Exception:
            pass  # 窗口正在关之类, 下次轮询再说

    # --- 退出 ---

    def quit(self, _icon=None, _item=None):
        self.quitting = True  # 必须先置位, 否则 destroy 会被 on_closing 拦下
        self.visible = False
        self._poll_stop.set()
        if self.icon is not None:
            self.icon.stop()
        self.window.destroy()

    # --- 开关 ---

    def _is_hover(self, _item=None):
        return self.hover_on

    def _toggle_hover(self, _icon=None, _item=None):
        self.hover_on = not self.hover_on
        if not self.hover_on:
            self._hide_hover()

    # --- 开机自启 ---

    def _is_autostart(self, _item=None):
        return is_autostart()

    def _toggle_autostart(self, _icon=None, _item=None):
        try:
            set_autostart(not is_autostart(), self.script)
        except OSError:
            pass

    # --- tooltip ---

    def update(self, usage, mem, gpus):
        lines = ["CPU " + _pct(usage)]
        if mem and mem.get("total"):
            lines[0] += "  ·  内存 " + _pct(mem["used"] / mem["total"] * 100)
        if gpus:
            cuda = [g["cuda"] for g in gpus if g.get("cuda") is not None]
            temp = [g["temp"] for g in gpus if g.get("temp") is not None]
            part = ["GPU"]
            if cuda:
                part.append(f"{round(max(cuda))}%")
            if temp:
                part.append(f"{round(max(temp))}°C")
            lines.append(" · ".join(part))
        else:
            lines.append("未检测到 GPU")
        if not self.admin:
            lines.append("无管理员权限 · 网络排行不可用")
        if lines == self._lines:
            return
        self._lines = lines
        if self.icon is not None:
            try:
                self.icon.title = "\n".join(lines)
            except Exception:
                pass  # 图标已经收了, 忽略


def _pct(value):
    return "--" if value is None else f"{round(value)}%"
