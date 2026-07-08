@echo off
REM %~dp0 = folder this .bat lives in (…\lstar-mpod\src\), with trailing backslash.
REM Push to the repo root (parent of src) so this works no matter where it's
REM launched from including a desktop shortcut with any "Start in" folder.
pushd "%~dp0.."

REM pythonw.exe runs the Tkinter GUI without a console window (clean double-click).
start "" ".venv\Scripts\pythonw.exe" "src\lstar_gui.py" %*

popd
