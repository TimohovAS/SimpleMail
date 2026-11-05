param(
    [Parameter(Mandatory = $true)][string]$TargetPath,
    [string]$CertificateThumbprint = $env:SIMPLEMAIL_SIGN_CERT_THUMBPRINT,
    [string]$TimestampUrl = $env:SIMPLEMAIL_TIMESTAMP_URL,
    [string]$SignToolPath = $env:SIGNTOOL_PATH
)
$ErrorActionPreference = 'Stop'
if (-not $TimestampUrl) { $TimestampUrl = 'http://timestamp.digicert.com' }
if ($CertificateThumbprint -notmatch '^[0-9a-fA-F]{40}$') {
    throw 'Supply a code-signing certificate thumbprint (SIMPLEMAIL_SIGN_CERT_THUMBPRINT).'
}
$certificate = Get-Item -LiteralPath "Cert:\CurrentUser\My\$CertificateThumbprint" -ErrorAction SilentlyContinue
$machineStore = $false
if (-not $certificate) {
    $certificate = Get-Item -LiteralPath "Cert:\LocalMachine\My\$CertificateThumbprint" -ErrorAction SilentlyContinue
    $machineStore = $true
}
$usages = @($certificate.Extensions | Where-Object { $_.Oid.Value -eq '2.5.29.37' } |
    ForEach-Object { $_.EnhancedKeyUsages } | ForEach-Object { $_.Value })
if (-not $certificate -or -not $certificate.HasPrivateKey -or
    $certificate.NotAfter -lt (Get-Date) -or $certificate.NotBefore -gt (Get-Date) -or
    '1.3.6.1.5.5.7.3.3' -notin $usages) {
    throw 'A valid code-signing certificate with its private key is required.'
}
if (-not $SignToolPath) {
    $tool = Get-Command signtool.exe -ErrorAction SilentlyContinue
    if ($tool) { $SignToolPath = $tool.Source }
    else {
        $tool = Get-ChildItem "${env:ProgramFiles(x86)}\Windows Kits\10\bin\*\x64\signtool.exe" -ErrorAction SilentlyContinue |
            Sort-Object { [version]$_.Directory.Parent.Name } -Descending | Select-Object -First 1
        if ($tool) { $SignToolPath = $tool.FullName }
    }
}
if (-not $SignToolPath -or -not (Test-Path -LiteralPath $SignToolPath)) {
    throw 'Install Windows SDK SignTool or set SIGNTOOL_PATH.'
}
$signArgs = @('sign', '/sha1', $CertificateThumbprint, '/fd', 'SHA256', '/tr', $TimestampUrl, '/td', 'SHA256')
if ($machineStore) { $signArgs += '/sm' }
& $SignToolPath @signArgs $TargetPath
if ($LASTEXITCODE -ne 0) { throw 'Authenticode signing failed.' }
& $SignToolPath verify /pa $TargetPath
if ($LASTEXITCODE -ne 0) { throw 'Authenticode signature verification failed.' }
