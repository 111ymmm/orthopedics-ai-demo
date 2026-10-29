@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

if not defined ORTHO_PYTHON goto project_python
"%ORTHO_PYTHON%" -c "import sys; assert sys.version_info >= (3,10)" >nul 2>nul
if errorlevel 1 goto project_python
set "_ORTHO_RUNTIME=%ORTHO_PYTHON%"
goto launch

:project_python
if not exist "%~dp0.venv\Scripts\python.exe" goto registered_python
"%~dp0.venv\Scripts\python.exe" -c "import sys; assert sys.version_info >= (3,10)" >nul 2>nul
if errorlevel 1 goto registered_python
set "_ORTHO_RUNTIME=%~dp0.venv\Scripts\python.exe"
goto launch

:registered_python
py -3 -c "import sys; assert sys.version_info >= (3,10)" >nul 2>nul
if errorlevel 1 goto existing_runtime
py -3 start.py %*
goto result

:existing_runtime
set "_ORTHO_RUNTIME=%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
if not exist "%_ORTHO_RUNTIME%" goto path_python
"%_ORTHO_RUNTIME%" -c "import sys; assert sys.version_info >= (3,10)" >nul 2>nul
if not errorlevel 1 goto launch

:path_python
python -c "import sys; assert sys.version_info >= (3,10)" >nul 2>nul
if errorlevel 1 goto missing_python
python start.py %*
goto result

:launch
"%_ORTHO_RUNTIME%" start.py %*
goto result

:missing_python
echo 未找到可用的 Python 3.10 或更新版本。
echo 请安装 Python，或将 ORTHO_PYTHON 环境变量指向已有的 python.exe。
pause
exit /b 1

:result
if errorlevel 1 (
    echo.
    echo 启动失败，请查看上方提示。首次安装依赖需要网络连接。
    pause
    exit /b 1
)
exit /b 0
