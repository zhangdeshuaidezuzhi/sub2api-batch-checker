@echo off
setlocal
chcp 65001 >nul

set "ROOT=%~dp0"
set "INPUT=%~1"
if "%INPUT%"=="" set "INPUT=D:\注册机最新版\results"

call "%ROOT%local_config.cmd" 2>nul

echo 输入目录: %INPUT%
echo 模式: dry-run，只转换并生成本地 SQL，不导入云端。
echo.
python "%ROOT%ops\import_register_results_fallback.py" --results-dir "%INPUT%" --dry-run --keep-local-sql
echo.
pause
