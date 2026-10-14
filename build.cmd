@echo off
rem 打包 Pulse: 生成 dist\Pulse.exe (单文件, 自带 Python 和全部依赖),
rem 并把 LibreHardwareMonitor 拷成并排文件夹一起发出去。
rem
rem LHM 不打进 exe —— 它是独立的 .NET 程序, 而且用户那边可能已经自己装了一份。
rem 放旁边由 pulse.py 启动时检测, 见 lhm.py。
setlocal
cd /d "%~dp0"

echo === 1/2  PyInstaller ===
pyinstaller pulse.spec --noconfirm || exit /b 1

echo === 2/2  拷贝 LibreHardwareMonitor ===
if exist "dist\LibreHardwareMonitor" rmdir /S /Q "dist\LibreHardwareMonitor"
xcopy /E /I /Y "LibreHardwareMonitor" "dist\LibreHardwareMonitor" >nul || exit /b 1

echo.
echo 完成。发给用户的是整个 dist 文件夹:
echo   dist\Pulse.exe                     主程序 (单文件, 不需要装 Python)
echo   dist\LibreHardwareMonitor\         温度传感器, 首次启动会问用户是否安装
endlocal
