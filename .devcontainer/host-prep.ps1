<#
.SYNOPSIS
  Runs on the HOST before the dev container is built (devcontainer.json initializeCommand).

.DESCRIPTION
  Makes sure every path that devcontainer.json bind-mounts exists, so Docker never
  turns a missing file into a directory, and seeds .devcontainer/.env from env.example
  on first use. Idempotent. Creates only what is missing; never reads, modifies or
  prints the contents of any config file.

  Windows: invoked automatically with Windows PowerShell 5.1.
  macOS/Linux: run once by hand ->  pwsh -File .devcontainer/host-prep.ps1
#>
[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'

$home_ = if ($env:USERPROFILE) { $env:USERPROFILE } else { $env:HOME }
if (-not $home_) { throw "Cannot determine the home directory (USERPROFILE/HOME not set)." }

# devcontainer.json builds mount paths as ${localEnv:HOME}${localEnv:USERPROFILE}, which
# only works when exactly one of the two is set (normal on Windows and on mac/linux).
# Launching VS Code / the devcontainer CLI from Git Bash or MSYS sets HOME as well and the
# paths double up - fail here with a clear message instead of a cryptic docker error.
if ($env:USERPROFILE -and $env:HOME) {
    throw "Both USERPROFILE and HOME are set (HOME='$($env:HOME)'). Launch VS Code from PowerShell / the Start menu, or run: `$env:HOME=''; code .  (Git Bash: env -u HOME code .)"
}

$created = @()

# Directories mounted into /home/vscode/...
$dirs = @('.aws', '.azure', '.claude', '.gemini',
          (Join-Path '.config' 'gh'), (Join-Path '.config' 'glab-cli'),
          (Join-Path '.config' 'gcloud'), (Join-Path '.config' 'op'))
foreach ($d in $dirs) {
    $p = Join-Path $home_ $d
    if (-not (Test-Path -LiteralPath $p)) {
        New-Item -ItemType Directory -Path $p -Force | Out-Null
        $created += $d
    }
}

# Files mounted into /home/vscode/... (a missing file would be created as a directory)
$cfg = Join-Path $home_ '.databrickscfg'
if (-not (Test-Path -LiteralPath $cfg)) {
    New-Item -ItemType File -Path $cfg -Force | Out-Null
    $created += '.databrickscfg (empty)'
}
elseif ((Get-Item -LiteralPath $cfg).PSIsContainer) {
    throw "$cfg is a directory (left over from an earlier bind mount). Remove it and re-run."
}

# The single keys file
$devDir  = $PSScriptRoot
$envFile = Join-Path $devDir '.env'
$example = Join-Path $devDir 'env.example'
if (-not (Test-Path -LiteralPath $envFile)) {
    Copy-Item -LiteralPath $example -Destination $envFile
    $created += '.devcontainer/.env (from env.example - fill it in, see SECRETS.md)'
}

if ($created.Count -gt 0) {
    Write-Host "[host-prep] created: $($created -join ', ')"
} else {
    Write-Host "[host-prep] all mount paths and .devcontainer/.env present"
}
