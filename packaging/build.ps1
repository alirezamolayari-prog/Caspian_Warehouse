# Build the Windows installer.
#
#   powershell -ExecutionPolicy Bypass -File packaging\build.ps1 -DownloadMariaDb
#
# Options
#   -DownloadMariaDb        download the MariaDB 11.8 LTS installer (checksum-verified, cached in
#                           build\cache) and bundle it as the optional "database server" component
#   -MariaDbMsi <path>      use an already downloaded mariadb-*-winx64.msi instead
#   -SkipServer             app-only installer (no database server component)
#   -InstallerVersion <v>   override the installer's version (upgrade tests only)
#
# Steps: version from pyproject.toml -> icon -> PyInstaller -> MariaDB client tools (from the
# MSI) -> smoke test of the frozen app -> Inno Setup -> SHA256SUMS.txt
param(
    [switch]$DownloadMariaDb,
    [string]$MariaDbMsi = "",
    [switch]$SkipServer,
    [string]$InstallerVersion = ""
)
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$build = Join-Path $root "build"
$dist = Join-Path $root "dist\CaspianWarehouse"
$outDir = Join-Path $root "dist\installer"
New-Item -ItemType Directory -Force (Join-Path $build "cache") | Out-Null

function Step($name) { Write-Host "== $name" }

Step "version"
$version = (uv run python -c "import tomllib; print(tomllib.load(open('pyproject.toml','rb'))['project']['version'])").Trim()
if ($InstallerVersion) { $version = $InstallerVersion }
$numeric = (($version -split '[^0-9]+' | Where-Object { $_ } | Select-Object -First 4) + @(0, 0, 0, 0))[0..3] -join "."
Write-Host "   $version ($numeric)"

Step "icon"
$env:QT_QPA_PLATFORM = "offscreen"
uv run python packaging/make_icon.py | Out-Null
if ($LASTEXITCODE -ne 0) { throw "icon generation failed" }

if (-not $SkipServer -and -not $MariaDbMsi -and $DownloadMariaDb) {
    Step "MariaDB installer (11.8 LTS)"
    $r = Invoke-RestMethod "https://downloads.mariadb.org/rest-api/mariadb/11.8/latest/"
    $files = $r.releases.PSObject.Properties | Select-Object -First 1 | ForEach-Object { $_.Value.files }
    $msi = $files | Where-Object { $_.file_name -like "*winx64.msi" } | Select-Object -First 1
    $MariaDbMsi = Join-Path $build "cache\$($msi.file_name)"
    if (-not (Test-Path $MariaDbMsi)) {
        Invoke-WebRequest ($msi.file_download_url -replace '^http:', 'https:') -OutFile $MariaDbMsi
    }
    $hash = (Get-FileHash $MariaDbMsi -Algorithm SHA256).Hash.ToLower()
    if ($hash -ne $msi.checksum.sha256sum) { Remove-Item $MariaDbMsi; throw "MariaDB checksum mismatch" }
    Write-Host "   $($msi.file_name) (sha256 ok)"
}
if ($SkipServer) { $MariaDbMsi = "" }

Step "PyInstaller"
uv run pyinstaller packaging/caspian.spec --noconfirm --distpath (Join-Path $root "dist") --workpath (Join-Path $build "pyinstaller")
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

if ($MariaDbMsi) {
    Step "MariaDB client tools (for backups on any PC)"
    $extract = Join-Path $build "mariadb-extract"
    if (-not (Get-ChildItem $extract -Recurse -Filter mariadb-dump.exe -ErrorAction SilentlyContinue)) {
        # Administrative extraction: unpacks the files without installing anything.
        $p = Start-Process msiexec.exe -ArgumentList "/a `"$MariaDbMsi`" /qn TARGETDIR=`"$extract`"" -Wait -PassThru
        if ($p.ExitCode -ne 0) { throw "extracting the MariaDB MSI failed ($($p.ExitCode))" }
    }
    $bin = (Get-ChildItem $extract -Recurse -Filter mariadb-dump.exe | Select-Object -First 1).DirectoryName
    $tools = Join-Path $dist "tools\mariadb"
    New-Item -ItemType Directory -Force $tools | Out-Null
    foreach ($f in "mariadb-dump.exe", "mariadb.exe", "zlib1.dll", "msvcp140.dll", "vcruntime140.dll", "vcruntime140_1.dll") {
        $src = Join-Path $bin $f
        if (Test-Path $src) { Copy-Item $src $tools -Force }
    }
} else {
    Write-Host "== (no MariaDB installer: app-only build, backups need MariaDB tools on the PC)"
}

Step "smoke test of the frozen app"
$p = Start-Process (Join-Path $dist "CaspianWarehouse.exe") -ArgumentList "--smoke-test" -Wait -PassThru
if ($p.ExitCode -ne 0) { throw "frozen app smoke test failed ($($p.ExitCode))" }
$env:QT_QPA_PLATFORM = $null

Step "Inno Setup"
$iscc = @(
    (Get-Command iscc -ErrorAction SilentlyContinue).Source,
    "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
    "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
    "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
    "E:\Tools\InnoSetup6\ISCC.exe"
) | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
if (-not $iscc) { throw "Inno Setup 6 (ISCC.exe) not found. Install it from https://jrsoftware.org/isinfo.php" }
$defines = @("/DAppVersion=$version", "/DAppVersionNumeric=$numeric", "/DDistDir=$dist", "/DOutputDir=$outDir")
if ($MariaDbMsi) { $defines += "/DMariaDbMsi=$MariaDbMsi" }
& $iscc /Q @defines packaging\installer.iss
if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed" }

$installer = Join-Path $outDir "CaspianWarehouse-Setup-$version.exe"
$sum = (Get-FileHash $installer -Algorithm SHA256).Hash.ToLower()
"$sum  $(Split-Path $installer -Leaf)" | Out-File -Encoding ascii (Join-Path $outDir "SHA256SUMS.txt")
Write-Host "== done: $installer ($([math]::Round((Get-Item $installer).Length / 1MB)) MB)"
