@echo off
setlocal
echo Jason-memory: permanently remove this package's memory and the shared global installation.
echo Close AI sessions before running. Other projects are not searched.
where py >nul 2>nul
if errorlevel 1 goto try_python
py -3 -c "import sys; sys.exit(sys.version_info < (3, 9))" >nul 2>nul
if errorlevel 1 goto try_python
py -3 -X utf8 "%~dp0tools\purge_memory.py" %*
goto done
:try_python
python -c "import sys; sys.exit(sys.version_info < (3, 9))" >nul 2>nul
if errorlevel 1 goto missing_python
python -X utf8 "%~dp0tools\purge_memory.py" %*
goto done
:missing_python
echo Python 3.9 or newer is required. Nothing has been removed.
exit /b 1
:done
set "JASON_PURGE_EXIT=%ERRORLEVEL%"
if not "%~1"=="" goto finish
pause
:finish
exit /b %JASON_PURGE_EXIT%
