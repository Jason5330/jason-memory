@echo off
setlocal
where py >nul 2>nul
if errorlevel 1 goto try_python
py -3 -c "import sys; sys.exit(sys.version_info < (3, 9))" >nul 2>nul
if errorlevel 1 goto try_python
py -3 "%~dp0tools\install_hooks.py" %*
goto done

:try_python
python -c "import sys; sys.exit(sys.version_info < (3, 9))" >nul 2>nul
if errorlevel 1 goto missing_python
python "%~dp0tools\install_hooks.py" %*
goto done

:missing_python
echo Python 3.9 or newer is required. Install Python and run INSTALL.cmd again.
cmd /c exit 1

:done
set "JASON_INSTALL_EXIT=%ERRORLEVEL%"
if "%~1"=="" pause
exit /b %JASON_INSTALL_EXIT%
