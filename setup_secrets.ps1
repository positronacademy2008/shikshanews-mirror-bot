# Push secrets to shikshanews-mirror-bot from a local file (never commit secrets.local.env)
$ErrorActionPreference = "Stop"
$repo = "positronacademy2008/shikshanews-mirror-bot"
$secretsFile = Join-Path $PSScriptRoot "secrets.local.env"

if (-not (Test-Path $secretsFile)) {
    Write-Host "Create secrets.local.env in this folder with lines like:" -ForegroundColor Yellow
    Write-Host "  BOT_TOKEN=123456:ABC..."
    Write-Host "  WP_USER=admin"
    Write-Host "  WP_PASS=xxxx xxxx xxxx"
    Write-Host "  GROQ_API_KEY=gsk_..."
    Write-Host "  ADMIN_CHAT_ID=123456789"
    Write-Host "  WP_POST_TYPE=pages"
    Write-Host ""
    Write-Host "Copy values from: https://github.com/positronacademy2008/teletotele270526/settings/secrets/actions"
    Copy-Item (Join-Path $PSScriptRoot ".env.example") $secretsFile
    notepad $secretsFile
    Read-Host "Save secrets.local.env then press Enter"
}

Get-Content $secretsFile | ForEach-Object {
    if ($_ -match '^\s*#' -or $_ -notmatch '=') { return }
    $name, $value = $_ -split '=', 2
    $name = $name.Trim()
    $value = $value.Trim().Trim('"')
    if (-not $name -or $value -match 'your_|replace_with') { return }
    Write-Host "Setting $name ..." -ForegroundColor Gray
    $value | gh secret set $name --repo $repo
}

Write-Host "Secrets uploaded. Triggering test run..." -ForegroundColor Cyan
gh workflow run "Shiksha News Mirror Bot" --repo $repo --ref main -f clean_previous_run=true
Write-Host "Done. Watch: https://github.com/$repo/actions"