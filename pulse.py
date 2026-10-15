"""Pulse 硬件监控: 用 pywebview 直接渲染 pulse.html, 显示 CPU 与 GPU 的使用率、温度。仅支持 Windows。

界面完全由同目录的 pulse.html 决定; 本程序只负责每秒采样, 并调用页面里的
window.pushSample(usage, temp, gpus, mem, io, power, threads, disk) 推送数据 (None 表示该项暂无数据)。换设计 = 换 HTML 文件。

CPU 使用率: 调用 kernel32.GetSystemTimes, 两次采样之间的差值即为占用比例。
            每个逻辑核的使用率另走 psutil.cpu_times(percpu=True) 的差值 (页面上的线程竖条)。
CPU 温度:   读取 LibreHardwareMonitor 的 Remote Web Server (http://localhost:8085/data.json)。
            没有运行它时温度面板显示"未检测到温度传感器"。见 lhm.py —— 程序旁边带了一份
            LHM (LibreHardwareMonitor\\ 文件夹), 首次启动会问用户要不要装到本机并开机自启。
GPU:        数值取 LHM: 每张卡的占用率、温度 (Temperatures\\GPU Core)、功耗都从这棵树里取
            (已用显存例外, 见下面那句)。
            面板上 "GPU 0 / 1" 的编号则按 PCI 顺序 —— 也就是 nvidia-smi 的 index, 和 Windows
            任务管理器里的编号是同一套 (本机实测两边都是 GPU 0 = PCI 总线 3)。
            **LHM 的枚举顺序不是这一套** (它走 NVAPI, 本机实测正好相反), 同型号的卡两边名字又一样,
            所以拿两边都会报的功耗/风扇转速把卡一张张对上号, 见 _pair_gpus —— 不能按位置硬配。
            nvidia-smi 因此只剩两个用处: 定 PCI 顺序当编号, 以及供 LHM 没有的功耗上限。
            已用显存单独走 Windows 的 GPU Adapter Memory 计数器: LHM 和 nvidia-smi 都看不到
            一张卡上那 20 GB (本机实测它们都只报 ~2 GB), 而计数器的值和任务管理器一致。
            没装 / 没在跑 LHM 时, GPU 面板整个退回 nvidia-smi (随 NVIDIA 驱动安装), 和以前一样。
内存:       kernel32.GlobalMemoryStatusEx 取物理内存已用/总量。
磁盘/网络: psutil 的 IO 计数器差值 (MB/s, Mbps)。

点击面板弹出"前10进程": 页面调用 window.pywebview.api.top(kind, gpu_index) 按需取数据;
CPU/内存/磁盘用 psutil, GPU 进程显存与 CUDA 用 Windows 性能计数器 (GPU Process Memory / GPU Engine),
网络用 ETW 内核日志的 TCP/IP 事件按进程累计字节数 (需要管理员权限, 依赖 pywintrace;
没权限时只有这一项排行是空的, 启动时会弹托盘气泡提醒, 菜单里可一键以管理员身份重启)。

依赖: pip install pywebview psutil pywintrace pystray pillow  (使用系统自带的 WebView2)

托盘: 常驻 Windows 系统托盘 (见 tray.py)。**启动时窗口是隐藏的** —— 只进托盘, 点托盘图标
(或菜单里的"显示窗口") 才弹出来, 再点一下收回去。窗口就摆在右下角 (和悬停弹窗同一处,
见 tray.start_pos), 所以显示的时候不用再挪, 不会闪。点窗口 × 也只是藏起来, 采样继续;
真正退出走托盘菜单的"退出 Pulse"。加 --tray 起的进程连气泡都不发 (开机自启用这个)。
开机自启写 HKCU 下 Software\\Microsoft\\Windows\\CurrentVersion\\Run 的 Pulse 值, 不需要管理员。
"""

import atexit
import ctypes
import itertools
import json
import re
import struct
import subprocess
import sys
import threading
import time
import urllib.request
import winreg
from pathlib import Path

import lhm
import psutil
import webview

import tray as tray_mod

FROZEN = getattr(sys, "frozen", False)  # PyInstaller 打出来的 exe (入口是 exe 自己, 没有脚本参数)


def base_dir():
    """程序自己带的资源 (pulse.html / pulse.ico) 所在的目录。

    打包成单文件 exe 后, 资源被解到 sys._MEIPASS 那个临时目录里, 不是 exe 旁边 ——
    所以不能写 Path(exe).parent。开发时就是脚本所在目录。tray.py 里有一份同样的。
    """
    if FROZEN:
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent


_ARGS = [a for a in sys.argv[1:] if a != "--tray"]  # --tray: 只进托盘, 不弹窗 (开机自启用)
TRAY_ONLY = len(_ARGS) != len(sys.argv) - 1
HTML = Path(_ARGS[0]) if _ARGS else base_dir() / "pulse.html"
# 开机自启/提权重启要重新拉起自己。打包后入口就是 exe 本身, 不能再带脚本路径 —— 那是解
# 包出来的临时目录, 下次启动早没了。tray._launch 按 frozen 判断带不带它。
SCRIPT = sys.executable if FROZEN else str(Path(__file__).resolve())
LHM_URL = "http://localhost:8085/data.json"
SIZE = (1052, 802)


def is_admin():
    """当前进程有没有管理员权限。

    按进程的网络排行走 ETW 内核日志, 没有权限时 StartTraceW 会失败、排行为空,
    所以启动时要能判断出来并提醒用户。判不出来就当没有, 提醒偏保守。
    """
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


