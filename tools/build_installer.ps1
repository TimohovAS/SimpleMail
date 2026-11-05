param(
    [switch]$SkipBuild,
    [string]$IsccPath = $env:ISCC_PATH,
    [string]$CertificateThumbprint = $env:SIMPLEMAIL_SIGN_CERT_THUMBPRINT,
    [string]$TimestampUrl = $env:SIMPLEMAIL_TIMESTAMP_URL
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$buildPython = Join-Path $projectRoot '.venv\Scripts\python.exe'
$bundleRoot = Join-Path $projectRoot 'dist\installer-build'
if (-not $IsccPath) {
    $compiler = Get-Command ISCC.exe -ErrorAction SilentlyContinue
    if ($compiler) { $IsccPath = $compiler.Source }
    foreach ($location in @(
        "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
    )) {
        if (-not $IsccPath -and (Test-Path -LiteralPath $location)) { $IsccPath = $location }
    }
}
if (-not $IsccPath -or -not (Test-Path -LiteralPath $IsccPath)) {
    throw 'Install Inno Setup 6 (https://jrsoftware.org/isdl.php) or pass -IsccPath.'
}
if (-not $SkipBuild) {
    & (Join-Path $PSScriptRoot 'build_windows.ps1') -DistPath $bundleRoot -WorkPath (Join-Path $projectRoot 'build\installer-build')
}
Push-Location $projectRoot
$previousCertificate = $env:SIMPLEMAIL_SIGN_CERT_THUMBPRINT
$previousTimestamp = $env:SIMPLEMAIL_TIMESTAMP_URL
try {
    & $buildPython (Join-Path $PSScriptRoot 'check_version.py')
    if ($LASTEXITCODE -ne 0) { throw 'Application and Windows versions differ.' }
    $version = & $buildPython -c 'from mailclient import __version__; print(__version__)'
    if ($LASTEXITCODE -ne 0) { throw 'Unable to read the application version.' }
    $bundleDir = Join-Path $bundleRoot 'SimpleMail'
    $appExecutable = Join-Path $bundleDir 'SimpleMail.exe'
    if (-not (Test-Path -LiteralPath $appExecutable)) { throw 'Build the application first.' }
    $info = (Get-Item -LiteralPath $appExecutable).VersionInfo
    if ($info.ProductVersion -ne $version -or $info.FileVersion -ne $version) {
        throw 'The EXE version differs from the source. Rebuild without -SkipBuild.'
    }
    $compilerArgs = @("/DAppVersion=$version", "/DBundleDir=$bundleDir")
    if ($CertificateThumbprint) {
        $env:SIMPLEMAIL_SIGN_CERT_THUMBPRINT = $CertificateThumbprint
        $env:SIMPLEMAIL_TIMESTAMP_URL = $TimestampUrl
        & (Join-Path $PSScriptRoot 'sign_windows.ps1') -TargetPath $appExecutable
        $signScript = (Join-Path $PSScriptRoot 'sign_windows.ps1').Replace('$', '$$')
        $compilerArgs += '/DEnableSigning'
        $compilerArgs += '/Ssimplemail_sign=powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $q' + $signScript + '$q -TargetPath $f'
    }
    & $IsccPath @compilerArgs (Join-Path $projectRoot 'installer\SimpleMail.iss')
    if ($LASTEXITCODE -ne 0) { throw 'Compiling the installer failed.' }
    $installer = Join-Path $projectRoot "dist\installer\SimpleMail-Setup-$version-x64.exe"
    $digest = (Get-FileHash -LiteralPath $installer -Algorithm SHA256).Hash.ToLowerInvariant()
    $sums = Join-Path (Split-Path -Parent $installer) 'SHA256SUMS.txt'
    "$digest  $(Split-Path -Leaf $installer)" | Set-Content -LiteralPath $sums -Encoding ascii
    Write-Output $installer
} finally {
    $env:SIMPLEMAIL_SIGN_CERT_THUMBPRINT = $previousCertificate
    $env:SIMPLEMAIL_TIMESTAMP_URL = $previousTimestamp
    Pop-Location
}
