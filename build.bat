@echo off
rem Builds dist\PUBG_Inviter\ (the program) and dist\PUBG_Inviter_Setup_<version>.exe (the installer).
setlocal
cd /d "%~dp0"

if not exist .venv\Scripts\python.exe (
    python -m venv .venv || goto :error
)
.venv\Scripts\python -m pip install -q -r requirements.txt pyinstaller || goto :error

for /f %%v in ('.venv\Scripts\python -c "from app import APP_VERSION; print(APP_VERSION)"') do set VERSION=%%v
echo Version %VERSION%

.venv\Scripts\python -m PyInstaller --noconfirm --clean --onedir --noconsole ^
    --name PUBG_Inviter --icon assets\app.ico --add-data "assets;assets" --add-data "ui;ui" ^
    --hidden-import pystray._win32 --collect-submodules winrt --collect-all webview ^
    app.py || goto :error

set ISCC=
for %%p in ("%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" "%ProgramFiles%\Inno Setup 6\ISCC.exe") do (
    if exist %%p set ISCC=%%p
)
if not defined ISCC (
    echo Inno Setup 6 not found - installer skipped. Program is in dist\PUBG_Inviter
    goto :end
)
%ISCC% /Q /DAppVersion=%VERSION% installer.iss || goto :error
echo Done: dist\PUBG_Inviter_Setup_%VERSION%.exe
goto :end

:error
echo BUILD FAILED
exit /b 1
:end
endlocal