ADMIN = is_admin()

# 标题栏里要显示的名字/邮箱。改这里就行, 不用动 HTML。
# 标题其余部分 (CPU 型号、显卡、内存) 都是启动时从本机读的, 见 _live_title。
APP_NAME = "Pulse"
APP_VERSION = "v1.0.0"
APP_AUTHOR = "wangyuxxx@gmail.com"


# ---------------------------------------------------------------- 数据采集

class _FileTime(ctypes.Structure):
    _fields_ = [("lo", ctypes.c_uint32), ("hi", ctypes.c_uint32)]


def _system_times():
    idle, kernel, user = _FileTime(), _FileTime(), _FileTime()
    ok = ctypes.windll.kernel32.GetSystemTimes(
        ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)
    )
    if not ok:
        raise OSError("GetSystemTimes failed")
    as_int = lambda f: (f.hi << 32) | f.lo
    return as_int(idle), as_int(kernel), as_int(user)


def _usage_between(prev, cur):
    # kernel 时间已包含 idle 时间
    d_idle = cur[0] - prev[0]
    d_total = (cur[1] - prev[1]) + (cur[2] - prev[2])
    if d_total <= 0:
        return None
    return max(0.0, min(100.0, (1 - d_idle / d_total) * 100))


def _busy_delta(prev, cur):
    # cpu_times 的 user/system 之外, Windows 上还有 interrupt / dpc
    busy = lambda t: t.user + t.system + getattr(t, "interrupt", 0.0) + getattr(t, "dpc", 0.0)
    return [(busy(b) - busy(a), b.idle - a.idle) for a, b in zip(prev, cur)]


def _threads_between(prev, cur):
    """每个逻辑核的使用率(%)。第一次采样 (prev=None) 返回 None。"""
    if not prev:
        return None
    out = []
    for d_busy, d_idle in _busy_delta(prev, cur):
        span = d_busy + d_idle
        out.append(None if span <= 0 else max(0.0, min(100.0, d_busy / span * 100)))
    return out


class _MemStatus(ctypes.Structure):
    _fields_ = [
        ("length", ctypes.c_uint32), ("load", ctypes.c_uint32),
        ("total", ctypes.c_uint64), ("avail", ctypes.c_uint64),
        ("total_page", ctypes.c_uint64), ("avail_page", ctypes.c_uint64),
        ("total_virt", ctypes.c_uint64), ("avail_virt", ctypes.c_uint64),
        ("avail_ext", ctypes.c_uint64),
    ]


def read_memory():
    """物理内存: used / total (GB); 失败返回 None。"""
    st = _MemStatus()
    st.length = ctypes.sizeof(st)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
        return None
    gb = 1024 ** 3
    return {"used": (st.total - st.avail) / gb, "total": st.total / gb}


def _disk_label():
    """物理磁盘的类型标签, 如 "NVMe SSD"。

    从注册表读设备 ID (SCSI\\Disk&Ven_NVMe&Prod_Samsung_SSD_980\\...) —— 瞬时, 不用
    起子进程。总线是 NVMe 就一定固态, 别的分不出来 (SATA 的 SSD/HDD 在设备 ID 里
    没区别), 那种情况就只写总线名, 不硬说是 SSD。
    """
    ids = []
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"SYSTEM\CurrentControlSet\Services\disk\Enum") as key:
            for i in range(winreg.QueryInfoKey(key)[1]):
                name, value, _ = winreg.EnumValue(key, i)
                if name.isdigit():
                    ids.append(value or "")
    except OSError:
        return None
    buses, models = [], []
    for dev in ids:
        ven = re.search(r"Ven_([A-Za-z0-9_]+)", dev)
        prod = re.search(r"Prod_([A-Za-z0-9_]+)", dev)
        if prod:
            model = prod.group(1).replace("_", " ").strip()
            if model and model not in models:
                models.append(model)
        if not ven:
            continue
        bus = ven.group(1).replace("_", " ").strip()
        low = bus.lower()
        bus = "SATA" if low in ("ata", "sata") else "NVMe" if low == "nvme" else bus
        if bus and bus not in buses:
            buses.append(bus)
    if not buses and not models:
        return None
    tag = " / ".join(buses)
    if len(buses) == 1 and buses[0] == "NVMe":
        tag += " SSD"
    return tag or models[0]


def read_disk(label=None):
    """固定磁盘的合计用量: {used(GB), total(GB), label}; 读不到返回 None。

    label 只取决于硬件, 调用方读一次缓存住即可, 不必每秒查注册表。
    这里用 kernel32 的两个调用而不用 psutil.disk_partitions —— 后者在 Windows 上
    要枚举卷/设备 (实测 ~370ms), 每秒跑一次会把采样循环拖住。
    """
    k32 = ctypes.windll.kernel32
    drives = k32.GetLogicalDrives()
    DRIVE_FIXED = 3
    used = total = 0
    for i in range(26):
        if not drives >> i & 1:
            continue
        root = f"{chr(ord('A') + i)}:\\"
        if k32.GetDriveTypeW(root) != DRIVE_FIXED:
            continue
        free, size = ctypes.c_ulonglong(0), ctypes.c_ulonglong(0)
        if not k32.GetDiskFreeSpaceExW(root, None, ctypes.byref(size), ctypes.byref(free)):
            continue
        total += size.value
        used += size.value - free.value
    if not total:
        return None
    gb = 1024 ** 3
    return {"used": used / gb, "total": total / gb, "label": label}


