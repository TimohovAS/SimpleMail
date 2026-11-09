param(
    [string]$IsccPath = "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
    [switch]$SkipCompile
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$testRoot = [IO.Path]::GetFullPath((Join-Path $projectRoot 'build\installer-validation'))
$installDir = Join-Path $testRoot 'app'
$dataDir = Join-Path $testRoot 'mail data & attachments'
$activeDataDir = Join-Path $testRoot 'active Google profile & mail'
$profileFile = Join-Path $testRoot 'profile.json'
$testAppData = Join-Path $testRoot 'roaming app data'
$testLocalAppData = Join-Path $testRoot 'local app data'
$bundleDir = Join-Path $projectRoot 'dist\installer-build\SimpleMail'
$buildPython = Join-Path $projectRoot '.venv\Scripts\python.exe'
$runKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
$testRunValue = 'SimpleMail.Installer.Validation'
$startupShortcut = Join-Path ([Environment]::GetFolderPath('Startup')) 'SimpleMail.Installer.Validation.lnk'
if (-not $testRoot.StartsWith($projectRoot + '\', [StringComparison]::OrdinalIgnoreCase)) {
    throw 'The test installation must remain inside the project.'
}
if (Test-Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\SimpleMail.Installer.Validation_is1') {
    throw 'A previous validation installation exists. Uninstall it before retrying.'
}
if (Get-ItemProperty -LiteralPath $runKey -Name $testRunValue -ErrorAction SilentlyContinue) {
    throw 'A previous validation startup entry exists. Remove it before retrying.'
}
if (Test-Path -LiteralPath $startupShortcut) {
    throw 'A previous validation startup shortcut exists. Remove it before retrying.'
}
New-Item -ItemType Directory -Path $testRoot -Force | Out-Null
$savedEnvironment = @{}
foreach ($name in @('APPDATA', 'LOCALAPPDATA', 'SIMPLEMAIL_PROFILE_FILE')) {
    $savedEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
}
Push-Location $projectRoot
try {
    # Every launched process inherits isolated profile discovery and OAuth storage.
    # DPAPI still uses the real Windows user, but no real account/cache is touched.
    $env:APPDATA = $testAppData
    $env:LOCALAPPDATA = $testLocalAppData
    $env:SIMPLEMAIL_PROFILE_FILE = $profileFile
    if (Test-Path -LiteralPath $profileFile) { Remove-Item -LiteralPath $profileFile }
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
from dataclasses import asdict
from pathlib import Path
from unittest import mock
sys.path.insert(0, sys.argv[1])
from mailclient.db import Database, Account, default_data_dir
p = Path(sys.argv[2])
db = Database(p / "mail.db")
mode = sys.argv[3]
if mode == "wait-ready":
    from PySide6.QtCore import QCoreApplication
    from PySide6.QtNetwork import QLocalSocket
    from mailclient.instance import socket_name
    app = QCoreApplication([])
    deadline = time.monotonic() + 60
    while True:
        socket = QLocalSocket()
        socket.connectToServer(socket_name(p))
        if socket.waitForConnected(250):
            # Probe readiness only: a normal second launch must show the window.
            socket.disconnectFromServer()
            break
        if time.monotonic() >= deadline:
            raise AssertionError("Hidden startup did not open the expected active profile")
        time.sleep(.1)
elif mode == "create":
    # Repeated validation runs must still exercise a fresh language preference.
    with db.conn() as connection:
        connection.execute("DELETE FROM settings WHERE key='language'")
    db.set_setting("auto_updates", "0")
    db.set_setting("installer_validation", "preserve")
    if not db.accounts():
        db.save_account(Account(name="Installer validation", enabled=False, storage_dir=str(p)))
    (p / "saved.eml").write_bytes(b"Subject: preserve\r\n\r\nmail")
elif mode in {"create-active", "check-active"}:
    from mailclient import google_oauth, oauth
    from mailclient.profiles import select_data_dir
    address = "installer-validation@example.invalid"
    tokens = {"refresh_token": "synthetic-installer-refresh-token",
              "access_token": "synthetic-installer-access-token", "expires_at": 4102444800}
    manifest_path = p / "validation-snapshot.json"
    if mode == "create-active":
        # Repeated smoke tests reuse only this synthetic account and these files.
        account = next((a for a in db.accounts() if a.email == address), Account())
        account.name = "Installer Google validation"
        account.email = account.username = address
        account.auth_method = "google"
        account.oauth_client_id = "installer-validation.apps.googleusercontent.com"
        account.oauth_client_secret = "synthetic-client-secret"
        account.imap_host, account.smtp_host = "imap.gmail.com", "smtp.gmail.com"
        account.enabled = False
        account.storage_dir = str(p)
        db.save_account(account)
        db.set_setting("auto_updates", "0")
        db.set_setting("close_to_tray", "0")
        db.set_setting("language", "sr_Latn")
        db.set_setting("installer_validation", "active-profile-preserved")
        (p / "saved.eml").write_bytes(b"Subject: synthetic Google validation\r\n\r\npreserve active mail")
        key = google_oauth._key(db, account)
        assert google_oauth._save(key, tokens), "Real DPAPI persistence is unavailable"
        cache_file = google_oauth._path(key)
        encrypted = cache_file.read_bytes()
        assert all(value.encode() not in encrypted for value in tokens.values() if isinstance(value, str))
        snapshot = {"accounts": [asdict(a) for a in db.accounts()], "cache": str(cache_file),
                    "cache_sha256": hashlib.sha256(encrypted).hexdigest(),
                    "mail_sha256": hashlib.sha256((p / "saved.eml").read_bytes()).hexdigest()}
        manifest_path.write_text(json.dumps(snapshot), encoding="utf-8")
        decoy = Database(default_data_dir() / "mail.db")
        assert not decoy.accounts(), "The isolated default profile must remain empty"
        decoy.set_setting("auto_updates", "0")
        decoy.set_setting("installer_validation", "empty-default-decoy")
        decoy.close()
    else:
        snapshot = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert [asdict(a) for a in db.accounts()] == snapshot["accounts"], "Accounts changed during upgrade"
        account = next(a for a in db.accounts() if a.email == address)
        assert db.get_setting("installer_validation") == "active-profile-preserved"
        assert db.get_setting("language") == "sr_Latn", "Upgrade replaced the active profile language"
        assert hashlib.sha256((p / "saved.eml").read_bytes()).hexdigest() == snapshot["mail_sha256"]
        key = google_oauth._key(db, account)
        cache_file = google_oauth._path(key)
        assert str(cache_file) == snapshot["cache"], "Google cache identity changed"
        assert hashlib.sha256(cache_file.read_bytes()).hexdigest() == snapshot["cache_sha256"], "Encrypted Google cache changed"
        google_oauth._memory.clear()
        with mock.patch("urllib.request.urlopen", side_effect=AssertionError("Smoke test attempted network access")):
            assert google_oauth._load(key) == tokens, "The persisted DPAPI session cannot be decrypted"
            assert oauth.has_session(db, account), "Google sign-in was lost"
        assert select_data_dir().resolve() == p.resolve(), "The active profile was not remembered"
else:
    assert db.get_setting("installer_validation") == "preserve"
    if sys.argv[3] in {"check-language", "check"}:
        assert db.get_setting("language") == "sr_Latn", "Setup language was not applied or a saved choice was overwritten"
assert all(not a.enabled for a in db.accounts()), "Validation profiles must never contain enabled network accounts"
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
    function Start-ValidationApp([string]$LaunchArguments, [switch]$CheckActivation, [string]$HiddenProfile) {
        # This is a native-window smoke test; helpers and Setup stay hidden.
        $start = @{ FilePath = $appExe; WindowStyle = 'Normal'; PassThru = $true }
        if ($LaunchArguments) { $start.ArgumentList = $LaunchArguments }
        $appProcess = Start-Process @start
        try {
            $null = $appProcess.WaitForInputIdle(20000)
            if ($HiddenProfile) {
                # The first launch can correctly stay in the tray. Confirm its
                # chosen profile before asking a normal second launch to show it.
                & $buildPython $fixture $projectRoot $HiddenProfile wait-ready
                if ($LASTEXITCODE -ne 0) { throw 'Minimized startup did not become ready in the active profile.' }
                $normalArguments = ($LaunchArguments -replace '(?:^|\s)--minimized(?=\s|$)', '').Trim()
                $activate = @{ FilePath = $appExe; WindowStyle = 'Normal'; PassThru = $true }
                if ($normalArguments) { $activate.ArgumentList = $normalArguments }
                $activationClient = Start-Process @activate
                try {
                    if (-not $activationClient.WaitForExit(15000) -or $activationClient.ExitCode -ne 0) {
                        throw 'Normal launch did not activate the minimized running copy.'
                    }
                    $appProcess.Refresh()
                    if ($appProcess.HasExited) { throw 'Activation closed the minimized application.' }
                } finally {
                    if (-not $activationClient.HasExited) { $activationClient | Stop-Process -Force }
                }
            }
            # First-run native dependency loading/antivirus scanning can be slow.
            for ($attempt = 0; $attempt -lt 120; $attempt++) {
                $appProcess.Refresh()
                if ($appProcess.HasExited) { throw 'Installed application exited at startup.' }
                if ($appProcess.MainWindowTitle -eq "SimpleMail $version") { break }
                Start-Sleep -Milliseconds 500
            }
            if ($appProcess.MainWindowTitle -ne "SimpleMail $version") { throw 'Installed application did not open its main window.' }
            if ($CheckActivation) {
                $activationClient = Start-Process @start
                try {
                    if (-not $activationClient.WaitForExit(15000) -or $activationClient.ExitCode -ne 0) {
                        throw 'A second launch did not activate the running copy and exit.'
                    }
                    $appProcess.Refresh()
                    if ($appProcess.HasExited) { throw 'Activation closed the original application.' }
                } finally {
                    if (-not $activationClient.HasExited) { $activationClient | Stop-Process -Force }
                }
            }
            if (-not $appProcess.CloseMainWindow()) { throw 'Unable to close test application gracefully.' }
            if (-not $appProcess.WaitForExit(20000)) {
                # Diagnostics for a hang on exit: child processes, threads and the application logs.
                Get-CimInstance Win32_Process -Filter "ParentProcessId=$($appProcess.Id)" |
                    ForEach-Object { "child: $($_.ProcessId) $($_.Name)" } | Write-Host
                $appProcess.Refresh()
                Write-Host "threads: $($appProcess.Threads.Count); main window: '$($appProcess.MainWindowTitle)'"
                foreach ($folder in @($dataDir, $activeDataDir)) {
                    foreach ($name in @('simplemail.log', 'crash.log')) {
                        $file = Join-Path $folder "logs\$name"
                        if (Test-Path -LiteralPath $file) { Write-Host "--- $file"; Get-Content -LiteralPath $file -Tail 30 | Write-Host }
                    }
                }
                throw 'Test application did not exit.'
            }
        } finally {
            if (-not $appProcess.HasExited) { $appProcess | Stop-Process -Force }
        }
    }
    try {
        $setup = Start-Process -FilePath $installerPath -ArgumentList ($setupArgs + "/LOG=`"$testRoot\install.log`"") -WindowStyle Hidden -Wait -PassThru
        if ($setup.ExitCode -ne 0) { throw "Initial install failed: $($setup.ExitCode)" }
        $appExe = Join-Path $installDir 'SimpleMail.exe'
        if ((Get-Item $appExe).VersionInfo.ProductVersion -ne $version) { throw 'Installed EXE has wrong version.' }
        $shortcutPath = Join-Path ([Environment]::GetFolderPath('Programs')) 'SimpleMail installer validation.lnk'
        $shortcut = (New-Object -ComObject WScript.Shell).CreateShortcut($shortcutPath)
        if ($shortcut.Arguments -ne "--profile-fallback `"$dataDir\.`"") { throw 'Custom data fallback was not written to the shortcut.' }
        # With no remembered profile, the legacy shortcut hint must open A.
        Start-ValidationApp $shortcut.Arguments -CheckActivation
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
        # A manually downloaded installer receives no /MAILDATA. Its previous
        # shortcut fallback is A, but the user has since opened populated profile B.
        & $buildPython $fixture $projectRoot $activeDataDir create-active
        if ($LASTEXITCODE -ne 0) { throw 'Unable to prepare the isolated Google profile and DPAPI cache.' }
        Start-ValidationApp "--data `"$activeDataDir`""
        & $buildPython $fixture $projectRoot $activeDataDir check-active
        if ($LASTEXITCODE -ne 0) { throw 'The frozen app did not remember the active Google profile.' }
        $defaultDb = Join-Path $testAppData 'SimpleMail\mail.db'
        $defaultHash = (Get-FileHash -LiteralPath $defaultDb).Hash
        $manualArgs = @('/CURRENTUSER', '/VERYSILENT', '/SUPPRESSMSGBOXES', '/SP-', '/NORESTART', '/LANG=russian', "/DIR=`"$installDir`"")
        # The first launch after updating can be Windows startup, before the
        # new application has had an opportunity to normalize an older command.
        New-Item -Path $runKey -Force | Out-Null
        Set-ItemProperty -LiteralPath $runKey -Name $testRunValue -Value "`"$appExe`" --data `"$dataDir`""
        # A file the new version no longer ships (e.g. Qt QML modules of 0.20.0).
        $staleFile = Join-Path $installDir '_internal\PySide6\qml\stale-from-previous-version.txt'
        New-Item -ItemType Directory -Path (Split-Path -Parent $staleFile) -Force | Out-Null
        Set-Content -LiteralPath $staleFile -Value 'old'
        $manual = Start-Process -FilePath $installerPath -ArgumentList ($manualArgs + "/LOG=`"$testRoot\manual-upgrade.log`"") -WindowStyle Hidden -Wait -PassThru
        if ($manual.ExitCode -ne 0) { throw 'Manual upgrade without /MAILDATA failed.' }
        if (Test-Path -LiteralPath $staleFile) { throw 'Upgrade left files of the previous version in _internal.' }
        # Registry startup of older versions becomes a Startup folder shortcut.
        $startupArguments = "--profile-fallback `"$dataDir`" --minimized"
        if (Get-ItemProperty -LiteralPath $runKey -Name $testRunValue -ErrorAction SilentlyContinue) {
            throw 'Upgrade left the registry startup entry.'
        }
        if (-not (Test-Path -LiteralPath $startupShortcut)) { throw 'Upgrade did not create the startup shortcut.' }
        $startupLink = (New-Object -ComObject WScript.Shell).CreateShortcut($startupShortcut)
        if ($startupLink.TargetPath -ne $appExe -or $startupLink.Arguments -ne $startupArguments) {
            throw "Upgrade did not normalize the existing startup profile before first launch: $($startupLink.TargetPath) $($startupLink.Arguments)"
        }
        $shortcut = (New-Object -ComObject WScript.Shell).CreateShortcut($shortcutPath)
        if ($shortcut.Arguments -ne "--profile-fallback `"$dataDir\.`"") { throw 'The previous custom data fallback was lost from the shortcut.' }
        # Windows startup, a direct EXE launch and a normal shortcut all resolve B.
        foreach ($launchArguments in @($startupArguments, '', $shortcut.Arguments)) {
            $crashLog = Join-Path $activeDataDir 'logs\crash.log'
            if (Test-Path -LiteralPath $crashLog) { Remove-Item -LiteralPath $crashLog }
            if ($launchArguments -eq $startupArguments) {
                Start-ValidationApp $launchArguments -HiddenProfile $activeDataDir
            } else {
                Start-ValidationApp $launchArguments
            }
            if (-not (Test-Path -LiteralPath $crashLog)) { throw 'The installed app opened another data profile.' }
            & $buildPython $fixture $projectRoot $activeDataDir check-active
            if ($LASTEXITCODE -ne 0) { throw 'Manual upgrade lost the active profile, mail, settings or Google session.' }
            if ((Get-FileHash -LiteralPath $defaultDb).Hash -ne $defaultHash -or
                (Test-Path -LiteralPath (Join-Path $testAppData 'SimpleMail\logs'))) {
                throw 'The empty default profile was opened or changed.'
            }
        }
        # A small older installer exercises the downgrade guard before unpacking.
        $miniBundle = Join-Path $testRoot 'mini-bundle'
        New-Item -ItemType Directory -Path $miniBundle -Force | Out-Null
        Copy-Item -LiteralPath $appExe -Destination $miniBundle
        & $IsccPath /Q /DValidationBuild /DAppVersion=0.0.0 "/DBundleDir=$miniBundle" "/O$testRoot" /Fdowngrade (Join-Path $projectRoot 'installer\SimpleMail.iss')
        if ($LASTEXITCODE -ne 0) { throw 'Unable to compile downgrade test.' }
        $downgrade = Start-Process -FilePath (Join-Path $testRoot 'downgrade.exe') -ArgumentList ($setupArgs + "/LOG=`"$testRoot\downgrade.log`"") -WindowStyle Hidden -Wait -PassThru
        if ($downgrade.ExitCode -eq 0) { throw 'An older version was allowed to install.' }
        if ((Get-Item $appExe).VersionInfo.ProductVersion -ne $version) { throw 'Downgrade changed the installed EXE.' }
        $shortcut = (New-Object -ComObject WScript.Shell).CreateShortcut($shortcutPath)
        if ($shortcut.Arguments -ne "--profile-fallback `"$dataDir\.`"") { throw 'Custom data directory was lost from the shortcut.' }
        # Both quoting styles are produced by Windows autostart commands.
        New-Item -Path $runKey -Force | Out-Null
        Set-ItemProperty -LiteralPath $runKey -Name $testRunValue -Value "`"$appExe`" --data `"$dataDir`" --minimized"
        $uninstall = Start-Process -FilePath $uninstaller -ArgumentList @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART') -WindowStyle Hidden -Wait -PassThru
        if ($uninstall.ExitCode -ne 0 -or (Get-ItemProperty -LiteralPath $runKey -Name $testRunValue -ErrorAction SilentlyContinue)) {
            throw 'Uninstall did not remove a quoted startup command.'
        }
        if (Test-Path -LiteralPath $startupShortcut) { throw 'Uninstall did not remove the startup shortcut.' }
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
    & $buildPython $fixture $projectRoot $activeDataDir check-active
    if ($LASTEXITCODE -ne 0) { throw 'Active profile and encrypted Google session did not survive removal.' }
    if (Test-Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\SimpleMail.Installer.Validation_is1') {
        throw 'Validation uninstall registration was not removed.'
    }
    if (Get-ItemProperty -LiteralPath $runKey -Name $testRunValue -ErrorAction SilentlyContinue) {
        throw 'Uninstall did not remove an unquoted startup command.'
    }
    Write-Output 'PASS: install, Serbian first launch, saved language on update, frozen launch, deferred update handoff, manual upgrade and minimized startup/normal activation/direct/shortcut launch with active custom profile, real DPAPI Google session, downgrade rejection, stale files removed on upgrade, registry startup moved to a Startup folder shortcut, both startup command formats, uninstall; mail, settings and sessions preserved.'
} finally {
    foreach ($name in $savedEnvironment.Keys) {
        [Environment]::SetEnvironmentVariable($name, $savedEnvironment[$name], 'Process')
    }
    Pop-Location
}
