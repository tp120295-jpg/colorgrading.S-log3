# Chạy slog3grade bằng Python trong .venv của skill (nếu có), giữ nguyên tham số. Dùng: run.ps1 <thư mục> [tuỳ chọn]
$skill = Split-Path -Parent $PSScriptRoot
$py = Join-Path $skill ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { $py = "python" }
$env:PYTHONIOENCODING = "utf-8"
Push-Location $skill
try { & $py -m slog3grade @args; exit $LASTEXITCODE } finally { Pop-Location }
