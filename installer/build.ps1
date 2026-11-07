# Builds dist\Pitagoras-Setup-<version>.exe:  .\installer\build.ps1 -Version 1.0.0
param([string]$Version = "1.0.0", [string]$Publisher = "", [string]$RepoUrl = "")
$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent

& "$root\launcher\build.ps1"   # Pitagoras.exe + icon

$iscc = @("$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe", "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
          "$env:ProgramFiles\Inno Setup 6\ISCC.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) { throw "Inno Setup 6 isn't installed (winget install JRSoftware.InnoSetup)" }

$defs = @("/DAppVersion=$Version")
if ($Publisher) { $defs += "/DPublisher=$Publisher" }
if ($RepoUrl) { $defs += "/DRepoUrl=$RepoUrl" }
& $iscc /Q @defs "$PSScriptRoot\Pitagoras.iss"
if ($LASTEXITCODE -ne 0) { throw "installer build failed" }
Get-Item "$root\dist\Pitagoras-Setup-$Version.exe" | ForEach-Object { "built $($_.FullName) ($([math]::Round($_.Length/1MB,1)) MB)" }
