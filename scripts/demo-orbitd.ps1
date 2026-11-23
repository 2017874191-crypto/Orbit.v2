[CmdletBinding()]
param(
    [string]$GoExecutable = 'go'
)

$ErrorActionPreference = 'Stop'
$goCommand = Get-Command -Name $GoExecutable -CommandType Application -ErrorAction SilentlyContinue |
    Select-Object -First 1
if (-not $goCommand) {
    throw 'Go 1.24+ is required. Add go to PATH or pass the path to go.exe with -GoExecutable.'
}
$uvCommand = Get-Command -Name uv -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $uvCommand) {
    throw 'uv is required to prepare the Python 3.12 environment.'
}

$root = Split-Path -Parent $PSScriptRoot
$binaryName = 'orbitd-demo-' + [Guid]::NewGuid().ToString('N') + '.exe'
$binary = Join-Path ([IO.Path]::GetTempPath()) $binaryName
Push-Location -LiteralPath $root
try {
    Write-Host 'Building orbitd for the local demo...'
    & $goCommand.Source build -o $binary ./cmd/orbitd
    if ($LASTEXITCODE -ne 0) {
        throw "go build failed with exit code $LASTEXITCODE."
    }

    Write-Host 'Running the two-workspace demo with local model responses...'
    & $uvCommand.Source run --frozen --no-dev --python 3.12 python -u tests/integration/verify_orbitd.py --orbitd $binary
    if ($LASTEXITCODE -ne 0) {
        throw "The demo failed with exit code $LASTEXITCODE."
    }
} finally {
    Pop-Location
    if (Test-Path -LiteralPath $binary -PathType Leaf) {
        Remove-Item -LiteralPath $binary
    }
}
