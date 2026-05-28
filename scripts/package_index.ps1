# Zip local data/ for hosting (e.g. private object storage). Do not commit the zip to a public repo.
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$data = Join-Path $root "data"
$out = Join-Path $root "relationship-expert-index.zip"
if (-not (Test-Path (Join-Path $data "chunks.jsonl"))) {
    Write-Error "No index at $data — build index locally first."
    exit 1
}
if (Test-Path $out) { Remove-Item $out -Force }
$items = Get-ChildItem (Join-Path $data "*")
Compress-Archive -Path $items.FullName -DestinationPath $out -Force
if (Test-Path (Join-Path $data "users.db")) {
    Write-Host "Included users.db in archive (per-account chat + logins)."
}
Write-Host "Created $out — upload and set INDEX_BUNDLE_URL in deploy env_vars."
