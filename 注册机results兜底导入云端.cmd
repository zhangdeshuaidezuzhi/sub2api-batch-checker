@echo off
setlocal
chcp 65001 >nul

set "ROOT=%~dp0"
set "MODE_OR_INPUT=%~1"
set "ARGS="
if /I "%MODE_OR_INPUT%"=="full" set "ARGS=--full"
if /I "%MODE_OR_INPUT%"=="latest" set "ARGS=--latest"
if not "%MODE_OR_INPUT%"=="" if "%ARGS%"=="" set "ARGS=--results-dir \"%MODE_OR_INPUT%\""

call "%ROOT%local_config.cmd" 2>nul

echo 选择规则: 第一次默认全量 results；成功后默认只筛最新批次。可传 full/latest/具体目录覆盖。
echo 说明: 将注册机原始 results 转为 Sub2API 包，并走云端 SSH SQL 导入。
echo.
python "%ROOT%ops\import_register_results_fallback.py" %ARGS%
echo.
pause
