$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$buildPython = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $buildPython)) {
    throw 'Create .venv and install requirements.txt before building.'
}
& $buildPython -m pip install -r (Join-Path $projectRoot 'requirements-build.txt')
if ($LASTEXITCODE -ne 0) { throw 'Installing build dependencies failed.' }
$previousPath = $env:PATH
try {
    # Не подхватываем одноимённые DLL из Git, Poppler и других инструментов.
    $env:PATH = "$(Split-Path -Parent $buildPython);$env:SystemRoot\System32;$env:SystemRoot"
    & $buildPython -m PyInstaller --clean --noconfirm --distpath (Join-Path $projectRoot 'dist') --workpath (Join-Path $projectRoot 'build') (Join-Path $projectRoot 'SimpleMail.spec')
    $buildResult = $LASTEXITCODE
} finally {
    $env:PATH = $previousPath
}
if ($buildResult -ne 0) { throw 'Building SimpleMail failed.' }
$appExecutable = Join-Path $projectRoot 'dist\SimpleMail\SimpleMail.exe'
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut((Join-Path $projectRoot 'SimpleMail.lnk'))
$shortcut.TargetPath = $appExecutable
$shortcut.WorkingDirectory = Split-Path -Parent $appExecutable
$shortcut.IconLocation = "$appExecutable,0"
$shortcut.Description = 'SimpleMail'
$shortcut.Save()
Write-Output $appExecutable
