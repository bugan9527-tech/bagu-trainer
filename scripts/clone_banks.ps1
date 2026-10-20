param(
  [Parameter(Mandatory = $true)]
  [ValidateNotNullOrEmpty()]
  [string[]]$Repositories,
  [string]$Proxy = ""
)

# Select sources after reading QUESTION_BANK.md and upstream terms.
# Example: .\scripts\clone_banks.ps1 -Repositories Snailclimb/JavaGuide
$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$manifest = Get-Content -LiteralPath (Join-Path $projectRoot "interview-banks/sources.json") -Raw |
  ConvertFrom-Json
$known = @($manifest.sources | ForEach-Object { $_.repository })
foreach ($repository in $Repositories) {
  if ($repository -notin $known) {
    throw "Unknown source: $repository. See interview-banks/sources.json."
  }
}
$userDirectory = if ($env:BAGU_USER_DATA_DIR) { $env:BAGU_USER_DATA_DIR } else { Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'BaguTrainer' }
$reposDir = Join-Path $userDirectory "interview-banks/repos"
New-Item -ItemType Directory -Force -Path $reposDir | Out-Null
$gitOptions = @()
if ($Proxy) {
  $gitOptions = @("-c", "http.proxy=$Proxy", "-c", "https.proxy=$Proxy")
}
$failed = @()
foreach ($repository in ($Repositories | Select-Object -Unique)) {
  $name = $repository.Split("/")[1]
  $dest = Join-Path $reposDir $name
  $expectedUrl = "https://github.com/$repository.git"
  if (Test-Path -LiteralPath $dest) {
    if (-not (Test-Path -LiteralPath (Join-Path $dest ".git"))) {
      throw "Existing directory is not a Git checkout: $dest"
    }
    $origin = & git -C $dest remote get-url origin
    if ($LASTEXITCODE -ne 0 -or $origin.TrimEnd('/') -ne $expectedUrl) {
      throw "Existing checkout has a different origin: $dest"
    }
    Write-Output "SKIP $repository (existing checkout; not updated)"
    continue
  }
  & git @gitOptions clone --depth 1 --single-branch $expectedUrl $dest
  if ($LASTEXITCODE -eq 0) {
    Write-Output "OK $repository"
  } else {
    $failed += $repository
  }
}
if ($failed.Count) {
  throw ("Download failed: " + ($failed -join ", "))
}
