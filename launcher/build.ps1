# Builds Pitagoras.exe (the launcher) into the Pitagoras folder.
# Uses the C# compiler that comes with Windows (.NET Framework 4), so nothing extra is needed.
$ErrorActionPreference = "Stop"
$here = $PSScriptRoot
$root = Split-Path $here -Parent
$csc = "$env:WINDIR\Microsoft.NET\Framework64\v4.0.30319\csc.exe"
if (-not (Test-Path $csc)) { $csc = "$env:WINDIR\Microsoft.NET\Framework\v4.0.30319\csc.exe" }

$exe = Get-Item "$root\Pitagoras.exe" -ErrorAction SilentlyContinue
$src = Get-Item "$here\Launcher.cs", "$here\make_icon.py", "$root\appearance.json" -ErrorAction SilentlyContinue
if ($exe -and -not ($src | Where-Object { $_.LastWriteTime -gt $exe.LastWriteTime })) {
    Write-Host "Pitagoras.exe is up to date"   # (and may be running, so it can't be overwritten anyway)
    return
}
& "$root\.venv\Scripts\python.exe" "$here\make_icon.py"
& $csc /nologo /target:winexe /optimize+ /platform:anycpu `
    /win32icon:"$here\pitagoras.ico" `
    /reference:System.Windows.Forms.dll `
    /out:"$root\Pitagoras.exe" "$here\Launcher.cs"
if ($LASTEXITCODE -ne 0) { throw "build failed" }
Write-Host "built $root\Pitagoras.exe"