# 本机可能配置了系统代理, 访问 localhost 必须绕开
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _number(text):
    m = re.search(r"-?\d+(?:[.,]\d+)?", text or "")
    return float(m.group().replace(",", ".")) if m else None


# LHM 用图标区分设备类型, 这几张是它给显卡的图标 (NVIDIA / AMD / Intel 核显)
_GPU_ICONS = ("nvidia", "ati", "intel")


def _devices(node, icons, out=None):
    """树里所有带某种图标的设备节点 (LHM 靠图标区分设备类型: cpu.png / nvidia.png ...)。

    要带子节点才算数 —— 真正的设备底下挂的是 Temperatures / Powers 这些分组。
    """
    out = [] if out is None else out
    if node.get("Children") and any(i in node.get("ImageURL", "").lower() for i in icons):
        out.append(node)
    for child in node.get("Children", []):
        _devices(child, icons, out)
    return out


def _section_values(dev, section):
    """设备节点下某个分组 (Temperatures / Powers ...) 里的传感器: [(名称, 数值)]。

    "Distance to TjMax" 是到温度墙的余量, 不是温度本身, 跳过。
    """
    out = []
    for group in dev.get("Children", []):
        if group.get("Text") != section:
            continue
        for leaf in group.get("Children", []):
            value = _number(leaf.get("Value"))
            if value is not None and "distance" not in leaf.get("Text", "").lower():
                out.append((leaf.get("Text", ""), value))
    return out


def _lhm_tree():
    """LHM 的整棵树 (CPU 温度/功耗和 GPU 温度都在里面); 它没在跑时返回 None。

    每秒只请求一次, 几个读数共用, 免得各读各的把那 0.8 秒超时耗两遍。
    """
    try:
        with _opener.open(LHM_URL, timeout=0.8) as resp:
            return json.load(resp)
    except (OSError, ValueError):
        return None


def read_cpu_sensors(tree):
    """从 LHM 的树里取 (CPU 温度 °C, CPU 封装功耗 W); 取不到的项为 None。"""
    cpus = _devices(tree or {}, ("cpu",))
    if not cpus:
        return None, None
    temps = _section_values(cpus[0], "Temperatures")
    temp = next((v for n, v in temps if "package" in n.lower()), None)
    if temp is None and temps:
        temp = max(v for _, v in temps)
    power = next((v for n, v in _section_values(cpus[0], "Powers") if "package" in n.lower()), None)
    return temp, power


def _norm_gpu(name):
    """显卡名的比对写法 —— LHM 和 nvidia-smi 用的都是驱动里那个全名, 只差大小写和空格。"""
    return re.sub(r"\s+", " ", re.sub(r"\((?:R|TM|C)\)", "", name or "", flags=re.I)).strip().lower()


def _sensor(dev, section, name):
    """某个分组里名为 name 的那个传感器 (名字不区分大小写)。"""
    return next((v for n, v in _section_values(dev, section) if n.strip().lower() == name), None)


def _lhm_gpus(tree):
    """LHM 里每张显卡的读数。

    "GPU Core" 在 Load 和 Temperatures 两个分组里都有, 分别是核心占用率和核心温度。
    功耗和风扇转速除了显示, 还要拿来认卡 —— LHM 和 nvidia-smi 的枚举顺序不一定一样,
    见 _pair_gpus。
    """
    cards = []
    for dev in _devices(tree or {}, _GPU_ICONS):
        used = _sensor(dev, "Data", "gpu memory used")
        total = _sensor(dev, "Data", "gpu memory total")
        cards.append({
            "name": (dev.get("Text") or "").strip(),
            "cuda": _sensor(dev, "Load", "gpu core"),
            "used": used / 1024 if used is not None else None,  # LHM 报 MB, 面板要 GB
            "total": total / 1024 if total is not None else None,
            "temp": _sensor(dev, "Temperatures", "gpu core"),
            "power": _sensor(dev, "Powers", "gpu package"),
            "fan": _sensor(dev, "Controls", "gpu fan"),
        })
    return cards


def _limits(rows):
    """nvidia-smi 的功耗上限, 按显卡名收着 —— 上限是型号的属性, 同型号的卡都一样。

    LHM 没有这一项, 所以它是面板上唯一还从 nvidia-smi 借的值。也正因为它是按型号来的,
    这里只看名字, 不需要 (也没法) 把同型号的卡一张张对上号。
    """
    out = {}
    for row in rows:
        out.setdefault(_norm_gpu(row["name"]), row["limit"])
    return out


# nvidia-smi 第 i 张卡 ↔ LHM 显卡列表里的第几个。还没认出来时是 None, 按位置兜底。
_pairing = None
_PAIR_MARGIN = 2.0  # 两种配法差不到这么多 (W 或 %), 就当分不出来
_UNLIKE = 1000.0  # 名字对不上时的差距, 大到任何真实的功耗/转速差都盖不过去


def _gap(nv, lhm):
    """两张卡有多像: 名字一样的话, 看功耗和风扇转速各差多少 (哪边缺数据就跳过哪一项)。

    名字对不上直接判不像 —— 两张不同型号的卡本来就该按名字配。名字在这里现归一化再比,
    不要求调用方先处理 (LHM 和 nvidia-smi 两边只差大小写和空格, 忘了归一化就会全都看着不像,
    于是每一次配对都打平、认不出卡 —— 这个坑踩过两次了)。
    """
    if nv["name"] and lhm["name"] and _norm_gpu(nv["name"]) != _norm_gpu(lhm["name"]):
        return _UNLIKE
    return sum(abs(nv[k] - lhm[k]) for k in ("power", "fan")
               if nv[k] is not None and lhm[k] is not None)


