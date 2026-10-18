@echo off
setlocal
pushd "%~dp0" >nul 2>&1
if errorlevel 1 (
    echo Unable to open the MTGA assistant project folder.
    pause
    exit /b 1
)

set "MTGA_PYTHON="
if exist "C:\Python312\python.exe" set "MTGA_PYTHON=C:\Python312\python.exe"
if not defined MTGA_PYTHON if exist "%LOCALAPPDATA%\Programs\Python\Python-312\python.exe" set "MTGA_PYTHON=%LOCALAPPDATA%\Programs\Python\Python-312\python.exe"
if not defined MTGA_PYTHON if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "MTGA_PYTHON=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not defined MTGA_PYTHON if exist "%LOCALAPPDATA%\Programs\Python\Python-311\python.exe" set "MTGA_PYTHON=%LOCALAPPDATA%\Programs\Python\Python-311\python.exe"
if not defined MTGA_PYTHON if exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" set "MTGA_PYTHON=%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
if not defined MTGA_PYTHON if exist "C:\Python311\python.exe" set "MTGA_PYTHON=C:\Python311\python.exe"
if not defined MTGA_PYTHON (
    where python.exe >nul 2>&1
    if not errorlevel 1 set "MTGA_PYTHON=python.exe"
)
if not defined MTGA_PYTHON (
    echo Native Python 3.10 or newer is required to run the Arena overlay.
    pause
    popd
    exit /b 2
)

"%MTGA_PYTHON%" -m mtga_assistant overlay
set "MTGA_OVERLAY_EXIT_CODE=%ERRORLEVEL%"
popd

if not "%MTGA_OVERLAY_EXIT_CODE%"=="0" (
    echo.
    echo The MTGA overlay exited with code %MTGA_OVERLAY_EXIT_CODE%.
    pause
)
exit /b %MTGA_OVERLAY_EXIT_CODE%
