param([string]$Python = "python")
$ErrorActionPreference = "Stop"
$repoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
Push-Location $repoRoot
try {
    & $Python -c "import sys,struct; assert sys.version_info[:2] == (3,13), 'Package with Python 3.13'; assert struct.calcsize('P') == 8, 'x64 Python required'"
    if ($LASTEXITCODE) { throw "Python 3.13 x64 is required" }
    cargo build --release --locked --manifest-path native/pyshot-recorder/Cargo.toml
    if ($LASTEXITCODE) { throw "Native build failed" }
    & $Python -m PyInstaller --noconfirm pyshot.spec
    if ($LASTEXITCODE) { throw "GUI build failed" }
    & $Python -m PyInstaller --noconfirm pyshot-mcp.spec
    if ($LASTEXITCODE) { throw "MCP build failed" }
    $bundle = Join-Path $repoRoot "dist/PyShot-3.0.0-win-x64"
    New-Item -ItemType Directory -Force -Path $bundle | Out-Null
    Copy-Item -LiteralPath "dist/PyShot.exe", "dist/pyshot-mcp.exe", "native/pyshot-recorder/target/release/pyshot-recorder.exe", "LICENSE", "README.md" -Destination $bundle
    $pythonRuntime = & $Python -c "import sys; print(sys.base_prefix)"
    foreach ($runtimeName in @("vcruntime140.dll", "vcruntime140_1.dll")) {
        $runtimePath = Join-Path $pythonRuntime $runtimeName
        if (Test-Path -LiteralPath $runtimePath) {
            Copy-Item -LiteralPath $runtimePath -Destination $bundle
        } elseif ($runtimeName -eq "vcruntime140.dll") {
            throw "Native helper requires the Python distribution's vcruntime140.dll"
        }
    }
    Copy-Item -LiteralPath (Join-Path $pythonRuntime "LICENSE.txt") -Destination (Join-Path $bundle "PYTHON-LICENSE.txt")
    Copy-Item -LiteralPath "docs" -Destination $bundle -Recurse -Force
    & "$bundle/pyshot-mcp.exe" --help
    if ($LASTEXITCODE) { throw "Packaged MCP startup failed" }
    & "$bundle/pyshot-recorder.exe" capabilities
    if ($LASTEXITCODE) { throw "Packaged recorder startup failed" }
    Get-ChildItem -LiteralPath $bundle -File | Where-Object { $_.Extension -in @('.exe', '.dll') } | Get-FileHash -Algorithm SHA256 |
        ForEach-Object { "$($_.Hash.ToLowerInvariant())  $([System.IO.Path]::GetFileName($_.Path))" } |
        Set-Content -LiteralPath (Join-Path $bundle "SHA256SUMS.txt") -Encoding utf8
    Compress-Archive -Path "$bundle/*" -DestinationPath "dist/PyShot-3.0.0-win-x64.zip" -Force
    Write-Output "Built dist/PyShot-3.0.0-win-x64.zip"
} finally { Pop-Location }