def _pair_gpus(rows, cards):
    """nvidia-smi 的每张卡对应 LHM 的哪一张 —— 返回下标列表, 分不出来就返回 None。

    两边的枚举顺序不能想当然: LHM 走 NVAPI 的顺序, nvidia-smi 按 PCI 顺序编号, 两张同型号的卡
    在这两套顺序里可能是反的 (本机就是: 同一时刻 43W、风扇 40% 的那张, nvidia-smi 里是 0 号,
    LHM 里是第 2 张)。同型号看名字分不出来, 所以拿两边都会报的功耗和风扇转速当指纹 —— 认出来
    的标准是"这一种配法明显比其他配法都像"。

    认出来之后就固定下来, 不再每秒重认 —— 两张卡负载一样的时候指纹是重合的, 重认会来回翻。
    """
    global _pairing
    n = len(rows)
    if _pairing is not None and len(_pairing) == n:
        return _pairing
    if not n or len(cards) < n:
        return None
    prints = [{"name": r["name"], "power": r["power"], "fan": r["fan"]} for r in rows]
    scored = sorted(
        (sum(_gap(prints[i], cards[p[i]]) for i in range(n)), p)
        for p in itertools.permutations(range(len(cards)), n)
    )
    if len(scored) > 1 and scored[0][0] + _PAIR_MARGIN > scored[1][0]:
        return None  # 几种配法一样像, 认不出来
    _pairing = list(scored[0][1])
    return _pairing


class _Rates:
    """磁盘读写 (MB/s) 与网络上下行 (Mbps): 两次采样之间的计数器差值。"""

    def __init__(self):
        self.prev = None

    def read(self):
        try:
            d, n, now = psutil.disk_io_counters(), psutil.net_io_counters(), time.monotonic()
            cur = (d.read_bytes, d.write_bytes, n.bytes_recv, n.bytes_sent, now)
        except (AttributeError, OSError, RuntimeError):
            return None
        prev, self.prev = self.prev, cur
        if prev is None or cur[4] <= prev[4]:
            return None
        dt = cur[4] - prev[4]
        rate = lambda i, k: max(0.0, (cur[i] - prev[i]) / dt * k)
        return {"dr": rate(0, 1 / 1e6), "dw": rate(1, 1 / 1e6), "nd": rate(2, 8 / 1e6), "nu": rate(3, 8 / 1e6)}


_rates = _Rates()

_GPU_QUERY = ("index,name,utilization.gpu,memory.used,memory.total,"
              "temperature.gpu,power.draw,power.limit,fan.speed")


def _field(text):
    # nvidia-smi 对不支持的项输出 [N/A] 之类, 视为暂无数据
    try:
        return float(text)
    except ValueError:
        return None


def _nvidia_gpus():
    """nvidia-smi 的每张卡: name, cuda(%), used/total(MiB), temp(°C), power/limit(W); 没有驱动/显卡时返回 []。

    这份读数现在只干两件事: 给面板供 LHM 没有的功耗上限, 以及 LHM 不在时整个兜底。
    """
    try:
        proc = subprocess.run(
            ["nvidia-smi", f"--query-gpu={_GPU_QUERY}", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=3,
            creationflags=0x08000000,  # CREATE_NO_WINDOW, 避免 pythonw 下闪黑框
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0:
        return []
    rows = []
    for line in proc.stdout.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != 9:
            continue
        _, name, cuda, used, total, temp, power, limit, fan = parts
        rows.append({
            "name": name,
            "cuda": _field(cuda),
            "used": _field(used),
            "total": _field(total),
            "temp": _field(temp),
            "power": _field(power),
            "limit": _field(limit),
            "fan": _field(fan),  # 只为认卡, 不显示
        })
    return rows


def _nvidia_row(row):
    """面板上的一行, 数值全用 nvidia-smi 自己的读数 (LHM 里没有这张卡时用)。"""
    return {
        "name": row["name"],
        "total": row["total"] / 1024 if row["total"] is not None else 0,
        "cuda": row["cuda"],
        "used": row["used"] / 1024 if row["used"] is not None else None,
        "temp": row["temp"],
        "power": row["power"],
        "limit": row["limit"],
    }


def _lhm_row(card, limit):
    """面板上的一行: 名字和数值都取自 LHM, 功耗上限由外面传 (LHM 没有这一项)。"""
    return {
        "name": card["name"],
        "total": card["total"] or 0,
        "cuda": card["cuda"],
        "used": card["used"],
        "temp": card["temp"],
        "power": card["power"],
        "limit": limit,
    }


def _nvidia_panel(rows):
    """LHM 不在时的整份面板数据: 全从 nvidia-smi 来, 顺序也就是 nvidia-smi 的 index。"""
    return [_nvidia_row(row) for row in rows]


def _panel_rows(rows, cards):
    """面板上的一行行数据: 顺序按 PCI (= nvidia-smi 的 index), 数值取 LHM。"""
    if not cards:
        return _nvidia_panel(rows)  # LHM 不在: 整份退回 nvidia-smi
    limits = _limits(rows)
    if not rows:
        return [_lhm_row(c, limits.get(_norm_gpu(c["name"]))) for c in cards]  # 没有 nvidia-smi: 按 LHM 的顺序
    pair = _pair_gpus(rows, cards)  # nvidia-smi 第 i 张 = cards[pair[i]]; None = 没认出来
    out = []
    for i, row in enumerate(rows):
        j = pair[i] if pair else i  # 认不出来就按位置配
        card = cards[j] if j < len(cards) else None
        out.append(_lhm_row(card, limits.get(_norm_gpu(card["name"]))) if card else _nvidia_row(row))
    return out


def read_gpus(tree=None):
    """每张显卡一项: name, total(GB), cuda(%), used(GB), temp(°C), power(W), limit(W); 一张卡都没有时返回 []。

    行顺序按 PCI 顺序 —— 面板上的 "GPU 0 / 1 / …" 用的是 nvidia-smi 的 index, 和 Windows
    任务管理器里的编号是同一套 (本机实测两边一致: GPU 0 = PCI 总线 3)。数值则取 LHM。

    两边的枚举顺序不保证一样 (LHM 走 NVAPI, 本机实测和 PCI 顺序正好相反), 所以不能按位置配,
    得拿功耗/风扇转速把卡一张张对上号, 见 _pair_gpus。

    已用显存是唯一的例外: 它单独走 Windows 的 GPU Adapter Memory 计数器。LHM 和 nvidia-smi 都
    看不到 WSL 显存虚拟化占掉的那部分 —— 本机实测那张 20 GB 的卡它们都报 ~2 GB, 而这个计数器的
    值和任务管理器一致。它的 phys 序号也是 PCI 顺序, 所以按位置对得上 (见 _adapter_mem)。

    LHM 没装 / 没在跑时整个退回 nvidia-smi; 反过来没有 nvidia-smi 时按 LHM 自己的顺序排。
    """
    gpus = _panel_rows(_nvidia_gpus(), _lhm_gpus(tree))
    used = _adapter_mem.used_gb(len(gpus)) if gpus else None
    if used:
        for g, u in zip(gpus, used):
            g["used"] = u
    return gpus


# ---------------------------------------------------------------- 进程排行 (点击面板时按需采集)

_pdh = ctypes.windll.pdh
_pdh.PdhOpenQueryW.argtypes = [ctypes.c_wchar_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_void_p)]
_pdh.PdhAddEnglishCounterW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_void_p)]
_pdh.PdhCollectQueryData.argtypes = [ctypes.c_void_p]
_pdh.PdhGetFormattedCounterArrayW.argtypes = [
    ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p,
]
_PDH_FMT_DOUBLE = 0x200


