<#
  Pitagoras setup
    .\setup.ps1                     venv + Python packages + Kokoro voice
    .\setup.ps1 -WithLlamaCpp       also download llama.cpp (llama-server.exe, CUDA build) for the "gguf" brain
    .\setup.ps1 -WithPiperPolish    also download a Polish Piper voice
    .\setup.ps1 -WithBrain          also download a brain: Google's Gemma 4 E4B (4.9 GB + 0.9 GB vision)
  The installer runs it with -FromInstaller (keeps the window open at the end).
#>
param([switch]$WithLlamaCpp, [switch]$WithPiperPolish, [switch]$WithBrain, [switch]$FromInstaller)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$ProgressPreference = "SilentlyContinue"
$Host.UI.RawUI.WindowTitle = "Pitagoras setup"

trap {
    Write-Host "`nSetup failed: $_" -ForegroundColor Red
    Write-Host "You can run it again later: right-click setup.ps1 > Run with PowerShell (in $PSScriptRoot)."
    if ($FromInstaller) { Read-Host "Press Enter to close" }
    exit 1
}

function Find-Python312 {
    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) {
        $exe = (& py -3.12 -c "import sys; print(sys.executable)" 2>$null)
        if ($LASTEXITCODE -eq 0 -and $exe) { return $exe.Trim() }
    }
    foreach ($p in "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe", "$env:ProgramFiles\Python312\python.exe") {
        if (Test-Path $p) { return $p }
    }
    return $null
}

function Get-File($url, $dest) {
    if (Test-Path $dest) { Write-Host "  have $dest"; return }
    New-Item -ItemType Directory -Force (Split-Path $dest) | Out-Null
    Write-Host "  downloading $url"
    Invoke-WebRequest $url -OutFile "$dest.part"
    Move-Item "$dest.part" $dest
}

if (-not (Test-Path .venv\Scripts\python.exe)) {
    $python = Find-Python312
    if (-not $python) {
        Write-Host "Python 3.12 isn't installed: installing it (just for you, no admin needed)..."
        $inst = Join-Path $env:TEMP "python-3.12.10-amd64.exe"
        Invoke-WebRequest "https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe" -OutFile $inst
        Start-Process $inst -ArgumentList "/quiet", "InstallAllUsers=0", "PrependPath=0", "Include_launcher=1", "Include_test=0" -Wait
        Remove-Item $inst -ErrorAction SilentlyContinue
        $python = Find-Python312
        if (-not $python) { throw "Python 3.12 could not be installed. Install it from python.org and run setup again." }
    }
    Write-Host "Creating venv (Python 3.12: $python)..."
    & $python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw "could not create the venv" }
}
Write-Host "Installing Python packages (PyTorch with CUDA is ~3 GB, this takes a while)..."
& .\.venv\Scripts\python.exe -m pip install --upgrade pip
# PyTorch first, from the CUDA 12.8 index (needed for RTX 50xx; works on older NVIDIA cards too)
& .\.venv\Scripts\python.exe -m pip install torch --index-url https://download.pytorch.org/whl/cu128
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }

Write-Host "Kokoro voice model..."
$k = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0"
Get-File "$k/kokoro-v1.0.onnx" "models\kokoro\kokoro-v1.0.onnx"
Get-File "$k/voices-v1.0.bin"  "models\kokoro\voices-v1.0.bin"

if ($WithPiperPolish) {
    Write-Host "Piper Polish voice..."
    $p = "https://huggingface.co/rhasspy/piper-voices/resolve/main/pl/pl_PL/darkman/medium"
    Get-File "$p/pl_PL-darkman-medium.onnx"      "models\piper\pl_PL-darkman-medium.onnx"
    Get-File "$p/pl_PL-darkman-medium.onnx.json" "models\piper\pl_PL-darkman-medium.onnx.json"
}

if ($WithLlamaCpp) {
    Write-Host "llama.cpp (latest CUDA build)..."
    # Driver's CUDA version (e.g. 13.4); a build of the same major version runs on it.
    $smi = (nvidia-smi 2>$null | Out-String)
    $drv = [regex]::Match($smi, 'CUDA (?:UMD )?Version:\s*([\d.]+)').Groups[1].Value
    $drvMajor = if ($drv) { [int]($drv.Split('.')[0]) } else { 12 }
    Write-Host "  driver CUDA $drv"
    # "latest" is sometimes an empty tag, so take the newest release that has Windows CUDA zips
    $rels = Invoke-RestMethod "https://api.github.com/repos/ggml-org/llama.cpp/releases?per_page=10"
    $best = $null
    foreach ($rel in $rels) {
        $assets = $rel.assets | Where-Object { $_.name -match '^llama-.*-bin-win-cuda-([\d.]+)-x64\.zip$' } |
            Where-Object { [int]([regex]::Match($_.name, 'cuda-(\d+)').Groups[1].Value) -le $drvMajor }
        $best = $assets | Sort-Object { [version]([regex]::Match($_.name, 'cuda-([\d.]+)').Groups[1].Value) } | Select-Object -Last 1
        if ($best) { break }
    }
    if (-not $best) { throw "No Windows CUDA build of llama.cpp found" }
    $cuda = [regex]::Match($best.name, 'cuda-([\d.]+)').Groups[1].Value
    $rt = $rel.assets | Where-Object { $_.name -eq "cudart-llama-bin-win-cuda-$cuda-x64.zip" } | Select-Object -First 1
    $dir = "bin\llama.cpp"
    Get-File $best.browser_download_url "bin\$($best.name)"
    Expand-Archive "bin\$($best.name)" $dir -Force
    if ($rt) { Get-File $rt.browser_download_url "bin\$($rt.name)"; Expand-Archive "bin\$($rt.name)" $dir -Force }
    Write-Host "  llama.cpp $($rel.tag_name) (CUDA $cuda) -> $dir"
}

if ($WithBrain) {
    Write-Host "Brain: Gemma 4 E4B by Google (4.9 GB + 0.9 GB to see the screen)..."
    $g = "https://huggingface.co/google/gemma-4-E4B-it-qat-q4_0-gguf/resolve/main"
    Write-Host "  (big download: Windows' downloader shows the progress)"
    foreach ($f in "gemma-4-E4B_q4_0-it.gguf", "gemma-4-E4B-it-mmproj.gguf") {
        $dest = "brains\Gemma-4-E4B\$f"
        if (Test-Path $dest) { Write-Host "  have $dest"; continue }
        New-Item -ItemType Directory -Force (Split-Path $dest) | Out-Null
        Start-BitsTransfer -Source "$g/$f" -Destination "$dest.part" -DisplayName "Pitagoras brain: $f"
        Move-Item "$dest.part" $dest
    }
}

Write-Host "`nDone! Start Pitagoras with Pitagoras.exe (or:  .\.venv\Scripts\python.exe run.py)" -ForegroundColor Green
if ($FromInstaller) { Start-Sleep 4 }
