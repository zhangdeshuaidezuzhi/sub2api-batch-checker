$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$toolDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$localConfig = Join-Path $toolDir "local_config.ps1"
if (Test-Path -LiteralPath $localConfig) {
    . $localConfig
}
$defaultInput = if ($env:SUB2API_CHECKER_DEFAULT_INPUT) { $env:SUB2API_CHECKER_DEFAULT_INPUT } else { $toolDir }
$outputDir = Join-Path $toolDir "outputs"

Write-Host ""
Write-Host "Sub2API batch checker" -ForegroundColor Cyan
Write-Host "Local only. Tokens will not be printed." -ForegroundColor DarkGray
Write-Host ""

$inputPath = Read-Host "Input JSON folder or file. Press Enter for [$defaultInput]"
if ([string]::IsNullOrWhiteSpace($inputPath)) {
    $inputPath = $defaultInput
}

if (-not (Test-Path -LiteralPath $inputPath)) {
    Write-Host "Input path not found: $inputPath" -ForegroundColor Red
    Read-Host "Press Enter to exit"
    exit 2
}

Write-Host ""
Write-Host "Choose check mode:"
Write-Host "1. Light auth check (/v1/models, recommended first)"
Write-Host "2. Real inference check (/v1/responses, may consume a tiny quota)"
$mode = Read-Host "Mode [1]"
if ([string]::IsNullOrWhiteSpace($mode)) {
    $mode = "1"
}

$endpoint = "https://api.openai.com/v1/models"
$suffix = "models"
if ($mode -eq "2") {
    $endpoint = "https://api.openai.com/v1/responses"
    $suffix = "responses"
}

$concurrencyText = Read-Host "Concurrency [8]"
$concurrency = 8
if (-not [string]::IsNullOrWhiteSpace($concurrencyText)) {
    $concurrency = [int]$concurrencyText
}

$timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
$csv = Join-Path $outputDir "desktop_${suffix}_${timestamp}.csv"
$good = Join-Path $outputDir "desktop_good_${suffix}_${timestamp}.json"
$bad = Join-Path $outputDir "desktop_bad_${suffix}_${timestamp}.json"

Write-Host ""
Write-Host "Running..."
Write-Host "Input: $inputPath"
Write-Host "Mode: $suffix"
Write-Host "CSV: $csv"
Write-Host ""

Set-Location $toolDir
python -m sub2api_batch_checker.cli "$inputPath" --endpoint $endpoint --concurrency $concurrency --timeout 40 --quiet --csv "$csv" --good-bundle "$good" --bad-bundle "$bad"

Write-Host ""
Write-Host "Done." -ForegroundColor Green
Write-Host "CSV: $csv"
Write-Host "Good bundle: $good"
Write-Host "Bad bundle: $bad"
Write-Host ""
Read-Host "Press Enter to exit"
