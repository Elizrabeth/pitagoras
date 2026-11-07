# Uploads Pitagoras to GitHub and publishes a release with the installer.
#   first time:   .\installer\publish.ps1 -Version 1.0.0 -Create
#   new version:  .\installer\build.ps1 -Version 1.1.0 ... ; then .\installer\publish.ps1 -Version 1.1.0
param([string]$Version = "1.0.0", [string]$Repo = "pitagoras", [switch]$Create)
$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
Set-Location $root
$git = "C:\Program Files\Git\cmd\git.exe"
$gh = (Get-Command gh -ErrorAction SilentlyContinue).Source
if (-not $gh) { $gh = Get-ChildItem "$env:LOCALAPPDATA\Microsoft\WinGet\Packages\GitHub.cli*\bin\gh.exe" | Select-Object -First 1 -ExpandProperty FullName }

$login = & $gh api user --jq .login
$id = & $gh api user --jq .id
if (-not $login) { throw "not signed in to GitHub (gh auth login)" }
$installer = "dist\Pitagoras-Setup-$Version.exe"
if (-not (Test-Path $installer)) { throw "$installer is missing: run .\installer\build.ps1 -Version $Version first" }

# commits use GitHub's private no-reply address, so your real email isn't public
& $git config user.name $login
& $git config user.email "$id+$login@users.noreply.github.com"
& $gh auth setup-git

& $git add -A
& $git diff --cached --quiet
if ($LASTEXITCODE -ne 0) { & $git commit -q -m "Pitagoras $Version" }

if ($Create) {
    & $gh repo view "$login/$Repo" --json name 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) { throw "github.com/$login/$Repo already exists: not touching it" }
    & $gh repo create $Repo --public --source . --remote origin --push `
        --description "A desktop AI companion: a little yellow ball that listens, talks, sees your screen, plays music and dances."
    & $gh repo edit "$login/$Repo" --add-topic "ai-assistant,desktop-assistant,voice-assistant,llm,llama-cpp,whisper,windows"
} else {
    & $git push origin HEAD
}

$notes = @"
**Download ``Pitagoras-Setup-$Version.exe`` below and run it.**

- Installs just for you (no admin); pick any folder.
- Tick a brain to download (Google's Gemma 4 E4B, 5.9 GB) or use LM Studio / Ollama / Claude later.
- The setup window then installs Python 3.12 if needed and the packages (~8 GB with CUDA PyTorch): give it a while.

Needs Windows 10/11 x64, ~15 GB free, and an NVIDIA GPU for local brains and fast ears.
Windows SmartScreen may warn because the installer isn't code-signed: **More info → Run anyway**.
"@
& $gh release create "v$Version" $installer --repo "$login/$Repo" --title "Pitagoras $Version" --notes $notes
Write-Host "`nDone: https://github.com/$login/$Repo" -ForegroundColor Green
