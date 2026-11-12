# CosyVoice FastAPI 服务启动脚本 (Windows PowerShell)
# 用法: powershell -ExecutionPolicy Bypass -File scripts\start.ps1 [-Port 8000] [-Model <模型key>]
# -Model 支持: cosyvoice3 / cosyvoice2 / indextts2 / indextts25 / qwen3tts / qwen3tts_base / qwen3tts_design
# 外部模型需先配置权重目录与解释器环境变量 (见 STARTUP.md)
param(
    [int]$Port = 8000,
    [string]$Model = ""
)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot

if (-not (Test-Path "$Root\.venv\Scripts\python.exe")) {
    Write-Host "[start] venv 不存在, 先运行安装步骤 (见 README)" -ForegroundColor Yellow
    exit 1
}

if ($Model) { $env:COSYVOICE_DEFAULT_MODEL = $Model }
Write-Host "[start] http://127.0.0.1:$Port" -ForegroundColor Cyan
Set-Location "$Root\server"
& "$Root\.venv\Scripts\python.exe" -m uvicorn app.main:app --host 0.0.0.0 --port $Port
