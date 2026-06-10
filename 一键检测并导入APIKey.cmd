@echo off
cd /d "%~dp0"
if exist "%~dp0local_config.cmd" call "%~dp0local_config.cmd"
python "%~dp0ops\check_import_api_key_upstream.py" %*
