# Build the Windows distribution:  powershell -File packaging\build.ps1 [-MariaDbBin <path to MariaDB bin>]
#   1. app icon  2. PyInstaller bundle  3. bundle MariaDB client tools (for backups)
#   4. smoke test of the frozen app  5. Inno Setup installer (if ISCC is available)
param(
    [string]$MariaDbBin = "",
    [string]$Version = "0.1.0"
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$dist = Join-Path $root "dist\CaspianWarehouse"

Write-Host "== icon"
$env:QT_QPA_PLATFORM = "offscreen"
uv run python packaging/make_icon.py
if ($LASTEXITCODE -ne 0) { throw "icon generation failed" }

Write-Host "== PyInstaller"
uv run pyinstaller packaging/caspian.spec --noconfirm --distpath (Join-Path $root "dist") --workpath (Join-Path $root "build")
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

if ($MariaDbBin -and (Test-Path $MariaDbBin)) {
    Write-Host "== MariaDB client tools from $MariaDbBin"
    $tools = Join-Path $dist "tools\mariadb"
    New-Item -ItemType Directory -Force $tools | Out-Null
    foreach ($f in "mariadb-dump.exe", "mariadb.exe", "zlib1.dll", "msvcp140.dll", "vcruntime140.dll", "vcruntime140_1.dll") {
        $src = Join-Path $MariaDbBin $f
        if (Test-Path $src) { Copy-Item $src $tools -Force }
    }
} else {
    Write-Host "== MariaDB client tools not bundled (pass -MariaDbBin to include them)"
}

Write-Host "== smoke test"
$p = Start-Process (Join-Path $dist "CaspianWarehouse.exe") -ArgumentList "--smoke-test" -Wait -PassThru
if ($p.ExitCode -ne 0) { throw "frozen app smoke test failed ($($p.ExitCode))" }
Remove-Item Env:QT_QPA_PLATFORM

$iscc = @(
    (Get-Command iscc -ErrorAction SilentlyContinue).Source,
    "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
    "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
) | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
if ($iscc) {
    Write-Host "== installer ($iscc)"
    $env:CASPIAN_VERSION = $Version
    $env:CASPIAN_DIST = $dist
    & $iscc packaging\installer.iss
    if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed" }
} else {
    Write-Host "== Inno Setup not found: skipping installer (the folder in dist\ is runnable as is)"
}
Write-Host "done"
