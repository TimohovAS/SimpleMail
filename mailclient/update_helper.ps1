param(
    [Parameter(Mandatory = $true)][string]$RequestPath,
    [switch]$QuietErrors
)
$ErrorActionPreference = 'Stop'
$installerExitCode = 1
$errorLog = [IO.Path]::ChangeExtension($RequestPath, '.error.log')
try {
    $request = Get-Content -LiteralPath $RequestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    # Keep an actual process handle while waiting; PID reuse cannot redirect the wait.
    $parentProcess = $null
    try { $parentProcess = [Diagnostics.Process]::GetProcessById([int]$request.parent_pid) }
    catch [ArgumentException] { } # Parent has already exited.
    if ($null -ne $parentProcess) {
        try {
            if (-not $parentProcess.WaitForExit(120000)) {
                throw 'SimpleMail did not exit. The update was not started.'
            }
        } finally { $parentProcess.Dispose() }
    }
    # Recheck after waiting: a cached installer may have changed during shutdown.
    $installer = Get-Item -LiteralPath $request.installer
    $stream = [IO.File]::OpenRead($installer.FullName)
    $sha256 = [Security.Cryptography.SHA256]::Create()
    try {
        $digest = [BitConverter]::ToString($sha256.ComputeHash($stream)).Replace('-', '').ToLowerInvariant()
    } finally {
        $stream.Dispose()
        $sha256.Dispose()
    }
    if ($installer.Length -ne [long]$request.size -or $digest -ne $request.sha256) {
        throw 'Installer verification failed. Download the update again.'
    }
    # Windows command-line quoting, including paths ending in a backslash.
    $arguments = foreach ($argument in $request.arguments) {
        $value = [string]$argument
        '"' + [regex]::Replace([regex]::Replace($value, '(\\*)"', '$1$1\"'), '(\\+)$', '$1$1') + '"'
    }
    $setup = Start-Process -FilePath $installer.FullName -ArgumentList ($arguments -join ' ') -WorkingDirectory $installer.DirectoryName -WindowStyle Normal -Wait -PassThru
    $installerExitCode = $setup.ExitCode
    if ($installerExitCode -notin @(0, 2, 5)) {
        throw "The installer failed (exit code $installerExitCode)."
    }
} catch {
    $_.Exception.Message | Set-Content -LiteralPath $errorLog -Encoding UTF8
    if (-not $QuietErrors) {
        Add-Type -AssemblyName System.Windows.Forms
        [void][Windows.Forms.MessageBox]::Show($_.Exception.Message, 'SimpleMail', 'OK', 'Error')
    }
} finally {
    Remove-Item -LiteralPath $RequestPath -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $PSCommandPath -Force -ErrorAction SilentlyContinue
}
exit $installerExitCode
