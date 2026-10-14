@echo off
rem Launch Pulse without a console window.
rem Prefer the packaged exe (dist\Pulse.exe, no Python needed); fall back to
rem running the script for development. For debugging, replace pythonw with
rem python to see errors.
cd /d "%~dp0"
if exist "%~dp0dist\Pulse.exe" (
    start "" "%~dp0dist\Pulse.exe" %*
) else (
    where pythonw >nul 2>nul && (start "" pythonw "%~dp0pulse.py" %*) || (start "" pyw "%~dp0pulse.py" %*)
)
