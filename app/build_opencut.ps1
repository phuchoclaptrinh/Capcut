# Build OpenCut (D:\ytb\opencut) thành bản standalone để app YT Downloader chạy.
# Chạy lại script này mỗi khi sửa code trong D:\ytb\opencut.
# Không dùng ErrorActionPreference=Stop: PowerShell 5.1 coi mọi dòng stderr của bun/next là lỗi.
# Lỗi build được kiểm tra qua mã thoát.
$root = Split-Path $PSScriptRoot -Parent
$web = Join-Path $root 'opencut\apps\web'
$env:Path = "$root\_tools\bun;$root\_tools\node;" + $env:Path
$env:NEXT_TELEMETRY_DISABLED = '1'

Push-Location $web
try {
    bun run build
    if ($LASTEXITCODE -ne 0) { throw "next build lỗi (mã $LASTEXITCODE)" }
} finally { Pop-Location }

# Bản standalone không tự kèm file tĩnh và cấu hình
$out = Join-Path $web '.next\standalone\apps\web'
foreach ($pair in @(@('.next\static', '.next\static'), @('public', 'public'))) {
    $src = Join-Path $web $pair[0]; $dst = Join-Path $out $pair[1]
    if (-not (Test-Path $src)) { continue }
    if (Test-Path $dst) { Remove-Item $dst -Recurse -Force -ErrorAction Stop }  # tránh Copy-Item chép lồng thành static\static
    Copy-Item $src $dst -Recurse -ErrorAction Stop
}
Copy-Item (Join-Path $web '.env.local') (Join-Path $out '.env.local') -Force -ErrorAction Stop
Write-Host "Xong: $out\server.js"
