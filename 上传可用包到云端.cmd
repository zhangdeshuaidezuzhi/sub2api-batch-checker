@echo off
cd /d "%~dp0"
if exist "%~dp0local_config.cmd" call "%~dp0local_config.cmd"
if "%~1"=="" (
  echo Usage: %~nx0 outputs\sub2api_good_accounts.json
  exit /b 2
)
python "%~dp0ops\import_sub2api_good_bundle.py" "%~1"