class _PdhValue(ctypes.Structure):
    _fields_ = [("status", ctypes.c_uint32), ("value", ctypes.c_double)]


class _PdhItem(ctypes.Structure):
    _fields_ = [("name", ctypes.c_wchar_p), ("fmt", _PdhValue)]


_PDH_NAME = re.compile(r"pid_(\d+)_.*?_phys_(\d+)")


class _Procs:
    def __init__(self):
        self.lock = threading.Lock()
        self.primed = False
        self.query = ctypes.c_void_p()
        self.mem = self.eng = None
        self.io_prev, self.io_time, self.io_snap = None, 0.0, None

    def _gpu_open(self):
        if self.query:
            return
        _pdh.PdhOpenQueryW(None, 0, ctypes.byref(self.query))

        def add(path):
            h = ctypes.c_void_p()
            return h if _pdh.PdhAddEnglishCounterW(self.query, path, 0, ctypes.byref(h)) == 0 else None

        self.mem = add(r"\GPU Process Memory(*)\Dedicated Usage")
        self.eng = add(r"\GPU Engine(*)\Utilization Percentage")
        _pdh.PdhCollectQueryData(self.query)

    def _array(self, handle):
        if handle is None:
            return []
        size, count = ctypes.c_uint32(0), ctypes.c_uint32(0)
        _pdh.PdhGetFormattedCounterArrayW(handle, _PDH_FMT_DOUBLE, ctypes.byref(size), ctypes.byref(count), None)
        if not size.value:
            return []
        buf = ctypes.create_string_buffer(size.value)
        if _pdh.PdhGetFormattedCounterArrayW(handle, _PDH_FMT_DOUBLE, ctypes.byref(size), ctypes.byref(count), buf) != 0:
            return []
        items = ctypes.cast(buf, ctypes.POINTER(_PdhItem))
        out = []
        for i in range(count.value):
            m = _PDH_NAME.search(items[i].name or "")
            if m and items[i].fmt.status in (0, 1):
                out.append((int(m.group(1)), int(m.group(2)), items[i].fmt.value))
        return out

    @staticmethod
    def _name(pid):
        try:
            return psutil.Process(pid).name()
        except psutil.Error:
            return f"PID {pid}"

    def _cpu(self):
        if not self.primed:
            for p in psutil.process_iter():
                try:
                    p.cpu_percent(None)
                except psutil.Error:
                    pass
            time.sleep(0.5)
            self.primed = True
        ncpu = psutil.cpu_count() or 1
        rows = []
        for p in psutil.process_iter():
            if p.pid == 0:
                continue
            try:
                rows.append({"name": p.name(), "pid": p.pid, "v": p.cpu_percent(None) / ncpu})
            except psutil.Error:
                pass
        return rows

    def _ram(self):
        rows = []
        for p in psutil.process_iter():
            try:
                rows.append({"name": p.name(), "pid": p.pid, "v": p.memory_info().rss / 1024 ** 3})
            except psutil.Error:
                pass
        return rows

    @staticmethod
    def _io_snapshot():
        snap, now = {}, time.monotonic()
        for p in psutil.process_iter():
            try:
                io = p.io_counters()
                snap[p.pid] = (p.name(), io.read_bytes, io.write_bytes, now)
            except (psutil.Error, OSError):
                pass
        return snap

    def _disk(self):
        # psutil 的 io_counters 只含文件 I/O, 不含网络 (网络排行见 _NetProcs)
        if self.io_prev is None or self.io_time == 0.0:
            self.io_prev, self.io_time = self._io_snapshot(), time.monotonic()
        prev, ptime = self.io_prev, self.io_time
        cur = self._io_snapshot()
        now = time.monotonic()
        if now - ptime > 3.5:  # 两次调用间隔过久 (刚打完点) 就先攒满 0.6 秒再算速率
            prev, ptime = cur, now
            time.sleep(0.6)
            cur, now = self._io_snapshot(), time.monotonic()
        self.io_prev, self.io_time = cur, now
        dt = max(now - ptime, 0.2)
        rows = []
        for pid, (name, r, w, t) in cur.items():
            if pid in prev:
                dr, dw = max(0, r - prev[pid][1]) / dt / 1e6, max(0, w - prev[pid][2]) / dt / 1e6
                if dr or dw:
                    rows.append({"name": name, "pid": pid, "r": dr, "w": dw, "v": dr + dw})
        return rows

    def _gpu(self, index):
        self._gpu_open()
        _pdh.PdhCollectQueryData(self.query)
        mem = {}
        for pid, phys, v in self._array(self.mem):
            if phys == index:
                mem[pid] = mem.get(pid, 0) + v
        eng = {}
        for pid, phys, v in self._array(self.eng):
            if phys == index:
                eng[pid] = max(eng.get(pid, 0), v)
        return [{"name": self._name(pid), "pid": pid, "v": m / 1024 ** 3, "ex": min(100.0, eng.get(pid, 0.0))}
                for pid, m in mem.items() if m > 0]

    def top(self, kind, index=0):
        with self.lock:
            rows = {"cpu": self._cpu, "ram": self._ram, "disk": self._disk,
                    "net": _net.read}.get(kind, lambda: self._gpu(int(index)))()
        rows.sort(key=lambda r: r["v"], reverse=True)
        return rows[:10]


