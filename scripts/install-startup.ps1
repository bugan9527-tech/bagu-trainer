param(
    [switch]$Uninstall,
    [switch]$NoStart,
    [string]$StartupDirectory = [Environment]::GetFolderPath('Startup'),
    [string]$PythonExe = '',
    [string]$AppExe = ''
)
$ErrorActionPreference = 'Stop'
$projectDirectory = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$entryPoint = Join-Path $projectDirectory 'scripts\start_desktop.py'
$shortcutPath = Join-Path $StartupDirectory 'BaguTrainer.lnk'
$portableExe = if ($AppExe) { (Resolve-Path -LiteralPath $AppExe).Path } else { Join-Path $projectDirectory 'BaguTrainer.exe' }
if (Test-Path -LiteralPath $portableExe) {
    $operation = if ($Uninstall) { '--uninstall-startup' } else { '--install-startup' }
    $process = Start-Process -FilePath $portableExe -ArgumentList @($operation, '--startup-directory', ('"' + $StartupDirectory + '"')) -Wait -PassThru -WindowStyle Hidden
    if ($process.ExitCode -ne 0) { throw 'The portable application could not update the startup shortcut.' }
    if (-not $Uninstall -and -not $NoStart) {
        Start-Process -FilePath $portableExe -ArgumentList '--background' -WindowStyle Hidden
    }
    exit 0
}
if ($Uninstall) {
    if (Test-Path -LiteralPath $shortcutPath) { Remove-Item -LiteralPath $shortcutPath }
    Write-Output 'Windows login startup disabled. Close the desktop window to stop the running app.'
    exit 0
}
$localPython = Join-Path $projectDirectory '.venv\Scripts\python.exe'
$selectedPython = if ($PythonExe) { $PythonExe } elseif (Test-Path -LiteralPath $localPython) { $localPython } else { 'python' }
try {
    # Windows PowerShell may decode native stdout with a different code page.
    # An ASCII envelope preserves interpreter paths containing Unicode characters.
    $encodedPath = (& $selectedPython -c 'import base64,sys; print(base64.b64encode(sys.executable.encode()).decode())').Trim()
    if ($LASTEXITCODE -ne 0 -or -not $encodedPath) { throw 'Interpreter check failed.' }
    $resolvedPython = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($encodedPath))
} catch {
    throw 'Python is unavailable. Install the project .venv or pass -PythonExe with an interpreter path.'
}
$pythonwExe = Join-Path (Split-Path -Parent $resolvedPython) 'pythonw.exe'
if (-not (Test-Path -LiteralPath $pythonwExe)) { throw 'pythonw.exe was not found.' }
& $resolvedPython -B -c 'import tkinter; import keyring'
if ($LASTEXITCODE -ne 0) { throw 'Install requirements.txt and enable Python tkinter before installing startup.' }
& $resolvedPython -B $entryPoint --check-runtime
if ($LASTEXITCODE -ne 0) { throw 'The desktop window could not initialize. Startup was not installed.' }
if (-not (Test-Path -LiteralPath $StartupDirectory)) { throw 'Startup directory does not exist.' }
# The same Unicode IShellLinkW implementation is used by source and EXE modes;
# WScript.Shell may roundtrip Chinese paths through the host ANSI code page.
& $resolvedPython -B $entryPoint --install-startup --startup-directory $StartupDirectory
if ($LASTEXITCODE -ne 0) { throw 'Could not create the Unicode startup shortcut.' }
Write-Output ('Windows login startup enabled: ' + $shortcutPath)
if (-not $NoStart) {
    Start-Process -FilePath $pythonwExe -ArgumentList @('-B', ('"' + $entryPoint + '"'), '--background') -WorkingDirectory $projectDirectory -WindowStyle Hidden
}
