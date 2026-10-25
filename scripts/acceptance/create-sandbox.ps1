param(
    [string]$OutputDirectory = (Join-Path $PSScriptRoot '..\..\output'),
    [string]$BuildOutputDirectory,
    [string]$Python = 'python'
)
$ErrorActionPreference = 'Stop'
$root = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\..')).Path
$null = New-Item -ItemType Directory -Path $OutputDirectory -Force
$output = (Resolve-Path -LiteralPath $OutputDirectory).Path
$inputsDirectory = Join-Path $output 'clean-windows-inputs'
$resultsDirectory = Join-Path $output 'clean-windows-results'
$targetReport = Join-Path $output 'PORTABLE_TARGET.json'
$destination = Join-Path $output 'bagu-clean-acceptance.wsb'
foreach($path in @($inputsDirectory,$resultsDirectory,$targetReport,$destination)){
    if(Test-Path -LiteralPath $path){throw ('Existing evidence; choose a new -OutputDirectory: '+$path)}
}
$arguments = @('-B',(Join-Path $root 'scripts\portable_artifacts.py'),'--with-driver','--report',$targetReport)
if($BuildOutputDirectory){$arguments += @('--output-dir',$BuildOutputDirectory)}
$preflight = & $Python @arguments
if($LASTEXITCODE -ne 0){throw ('Artifact preflight failed; see '+$targetReport)}
$target = Get-Content -LiteralPath $targetReport -Raw -Encoding UTF8 | ConvertFrom-Json
$null = New-Item -ItemType Directory -Path $inputsDirectory
$null = New-Item -ItemType Directory -Path $resultsDirectory
foreach($file in @($target.portable.path,$target.portable.checksum_path,$target.driver.archive.path,$target.driver.archive.checksum_path)){
    Copy-Item -LiteralPath $file -Destination $inputsDirectory
}
Copy-Item -LiteralPath $targetReport -Destination $inputsDirectory
$bootstrap = @'
$ErrorActionPreference = 'Stop'
$commands = @('python','python3','py','git') | ForEach-Object { $c=Get-Command $_ -ErrorAction SilentlyContinue; @{name=$_;path=if($c){$c.Source}else{$null}} }
$present = @($commands | Where-Object { $_.path -and $_.path -notlike '*\WindowsApps\*' })
$osInfo=Get-CimInstance Win32_OperatingSystem
@{os=$osInfo.Caption;version=$osInfo.Version;architecture=$osInfo.OSArchitecture;commands=$commands;
  cleanMachine=($present.Count -eq 0);mcpConfigured=$false;date=(Get-Date).ToUniversalTime().ToString('o')} |
  ConvertTo-Json -Depth 6 | Set-Content -LiteralPath C:\BaguResults\ENVIRONMENT.json -Encoding UTF8
if($present.Count){throw 'This is not a clean guest: Python/Git commands found.'}
$production=@(Get-ChildItem C:\BaguInputs\BaguTrainer-*-windows-x64.zip)
if($production.Count -ne 1){throw 'Expected exactly one production ZIP.'}
$production=$production[0]
$digest=(Get-FileHash -LiteralPath $production.FullName -Algorithm SHA256).Hash.ToLower()
if((Get-Content -LiteralPath ($production.FullName+'.sha256') -Raw).Trim() -cne ($digest+'  '+$production.Name)){throw 'SHA-256 mismatch.'}
Expand-Archive -LiteralPath $production.FullName -DestinationPath 'C:\验收 程序'
$acceptanceZip='C:\BaguInputs\BaguTrainerAcceptance-windows-x64.zip'
$acceptanceDigest=(Get-FileHash -LiteralPath $acceptanceZip -Algorithm SHA256).Hash.ToLower()
if((Get-Content -LiteralPath ($acceptanceZip+'.sha256') -Raw).Trim() -cne ($acceptanceDigest+'  BaguTrainerAcceptance-windows-x64.zip')){throw 'Acceptance ZIP SHA-256 mismatch.'}
Expand-Archive -LiteralPath C:\BaguInputs\BaguTrainerAcceptance-windows-x64.zip -DestinationPath 'C:\验收 程序'
Get-ChildItem -LiteralPath C:\BaguInputs -File | Where-Object { $_.Name -like '*.zip' -or $_.Name -like '*.zip.sha256' } |
  Copy-Item -Destination 'C:\验收 程序'
$run=Start-Process -FilePath 'C:\验收 程序\BaguTrainerAcceptance\BaguTrainerAcceptance.exe' -ArgumentList '--work "C:\BaguResults\自动 验收" --production-exe "C:\验收 程序\BaguTrainer\BaguTrainer.exe"' -WindowStyle Hidden -Wait -PassThru
@{exitCode=$run.ExitCode;zipSha256=$digest} | ConvertTo-Json | Set-Content -LiteralPath C:\BaguResults\EXIT.json -Encoding UTF8
if($run.ExitCode){throw 'Acceptance failed; see ERROR.json.'}
'@
# Windows PowerShell 5 needs a BOM for the Chinese paths.
[IO.File]::WriteAllText((Join-Path $inputsDirectory 'run-acceptance.ps1'),$bootstrap,(New-Object Text.UTF8Encoding($true)))
$escape = [Security.SecurityElement]
$inputsXml = $escape::Escape($inputsDirectory)
$resultsXml = $escape::Escape($resultsDirectory)
$xml = @"
<Configuration>
  <MappedFolders>
    <MappedFolder><HostFolder>$inputsXml</HostFolder><SandboxFolder>C:\BaguInputs</SandboxFolder><ReadOnly>true</ReadOnly></MappedFolder>
    <MappedFolder><HostFolder>$resultsXml</HostFolder><SandboxFolder>C:\BaguResults</SandboxFolder><ReadOnly>false</ReadOnly></MappedFolder>
  </MappedFolders>
  <LogonCommand><Command>powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\BaguInputs\run-acceptance.ps1</Command></LogonCommand>
</Configuration>
"@
[IO.File]::WriteAllText($destination,$xml,(New-Object Text.UTF8Encoding($true)))
Write-Output $destination
if(-not (Test-Path -LiteralPath (Join-Path $env:WINDIR 'System32\WindowsSandbox.exe'))){
    Write-Warning 'Windows Sandbox is unavailable on this host. Configuration generated, not executed or verified.'
}