_procs = _Procs()


# ---------------------------------------------------------------- 每进程网络流量 (ETW)
# Windows 没有现成的"每进程网络字节数"接口: psutil 只有系统总量, GetPerTcpConnectionEStats 在本机
# 实测读不出数据。这里开一个 "NT Kernel Logger" 会话订阅 TCP/IP 内核事件: 发送/接收事件带有
# 进程号和字节数 (TCP、UDP, IPv4/IPv6 都有)。需要管理员权限, 没有权限或没装 pywintrace 时排行为空。
# 会话在第一次点开网络排行时才启动, 程序退出时停止。

_KERNEL_LOGGER = "NT Kernel Logger"
_NETWORK_TCPIP = 0x00010000
_ALREADY_EXISTS = 183


class _NetProcs:
    _SEND, _RECV = (10, 26), (11, 27)  # IPv4 / IPv6 的 opcode
    WINDOW = 3.0  # 速率取最近几秒的平均

    def __init__(self):
        self.lock = threading.Lock()
        self.buckets = {}  # 半秒一格: key -> {pid: [收字节, 发字节]}
        self.names = {}
        self.started = None
        self.failed = False
        self.handle = self.props = self.consumer = None

    def _new_props(self):
        from etw import wmistr
        from etw import evntrace
        from etw.etw import TraceProperties

        props = TraceProperties()
        p = props.get().contents
        p.Wnode.Flags = wmistr.WNODE_FLAG_TRACED_GUID
        p.LogFileMode = evntrace.EVENT_TRACE_REAL_TIME_MODE
        p.EnableFlags = _NETWORK_TCPIP
        p.FlushTimer = 1
        return props

    def _start(self):
        try:
            from etw import evntrace as et
            from etw.common import RETURN_RAW_DATA_ONLY
            from etw.etw import EventConsumer

            for _ in range(2):
                self.props, self.handle = self._new_props(), et.TRACEHANDLE()
                rc = et.StartTraceW(ctypes.byref(self.handle), _KERNEL_LOGGER, self.props.get())
                if rc != _ALREADY_EXISTS:
                    break
                # 上次程序被强行结束会留下会话, 先把它停掉再重开
                et.ControlTraceW(et.TRACEHANDLE(0), _KERNEL_LOGGER, self.props.get(), et.EVENT_TRACE_CONTROL_STOP)
            if rc != 0:
                raise OSError(rc)
            self.consumer = EventConsumer(_KERNEL_LOGGER, event_callback=self._on_event,
                                          callback_data_flag=RETURN_RAW_DATA_ONLY)
            self.consumer.start()
            self.started = time.monotonic()
        except Exception:
            self.failed = True

    def stop(self):
        if self.consumer is None:
            return
        try:
            from etw import evntrace as et
            self.consumer.stop()
            et.ControlTraceW(self.handle, _KERNEL_LOGGER, self.props.get(), et.EVENT_TRACE_CONTROL_STOP)
        except Exception:
            pass
        self.consumer = None

    def _on_event(self, evt):
        try:
            event = evt[1]
            op = event["EventHeader"]["EventDescriptor"]["Opcode"]
            if op in self._SEND:
                direction = 1
            elif op in self._RECV:
                direction = 0
            else:
                return
            pid, size = struct.unpack_from("<II", event["UserData"], 0)
        except Exception:
            return
        key = int(time.monotonic() * 2)
        with self.lock:
            row = self.buckets.setdefault(key, {}).setdefault(pid, [0, 0])
            row[direction] += size

    def read(self):
        if self.started is None and not self.failed:
            self._start()
        if self.failed or self.started is None:
            return []
        now = time.monotonic()
        span = min(self.WINDOW, max(now - self.started, 1.0))
        first = int((now - span) * 2) + 1
        totals = {}
        with self.lock:
            for key in [k for k in self.buckets if k < first - 20]:
                del self.buckets[key]
            for key, pids in self.buckets.items():
                if key >= first:
                    for pid, (rx, tx) in pids.items():
                        t = totals.setdefault(pid, [0, 0])
                        t[0] += rx
                        t[1] += tx
        mbps = 8 / 1e6 / span
        return [{"name": self._name(pid), "pid": pid, "r": rx * mbps, "w": tx * mbps, "v": (rx + tx) * mbps}
                for pid, (rx, tx) in totals.items() if rx or tx]

    def _name(self, pid):
        if pid not in self.names:
            try:
                self.names[pid] = psutil.Process(pid).name()
            except psutil.Error:
                self.names[pid] = "System" if pid == 4 else f"PID {pid}"
        return self.names[pid]


