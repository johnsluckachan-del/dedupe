@echo off
setlocal
set "PYTHONPATH=%~dp0;%PYTHONPATH%"
python -m dedupe %*
exit /b %ERRORLEVEL%
