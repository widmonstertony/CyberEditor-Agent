[CmdletBinding()]
param(
    [ValidateSet("auto", "cpu", "cu126", "cu130")]
    [string]$ComputePlatform = "auto",
    [string]$PythonCommand = "py"
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$venvRoot = Join-Path $projectRoot ".venv"
$venvPython = Join-Path $venvRoot "Scripts\python.exe"

function Write-Step {
    param([string]$Chinese, [string]$English)
    Write-Host "`n==> $Chinese / $English" -ForegroundColor Cyan
}

if ($env:OS -ne "Windows_NT") {
    throw "此安装器仅支持 Windows。 / This installer supports Windows only."
}

if (-not (Test-Path -LiteralPath $venvPython)) {
    Write-Step "创建 Python 虚拟环境" "Creating Python virtual environment"
    if ($PythonCommand -eq "py") {
        & py -3.11 -m venv $venvRoot
    }
    else {
        & $PythonCommand -m venv $venvRoot
    }
    if ($LASTEXITCODE -ne 0) {
        throw "无法创建 .venv；请安装 Python 3.11。 / Could not create .venv; install Python 3.11."
    }
}

Write-Step "升级 pip" "Upgrading pip"
& $venvPython -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) {
    throw "pip 升级失败。 / pip upgrade failed."
}

Write-Step "安装项目依赖" "Installing project dependencies"
& $venvPython -m pip install -r (Join-Path $projectRoot "requirements.txt")
if ($LASTEXITCODE -ne 0) {
    throw "依赖安装失败。 / Dependency installation failed."
}

$selectedPlatform = $ComputePlatform
$nvidiaSmi = Get-Command nvidia-smi -ErrorAction SilentlyContinue
if ($selectedPlatform -eq "auto") {
    if ($null -eq $nvidiaSmi) {
        $selectedPlatform = "cpu"
    }
    else {
        $driverText = & $nvidiaSmi.Source --query-gpu=driver_version --format=csv,noheader |
            Select-Object -First 1
        $driverVersion = [version]($driverText.Trim())
        # CUDA 13.x needs an R580+ Windows driver. CUDA 12.x retains
        # minor-version compatibility on R528.33+ Windows drivers.
        if ($driverVersion -ge [version]"580.65") {
            $selectedPlatform = "cu130"
        }
        elseif ($driverVersion -ge [version]"528.33") {
            $selectedPlatform = "cu126"
        }
        else {
            Write-Warning (
                "NVIDIA 驱动 $driverVersion 太旧，将使用 CPU。请升级驱动后重新运行本脚本。 " +
                "/ NVIDIA driver $driverVersion is too old; using CPU. Upgrade it and rerun."
            )
            $selectedPlatform = "cpu"
        }
    }
}

if ($selectedPlatform -in @("cu126", "cu130")) {
    Write-Step "安装 NVIDIA CUDA 版 PyTorch ($selectedPlatform)" "Installing CUDA PyTorch ($selectedPlatform)"
    $torchIndex = "https://download.pytorch.org/whl/$selectedPlatform"
    # Dependencies were installed above. Replacing only torch avoids letting
    # the generic PyPI CPU wheel silently win on Windows.
    & $venvPython -m pip install --upgrade --force-reinstall --no-deps `
        --index-url $torchIndex torch
    if ($LASTEXITCODE -ne 0) {
        throw "CUDA PyTorch 安装失败。 / CUDA PyTorch installation failed."
    }
}

Write-Step "验证 PyTorch 运行时" "Verifying the PyTorch runtime"
$verification = @'
import json
import torch
ready = bool(torch.cuda.is_available())
result = {
    "torch": str(torch.__version__),
    "built_cuda": torch.version.cuda,
    "cuda_available": ready,
    "device": torch.cuda.get_device_name(0) if ready else "CPU",
}
print(json.dumps(result, ensure_ascii=False))
if ready:
    value = torch.randn((1024, 1024), device="cuda")
    result_value = value @ value
    torch.cuda.synchronize()
    del value, result_value
    torch.cuda.empty_cache()
'@
& $venvPython -c $verification
if ($LASTEXITCODE -ne 0) {
    throw "PyTorch 验证失败。 / PyTorch verification failed."
}

if ($selectedPlatform -ne "cpu") {
    $cudaReady = & $venvPython -c "import torch; print(int(torch.cuda.is_available()))"
    if ($cudaReady.Trim() -ne "1") {
        throw (
            "已安装 CUDA 构建，但 torch.cuda.is_available() 仍为 False。请升级 NVIDIA 驱动。 " +
            "/ CUDA wheel installed, but torch.cuda.is_available() is still False. Update the NVIDIA driver."
        )
    }
}

Write-Host "`n安装完成。双击 launch_ui.bat 启动。 / Installation complete. Run launch_ui.bat." `
    -ForegroundColor Green
