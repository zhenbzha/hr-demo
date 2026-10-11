[CmdletBinding()]
param(
    [string] $PackagePath = (
        Join-Path $PSScriptRoot `
            '..\src\ask-hr\activityAppPackage\ask-hr-activity-poc.zip'
    ),
    [string] $TeamsCliPath
)

$ErrorActionPreference = 'Stop'
$env:TEAMS_NO_INTERACTIVE = '1'

function Invoke-Checked {
    param(
        [Parameter(Mandatory)][scriptblock] $Script,
        [Parameter(Mandatory)][string] $Description
    )

    $output = & $Script
    if ($LASTEXITCODE -ne 0) {
        throw "$Description failed with exit code $LASTEXITCODE."
    }
    return $output
}

if (-not (Test-Path -LiteralPath $PackagePath)) {
    throw "Teams package not found: $PackagePath"
}

if (-not $TeamsCliPath) {
    $toolRoot = Join-Path $env:LOCALAPPDATA 'AskHr\teams-cli'
    $TeamsCliPath = Join-Path $toolRoot 'node_modules\.bin\teams.cmd'
    if (-not (Test-Path -LiteralPath $TeamsCliPath)) {
        New-Item -ItemType Directory -Path $toolRoot -Force | Out-Null
        Invoke-Checked {
            pnpm --dir $toolRoot add '@microsoft/teams.cli@latest'
        } 'Install the isolated Teams CLI'
        Invoke-Checked {
            pnpm --dir $toolRoot rebuild '@azure/msal-node-extensions' keytar
        } 'Enable Teams CLI credential storage'
    }
}

if (-not (Test-Path -LiteralPath $TeamsCliPath)) {
    throw "Teams CLI not found: $TeamsCliPath"
}

$status = (
    Invoke-Checked {
        & $TeamsCliPath status --json
    } 'Read Teams CLI authentication status'
) | ConvertFrom-Json
if (-not $status.loggedIn) {
    throw (
        "Teams CLI authentication is required. Run: " +
        "& '$TeamsCliPath' login --device-code"
    )
}

Add-Type -AssemblyName System.IO.Compression.FileSystem
$archive = [IO.Compression.ZipFile]::OpenRead(
    (Resolve-Path -LiteralPath $PackagePath)
)
try {
    $manifestEntry = $archive.GetEntry('manifest.json')
    if (-not $manifestEntry) {
        throw 'The Teams package does not contain manifest.json.'
    }
    $reader = [IO.StreamReader]::new($manifestEntry.Open())
    try {
        $manifest = $reader.ReadToEnd() | ConvertFrom-Json
    }
    finally {
        $reader.Dispose()
    }
}
finally {
    $archive.Dispose()
}
$appId = $manifest.id
$version = $manifest.version
if (-not $appId -or -not $version) {
    throw 'The Teams manifest must contain id and version values.'
}

$teamsCliRoot = Split-Path (
    Split-Path (
        Split-Path $TeamsCliPath -Parent
    ) -Parent
) -Parent
$importScript = @'
import fs from "node:fs";
import { pathToFileURL } from "node:url";
const root = process.argv[1];
const zipPath = process.argv[2];
const auth = await import(pathToFileURL(
  root + "/node_modules/@microsoft/teams.cli/dist/auth/index.js"
));
const tdp = await import(pathToFileURL(
  root + "/node_modules/@microsoft/teams.cli/dist/apps/tdp.js"
));
const account = await auth.getAccount();
if (!account) throw new Error("Teams CLI authentication is required");
const token = await auth.getTokenSilent(auth.teamsDevPortalScopes);
if (!token) throw new Error("Unable to acquire a Teams Developer Portal token");
await tdp.importAppPackage(token, fs.readFileSync(zipPath), true);
'@
Invoke-Checked {
    node --input-type=module -e `
        $importScript `
        $teamsCliRoot `
        (Resolve-Path -LiteralPath $PackagePath)
} 'Upload the complete Teams app package' | Out-Null

$remote = (
    Invoke-Checked {
        & $TeamsCliPath app get $appId --json
    } 'Verify the Teams app'
) | ConvertFrom-Json
if ($remote.version -ne $version) {
    throw (
        "Teams app version mismatch. Expected $version, " +
        "received $($remote.version)."
    )
}

[pscustomobject]@{
    appId = $remote.appId
    name = $remote.name
    version = $remote.version
    installLink = $remote.installLink
} | ConvertTo-Json -Compress
