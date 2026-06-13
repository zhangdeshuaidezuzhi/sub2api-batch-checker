@echo off
setlocal
chcp 65001 >nul

set "ROOT=%~dp0"
set "INPUT=%~1"
if "%INPUT%"=="" set "INPUT=D:\注册机最新版\results"

call "%ROOT%local_config.cmd" 2>nul

echo 输入目录: %INPUT%
echo 说明: 将注册机原始 results 转为 Sub2API 包，并走云端 SSH SQL 导入。
echo.
python "%ROOT%ops\import_register_results_fallback.py" --results-dir "%INPUT%"
echo.
pause
