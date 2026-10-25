param(
    [Parameter(Mandatory=$true)][string]$Archive,
    [Parameter(Mandatory=$true)][string]$TargetReport,
    [Parameter(Mandatory=$true)][string]$OutputDirectory,
    [switch]$AllowDeveloperMachine,
    [switch]$FailDownload
)
$ErrorActionPreference = 'Stop'
$Archive = (Resolve-Path -LiteralPath $Archive).Path
$target = Get-Content -LiteralPath $TargetReport -Raw -Encoding UTF8 | ConvertFrom-Json
if($target.passed -ne $true -or $target.version -ne $target.expected_version -or -not $target.source_tree_sha256){
    throw 'A successful portable_artifacts.py target report is required.'
}
if((Get-FileHash -LiteralPath $Archive -Algorithm SHA256).Hash.ToLower() -cne $target.portable.sha256){
    throw 'Archive differs from the explicitly verified target report.'
}
$null = New-Item -ItemType Directory -Path $OutputDirectory -Force
$OutputDirectory = (Resolve-Path -LiteralPath $OutputDirectory).Path
$commands = @('python','python3','py','git') | ForEach-Object {
    $found = Get-Command $_ -ErrorAction SilentlyContinue
    @{name=$_;path=if($found){$found.Source}else{$null};appAlias=[bool]($found -and $found.Source -like '*\WindowsApps\*')}
}
$installed = @($commands | Where-Object { $_.path -and -not $_.appAlias })
$osInfo = Get-CimInstance Win32_OperatingSystem
$report = @{date=(Get-Date).ToUniversalTime().ToString('o');os=$osInfo.Caption;version=$osInfo.Version;
    architecture=$osInfo.OSArchitecture;commands=$commands;cleanMachine=($installed.Count -eq 0);
    mcpConfiguredByHarness=$false;externalToolsOnAppPath=$false;archiveSha256=(Get-FileHash -LiteralPath $Archive -Algorithm SHA256).Hash.ToLower();
    artifactPath=$Archive;artifactVersion=$target.version;sourceTreeSha256=$target.source_tree_sha256;manifestSha256=$target.manifest_sha256}
if(Test-Path -LiteralPath (Join-Path $OutputDirectory 'ENVIRONMENT.json')){throw 'Existing evidence; choose a new -OutputDirectory.'}
$report | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath (Join-Path $OutputDirectory 'ENVIRONMENT.json') -Encoding UTF8
if ($installed.Count -gt 0 -and -not $AllowDeveloperMachine) {
    throw 'Python/Git commands were found. Use a clean Windows VM, or explicitly select -AllowDeveloperMachine for partial evidence only.'
}
$expected = (Get-Content -LiteralPath ($Archive + '.sha256') -Raw).Trim()
if ($expected -cne ($report.archiveSha256 + '  ' + [IO.Path]::GetFileName($Archive))) { throw 'ZIP SHA-256 mismatch.' }
$installation = Join-Path $OutputDirectory '中文 空格 程序'
if(Test-Path -LiteralPath $installation){throw 'Existing extracted application; choose a new -OutputDirectory.'}
Expand-Archive -LiteralPath $Archive -DestinationPath $installation
$exe = Join-Path $installation 'BaguTrainer\BaguTrainer.exe'
if (-not (Test-Path -LiteralPath $exe)) { throw 'Portable EXE missing.' }
$manifestPath = Join-Path $installation 'BaguTrainer\BUILD_MANIFEST.json'
$manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
if($manifest.version -ne $target.version -or $manifest.source_tree.sha256 -cne $target.source_tree_sha256 -or
   (Get-FileHash -LiteralPath $manifestPath -Algorithm SHA256).Hash.ToLower() -cne $target.manifest_sha256){throw 'Extracted manifest differs from target.'}
foreach($entry in $manifest.files){
    $file = Join-Path (Split-Path -Parent $exe) $entry.path
    if(-not (Test-Path -LiteralPath $file) -or (Get-Item -LiteralPath $file).Length -ne $entry.bytes -or
       (Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash.ToLower() -cne $entry.sha256){throw 'Extracted payload differs from manifest.'}
}
$mockDirectory = Join-Path $OutputDirectory 'mock'
$mockFile = Join-Path $mockDirectory 'mock.json'
if (-not (Test-Path -LiteralPath $mockFile)) {
    $powershell = Join-Path $PSHOME 'powershell.exe'
    $mockScript = Join-Path $PSScriptRoot 'mock-model.ps1'
    $null = Start-Process -FilePath $powershell -ArgumentList @('-NoProfile','-ExecutionPolicy','Bypass','-File',('"'+$mockScript+'"'),'-OutputDirectory',('"'+$mockDirectory+'"')) -WindowStyle Hidden -PassThru
    for($attempt=0;$attempt -lt 50 -and -not (Test-Path -LiteralPath $mockFile);$attempt++){Start-Sleep -Milliseconds 100}
}
$mock = Get-Content -LiteralPath $mockFile -Raw -Encoding UTF8 | ConvertFrom-Json
$null = Invoke-RestMethod -Uri ($mock.base_url.Replace('/v1','/status'))
$project = Join-Path $OutputDirectory '监听 项目'
$null = New-Item -ItemType Directory -Path $project -Force
if (-not (Test-Path -LiteralPath (Join-Path $project 'query.py'))) {
    Set-Content -LiteralPath (Join-Path $project 'query.py') -Encoding UTF8 -Value 'query = "SELECT id FROM orders"'
}
$profile = Join-Path $OutputDirectory '用户 数据'
$names = @('PATH','BAGU_USER_DATA_DIR','BAGU_LLM_API_KEY','PYTHONHOME','PYTHONPATH','PYTHON_KEYRING_BACKEND','HTTPS_PROXY')
$saved = @{}
foreach($name in $names){$saved[$name]=[Environment]::GetEnvironmentVariable($name,'Process')}
try {
    foreach($name in @('BAGU_LLM_API_KEY','PYTHONHOME','PYTHONPATH','PYTHON_KEYRING_BACKEND')){[Environment]::SetEnvironmentVariable($name,$null,'Process')}
    [Environment]::SetEnvironmentVariable('PATH','','Process')
    [Environment]::SetEnvironmentVariable('BAGU_USER_DATA_DIR',$profile,'Process')
    if($FailDownload){[Environment]::SetEnvironmentVariable('HTTPS_PROXY','http://127.0.0.1:9','Process')}
    # The interactive application is the test target; the mock helper stays hidden.
    $process = Start-Process -FilePath $exe -WorkingDirectory ([IO.Path]::GetDirectoryName($exe)) -PassThru
    @{appPid=$process.Id;mockPid=$mock.pid;exe=$exe;userData=$profile;watchedProject=$project;
      baseUrl=$mock.base_url;model=$mock.model;fixtureKey=$mock.fixture_key;downloadFailure=$FailDownload.IsPresent} |
        ConvertTo-Json | Set-Content -LiteralPath (Join-Path $OutputDirectory 'SESSION.json') -Encoding UTF8
} finally {
    foreach($name in $names){[Environment]::SetEnvironmentVariable($name,$saved[$name],'Process')}
}
Write-Output ('Session details: ' + (Join-Path $OutputDirectory 'SESSION.json'))
