[简体中文](README.md) | **English**

# Pulse

A small Windows hardware monitor. When you run local AI models, the machine itself is the thing to
watch: how much VRAM is left, whether the GPU is throttling because it hit its temperature limit,
whether the CPU or RAM is the bottleneck. But that data is scattered — Task Manager shows
utilization but no GPU temperature, GPU temperature and power live in LibreHardwareMonitor behind a
tree of sensors you have to dig through, and `nvidia-smi` means opening a console. And after all that
window-switching, the numbers aren't even from the same moment, so they don't line up.

Pulse puts it all on one screen: utilization / temperature / power / VRAM for the CPU, each GPU,
memory, disks and network — plus the top 10 processes for whichever panel you click. The UI is HTML
rendered by pywebview, and Pulse **lives in the system tray: move the mouse over the tray icon and
the window pops up, move it away and the window hides** — checking on the machine never means
switching windows.

![Pulse UI: CPU utilization / temperature / per-thread bars / chart on the left; one column per GPU on the right (CUDA, VRAM, temperature, power); a summary row across the top; disk and network throughput at the bottom](pictures/pulse-ui.png)

*(The app's interface text is Chinese.)*

## Dependencies

Pulse can't read GPU state by itself — it relies on two external programs:

| What you're monitoring | Where it comes from |
|---|---|
| GPU utilization / temperature / power | [LibreHardwareMonitor](https://github.com/LibreHardwareMonitor/LibreHardwareMonitor)'s web server (`localhost:8085`) |
| GPU numbering, power limit | `nvidia-smi` (ships with the NVIDIA driver) |
| GPU VRAM in use | Windows' `GPU Adapter Memory` counters |
| CPU temperature / package power | LibreHardwareMonitor |
| Top 10 network processes | ETW kernel log (`pywintrace`) |

- **LibreHardwareMonitor** is a standalone .NET program, not a Python library — every temperature and
  power reading comes from it. A copy ships with the release; Pulse runs fine without it, except the
  temperature cells stay blank (GPU temperature falls back to what `nvidia-smi` reports). Its
  manifest is `requireAdministrator`, so Pulse launching it always triggers a UAC prompt.
- **`nvidia-smi`** comes with the NVIDIA driver — nothing extra to install. On a machine with no
  NVIDIA GPU or driver, the GPU panel has no data.
- **Administrator rights**: the network ranking uses the ETW kernel log; without them that one panel
  is empty (the tray notifies you, and the menu can restart Pulse elevated).

Why the GPU values and the GPU numbering come from two different sources — and why VRAM bypasses both
— is explained in the comments on `_pair_gpus` and `_AdapterMem` in `pulse.py`.

## Running it

Needs Python 3.10+ and Windows 10/11 (the WebView2 runtime is required; Windows 11 and recent
Windows 10 ship with it).

```cmd
pip install pywebview psutil pywintrace pystray pillow
python pulse.py
```

**The window starts hidden**, in the tray — hover the icon and it pops up, move away and it hides.
Clicking the icon also shows/hides it manually, and the menu has "Show window". Closing the window
just puts it back in the tray; sampling continues. The window sits next to the tray icon
(bottom-right of the work area). `--tray` is for autostart: it also starts hidden, and sends no
balloon notifications.

## Building a single-file exe

```cmd
build.cmd
```

The output lands in `dist\`:

| Path | What it is |
|---|---|
| `dist\Pulse.exe` | Single file; bundles the Python interpreter and all dependencies, so the target machine needs nothing installed |
| `dist\LibreHardwareMonitor\` | The temperature sensors. On first launch Pulse asks the user whether to install them locally |

Ship the **whole `dist` folder**, not just the exe.

### Preparing LibreHardwareMonitor

`LibreHardwareMonitor/` is a **third-party runtime** and is not committed to this repo (see
`.gitignore`). Before building, put it in the project root:

1. Download a release from
   [LibreHardwareMonitor releases](https://github.com/LibreHardwareMonitor/LibreHardwareMonitor/releases)
   and extract it into `LibreHardwareMonitor/`.
2. Delete `Aga.Controls.pdb`, `LibreHardwareMonitorLib.pdb` and `LibreHardwareMonitorLib.xml`
   (debug symbols, unused at runtime — saves 1.4 MB).
3. **Replace `LibreHardwareMonitor.config` with a trimmed version.** The stock file (or the one on
   your own machine) carries hundreds of per-sensor keys (`/intelcpu/0/load/2/plot` and friends) that
   were learned on one specific machine; shipping them just makes LHM load a pile of paths that don't
   exist:

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

   `runWebServerMenuItem=true` is "start the web server automatically on launch" — no command-line
   argument needed (LHM doesn't accept any anyway).
4. Include the full MPL-2.0 license text as `LibreHardwareMonitor/LICENSE.txt` — redistribution
   requires it.

About 11 MB in the end.

## Layout

```
pulse.py        Entry point. Sampling loop, ctypes wrappers for win32 counters, pushSample injection
pulse.html      The whole UI. CSS variables are the design's palette — change colors here
tray.py         System tray: icon, menu, hover popup, autostart
lhm.py          LibreHardwareMonitor detection / install / launch / autostart
pulse.spec      PyInstaller config
build.cmd       Build = pyinstaller + copy LHM
backup/         Design backups (from the doop canvas): frame HTML, 2x screenshot, metadata
pictures/       UI screenshots used by the README
```

The UI is entirely determined by `pulse.html`; `pulse.py` only samples once a second and calls into
the page:

```js
window.pushSample(usage, temp, gpus, mem, io, power, threads, disk)
```

`None` means that item has no data right now. **New design = new HTML file.**

## License

The code in this repository is released under the MIT license.

`LibreHardwareMonitor/` is third-party software, not part of this repo, and is licensed under
**MPL-2.0**. `Aga.Controls` and the other DLLs each carry their own license — respect them when
redistributing.
