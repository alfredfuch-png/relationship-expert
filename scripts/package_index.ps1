# Zip knowledge-base index only (no users.db). For public GitHub Release / INDEX_BUNDLE_URL.
# Usage:
#   .\scripts\package_index.ps1              # legacy root data/ → relationship-expert-index.zip
#   .\scripts\package_index.ps1 -ExpertId afu  # data/experts/afu → relationship-expert-index-afu.zip
param(
    [string]$ExpertId = ""
)

$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$dataRoot = Join-Path $root "data"
$indexFiles = @("chunks.jsonl", "embeddings.npz", "tag_embeddings.npz", "index_meta.json")

if ($ExpertId) {
    $data = Join-Path $dataRoot "experts\$ExpertId"
    $out = Join-Path $root "relationship-expert-index-$ExpertId.zip"
} else {
    $data = $dataRoot
    $out = Join-Path $root "relationship-expert-index.zip"
}

if (-not (Test-Path (Join-Path $data "chunks.jsonl"))) {
    Write-Error "No index at $data — build index locally first."
    exit 1
}

$toPack = @()
foreach ($name in $indexFiles) {
    $p = Join-Path $data $name
    if (Test-Path $p) { $toPack += $p }
}
if ($toPack.Count -eq 0) {
    Write-Error "No index files found under $data"
    exit 1
}

if (Test-Path $out) { Remove-Item $out -Force }
Compress-Archive -Path $toPack -DestinationPath $out -Force
Write-Host "Created $out (knowledge index only; users.db excluded)."
if ($ExpertId) {
    Write-Host "Set INDEX_BUNDLE_URLS to include $ExpertId=<public-zip-url> on deploy."
} else {
    Write-Host "Upload to public Release, then set INDEX_BUNDLE_URL in deploy-config / deploy."
}
if (Test-Path (Join-Path $dataRoot "users.db")) {
    Write-Host "Note: data/users.db exists — back it up with scripts/package_users_db.ps1 and USERS_DB_URL (private)."
}
