param(
    [Parameter(Mandatory)]
    [string] $BotAppId,
    [Parameter(Mandatory)]
    [string] $BotDomain
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$templatePath = Join-Path $projectRoot 'appManifest\manifest.template.json'
$outputDirectory = Join-Path $projectRoot 'appPackage'
$zipPath = Join-Path $outputDirectory 'ask-hr.zip'

if (Test-Path $outputDirectory) {
    Remove-Item $outputDirectory -Recurse -Force
}
New-Item -ItemType Directory -Path $outputDirectory | Out-Null

$manifest = (Get-Content $templatePath -Raw).
    Replace('${BOT_APP_ID}', $BotAppId).
    Replace('${BOT_DOMAIN}', $BotDomain)
Set-Content `
    -Path (Join-Path $outputDirectory 'manifest.json') `
    -Value $manifest `
    -Encoding utf8NoBOM `
    -NoNewline

Copy-Item `
    -LiteralPath (Join-Path $projectRoot 'appManifest\color.png') `
    -Destination (Join-Path $outputDirectory 'color.png')
Copy-Item `
    -LiteralPath (Join-Path $projectRoot 'appManifest\outline.png') `
    -Destination (Join-Path $outputDirectory 'outline.png')

Compress-Archive `
    -Path (Join-Path $outputDirectory 'manifest.json'),
          (Join-Path $outputDirectory 'color.png'),
          (Join-Path $outputDirectory 'outline.png') `
    -DestinationPath $zipPath `
    -Force

Write-Host "Microsoft 365 app package: $zipPath"
