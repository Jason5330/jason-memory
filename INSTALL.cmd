@echo off
python "%~dp0tools\install_hooks.py" %*
if errorlevel 1 (
  echo Installation failed. Python 3.9 or newer is required. Existing memories are preserved.
  exit /b 1
)
