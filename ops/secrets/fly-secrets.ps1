<#
.SYNOPSIS
Keep Fly secrets and Bitwarden in step without ever printing a value. See README.md next to this file.

.EXAMPLE
$env:BW_SESSION = bw unlock --raw
.\fly-secrets.ps1 -Action Capture -App interviewer-ms     # live machine -> Bitwarden note fly/interviewer-ms
.\fly-secrets.ps1 -Action Check   -App interviewer-ms     # names: Fly vs Bitwarden
.\fly-secrets.ps1 -Action Verify  -App interviewer-ms     # values: Bitwarden vs live machine (match / DIFFERENT)
.\fly-secrets.ps1 -Action Pairs   -Env prod               # values that must match across apps
.\fly-secrets.ps1 -Action Push    -App interviewer-ms     # Bitwarden -> fly secrets import --stage
#>
param(
    [Parameter(Mandatory = $true)][ValidateSet('Capture', 'Check', 'Verify', 'Pairs', 'Push')][string]$Action,
    [string]$App,
    [ValidateSet('prod', 'staging')][string]$Env = 'prod'
)

$ErrorActionPreference = 'Stop'
$utf8 = New-Object System.Text.UTF8Encoding($false)
$OutputEncoding = $utf8                 # what PowerShell pipes into bw / fly
[Console]::OutputEncoding = $utf8       # what it reads back from them

# ----------------------------------------------------------------------------- helpers

function Get-FolderName([string]$app) { if ($app -like '*-staging') { 'RTJ Staging' } else { 'RTJ Production' } }
function Get-NoteName([string]$app) { "fly/$app" }

# Windows PowerShell 5.1 does not enumerate a JSON array from ConvertFrom-Json, so always loop with foreach.
function Read-Json($lines) { ($lines -join "`n") | ConvertFrom-Json }

function Assert-Unlocked {
    if (-not (Get-Command bw -ErrorAction SilentlyContinue)) { throw 'Bitwarden CLI not found. Install it: winget install Bitwarden.CLI' }
    $status = (Read-Json (bw status)).status
    if ($status -ne 'unlocked') { throw "Bitwarden is $status. Run: `$env:BW_SESSION = bw unlock --raw" }
    bw sync | Out-Null
}

function Get-Note([string]$app) {
    $name = Get-NoteName $app
    $found = @()
    foreach ($item in (Read-Json (bw list items --search $name))) { if ($item.name -ceq $name) { $found += $item } }
    if ($found.Count -gt 1) { throw "More than one Bitwarden item is named '$name'. Keep only one." }
    if ($found.Count -eq 0) { return $null }
    $found[0]
}

function Get-RequiredNote([string]$app) {
    $note = Get-Note $app
    if (-not $note) { throw "No Bitwarden note '$(Get-NoteName $app)'. Run: .\fly-secrets.ps1 -Action Capture -App $app" }
    $note
}

function Get-NoteValues($note) {
    $map = @{}
    foreach ($field in @($note.fields)) { if ($field.name) { $map[$field.name] = [string]$field.value } }
    $map
}

function Get-FlyNames([string]$app) {
    $raw = fly secrets list -a $app --json
    if ($LASTEXITCODE -ne 0) { throw "fly secrets list failed for $app" }
    $names = @()
    foreach ($secret in (Read-Json $raw)) { $names += [string]$secret.Name }
    $names | Sort-Object
}

# The environment of one started machine of the app (secrets included). Held in memory only.
function Get-MachineEnv([string]$app) {
    $machines = Read-Json (fly machine list -a $app --json)
    $pick = $null
    foreach ($m in $machines) { if (-not $pick -and $m.state -eq 'started' -and $m.config.metadata.fly_process_group -eq 'app') { $pick = $m } }
    foreach ($m in $machines) { if (-not $pick -and $m.state -eq 'started') { $pick = $m } }
    if (-not $pick) { throw "No started machine in $app. Start one first: fly machine list -a $app, then fly machine start <id> -a $app" }
    Write-Host "Reading the environment of machine $($pick.id) ($app)..."
    $lines = fly ssh console -a $app --machine $pick.id -C printenv
    if ($LASTEXITCODE -ne 0) { throw "fly ssh failed for $app" }
    $map = @{}
    foreach ($line in $lines) {
        $i = $line.IndexOf('=')
        if ($i -gt 0) { $map[$line.Substring(0, $i)] = $line.Substring($i + 1) }
    }
    $map
}

