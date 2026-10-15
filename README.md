# Pulse

Windows 硬件监控小工具。CPU / GPU 的使用率、温度、功耗、显存，磁盘和网络吞吐，
点击面板看前 10 进程排行。界面用 pywebview 渲染 HTML，常驻系统托盘。

![Pulse 界面：左边是 CPU 使用率 / 温度 / 线程竖条 / 折线图，右边每张显卡一栏（CUDA、显存、温度、功耗），顶部一排汇总，底部是磁盘读写和网络吞吐](pictures/pulse-ui.png)

## 依赖

Pulse 自己的 Python 依赖就是 `pip install` 那几行（见下面「跑起来」）。
**但显卡的状态 Pulse 自己读不到 —— 全靠下面两个外部程序**：

| 要监控什么 | 靠谁 | 没有它的时候 |
|---|---|---|
| GPU 的占用率 / 温度 / 功耗 | **LibreHardwareMonitor** 的 Web 服务（`localhost:8085`） | 整个 GPU 面板退回 `nvidia-smi` |
| GPU 的编号顺序、功耗上限 | **`nvidia-smi`**（随 NVIDIA 驱动安装） | 面板按 LHM 自己的顺序排；功率条显示 `/ -- W` |
| GPU 已用显存 | Windows 的 `GPU Adapter Memory` 计数器 | 退回 LHM / `nvidia-smi` 的读数 |
| CPU 温度 / 封装功耗 | LibreHardwareMonitor | 温度显示 `--` |
| 网络前 10 进程排行 | ETW 内核日志（`pywintrace`） | 只有这一项排行是空的 |

**LibreHardwareMonitor** 是传感器数据的来源，CPU 和 GPU 的温度、功耗都问它。它是个独立的
.NET 程序（不是 Python 库），Pulse 通过它的 Web 服务（`http://localhost:8085/data.json`）读数。
分发包里带了一份，首次启动会问用户要不要装到本机并开机自启（打包前怎么准备见下面
「准备 LibreHardwareMonitor」）；没装也能跑，只是温度那几格空着（GPU 温度退回 `nvidia-smi`
自己报的值）。有个绕不开的点：它的 manifest 是 `requireAdministrator`，所以由 Pulse 拉起时
**必然弹一次 UAC**，加进开机自启就是每次开机弹一次。

**`nvidia-smi`** 随 NVIDIA 驱动一起装，不用单独准备。Pulse 只用它两件事：给面板定**编号顺序**
（PCI 顺序，和任务管理器一致），以及提供 LHM 没有的**功耗上限**。机器上没有 NVIDIA 显卡或驱动时
它就不存在，GPU 面板会显示「未检测到 NVIDIA 显卡」。

**管理员权限**：网络排行靠 ETW 内核日志，非管理员启动时那一项是空的 —— 托盘会发气泡提醒，
菜单里有「以管理员身份重新启动」。

### GPU 的数值和编号为什么来自两个地方

数值取 LHM（占用率 `Load\GPU Core`、温度 `Temperatures\GPU Core`、功耗 `Powers\GPU Package`）；
**编号**（「GPU 0 / 1」）取 `nvidia-smi` 的 `index`，也就是 **PCI 顺序** —— 和 Windows 任务管理器
里的编号是同一套。

两个来源的**枚举顺序不保证一样**：`nvidia-smi` 按 PCI 顺序，LHM 走 NVAPI 的顺序，本机实测正好相反：

| | 功耗 | 风扇 | nvidia-smi / 任务管理器 | LHM 树 |
|---|---|---|---|---|
| 同一张卡 | 17 W | 45 % | `index 1`（PCI bus 4） | 第 1 张 |
| 另一张 | 42 W | 39 % | `index 0`（PCI bus 3） | 第 2 张 |

同型号的两张卡名字一模一样，靠名字分不出来；LHM 的 JSON 里也只有 `HardwareId: /gpu-nvidia/0`
这种自有序号，不带 PCI 信息。所以**不能按位置硬配** —— 那样温度、功耗、显存会整卡错位一张。
Pulse 拿两边都会报的**功耗和风扇转速**当指纹，把卡一张张对上号（`pulse.py` 的 `_pair_gpus`）。
功耗上限则按显卡型号名取：上限是型号的属性，同型号的卡是同一个值，不需要对上号。

