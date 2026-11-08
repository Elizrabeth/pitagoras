# Builds dist\Pitagoras-Setup-<version>.exe:  .\installer\build.ps1 -Version 1.2.0
#   -Personal                      also bundle YOUR current personality.txt (a snapshot is copied to
#                                  installer\personal\, never committed). The file is named ...-personal.exe,
#                                  and publish.ps1 never uploads it.
#   -Personality other.txt         the same, with another file
param([string]$Version = "1.0.0", [string]$Publisher = "", [string]$RepoUrl = "", [string]$Personality = "",
      [switch]$Personal)
$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent

& "$root\launcher\build.ps1"   # Pitagoras.exe + icon

$iscc = @("$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe", "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
          "$env:ProgramFiles\Inno Setup 6\ISCC.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) { throw "Inno Setup 6 isn't installed (winget install JRSoftware.InnoSetup)" }

$defs = @("/DAppVersion=$Version")
if ($Publisher) { $defs += "/DPublisher=$Publisher" }
if ($RepoUrl) { $defs += "/DRepoUrl=$RepoUrl" }
if ($Personal -and -not $Personality) { $Personality = "$root\personality.txt" }
$suffix = ""
if ($Personality) {
    $src = (Resolve-Path $Personality).Path
    $snap = "$PSScriptRoot\personal\personality.txt"
    New-Item -ItemType Directory -Force (Split-Path $snap) | Out-Null
    Copy-Item $src $snap -Force
    Write-Host "bundling the personality from $src"
    $suffix = "-personal"
    $defs += "/DPersonality=$snap", "/DSuffix=$suffix"
}
& $iscc /Q @defs "$PSScriptRoot\Pitagoras.iss"
if ($LASTEXITCODE -ne 0) { throw "installer build failed" }
Get-Item "$root\dist\Pitagoras-Setup-$Version$suffix.exe" | ForEach-Object { "built $($_.FullName) ($([math]::Round($_.Length/1MB,1)) MB)" }