function Save-Note([string]$app, [hashtable]$values) {
    $folderName = Get-FolderName $app
    $folder = $null
    foreach ($f in (Read-Json (bw list folders --search $folderName))) { if ($f.name -ceq $folderName) { $folder = $f } }
    if (-not $folder) {
        $encoded = (ConvertTo-Json -InputObject @{ name = $folderName } -Compress) | bw encode
        $folder = Read-Json (bw create folder $encoded)
        Write-Host "Created Bitwarden folder '$folderName'."
    }
    $fields = @()
    foreach ($k in ($values.Keys | Sort-Object)) { $fields += @{ name = $k; value = $values[$k]; type = 1 } }   # 1 = hidden
    $note = Get-Note $app
    if ($note) {
        $note | Add-Member -NotePropertyName fields -NotePropertyValue $fields -Force
        $note | Add-Member -NotePropertyName folderId -NotePropertyValue $folder.id -Force
        $encoded = (ConvertTo-Json -InputObject $note -Depth 10 -Compress) | bw encode
        bw edit item $note.id $encoded | Out-Null
        Write-Host "Updated Bitwarden note '$(Get-NoteName $app)' in '$folderName' ($($fields.Count) fields)."
    } else {
        $item = @{
            type       = 2                      # secure note
            secureNote = @{ type = 0 }
            name       = Get-NoteName $app
            folderId   = $folder.id
            notes      = "Fly secrets of $app. One hidden field per secret; field name = variable name. Managed with ops/secrets/fly-secrets.ps1 (see ops/secrets/README.md)."
            fields     = $fields
        }
        $encoded = (ConvertTo-Json -InputObject $item -Depth 10 -Compress) | bw encode
        bw create item $encoded | Out-Null
        Write-Host "Created Bitwarden note '$(Get-NoteName $app)' in '$folderName' ($($fields.Count) fields)."
    }
}

function Write-Result([string]$label, $ok) {
    if ($null -eq $ok) { Write-Host ("  {0,-52} missing" -f $label) -ForegroundColor Yellow }
    elseif ($ok) { Write-Host ("  {0,-52} match" -f $label) -ForegroundColor Green }
    else { Write-Host ("  {0,-52} DIFFERENT" -f $label) -ForegroundColor Red }
}

function Test-Equal([string]$a, [string]$b) {
    if ([string]::IsNullOrEmpty($a) -or [string]::IsNullOrEmpty($b)) { return $null }
    $a -ceq $b
}

function Test-ApiKeyPair([string]$apiKeys, [string]$key) {
    if ([string]::IsNullOrEmpty($apiKeys) -or [string]::IsNullOrEmpty($key)) { return $null }
    foreach ($entry in $apiKeys.Split(',')) {
        $parts = $entry.Trim().Split(':')
        if ($parts[$parts.Count - 1] -ceq $key) { return $true }   # entries are "tenant:key" or "key"
    }
    $false
}

function Test-CallbackHost([string]$allowed, [string]$callbackUrl) {
    if ([string]::IsNullOrEmpty($allowed) -or [string]::IsNullOrEmpty($callbackUrl)) { return $null }
    $hostName = ([uri]$callbackUrl).Host
    foreach ($h in $allowed.Split(',')) { if ($h.Trim() -eq $hostName) { return $true } }
    $false
}

# ----------------------------------------------------------------------------- actions

if ($Action -ne 'Pairs' -and -not $App) { throw "-App is required for $Action (for example -App interviewer-ms)." }
Assert-Unlocked

