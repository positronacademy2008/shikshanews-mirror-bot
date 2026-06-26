# Copy GitHub Actions secrets from teletotele270526 to shikshanews-mirror-bot
# Run once: you must paste secret values when gh prompts (GitHub does not expose stored values).
$ErrorActionPreference = "Stop"
$names = @(
    "BOT_TOKEN", "WP_USER", "WP_PASS", "GROQ_API_KEY", "WP_POST_TYPE", "ADMIN_CHAT_ID"
)
$target = "positronacademy2008/shikshanews-mirror-bot"
Write-Host "Setting secrets on $target" -ForegroundColor Cyan
Write-Host "GitHub will prompt for each value — copy from teletotele270526 secrets page:"
Write-Host "https://github.com/positronacademy2008/teletotele270526/settings/secrets/actions"
foreach ($name in $names) {
    Write-Host ""
    Write-Host "Secret: $name" -ForegroundColor Yellow
    gh secret set $name --repo $target
}
Write-Host ""
Write-Host "Done. Trigger workflow:"
Write-Host "gh workflow run `"Shiksha News Mirror Bot`" --repo $target --ref main"