class _AdapterMem:
    """WDDM 下 nvidia-smi 的 memory.used 对部分显卡不准, 已用显存改取 Windows 的 GPU Adapter Memory 计数器。"""

    _NAME = re.compile(r"luid_(\w+_\w+)_phys_(\d+)")

    def __init__(self):
        self.query, self.handle = ctypes.c_void_p(), ctypes.c_void_p()
        self.ok = (
            _pdh.PdhOpenQueryW(None, 0, ctypes.byref(self.query)) == 0
            and _pdh.PdhAddEnglishCounterW(
                self.query, r"\GPU Adapter Memory(*)\Dedicated Usage", 0, ctypes.byref(self.handle)) == 0
        )

    def used_gb(self, count):
        """返回 count 张卡各自的已用显存(GB); 取不到返回 None。"""
        if not self.ok:
            return None
        _pdh.PdhCollectQueryData(self.query)
        size, n = ctypes.c_uint32(0), ctypes.c_uint32(0)
        _pdh.PdhGetFormattedCounterArrayW(self.handle, _PDH_FMT_DOUBLE, ctypes.byref(size), ctypes.byref(n), None)
        if not size.value:
            return None
        buf = ctypes.create_string_buffer(size.value)
        if _pdh.PdhGetFormattedCounterArrayW(self.handle, _PDH_FMT_DOUBLE, ctypes.byref(size), ctypes.byref(n), buf) != 0:
            return None
        items = ctypes.cast(buf, ctypes.POINTER(_PdhItem))
        adapters = {}
        for i in range(n.value):
            m = self._NAME.search(items[i].name or "")
            if m and items[i].fmt.status in (0, 1):
                adapters.setdefault(m.group(1), {})[int(m.group(2))] = items[i].fmt.value / 1024 ** 3
        # 同一批 NVIDIA 卡共用一个 luid, 其 phys 序号覆盖 0..count-1; 另有虚拟适配器时取显存最大的那组
        full = [a for a in adapters.values() if all(k in a for k in range(count))]
        if not full:
            return None
        best = max(full, key=lambda a: sum(a.values()))
        return [best[k] for k in range(count)]


_adapter_mem = _AdapterMem()
_net = _NetProcs()
atexit.register(_net.stop)


class Api:
    def top(self, kind, index=0):
        try:
            return _procs.top(kind, index)
        except Exception:
            return []


# ---------------------------------------------------------------- 界面

def read_cpu_name():
    """CPU 型号。注册表里那个字符串带 "(R)"/"(TM)"、末尾 "@ 2.20GHz", 中间还有多余的
    "CPU" —— 精简成画布标题里的写法 (Intel Xeon E5-2696 v4)。"""
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as key:
            name = winreg.QueryValueEx(key, "ProcessorNameString")[0]
    except OSError:
        return None
    name = re.sub(r"\((?:R|TM|C)\)", "", name, flags=re.I)  # Intel(R) -> Intel
    name = re.sub(r"\s*@\s*[\d.]+\s*GHz\s*$", "", name)  # 去掉频率
    name = re.sub(r"\s+CPU\b", "", name, flags=re.I)  # Intel Xeon CPU E5 -> Intel Xeon E5
    return re.sub(r"\s{2,}", " ", name).strip() or None


def _short_gpu(name):
    """只用来判重 (名字一样的两张卡在标题里合并成 ×N), 显示时还是用全名。"""
    return re.sub(r"^(NVIDIA|AMD|Intel)\s+", "", name, flags=re.I).strip().lower()


def _join(parts):
    return " · ".join(p for p in parts if p)


