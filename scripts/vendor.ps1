# Vendor the CHECKER subset into swarm_mcp/vendored/ at the pinned SHAs and
# record per-file sha256 hashes in .github/pins.json. Generalized from
# trading-swarm-alpha's scripts/vendor_guardrails.ps1: pins guardrails AND
# bars_fetch.py / order_checks.py / gym/ / shared/swing_screens.py.
#
# Usage:
#   scripts\vendor.ps1                              # uses .github/pins.json SHAs
#   scripts\vendor.ps1 -GuardrailsSha <sha> -AlphaSha <sha>
#   scripts\vendor.ps1 -FromWorktree                # pre-commit bootstrap:
#                                                   # vendors the sibling working
#                                                   # trees as-is (pins marked
#                                                   # worktree_pin=true)
#
# Requires sibling checkouts ..\trading-swarm-guardrails and ..\trading-swarm-alpha.
# Vendored files ARE committed here (uvx installs must be self-contained —
# users have no access to the private repos); CI verifies the tree matches pins.
param(
    [string]$GuardrailsSha = "",
    [string]$AlphaSha = "",
    [string]$VolpredSha = "",
    [switch]$FromWorktree
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
$pinsPath = Join-Path $repoRoot ".github\pins.json"
$guardrailsRepo = Join-Path (Split-Path -Parent $repoRoot) "trading-swarm-guardrails"
$alphaRepo = Join-Path (Split-Path -Parent $repoRoot) "trading-swarm-alpha"
$volpredRepo = Join-Path (Split-Path -Parent $repoRoot) "trade_bot_volume_predictor"

if ((-not $GuardrailsSha -or -not $AlphaSha -or -not $VolpredSha) -and (Test-Path $pinsPath) -and -not $FromWorktree) {
    $pins = Get-Content $pinsPath -Raw | ConvertFrom-Json
    if (-not $GuardrailsSha) { $GuardrailsSha = $pins.guardrails.sha }
    if (-not $AlphaSha) { $AlphaSha = $pins.alpha.sha }
    if (-not $VolpredSha) { $VolpredSha = $pins.volpred.sha }
}

function Assert-Sha([string]$sha, [string]$what) {
    if ($FromWorktree) { return }
    if (-not ($sha -match '^[0-9a-f]{40}$')) {
        throw "no valid 40-char SHA for $what (pass -GuardrailsSha/-AlphaSha/-VolpredSha or populate .github/pins.json)"
    }
}
Assert-Sha $GuardrailsSha "guardrails"
Assert-Sha $AlphaSha "alpha"
Assert-Sha $VolpredSha "volpred"

if (-not (Test-Path $guardrailsRepo)) { throw "sibling guardrails checkout not found at $guardrailsRepo" }
if (-not (Test-Path $alphaRepo)) { throw "sibling alpha checkout not found at $alphaRepo" }
if (-not (Test-Path $volpredRepo)) { throw "sibling volume predictor checkout not found at $volpredRepo" }

if (-not $FromWorktree) {
    Push-Location $guardrailsRepo
    try {
        git fetch --quiet
        git checkout --quiet $GuardrailsSha
        if ($LASTEXITCODE -ne 0) { throw "guardrails checkout at $GuardrailsSha failed" }
    } finally { Pop-Location }

    Push-Location $alphaRepo
    try {
        git fetch --quiet
        git checkout --quiet $AlphaSha
        if ($LASTEXITCODE -ne 0) { throw "alpha checkout at $AlphaSha failed" }
    } finally { Pop-Location }

    Push-Location $volpredRepo
    try {
        git fetch --quiet
        git checkout --quiet $VolpredSha
        if ($LASTEXITCODE -ne 0) { throw "volume predictor checkout at $VolpredSha failed" }
    } finally { Pop-Location }
}

if ($FromWorktree) {
    Push-Location $guardrailsRepo
    $GuardrailsSha = (git rev-parse HEAD)
    Pop-Location
    Push-Location $alphaRepo
    $AlphaSha = (git rev-parse HEAD)
    Pop-Location
    Push-Location $volpredRepo
    $VolpredSha = (git rev-parse HEAD)
    Pop-Location
    Write-Warning "-FromWorktree: pinning the sibling WORKING TREES at $GuardrailsSha / $AlphaSha / $VolpredSha (uncommitted state included)."
}

$vendorArgs = @(
    (Join-Path $PSScriptRoot "perform_vendor.py"),
    "--guardrails-src", $guardrailsRepo,
    "--alpha-src", $alphaRepo,
    "--volpred-src", $volpredRepo,
    "--guardrails-sha", $GuardrailsSha,
    "--alpha-sha", $AlphaSha,
    "--volpred-sha", $VolpredSha
)
if ($FromWorktree) { $vendorArgs += "--worktree" }
py -3.11 @vendorArgs
if ($LASTEXITCODE -ne 0) { throw "perform_vendor.py failed" }
