<#
.SYNOPSIS
Export per-day ActivityWatch aggregates into monthly JSONL files inside a vault.

.DESCRIPTION
Reads window events limited to not-afk time from the local ActivityWatch server
and writes one JSON line per day and device to
<Vault>\data\activitywatch\<yyyy-MM>.jsonl. Only durations per application and
per hour are written; window titles and URLs never leave ActivityWatch.

The last -Days days (today included) are recomputed on every run, and a file is
rewritten only when its content changes. This computer is the only writer of
its own lines; lines from other devices are kept as they are.

Keep this file ASCII-only: Windows PowerShell 5.1 reads scripts without a BOM
in the system code page.

.EXAMPLE
powershell -NoProfile -ExecutionPolicy Bypass -File activitywatch-export.ps1 -Vault "D:\Obsidian\vault"
#>
param(
    [Parameter(Mandatory = $true)][string]$Vault,
    [int]$Days = 3,
    [string]$Server = 'http://localhost:5600',
    [string]$Device = $env:COMPUTERNAME,
    [string]$Folder = 'data\activitywatch',
    [int]$MinSeconds = 60,
    [int]$MaxApps = 30
)

Set-StrictMode -Version 2
$ErrorActionPreference = 'Stop'
$invariant = [System.Globalization.CultureInfo]::InvariantCulture
$utf8 = New-Object System.Text.UTF8Encoding($false)

if (-not (Test-Path -LiteralPath $Vault -PathType Container)) {
    Write-Error "Vault folder not found: $Vault"
    exit 1
}
if ($Days -lt 1 -or $Days -gt 60) {
    Write-Error 'Use -Days between 1 and 60'
    exit 1
}

function Get-EventList($node) {
    # The response is a list with one event list per time period; PowerShell
    # versions differ in how they unroll nested arrays, so walk it explicitly.
    if ($null -eq $node) { return }
    if ($node -is [System.Array]) {
        foreach ($item in $node) { Get-EventList $item }
        return
    }
    if ($node.PSObject.Properties['duration']) { $node }
}

function ConvertTo-Offset($value) {
    if ($value -is [System.DateTimeOffset]) { return $value }
    if ($value -is [System.DateTime]) { return [System.DateTimeOffset]$value }
    return [System.DateTimeOffset]::Parse([string]$value, $invariant)
}

function Get-DayRecord([System.DateTime]$day) {
    $start = [System.DateTimeOffset]$day
    $end = [System.DateTimeOffset]$day.AddDays(1)
    $period = $start.ToString('yyyy-MM-ddTHH:mm:sszzz', $invariant) + '/' + $end.ToString('yyyy-MM-ddTHH:mm:sszzz', $invariant)
    $body = @{
        timeperiods = @($period)
        query       = @(
            'events = flood(query_bucket(find_bucket("aw-watcher-window_")));',
            'not_afk = flood(query_bucket(find_bucket("aw-watcher-afk_")));',
            'not_afk = filter_keyvals(not_afk, "status", ["not-afk"]);',
            'events = filter_period_intersect(events, not_afk);',
            'RETURN = events;'
        )
    } | ConvertTo-Json -Depth 4 -Compress

    $client = New-Object System.Net.WebClient
    $client.Encoding = [System.Text.Encoding]::UTF8
    $client.Proxy = $null
    $client.Headers['Content-Type'] = 'application/json'
    try {
        $text = $client.UploadString($Server.TrimEnd('/') + '/api/0/query/', 'POST', $body)
    } finally {
        $client.Dispose()
    }

    $apps = @{}
    $hours = New-Object 'double[]' 24
    $total = 0.0
    foreach ($item in (Get-EventList (ConvertFrom-Json $text))) {
        $from = ConvertTo-Offset $item.timestamp
        $to = $from.AddSeconds([double]$item.duration)
        if ($from -lt $start) { $from = $start }
        if ($to -gt $end) { $to = $end }
        if ($to -le $from) { continue }
        $app = 'unknown'
        if ($item.PSObject.Properties['data'] -and $item.data -and $item.data.PSObject.Properties['app'] -and $item.data.app) {
            $app = [string]$item.data.app
        }
        $seconds = ($to - $from).TotalSeconds
        $total += $seconds
        if ($apps.ContainsKey($app)) { $apps[$app] += $seconds } else { $apps[$app] = $seconds }
        # Split the event over local clock hours.
        $cursor = $from
        while ($cursor -lt $to) {
            $index = [Math]::Min(23, [int][Math]::Floor(($cursor - $start).TotalHours))
            $boundary = $start.AddHours($index + 1)
            if ($index -eq 23 -or $boundary -gt $to) { $boundary = $to }
            $hours[$index] += ($boundary - $cursor).TotalSeconds
            $cursor = $boundary
        }
    }
    if ($total -lt 1) { return $null }

    $rows = @($apps.GetEnumerator() |
        Where-Object { $_.Value -ge $MinSeconds } |
        Sort-Object -Property @{ Expression = 'Value'; Descending = $true }, @{ Expression = 'Key' } |
        Select-Object -First $MaxApps |
        ForEach-Object { [ordered]@{ app = $_.Key; seconds = [int][Math]::Round($_.Value) } })

    $record = [ordered]@{
        date           = $day.ToString('yyyy-MM-dd', $invariant)
        device         = $Device
        source         = 'activitywatch'
        tz             = $start.ToString('zzz', $invariant)
        active_seconds = [int][Math]::Round($total)
        hours          = @($hours | ForEach-Object { [int][Math]::Round($_) })
        apps           = $rows
    }
    return ($record | ConvertTo-Json -Depth 5 -Compress)
}