def _live_title(template, cpu, gpus, mem):
    """标题栏: <程序名> <版本> · 作者 <邮箱> — <本机 CPU> · <显卡 ×N> · 内存 <GB> GB · 每秒采样 · 实时数据

    机型信息都是启动时从本机读的, 所以这里不是简单替换模板, 而是整条重建
    (模板只在读不到机型时兜底 —— 比如纯浏览器预览时看的还是画布里的占位值)。
    """
    gpu_part = None
    if gpus:
        seen, counts = {}, {}
        for g in gpus:
            full = (g.get("name") or "").strip()
            if not full:
                continue
            key = _short_gpu(full)
            seen.setdefault(key, full)  # 同名只留一个写法
            counts[key] = counts.get(key, 0) + 1
        if seen:
            gpu_part = " / ".join(
                name + (f" × {counts[key]}" if counts[key] > 1 else "")
                for key, name in seen.items()
            )
    mem_part = f"内存 {round(mem['total'])} GB" if mem and mem.get("total") else None
    tail = ["每秒采样", "实时数据"]
    if not cpu and not gpu_part and not mem_part:
        return template  # 什么都读不到, 保留画布里的模板
    brand = f"{APP_NAME} {APP_VERSION} · 作者 {APP_AUTHOR}"
    specs = _join([cpu, gpu_part, mem_part])
    # 作者和机型之间是破折号, 其余用间隔号 —— 跟画布一致
    return _join([f"{brand} — {specs}" if specs else brand, " · ".join(tail)])


def sample_loop(window, loaded, stop, tray):
    loaded.wait()
    prev = _system_times()
    prev_thr = psutil.cpu_times(percpu=True)  # 每个逻辑核的使用率要两次采样才能算差值
    # 窗口尺寸由 create_window 的 min_size 钉住 (内容区 1052×802), 这里不再手动补偿
    # 标题栏 —— 托盘悬停要挪窗口, 手动改尺寸会跟它打架。
    # 窗口起始是隐藏的, 这一刻不去碰页面 (evaluate_js 对没显示出来的窗口不保险); 标题每秒都会
    # 按本机实况重建, 这个模板只在"CPU/显卡/内存全都读不到"时才用得上, 留空即可。
    template = ""
    cpu_name = read_cpu_name()  # 型号不会变, 只读一次
    disk_label = _disk_label()  # 磁盘类型也不会变, 读一次就够
    shown = None
    while not stop.wait(1.0):
        cur = _system_times()
        cur_thr = psutil.cpu_times(percpu=True)
        tree = _lhm_tree()  # CPU 温度/功耗和 GPU 温度都从这棵树里取, 一次请求
        usage, (temp, power), gpus, mem = _usage_between(prev, cur), read_cpu_sensors(tree), read_gpus(tree), read_memory()
        threads = _threads_between(prev_thr, cur_thr)
        disk = read_disk(disk_label)
        io = _rates.read()
        prev, prev_thr = cur, cur_thr
        tray.update(usage, mem, gpus)  # 先更新 tooltip, 窗口藏着的时候也要刷新
        args = ",".join(json.dumps(x) for x in (usage, temp, gpus, mem, io, power, threads, disk))
        try:
            # 标题跟显隐无关: 窗口一露出来就该是完整标题, 不用等下一次采样。机型基本不变,
            # 所以 title != shown 几乎总是假, 这个 set_title 实际上只在开头调一两次。
            title = _live_title(template, cpu_name, gpus, mem)
            if title and title != shown:
                window.set_title(title)
                shown = title
            if not tray.visible:
                continue  # 藏在托盘里, 页面反正看不见, 省掉每秒的推送
            window.evaluate_js(f"window.pushSample({args})")
        except Exception:
            return  # 窗口已关闭


def main():
    if not HTML.is_file():
        raise SystemExit(f"找不到界面文件: {HTML}")

    # 温度传感器 (LibreHardwareMonitor) 没装的话, 这里问一次要不要装。必须放在
    # create_window 之前: 那是个模态框, 后建的窗口会盖在它上面。
    # --tray (开机自启) 走非交互路径, 结果交给托盘图标起来之后发气泡说。
    sensors = lhm.ensure(interactive=not TRAY_ONLY)

    # #live: 让页面不跑模拟数据, 等待本程序推送
    # min_size: 把内容区钉在 1052×802 (设计稿尺寸), 不用再去量标题栏补差。窗口本身
    # 不会更大 (resizable=False), 挪窗口时也不会被改小。
    # on_top: 监控窗要一直看得见, 不能被别的窗口压到底下 (winforms 后端用 TopMost)。
    # hidden/minimized: 启动时不弹窗, 直接进托盘 (见 tray.Tray 的 visible=False) ——
    # 显示靠点托盘图标或菜单, 跟 --tray 无关, --tray 只额外管"不发气泡"。
    # x/y: 一开始就摆在右下角 (和悬停弹窗同一个位置), 之后显示出来时就不用再挪 ——
    # 不然会先在屏幕中间露一下再跳过去。
    x, y = tray_mod.start_pos(*SIZE)
    window = webview.create_window(
        "Pulse", url=HTML.resolve().as_uri() + "#live", js_api=Api(),
        width=SIZE[0], height=SIZE[1], min_size=SIZE, resizable=False, on_top=True,
        background_color="#DCE1D8", hidden=True, minimized=True, x=x, y=y,
    )
    loaded, stop = threading.Event(), threading.Event()
    tray = tray_mod.Tray(window, stop, SCRIPT, admin=ADMIN, tray_only=TRAY_ONLY,
                         visible=False, sensors=sensors)  # visible=False: 窗口起始是隐藏的
    window.events.loaded += loaded.set
    window.events.closed += stop.set
    window.events.closing += tray.on_closing
    window.events.closed += tray.on_closed
    threading.Thread(target=_net.read, daemon=True).start()  # 提前开始抓包, 首次点开排行才有数据
    tray.start()  # 要在 webview.start() 之前: 后端要求先进消息循环, 之后设图标是不生效的
    try:
        webview.start(lambda: sample_loop(window, loaded, stop, tray), icon=str(tray_mod.ICON))
    finally:
        tray.on_closed()


if __name__ == "__main__":
    main()
