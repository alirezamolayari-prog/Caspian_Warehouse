# Automated installer test (needs an elevated PowerShell: the installer requires admin rights).
#
#   packaging\test_installer.ps1 -Installer dist\installer\CaspianWarehouse-Setup-1.0.0.exe
#       [-InstallDir E:\CaspianInstallTest] [-WithDatabase -DbPassword <pw>]
#       [-UpgradeFrom <older installer>] [-Log <file>]
#
# Checks: silent install -> Apps & Features entry (name, publisher, version) -> frozen app smoke
# test -> [database server installed + app connects] -> [upgrade keeps user data, one entry]
# -> silent uninstall removes the program but keeps user data.
param(
    [Parameter(Mandatory)] [string]$Installer,
    [string]$InstallDir = (Join-Path $env:TEMP "CaspianInstallTest"),
    [switch]$WithDatabase,
    [string]$DbPassword = "",
    [string]$UpgradeFrom = "",
    [string]$Log = ""
)
$ErrorActionPreference = "Stop"
if ($Log) { Start-Transcript -Path $Log -Force | Out-Null }
$AppKey = "HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall\{6C1C8C2E-8B7E-4C63-9E3A-2E6A4F1B7D11}_is1"
$UserData = Join-Path $env:LOCALAPPDATA "CaspianWarehouse"
$Marker = Join-Path $UserData "installer-test-marker.txt"
$failures = 0

function Check($ok, $what) {
    if ($ok) { Write-Host "  PASS  $what"; return }
    $script:failures++
    if ($env:GITHUB_ACTIONS) { Write-Host "::error::FAIL $what" } else { Write-Host "  FAIL  $what" }
}

$SetupLog = Join-Path $env:TEMP "caspian-setup-test.log"

# Wait for exactly this process (Start-Process -Wait would also wait for anything it
# leaves running, e.g. a database server), with a timeout.
function RunWait($exe, $arguments, $timeoutSec) {
    $p = Start-Process $exe -ArgumentList $arguments -PassThru
    if (-not $p.WaitForExit($timeoutSec * 1000)) {
        Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue
        return -999
    }
    return $p.ExitCode
}

function Diagnostics {
    $annotate = [bool]$env:GITHUB_ACTIONS
    foreach ($file in $SetupLog, (Join-Path $UserData "Logs\caspian.log")) {
        if (Test-Path $file) {
            Write-Host "--- tail of $file"
            # Annotations are readable without downloading the job log.
            Get-Content $file -Tail 25 | ForEach-Object {
                if ($annotate) { Write-Host "::warning::$(Split-Path $file -Leaf): $_" } else { Write-Host $_ }
            }
        }
    }
}

function Install($exe, $extra = "") {
    $exe = (Resolve-Path $exe).Path
    $setupArgs = "/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /LOG=`"$SetupLog`" /DIR=`"$InstallDir`" $extra"
    $code = RunWait $exe $setupArgs 1200
    Check ($code -eq 0) "install $(Split-Path $exe -Leaf) (exit $code; -999 = timed out)"
    if ($code -ne 0) { Diagnostics }
}

function ExpectedVersion($exe) {
    return ((Split-Path $exe -Leaf) -replace '^CaspianWarehouse-Setup-', '' -replace '\.exe$', '')
}

function RunApp($arguments) {
    $env:QT_QPA_PLATFORM = "offscreen"
    $code = RunWait (Join-Path $InstallDir "CaspianWarehouse.exe") $arguments 300
    $env:QT_QPA_PLATFORM = $null
    return $code
}

New-Item -ItemType Directory -Force $UserData | Out-Null

if ($UpgradeFrom) {
    Write-Host "== install the previous version"
    Install $UpgradeFrom
    "created before the upgrade" | Out-File $Marker -Encoding utf8
}

Write-Host "== install $(Split-Path $Installer -Leaf)"
$extra = ""
if ($WithDatabase) { $extra = "/TASKS=`"dbserver`" /DBPASSWORD=`"$DbPassword`"" }
Install $Installer $extra

$entry = Get-ItemProperty $AppKey -ErrorAction SilentlyContinue
Check ($null -ne $entry) "Apps & Features entry exists"
if ($entry) {
    Check ($entry.DisplayName -eq "Caspian Warehouse") "display name = $($entry.DisplayName)"
    Check ($entry.Publisher -eq "Caspian Furniture Market") "publisher = $($entry.Publisher)"
    Check ($entry.DisplayVersion -eq (ExpectedVersion $Installer)) "version = $($entry.DisplayVersion)"
}
Check (Test-Path (Join-Path $InstallDir "CaspianWarehouse.exe")) "program installed"
Check (Test-Path (Join-Path $InstallDir "licenses\LGPL-3.0.txt")) "license files installed"
Check ((RunApp "--smoke-test") -eq 0) "installed app smoke test"

if ($WithDatabase) {
    $svc = Get-Service MariaDB -ErrorAction SilentlyContinue
    Check ($svc -and $svc.Status -eq "Running") "MariaDB service running (status: $($svc.Status))"
    if (-not $svc) { Diagnostics }
    $dbOk = (RunApp "--check-db") -eq 0
    Check $dbOk "app connects to the provisioned database"
    if (-not $dbOk) { Diagnostics }
}

if ($UpgradeFrom) {
    Check (Test-Path $Marker) "user data kept across the upgrade"
    $entries = @(Get-ChildItem "HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall" |
        Where-Object { (Get-ItemProperty $_.PSPath).DisplayName -eq "Caspian Warehouse" })
    Check ($entries.Count -eq 1) "exactly one Apps & Features entry after the upgrade"
}

Write-Host "== uninstall"
"kept after uninstall?" | Out-File $Marker -Encoding utf8
$code = RunWait (Join-Path $InstallDir "unins000.exe") "/VERYSILENT /SUPPRESSMSGBOXES /NORESTART" 600
# The uninstaller relaunches itself from a temporary copy: wait until the files are gone.
for ($i = 0; $i -lt 120 -and (Test-Path (Join-Path $InstallDir "CaspianWarehouse.exe")); $i++) { Start-Sleep 1 }
Start-Sleep 2
Check ($code -eq 0) "uninstall (exit $code)"
$removed = -not (Test-Path (Join-Path $InstallDir "CaspianWarehouse.exe"))
Check $removed "program files removed"
if (-not $removed) { Get-ChildItem $InstallDir -Recurse -File | Select-Object -First 10 | ForEach-Object { Write-Host "::warning::left behind: $($_.FullName)" } }
Check ($null -eq (Get-ItemProperty $AppKey -ErrorAction SilentlyContinue)) "Apps & Features entry removed"
Check (Test-Path $Marker) "user data kept after uninstall"
Remove-Item $Marker -ErrorAction SilentlyContinue  # our own test file only

Write-Host "== $failures failure(s)"
if ($Log) { Stop-Transcript | Out-Null }
exit $failures
