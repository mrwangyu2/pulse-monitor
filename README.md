# Pulse

Windows 硬件监控小工具。CPU / GPU 的使用率、温度、功耗、显存，磁盘和网络吞吐，
点击面板看前 10 进程排行。界面用 pywebview 渲染 HTML，常驻系统托盘。

```
┌─────────────────────────────────────────────────────────────┐
│ Pulse v1.0.0 · 作者 … — Xeon E5-2696 v4 · RTX 2080 Ti ×2     │
│                        · 内存 64 GB · 每秒采样 · 实时数据      │
├──────────────────────────────┬──────────────────────────────┤
│ CPU  使用率 / 温度 / 功耗      │ GPU  每张卡一栏               │
│      44 线程竖条 + 折线图      │      CUDA / 显存 / 温度 / 功耗 │
│ 前 10 进程                    │ 前 10 进程 (按显存)            │
├──────────────────────────────┴──────────────────────────────┤
│ 磁盘 读/写 · 网络 上行/下行                                  │
└─────────────────────────────────────────────────────────────┘
```

## 跑起来

需要 Python 3.10+ 和 Windows 10/11（依赖 WebView2 运行时，Win11 和较新的 Win10 自带）。

```cmd
pip install pywebview psutil pywintrace pystray pillow
python pulse.py
```

CPU 温度需要 [LibreHardwareMonitor](https://github.com/LibreHardwareMonitor/LibreHardwareMonitor)，
见下面「准备 LHM」——没装也能跑，只是温度那两格显示 `--`。

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

## 准备 LHM

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

## 两个外部依赖

| 数据 | 从哪来 | 缺了会怎样 |
|---|---|---|
| GPU 全部指标 | `nvidia-smi`（随 NVIDIA 驱动安装） | GPU 面板整体显示"未检测到 NVIDIA 显卡" |
| CPU 温度 / 封装功耗 | LibreHardwareMonitor 的 Web 服务（`localhost:8085`） | 温度显示 `--` |
| 网络前 10 进程排行 | ETW 内核日志（`pywintrace`） | 只有这一项排行是空的 |

ETW 需要**管理员权限**。非管理员启动时托盘会发气泡提醒，菜单里有「以管理员身份重新启动」。
注意 LHM 自己的 manifest 是 `requireAdministrator`，所以由 Pulse 拉起它时必然弹一次 UAC；
把它加进开机自启意味着**每次开机弹一次**。

## 结构

```
pulse.py          入口。采样循环、win32 计数器的 ctypes 封装、注入页面的 pushSample
pulse.html        全部界面。CSS 变量是设计稿的配色，改配色改这里
tray.py           系统托盘：图标、菜单、悬停弹窗、开机自启
lhm.py            LibreHardwareMonitor 的检测 / 安装 / 启动 / 自启
pulse.spec        PyInstaller 配置
build.cmd         打包 = pyinstaller + 拷贝 LHM
backup/           设计稿备份（来自 doop 画布）：frame HTML、2x 截图、元数据
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
