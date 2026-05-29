# Zip users.db only for PRIVATE hosting (USERS_DB_URL). Do not upload to public GitHub Release.
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$data = Join-Path $root "data"
$db = Join-Path $data "users.db"
$out = Join-Path $root "relationship-expert-users.zip"

if (-not (Test-Path $db)) {
    Write-Error "No data/users.db — register a user locally or copy from server first."
    exit 1
}

if (Test-Path $out) { Remove-Item $out -Force }
Compress-Archive -Path $db -DestinationPath $out -Force
Write-Host "Created $out"
Write-Host "Upload to PRIVATE storage (not public Release). Set USERS_DB_URL in .env to the HTTPS download URL, then deploy."