function Get-LineKey([string]$line) {
    # Lines written by this script start with date and device, in that order.
    $match = [regex]::Match($line, '^\{"date":"(\d{4}-\d{2}-\d{2})","device":"((?:[^"\\]|\\.)*)"')
    if ($match.Success) { return $match.Groups[1].Value + [char]9 + $match.Groups[2].Value }
    return $null
}

$today = (Get-Date).Date
$fresh = @{}
try {
    for ($back = $Days - 1; $back -ge 0; $back--) {
        $day = $today.AddDays(-$back)
        $line = Get-DayRecord $day
        if ($null -eq $line) { continue }
        $month = $day.ToString('yyyy-MM', $invariant)
        if (-not $fresh.ContainsKey($month)) { $fresh[$month] = @{} }
        $fresh[$month][(Get-LineKey $line)] = $line
    }
} catch {
    Write-Error ("ActivityWatch query failed at $Server. Is ActivityWatch running? " + $_.Exception.Message)
    exit 1
}

$target = Join-Path $Vault $Folder
if (-not (Test-Path -LiteralPath $target)) { New-Item -ItemType Directory -Path $target -Force | Out-Null }

$written = 0
foreach ($month in ($fresh.Keys | Sort-Object)) {
    $file = Join-Path $target ($month + '.jsonl')
    $lines = @{}
    $unknown = @()
    $before = ''
    if (Test-Path -LiteralPath $file) {
        $before = [System.IO.File]::ReadAllText($file, $utf8)
        foreach ($line in ($before -split "`r?`n")) {
            if (-not $line.Trim()) { continue }
            $key = Get-LineKey $line
            if ($null -eq $key) { $unknown += $line } else { $lines[$key] = $line }
        }
    }
    foreach ($key in $fresh[$month].Keys) { $lines[$key] = $fresh[$month][$key] }
    $ordered = @($lines.Keys | Sort-Object | ForEach-Object { $lines[$_] }) + $unknown
    $after = ($ordered -join "`n") + "`n"
    if ($after -ceq $before) { continue }
    $temporary = $file + '.tmp'
    [System.IO.File]::WriteAllText($temporary, $after, $utf8)
    Move-Item -LiteralPath $temporary -Destination $file -Force
    $written++
}

Write-Output ("ActivityWatch export: {0} day(s) with data, {1} file(s) updated in {2}" -f (($fresh.Values | ForEach-Object { $_.Count } | Measure-Object -Sum).Sum + 0), $written, $target)
