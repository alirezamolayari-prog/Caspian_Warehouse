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
    if ($ok) { Write-Host "  PASS  $what" } else { Write-Host "  FAIL  $what"; $script:failures++ }
}

function Install($exe, $extra = "") {
    $exe = (Resolve-Path $exe).Path
    $setupArgs = "/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /DIR=`"$InstallDir`" $extra"
    $p = Start-Process $exe -ArgumentList $setupArgs -Wait -PassThru
    Check ($p.ExitCode -eq 0) "install $(Split-Path $exe -Leaf) (exit $($p.ExitCode))"
}

function ExpectedVersion($exe) {
    return ((Split-Path $exe -Leaf) -replace '^CaspianWarehouse-Setup-', '' -replace '\.exe$', '')
}

function RunApp($arguments) {
    $env:QT_QPA_PLATFORM = "offscreen"
    $p = Start-Process (Join-Path $InstallDir "CaspianWarehouse.exe") -ArgumentList $arguments -Wait -PassThru
    $env:QT_QPA_PLATFORM = $null
    return $p.ExitCode
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
    Check ($svc -and $svc.Status -eq "Running") "MariaDB service running"
    Check ((RunApp "--check-db") -eq 0) "app connects to the provisioned database"
}

if ($UpgradeFrom) {
    Check (Test-Path $Marker) "user data kept across the upgrade"
    $entries = @(Get-ChildItem "HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall" |
        Where-Object { (Get-ItemProperty $_.PSPath).DisplayName -eq "Caspian Warehouse" })
    Check ($entries.Count -eq 1) "exactly one Apps & Features entry after the upgrade"
}

Write-Host "== uninstall"
"kept after uninstall?" | Out-File $Marker -Encoding utf8
$p = Start-Process (Join-Path $InstallDir "unins000.exe") -ArgumentList "/VERYSILENT /SUPPRESSMSGBOXES /NORESTART" -Wait -PassThru
Start-Sleep 3  # the uninstaller finishes removing files from a helper process
Check ($p.ExitCode -eq 0) "uninstall (exit $($p.ExitCode))"
Check (-not (Test-Path (Join-Path $InstallDir "CaspianWarehouse.exe"))) "program files removed"
Check ($null -eq (Get-ItemProperty $AppKey -ErrorAction SilentlyContinue)) "Apps & Features entry removed"
Check (Test-Path $Marker) "user data kept after uninstall"
Remove-Item $Marker -ErrorAction SilentlyContinue  # our own test file only

Write-Host "== $failures failure(s)"
if ($Log) { Stop-Transcript | Out-Null }
exit $failures