**已用显存是唯一没走 LHM 的**：本机实测那张占 20 GB 的卡，LHM（NVAPI）和 `nvidia-smi`（NVML）
都只报 ~2 GB —— 这两个驱动 API 看不到 WDDM 口径里的那部分，而 Windows 的 `GPU Adapter Memory`
计数器的值和任务管理器一致（都是 20.0 GB）。它的 `phys` 序号也是 PCI 顺序，所以按位置对得上，
见 `pulse.py` 的 `_AdapterMem`。

## 跑起来

需要 Python 3.10+ 和 Windows 10/11（依赖 WebView2 运行时，Win11 和较新的 Win10 自带）。

```cmd
pip install pywebview psutil pywintrace pystray pillow
python pulse.py
```

**启动时窗口是隐藏的**，只进系统托盘（托盘图标悬停会弹出小窗，点图标或菜单里的「显示窗口」才
正常打开；关窗口只是收回托盘，采样继续）。窗口一开始就摆在**工作区右下角** —— 和悬停弹窗同一个
位置，所以显示出来时不用再挪，不会先出现在屏幕中间再跳过去。`--tray` 是给开机自启用的：一样
隐藏启动，另外不再发气泡提醒。

## 打包成单文件 exe

```cmd
build.cmd
```

产物在 `dist\`：

| 路径 | 说明 |
|---|---|
| `dist\Pulse.exe` | 单文件，自带 Python 解释器和全部依赖，目标机器不需要装任何东西 |
| `dist\LibreHardwareMonitor\` | 温度传感器。首次启动会问用户要不要装到本机 |

发给用户的是**整个 `dist` 文件夹**，不是一个 exe。

### 准备 LibreHardwareMonitor

`LibreHardwareMonitor/` 是**第三方运行时**，没有提交进仓库（见 `.gitignore`）。
打包前把它放到项目根目录：

1. 从 [LibreHardwareMonitor releases](https://github.com/LibreHardwareMonitor/LibreHardwareMonitor/releases)
   下载发布包并解压到 `LibreHardwareMonitor/`。
2. 删掉 `Aga.Controls.pdb`、`LibreHardwareMonitorLib.pdb`、`LibreHardwareMonitorLib.xml`
   （调试符号，运行时用不到，省 1.4 MB）。
3. **把 `LibreHardwareMonitor.config` 换成精简版**——发布包或你自己机器上那份会带着
   几百个传感器状态 key（`/intelcpu/0/load/2/plot` 之类），那是每台机器学出来的，发给别人只会
   加载一堆不存在的路径：

   ```xml
   <?xml version="1.0" encoding="utf-8"?>
   <configuration>
     <appSettings>
       <add key="runWebServerMenuItem" value="true" />
       <add key="listenerPort" value="8085" />
       <add key="startMinMenuItem" value="true" />
       <add key="minTrayMenuItem" value="true" />
     </appSettings>
   </configuration>
   ```

   `runWebServerMenuItem=true` 就是「启动时自动开 Web 服务」，不需要命令行参数
   （LHM 也不接受任何命令行参数）。
4. 放一份 MPL-2.0 许可证全文为 `LibreHardwareMonitor/LICENSE.txt`——再分发必须随附。

最终约 11 MB。

## 结构

```
pulse.py          入口。采样循环、win32 计数器的 ctypes 封装、注入页面的 pushSample
pulse.html        全部界面。CSS 变量是设计稿的配色，改配色改这里
tray.py           系统托盘：图标、菜单、悬停弹窗、开机自启
lhm.py            LibreHardwareMonitor 的检测 / 安装 / 启动 / 自启
pulse.spec        PyInstaller 配置
build.cmd         打包 = pyinstaller + 拷贝 LHM
backup/           设计稿备份（来自 doop 画布）：frame HTML、2x 截图、元数据
pictures/         README 用的界面截图
```

界面完全由 `pulse.html` 决定，`pulse.py` 只负责每秒采样一次，然后调用页面里的

```js
window.pushSample(usage, temp, gpus, mem, io, power, threads, disk)
```

`None` 表示该项暂无数据。**换设计 = 换 HTML 文件。**

## 许可

本仓库的代码以 MIT 许可发布。

`LibreHardwareMonitor/` 是第三方软件，不属于本仓库，以 **MPL-2.0** 授权。
`Aga.Controls` 等其他 DLL 各有其许可证，再分发时请一并遵守。
