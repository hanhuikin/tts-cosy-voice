# CosyVoice FastAPI 服务启动脚本 (Windows PowerShell)
# 用法: powershell -ExecutionPolicy Bypass -File scripts\start.ps1 [-Port 8000] [-Model <模型key>]
# -Model 支持: cosyvoice3 / cosyvoice2 / indextts2 / indextts25 / qwen3tts / qwen3tts_base / qwen3tts_design
# 权重目录 / 解释器可写在仓库根目录 .env (复制 .env.example)，服务启动时自动读取；
# 也可在本会话用 $env:XXX 临时覆盖，改完均需重启 (见 STARTUP.md)
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
if (Test-Path "$Root\.env") {
    Write-Host "[start] 配置: $Root\.env (进程环境变量优先, 改完需重启)" -ForegroundColor DarkGray
}
Write-Host "[start] http://127.0.0.1:$Port" -ForegroundColor Cyan
Set-Location "$Root\server"
& "$Root\.venv\Scripts\python.exe" -m uvicorn app.main:app --host 0.0.0.0 --port $Port