switch ($Action) {
    'Capture' {
        $names = Get-FlyNames $App
        $machineEnv = Get-MachineEnv $App
        $values = @{}
        $skipped = @()
        foreach ($n in $names) {
            if ($machineEnv.ContainsKey($n)) { $values[$n] = $machineEnv[$n] } else { $skipped += $n }
        }
        if ($skipped.Count) { Write-Warning "Not on the running machine (staged but not deployed?), not captured: $($skipped -join ', ')" }
        Save-Note $App $values
    }

    'Check' {
        $flyNames = Get-FlyNames $App
        $bwNames = @((Get-NoteValues (Get-RequiredNote $App)).Keys | Sort-Object)
        $onlyFly = @($flyNames | Where-Object { $bwNames -notcontains $_ })
        $onlyBw = @($bwNames | Where-Object { $flyNames -notcontains $_ })
        Write-Host "$App : $($flyNames.Count) in Fly, $($bwNames.Count) in Bitwarden"
        if ($onlyFly.Count) { Write-Host "  In Fly, missing from Bitwarden: $($onlyFly -join ', ')" -ForegroundColor Yellow }
        if ($onlyBw.Count) { Write-Host "  In Bitwarden, not in Fly:       $($onlyBw -join ', ')" -ForegroundColor Yellow }
        if (-not $onlyFly.Count -and -not $onlyBw.Count) { Write-Host '  Names match.' -ForegroundColor Green }
    }

    'Verify' {
        $bw = Get-NoteValues (Get-RequiredNote $App)
        $machineEnv = Get-MachineEnv $App
        Write-Host "$App : Bitwarden vs running machine"
        foreach ($n in ($bw.Keys | Sort-Object)) {
            if ($machineEnv.ContainsKey($n)) { Write-Result $n ($bw[$n] -ceq $machineEnv[$n]) } else { Write-Result $n $null }
        }
        Write-Host '  (A staged secret that is not deployed yet shows as DIFFERENT until the next deploy.)'
    }

    'Pairs' {
        $suffix = ''
        if ($Env -eq 'staging') { $suffix = '-staging' }
        $iv = Get-NoteValues (Get-RequiredNote "interviewer-ms$suffix")
        $api = Get-NoteValues (Get-RequiredNote "resumetojob-api$suffix")
        Write-Host "Pairs ($Env), from Bitwarden"
        Write-Result 'Interviewer API key (API_KEYS / RTJ_INTERVIEWER_API_KEY)' (Test-ApiKeyPair $iv['API_KEYS'] $api['RTJ_INTERVIEWER_API_KEY'])
        Write-Result 'Webhook secret (WEBHOOK_SECRET / RTJ_INTERVIEWER_...)' (Test-Equal $iv['WEBHOOK_SECRET'] $api['RTJ_INTERVIEWER_WEBHOOK_SECRET'])
        Write-Result 'Callback host allowed (CALLBACK_ALLOWED_HOSTS)' (Test-CallbackHost $iv['CALLBACK_ALLOWED_HOSTS'] $api['RTJ_INTERVIEWER_CALLBACK_URL'])
        if ($Env -eq 'prod') {
            $tl = Get-NoteValues (Get-RequiredNote 'resumetojob-tailoring')
            Write-Result 'Tailoring token (TAILORING_JWT_SECRET)' (Test-Equal $tl['TAILORING_JWT_SECRET'] $api['RTJ_TAILORING_JWT_SECRET'])
        }
        Write-Host '  Not checked here (one side is outside Fly): the Stripe webhook signing secret and the Supabase role passwords.'
    }

    'Push' {
        $values = Get-NoteValues (Get-RequiredNote $App)
        $lines = @()
        foreach ($k in ($values.Keys | Sort-Object)) {
            if ([string]::IsNullOrEmpty($values[$k])) { Write-Warning "Skipping $k : empty in Bitwarden"; continue }
            $lines += "$k=$($values[$k])"
        }
        if ($App -notlike '*-staging') {
            $answer = Read-Host "This stages $($lines.Count) secrets on PRODUCTION app '$App'. Type the app name to continue"
            if ($answer -cne $App) { Write-Host 'Cancelled.'; return }
        }
        ($lines -join "`n") | fly secrets import --stage -a $App
        if ($LASTEXITCODE -ne 0) { throw "fly secrets import failed for $App" }
        Write-Host "Staged $($lines.Count) secrets on $App. They apply on the next deploy, or now with: fly secrets deploy -a $App"
    }
}
