@echo off
cd /d "%~dp0"
if exist "%~dp0local_config.cmd" call "%~dp0local_config.cmd"
python -m sub2api_batch_checker.gui
