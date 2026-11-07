param(
    [string]$IsccPath = "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
    [switch]$SkipCompile
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$testRoot = [IO.Path]::GetFullPath((Join-Path $projectRoot 'build\installer-validation'))
$installDir = Join-Path $testRoot 'app'
$dataDir = Join-Path $testRoot 'mail data & attachments'
$bundleDir = Join-Path $projectRoot 'dist\installer-build\SimpleMail'
$buildPython = Join-Path $projectRoot '.venv\Scripts\python.exe'
$runKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
$testRunValue = 'SimpleMail.Installer.Validation'
if (-not $testRoot.StartsWith($projectRoot + '\', [StringComparison]::OrdinalIgnoreCase)) {
    throw 'The test installation must remain inside the project.'
}
if (Test-Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\SimpleMail.Installer.Validation_is1') {
    throw 'A previous validation installation exists. Uninstall it before retrying.'
}
if (Get-ItemProperty -LiteralPath $runKey -Name $testRunValue -ErrorAction SilentlyContinue) {
    throw 'A previous validation startup entry exists. Remove it before retrying.'
}
New-Item -ItemType Directory -Path $testRoot -Force | Out-Null
Push-Location $projectRoot
try {
    $version = & $buildPython -c 'from mailclient import __version__; print(__version__)'
    if ($LASTEXITCODE -ne 0) { throw 'Unable to read version.' }
    if (-not $SkipCompile) {
        & $IsccPath /Q /DValidationBuild "/DAppVersion=$version" "/DBundleDir=$bundleDir" "/O$testRoot" /Fsetup (Join-Path $projectRoot 'installer\SimpleMail.iss')
        if ($LASTEXITCODE -ne 0) { throw 'Validation build failed.' }
    }
    $fixture = Join-Path $testRoot 'fixture.py'
    @'
import hashlib
import json
import sys
import time
from pathlib import Path
from unittest import mock
sys.path.insert(0, sys.argv[1])
from mailclient.db import Database, Account
p = Path(sys.argv[2])
db = Database(p / "mail.db")
if sys.argv[3] == "create":
    # Repeated validation runs must still exercise a fresh language preference.
    with db.conn() as connection:
        connection.execute("DELETE FROM settings WHERE key='language'")
    db.set_setting("auto_updates", "0")
    db.set_setting("installer_validation", "preserve")
    if not db.accounts():
        db.save_account(Account(name="Installer validation", enabled=False, storage_dir=str(p)))
    (p / "saved.eml").write_bytes(b"Subject: preserve\r\n\r\nmail")
else:
    assert db.get_setting("installer_validation") == "preserve"
    if sys.argv[3] in {"check-language", "check"}:
        assert db.get_setting("language") == "sr_Latn", "Setup language was not applied or a saved choice was overwritten"
db.close()
if sys.argv[3] == "handoff":
    from mailclient import __version__, updates
    setup = Path(sys.argv[5]).resolve()
    log = Path(sys.argv[6]).resolve()
    cache = log.parent / "update cache"
    release = updates.Release(__version__, f"{updates.RELEASES_URL}/download/v{__version__}/SimpleMail-Setup-{__version__}-x64.exe",
                              hashlib.sha256(setup.read_bytes()).hexdigest(), setup.stat().st_size)
    original_command = updates._installer_command
    original_start = updates._start_update_helper
    def silent_command(*args):
        return original_command(*args) + ["/CURRENTUSER", "/VERYSILENT", "/SUPPRESSMSGBOXES", "/LANG=russian", "/LOG=" + str(log)]
    with mock.patch.object(sys, "frozen", True, create=True), \
            mock.patch.object(sys, "executable", sys.argv[4]), \
            mock.patch.object(updates, "cache_dir", return_value=cache), \
            mock.patch.object(updates, "_installer_command", side_effect=silent_command), \
            mock.patch.object(updates, "_start_update_helper", side_effect=lambda command, directory: original_start(command + ["-QuietErrors"], directory)):
        lease = updates.hold_installation_mutex()
        assert lease
        helper = updates.launch_installer(setup, release, p)
        (log.parent / "handoff-process.json").write_text(json.dumps({"pid": helper.pid}), encoding="utf-8")
        time.sleep(2)  # An intentionally slow shutdown must never start Setup early.
        assert not log.exists(), "Installer started while the application still held its mutex"
    # Keep the mutex until process exit, just like main.py.
'@ | Set-Content -LiteralPath $fixture -Encoding UTF8
    & $buildPython $fixture $projectRoot $dataDir create
    if ($LASTEXITCODE -ne 0) { throw 'Unable to prepare test mail.' }
    $dataHash = (Get-FileHash (Join-Path $dataDir 'saved.eml')).Hash
    $installerPath = Join-Path $testRoot 'setup.exe'
    $setupArgs = @('/CURRENTUSER', '/VERYSILENT', '/SUPPRESSMSGBOXES', '/SP-', '/NORESTART', '/LANG=serbianlatin', "/DIR=`"$installDir`"", "/MAILDATA=`"$dataDir`"")
    $uninstaller = Join-Path $installDir 'unins000.exe'
    try {
        $setup = Start-Process -FilePath $installerPath -ArgumentList ($setupArgs + "/LOG=`"$testRoot\install.log`"") -WindowStyle Hidden -Wait -PassThru
        if ($setup.ExitCode -ne 0) { throw "Initial install failed: $($setup.ExitCode)" }
        $appExe = Join-Path $installDir 'SimpleMail.exe'
        if ((Get-Item $appExe).VersionInfo.ProductVersion -ne $version) { throw 'Installed EXE has wrong version.' }
        # Launch the actual frozen application with an isolated database and no network accounts.
        $appProcess = Start-Process -FilePath $appExe -ArgumentList "--data `"$dataDir`"" -WindowStyle Normal -PassThru
        try {
            $null = $appProcess.WaitForInputIdle(20000)
            # First-run native dependency loading/antivirus scanning can be slow.
            for ($attempt = 0; $attempt -lt 120; $attempt++) {
                $appProcess.Refresh()
                if ($appProcess.HasExited) { throw 'Installed application exited at startup.' }
                if ($appProcess.MainWindowTitle -eq "SimpleMail $version") { break }
                Start-Sleep -Milliseconds 500
            }
            if ($appProcess.MainWindowTitle -ne "SimpleMail $version") { throw 'Installed application did not open its main window.' }
            $activationClient = Start-Process -FilePath $appExe -ArgumentList "--data `"$dataDir`"" -WindowStyle Normal -PassThru
            try {
                if (-not $activationClient.WaitForExit(15000) -or $activationClient.ExitCode -ne 0) {
                    throw 'A second launch did not activate the running copy and exit.'
                }
                $appProcess.Refresh()
                if ($appProcess.HasExited) { throw 'Activation closed the original application.' }
            } finally {
                if (-not $activationClient.HasExited) { $activationClient | Stop-Process -Force }
            }
            if (-not $appProcess.CloseMainWindow()) { throw 'Unable to close test application gracefully.' }
            if (-not $appProcess.WaitForExit(20000)) { throw 'Test application did not exit.' }
        } finally {
            if (-not $appProcess.HasExited) { $appProcess | Stop-Process -Force }
        }
        & $buildPython $fixture $projectRoot $dataDir check-language
        if ($LASTEXITCODE -ne 0) { throw 'The installed application did not start in Serbian Latin.' }
        # Reinstall over the same directory, replacing a deliberately changed packaged resource.
        $resource = Join-Path $installDir '_internal\mailclient\icons\info.svg'
        $packagedHelper = Join-Path $installDir '_internal\mailclient\update_helper.ps1'
        if ((Get-FileHash $packagedHelper).Hash -ne (Get-FileHash (Join-Path $projectRoot 'mailclient\update_helper.ps1')).Hash) {
            throw 'The packaged update helper is missing or outdated.'
        }
        $originalHash = (Get-FileHash $resource).Hash
        Set-Content -LiteralPath $resource -Value 'old resource'
        $handoffLog = Join-Path $testRoot 'handoff.log'
        if (Test-Path -LiteralPath $handoffLog) { Remove-Item -LiteralPath $handoffLog }
        & $buildPython $fixture $projectRoot $dataDir handoff $appExe $installerPath $handoffLog
        if ($LASTEXITCODE -ne 0) { throw 'Update handoff failed before application exit.' }
        $handoffProcess = Get-Content (Join-Path $testRoot 'handoff-process.json') -Raw | ConvertFrom-Json
        try { $helper = [Diagnostics.Process]::GetProcessById([int]$handoffProcess.pid) }
        catch [ArgumentException] { $helper = $null }
        if ($null -ne $helper) {
            try {
                if (-not $helper.WaitForExit(120000)) { throw 'Update helper did not finish.' }
            } finally { $helper.Dispose() }
        }
        if (-not (Test-Path -LiteralPath $handoffLog) -or
            -not (Select-String -LiteralPath $handoffLog -SimpleMatch 'Installation process succeeded.')) {
            throw 'Setup did not complete after the application exited. See handoff.log and update cache/*.error.log.'
        }
        if (Get-ChildItem (Join-Path $testRoot 'update cache') -Filter '*.error.log' -ErrorAction SilentlyContinue) {
            throw 'The update helper reported an error.'
        }
        if ((Get-FileHash $resource).Hash -ne $originalHash) { throw 'Upgrade did not replace application resources.' }
        if ((Get-Content -LiteralPath (Join-Path $installDir 'installer-language.txt') -Raw).Trim() -ne 'ru') {
            throw 'The update did not record its Russian installer language.'
        }
        & $buildPython $fixture $projectRoot $dataDir check-language
        if ($LASTEXITCODE -ne 0) { throw 'The update replaced the saved application language.' }
        # A small older installer exercises the downgrade guard before unpacking.
        $miniBundle = Join-Path $testRoot 'mini-bundle'
        New-Item -ItemType Directory -Path $miniBundle -Force | Out-Null
        Copy-Item -LiteralPath $appExe -Destination $miniBundle
        & $IsccPath /Q /DValidationBuild /DAppVersion=0.0.0 "/DBundleDir=$miniBundle" "/O$testRoot" /Fdowngrade (Join-Path $projectRoot 'installer\SimpleMail.iss')
        if ($LASTEXITCODE -ne 0) { throw 'Unable to compile downgrade test.' }
        $downgrade = Start-Process -FilePath (Join-Path $testRoot 'downgrade.exe') -ArgumentList ($setupArgs + "/LOG=`"$testRoot\downgrade.log`"") -WindowStyle Hidden -Wait -PassThru
        if ($downgrade.ExitCode -eq 0) { throw 'An older version was allowed to install.' }
        if ((Get-Item $appExe).VersionInfo.ProductVersion -ne $version) { throw 'Downgrade changed the installed EXE.' }
        $shortcutPath = Join-Path ([Environment]::GetFolderPath('Programs')) 'SimpleMail installer validation.lnk'
        $shortcut = (New-Object -ComObject WScript.Shell).CreateShortcut($shortcutPath)
        if ($shortcut.Arguments -ne "--data `"$dataDir\.`"") { throw 'Custom data directory was lost from the shortcut.' }
        # Both quoting styles are produced by Windows autostart commands.
        New-Item -Path $runKey -Force | Out-Null
        Set-ItemProperty -LiteralPath $runKey -Name $testRunValue -Value "`"$appExe`" --data `"$dataDir`" --minimized"
        $uninstall = Start-Process -FilePath $uninstaller -ArgumentList @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART') -WindowStyle Hidden -Wait -PassThru
        if ($uninstall.ExitCode -ne 0 -or (Get-ItemProperty -LiteralPath $runKey -Name $testRunValue -ErrorAction SilentlyContinue)) {
            throw 'Uninstall did not remove a quoted startup command.'
        }
        $setup = Start-Process -FilePath $installerPath -ArgumentList $setupArgs -WindowStyle Hidden -Wait -PassThru
        if ($setup.ExitCode -ne 0) { throw 'Reinstall for the unquoted startup test failed.' }
        Set-ItemProperty -LiteralPath $runKey -Name $testRunValue -Value "$appExe --data `"$dataDir`""
    } finally {
        if (Test-Path -LiteralPath $uninstaller) {
            $uninstall = Start-Process -FilePath $uninstaller -ArgumentList @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', "/LOG=`"$testRoot\uninstall.log`"") -WindowStyle Hidden -Wait -PassThru
            if ($uninstall.ExitCode -ne 0) { throw "Validation uninstall failed: $($uninstall.ExitCode)" }
        }
    }
    if ((Get-FileHash (Join-Path $dataDir 'saved.eml')).Hash -ne $dataHash) { throw 'Mail file changed.' }
    & $buildPython $fixture $projectRoot $dataDir check
    if ($LASTEXITCODE -ne 0) { throw 'Mail settings did not survive installation and removal.' }
    if (Test-Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\SimpleMail.Installer.Validation_is1') {
        throw 'Validation uninstall registration was not removed.'
    }
    if (Get-ItemProperty -LiteralPath $runKey -Name $testRunValue -ErrorAction SilentlyContinue) {
        throw 'Uninstall did not remove an unquoted startup command.'
    }
    Write-Output 'PASS: install, Serbian first launch, saved language on update, frozen launch, deferred update handoff, downgrade rejection, both startup command formats, uninstall; mail and settings preserved.'
} finally {
    Pop-Location
}
