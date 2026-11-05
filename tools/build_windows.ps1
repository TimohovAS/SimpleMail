param(
    [string]$DistPath = '',
    [string]$WorkPath = ''
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
if (-not $DistPath) { $DistPath = Join-Path $projectRoot 'dist' }
if (-not $WorkPath) { $WorkPath = Join-Path $projectRoot 'build' }
$buildPython = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $buildPython)) {
    throw 'Create .venv and install requirements.txt before building.'
}
& $buildPython (Join-Path $PSScriptRoot 'check_version.py')
if ($LASTEXITCODE -ne 0) { throw 'Application and Windows versions differ.' }
& $buildPython -m pip install -r (Join-Path $projectRoot 'requirements-build.txt')
if ($LASTEXITCODE -ne 0) { throw 'Installing build dependencies failed.' }
$previousPath = $env:PATH
try {
    # Не подхватываем одноимённые DLL из Git, Poppler и других инструментов.
    $env:PATH = "$(Split-Path -Parent $buildPython);$env:SystemRoot\System32;$env:SystemRoot"
    & $buildPython -m PyInstaller --clean --noconfirm --distpath $DistPath --workpath $WorkPath (Join-Path $projectRoot 'SimpleMail.spec')
    $buildResult = $LASTEXITCODE
} finally {
    $env:PATH = $previousPath
}
if ($buildResult -ne 0) { throw 'Building SimpleMail failed.' }
$appExecutable = Join-Path $DistPath 'SimpleMail\SimpleMail.exe'
if ([IO.Path]::GetFullPath($DistPath) -ne [IO.Path]::GetFullPath((Join-Path $projectRoot 'dist'))) {
    Write-Output $appExecutable
    return
}
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut((Join-Path $projectRoot 'SimpleMail.lnk'))
$shortcut.TargetPath = $appExecutable
$shortcut.WorkingDirectory = Split-Path -Parent $appExecutable
$shortcut.IconLocation = "$appExecutable,0"
$shortcut.Description = 'SimpleMail'
$shortcut.Save()
Write-Output $appExecutable
