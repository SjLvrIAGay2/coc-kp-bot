param([ValidateSet('terminal','serve','demo')][string]$Mode='terminal', [switch]$Real)
$ErrorActionPreference='Stop'
$projectDirectory=Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectDirectory
$env:PYTHONUTF8='1'
$pythonPath=Join-Path $projectDirectory '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) {
    python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw '创建虚拟环境失败' }
    & $pythonPath -m pip install --index-url https://pypi.org/simple -r requirements.lock
    if ($LASTEXITCODE -ne 0) { throw '安装锁定依赖失败' }
    & $pythonPath -m pip install --no-deps --no-build-isolation -e .
    if ($LASTEXITCODE -ne 0) { throw '安装项目失败' }
}
if ($Mode -eq 'demo') {
    $demoDatabase=Join-Path 'data' ('demo-' + [guid]::NewGuid().ToString('N') + '.sqlite3')
    & $pythonPath -m cockp --database $demoDatabase demo
}
elseif ($Real -and $Mode -eq 'terminal') { & $pythonPath -m cockp terminal --real }
else { & $pythonPath -m cockp $Mode }
