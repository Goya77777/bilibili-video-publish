param(
    [Parameter(Mandatory = $true)]
    [string]$ManifestPath,

    [string]$AuthPath = (Join-Path $env:USERPROFILE ".codex\secrets\bilibili-sanlian-ads-auth.json"),
    [string]$OutputPath = "",
    [string]$StageDir = "",
    [int]$PollIntervalSeconds = 15,
    [int]$MaxPollMinutes = 60,
    [switch]$VerifyOnly
)

$ErrorActionPreference = "Stop"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

function Write-Utf8([string]$Path, [string]$Text) {
    [IO.File]::WriteAllText($Path, $Text, [Text.UTF8Encoding]::new($false))
}

function Write-JsonAtomic([string]$Path, $Value) {
    $directory = Split-Path -Parent $Path
    if (-not (Test-Path -LiteralPath $directory)) {
        New-Item -ItemType Directory -Path $directory | Out-Null
    }
    $tempPath = "$Path.tmp.$PID"
    Write-Utf8 $tempPath ($Value | ConvertTo-Json -Depth 30)
    Move-Item -LiteralPath $tempPath -Destination $Path -Force
}

function Get-StagedCopy([string]$SourcePath, [string]$StageRoot) {
    # HARDENED (2026-09-22): Invoke-WebRequest/-RestMethod -InFile against a UNC path
    # can fail with System.UnauthorizedAccessException before any byte is sent.
    # Stage a local copy, verify its size, and upload from the local path.
    if (-not (Test-Path -LiteralPath $StageRoot)) {
        New-Item -ItemType Directory -Force -Path $StageRoot | Out-Null
    }
    $target = Join-Path $StageRoot ([IO.Path]::GetFileName($SourcePath))
    if (Test-Path -LiteralPath $target) { [System.IO.File]::Delete($target) }
    Copy-Item -LiteralPath $SourcePath -Destination $target -Force
    $sourceLength = (Get-Item -LiteralPath $SourcePath).Length
    $stagedLength = (Get-Item -LiteralPath $target).Length
    if ($sourceLength -ne $stagedLength) {
        throw "Staged copy size mismatch for $SourcePath ($sourceLength -> $stagedLength)"
    }
    return $target
}

function Get-AccountIdsFromRecord($Record) {
    $ids = New-Object System.Collections.Generic.List[string]
    foreach ($id in @($Record.selected_account_ids)) {
        if (-not [string]::IsNullOrWhiteSpace([string]$id)) { $ids.Add([string]$id) }
    }
    foreach ($account in @($Record.advertiser_accounts)) {
        foreach ($name in @("account_id", "id", "advertiser_id")) {
            if ($null -ne $account.$name -and -not [string]::IsNullOrWhiteSpace([string]$account.$name)) {
                $ids.Add([string]$account.$name)
            }
        }
    }
    return @($ids | Select-Object -Unique)
}

function New-QueryUri([string]$BaseUri, [hashtable]$Params) {
    $pairs = foreach ($key in $Params.Keys) {
        "{0}={1}" -f [Uri]::EscapeDataString([string]$key), [Uri]::EscapeDataString([string]$Params[$key])
    }
    return "$BaseUri`?$($pairs -join '&')"
}

function Repair-Utf8Mojibake([string]$Text) {
    if ([string]::IsNullOrEmpty($Text)) { return $Text }
    $characters = $Text.ToCharArray()
    if (($characters | Where-Object { [int]$_ -gt 255 }).Count -gt 0) { return $Text }
    if ($Text -notmatch '[\x80-\xFF]') { return $Text }

    $bytes = [Text.Encoding]::GetEncoding(28591).GetBytes($Text)
    $repaired = [Text.Encoding]::UTF8.GetString($bytes)
    if ($repaired.Contains([char]0xfffd)) { return $Text }
    return $repaired
}

function Invoke-MapiJson {
    param(
        [Parameter(Mandatory = $true)][string]$Method,
        [Parameter(Mandatory = $true)][string]$Uri,
        [Parameter(Mandatory = $true)][hashtable]$Headers,
        $Body = $null
    )

    $isWrite = ($Method -ne "GET")
    $waits = @(3, 5, 10)
    for ($attempt = 0; $attempt -le $waits.Count; $attempt++) {
        try {
            if ($null -eq $Body) {
                return Invoke-RestMethod -Method $Method -Uri $Uri -Headers $Headers -TimeoutSec 60
            }
            $json = $Body | ConvertTo-Json -Depth 20 -Compress
            $bytes = [Text.Encoding]::UTF8.GetBytes($json)
            return Invoke-RestMethod -Method $Method -Uri $Uri -Headers $Headers -ContentType "application/json; charset=utf-8" -Body $bytes -TimeoutSec 60
        } catch {
            if ($isWrite) {
                throw "Write request outcome unknown; failing closed without retry. $($_.Exception.Message)"
            }
            if ($attempt -ge $waits.Count) { throw }
            Start-Sleep -Seconds $waits[$attempt]
        }
    }
}

function Refresh-Authorization($Store, $Record) {
    # HARDENED: a cached refresh_token_expires_at is not authoritative evidence that
    # the token is dead. Attempt the refresh and let the server decide. This endpoint
    # rotates nothing (the refresh_token is reused), so a second attempt after a lost
    # response is safe.
    $form = @{
        grant_type = "refresh_token"
        refresh_token = [string]$Record.refresh_token
    }
    $response = $null
    try {
        $response = Invoke-RestMethod -Method POST -Uri "https://cm.bilibili.com/takumi/api/open_api/oauth2/token" -Headers @{ "X-Call-Source" = "sanlian-skills" } -ContentType "application/x-www-form-urlencoded" -Body $form -TimeoutSec 60
    } catch {
        $response = $null
    }
    if ($null -eq $response -or [string]$response.code -notin @("0", "200")) {
        $form = @{
            grant_type = "refresh_token"
            refresh_token = [string]$Record.refresh_token
            client_id = [string]$Record.client_id
            client_secret = [string]$Record.client_secret
        }
        try {
            $response = Invoke-RestMethod -Method POST -Uri "https://cm.bilibili.com/takumi/api/open_api/oauth2/token" -Headers @{ "X-Call-Source" = "sanlian-skills" } -ContentType "application/x-www-form-urlencoded" -Body $form -TimeoutSec 60
        } catch {
            $response = $null
        }
    }
    $data = $response.data
    if ($null -eq $data) { $data = $response.result }
    if ([string]$response.code -notin @("0", "200") -or [string]::IsNullOrWhiteSpace([string]$data.access_token)) {
        throw "Token refresh failed."
    }

    $now = [DateTimeOffset]::UtcNow
    $Record.access_token = [string]$data.access_token
    if (-not [string]::IsNullOrWhiteSpace([string]$data.refresh_token)) {
        $Record.refresh_token = [string]$data.refresh_token
    }
    $Record.access_token_expires_at = $now.AddSeconds([double]$data.expires_in).ToString("o")
    if ($null -ne $data.refresh_expires_in) {
        $Record.refresh_token_expires_at = $now.AddSeconds([double]$data.refresh_expires_in).ToString("o")
    }
    $Record.updated_at = $now.ToString("o")
    $Store.updated_at = $now.ToString("o")
    Write-JsonAtomic $AuthPath $Store
}

function Invoke-SignedUpload([string]$Uri, [string]$Path) {
    $waits = @(5, 10, 20, 40, 60)
    $lastError = $null
    for ($attempt = 0; $attempt -le $waits.Count; $attempt++) {
        try {
            Invoke-WebRequest -Method PUT -Uri $Uri -ContentType "video/mp4" -InFile $Path -TimeoutSec 900 -UseBasicParsing | Out-Null
            return
        } catch {
            $lastError = $_.Exception
            if ($attempt -ge $waits.Count) {
                throw "Signed upload failed for $Path :: $($lastError.GetType().FullName) :: $($lastError.Message)"
            }
            Start-Sleep -Seconds $waits[$attempt]
        }
    }
}

function Save-Run($Run) {
    $Run.updated_at = [DateTimeOffset]::Now.ToString("o")
    Write-JsonAtomic $OutputPath $Run
}

$manifest = Get-Content -Raw -Encoding UTF8 -LiteralPath $ManifestPath | ConvertFrom-Json
$ManifestPath = (Resolve-Path -LiteralPath $ManifestPath).Path
if ([string]::IsNullOrWhiteSpace($OutputPath)) {
    $manifestDirectory = Split-Path -Parent $ManifestPath
    $manifestName = [IO.Path]::GetFileNameWithoutExtension($ManifestPath)
    $OutputPath = Join-Path $manifestDirectory "${manifestName}_bound_mid_run.json"
}
$accountId = [string]$manifest.account_id
$mid = [string]$manifest.mid
if ([string]::IsNullOrWhiteSpace($accountId) -or [string]::IsNullOrWhiteSpace($mid)) {
    throw "Manifest account_id and mid are required."
}

$enforceBookInTitle = $true
if ($null -ne $manifest.PSObject.Properties['require_book_in_title']) {
    $enforceBookInTitle = [bool]$manifest.require_book_in_title
}

$seenPaths = @{}
$seenTitles = @{}
foreach ($source in @($manifest.items)) {
    $path = [string]$source.path
    $title = [string]$source.title
    $book = [string]$source.book
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "Video not found: $path" }
    if ([IO.Path]::GetExtension($path) -ine ".mp4") { throw "Only mp4 is supported: $path" }
    if ($title.Length -gt 80) { throw "Title exceeds 80 characters: $title" }
    if (-not $title.Contains($book)) {
        if ($enforceBookInTitle) { throw "Title does not contain book name: $title" }
        Write-Host "NOTE: title does not contain the book field (allowed by manifest): $title"
    }
    if ($seenPaths.ContainsKey($path)) { throw "Duplicate video path: $path" }
    if ($seenTitles.ContainsKey($title)) { throw "Duplicate title: $title" }
    $seenPaths[$path] = $true
    $seenTitles[$title] = $true
}

$store = Get-Content -Raw -Encoding UTF8 -LiteralPath $AuthPath | ConvertFrom-Json
$base = "https://cm.bilibili.com/takumi/api"
$midUri = New-QueryUri "$base/open_api/launch/cpc/meta_data/enterprise/mid" @{ account_id = $accountId }

# HARDENED: never select the business token from cached account lists alone.
# Enumerate every candidate record, then live-verify each one against the target
# account and MID before using it.
$candidates = @($store.records |
    Where-Object { (Get-AccountIdsFromRecord $_) -contains $accountId } |
    Sort-Object { try { [DateTimeOffset]$_.access_token_expires_at } catch { [DateTimeOffset]::MinValue } } -Descending)
if ($candidates.Count -eq 0) { throw "No authorization record contains account_id $accountId." }

$record = $null
$midItem = $null
$headers = $null
foreach ($candidate in $candidates) {
    $accessExpiry = [DateTimeOffset]::MinValue
    [DateTimeOffset]::TryParse([string]$candidate.access_token_expires_at, [ref]$accessExpiry) | Out-Null
    if ($accessExpiry -le [DateTimeOffset]::UtcNow.AddMinutes(2)) {
        Write-Host "Refreshing access token..."
        Refresh-Authorization $store $candidate
    }
    $candidateHeaders = @{
        Authorization = "Bearer $($candidate.access_token)"
        "X-Call-Source" = "sanlian-skills"
    }
    try {
        $midResponse = Invoke-MapiJson -Method GET -Uri $midUri -Headers $candidateHeaders
    } catch {
        Write-Host "Candidate authorization record rejected by live check; trying next."
        Start-Sleep -Seconds 1
        continue
    }
    $candidateMid = @($midResponse.result) | Where-Object { [string]$_.mid -eq $mid } | Select-Object -First 1
    if ($null -ne $candidateMid) {
        $record = $candidate
        $midItem = $candidateMid
        $headers = $candidateHeaders
        break
    }
    Start-Sleep -Seconds 1
}
if ($null -eq $record) { throw "No stored authorization record could be live-verified for account $accountId and MID $mid." }

$ProgressPreference = "SilentlyContinue"
$StageRoot = $StageDir
if ([string]::IsNullOrWhiteSpace($StageRoot)) {
    $StageRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("bound-mid-stage-" + $PID)
}

$existingRun = $null
if (Test-Path -LiteralPath $OutputPath) {
    $existingRun = Get-Content -Raw -Encoding UTF8 -LiteralPath $OutputPath | ConvertFrom-Json
}

$results = New-Object System.Collections.Generic.List[object]
foreach ($source in @($manifest.items)) {
    $file = Get-Item -LiteralPath ([string]$source.path)
    Write-Host "Hashing: $($file.Name)"
    $md5 = (Get-FileHash -Algorithm MD5 -LiteralPath $file.FullName).Hash.ToLowerInvariant()
    $previous = @($existingRun.results) | Where-Object { [string]$_.md5 -eq $md5 } | Select-Object -First 1
    if ($null -ne $previous) {
        $previous.title = [string]$source.title
        $previous.book = [string]$source.book
        $results.Add($previous)
        continue
    }
    $results.Add([pscustomobject]@{
        book = [string]$source.book
        name = $file.Name
        path = $file.FullName
        title = [string]$source.title
        bytes = [long]$file.Length
        md5 = $md5
        uploaded_at = $null
        process_id = $null
        create_error_code = $null
        create_error_msg = $null
        status = $null
        status_message = $null
        reason = $null
        bvid = $null
        avid = $null
        found_in_mid_archive = $false
        archive_title = $null
        archive_mid = $null
        public_code = $null
        public_title = $null
        public_owner_mid = $null
        public_accessible = $false
    })
}

$run = [pscustomobject]@{
    account_id = $accountId
    mid = $mid
    up_name = Repair-Utf8Mojibake ([string]$midItem.name)
    manifest_path = $ManifestPath
    stage_root = $StageRoot
    started_at = if ($null -ne $existingRun.started_at) { $existingRun.started_at } else { [DateTimeOffset]::Now.ToString("o") }
    updated_at = $null
    completed_at = $null
    count = $results.Count
    results = $results
}
Save-Run $run

# Recover already-published archives by MID and MD5 before uploading. This is
# the duplicate-prevention fallback when a local run file is missing.
foreach ($item in @($results | Where-Object { -not $_.found_in_mid_archive -and [string]::IsNullOrWhiteSpace([string]$_.process_id) })) {
    $archiveUri = New-QueryUri "$base/open_api/launch/cpc/meta_data/v3/archive/get_archive" @{
        account_id = $accountId
        arc_type = 1
        mid = $mid
        video_md5 = [string]$item.md5
        page = 1
        size = 10
        need_material_tag = 1
    }
    $archiveResponse = Invoke-MapiJson -Method GET -Uri $archiveUri -Headers $headers
    $archive = @($archiveResponse.result.data) | Where-Object { [string]$_.mid -eq $mid -and [string]$_.video_md5 -eq [string]$item.md5 } | Select-Object -First 1
    if ($null -ne $archive) {
        $item.uploaded_at = "remote-existing"
        $item.status = 1
        $item.status_message = "Recovered existing archive"
        $item.found_in_mid_archive = $true
        $item.archive_title = Repair-Utf8Mojibake ([string]$archive.title)
        $item.archive_mid = [string]$archive.mid
        $item.bvid = [string]$archive.bvid
        $item.avid = [string]$archive.aid
        Save-Run $run
    }
    Start-Sleep -Seconds 1
}

if ($VerifyOnly) {
    $unrecoverable = @($results | Where-Object { -not $_.found_in_mid_archive -and [string]::IsNullOrWhiteSpace([string]$_.process_id) })
    if ($unrecoverable.Count -gt 0) {
        throw "VerifyOnly cannot continue: $($unrecoverable.Count) item(s) have no existing archive or process_id."
    }
}

foreach ($item in @($results | Where-Object { -not $VerifyOnly -and [string]::IsNullOrWhiteSpace([string]$_.uploaded_at) })) {
    Write-Host "Uploading: $($item.name)"
    $signedUri = New-QueryUri "$base/open_api/launch/cpc/meta_data/signed_urls" @{ account_id = $accountId; key_list = $item.md5 }
    $signedResponse = Invoke-MapiJson -Method GET -Uri $signedUri -Headers $headers
    $signedItem = @($signedResponse.result.archive_signed_urls) | Where-Object { [string]$_.key -eq [string]$item.md5 } | Select-Object -First 1
    if ($null -eq $signedItem -or [string]::IsNullOrWhiteSpace([string]$signedItem.signed_url)) {
        throw "No signed URL returned for $($item.name)."
    }
    if ($null -ne $signedItem.error_code -and [string]$signedItem.error_code -notin @("", "0")) {
        throw "Signed URL rejected for $($item.name): $($signedItem.error_msg)"
    }
    $stagedPath = Get-StagedCopy -SourcePath ([string]$item.path) -StageRoot $StageRoot
    try {
        Invoke-SignedUpload -Uri ([string]$signedItem.signed_url) -Path $stagedPath
    } finally {
        if (Test-Path -LiteralPath $stagedPath) {
            try { [System.IO.File]::Delete($stagedPath) } catch { }
        }
    }
    $item.uploaded_at = [DateTimeOffset]::Now.ToString("o")
    Save-Run $run
    Start-Sleep -Seconds 2
}

$toCreate = @($results | Where-Object { -not $VerifyOnly -and [string]::IsNullOrWhiteSpace([string]$_.process_id) -and -not $_.found_in_mid_archive })
for ($offset = 0; $offset -lt $toCreate.Count; $offset += 5) {
    $batch = @($toCreate | Select-Object -Skip $offset -First 5)
    $details = foreach ($item in $batch) {
        @{
            video_md5 = [string]$item.md5
            video_file_name = [string]$item.name
            title = [string]$item.title
            mid = [long]$mid
            bili_category_one = [int]$manifest.bili_category_one
            bili_category_two = [int]$manifest.bili_category_two
            arc_tags = @($manifest.arc_tags)
        }
    }
    Write-Host "Creating archive batch: $($batch.Count)"
    $createResponse = Invoke-MapiJson -Method POST -Uri "$base/open_api/launch/cpc/meta_data/create/archives" -Headers $headers -Body @{ account_id = $accountId; archive_details = @($details) }
    if ([string]$createResponse.status -ne "success") { throw "Archive creation request failed." }
    foreach ($item in $batch) {
        $info = @($createResponse.result.archive_infos) | Where-Object { [string]$_.video_md5 -eq [string]$item.md5 } | Select-Object -First 1
        if ($null -eq $info) { throw "No process result returned for $($item.name)." }
        $item.process_id = [string]$info.process_id
        $item.create_error_code = $info.error_code
        $item.create_error_msg = $info.error_msg
    }
    Save-Run $run
    Start-Sleep -Seconds 3
}

$deadline = [DateTimeOffset]::Now.AddMinutes($MaxPollMinutes)
while ($true) {
    $pending = @($results | Where-Object { $null -eq $_.status -or [int]$_.status -eq 0 })
    if ($pending.Count -eq 0) { break }
    if ([DateTimeOffset]::Now -ge $deadline) { break }

    for ($offset = 0; $offset -lt $pending.Count; $offset += 20) {
        $batch = @($pending | Select-Object -Skip $offset -First 20)
        $processIds = (@($batch | ForEach-Object { [string]$_.process_id }) -join ",")
        $progressUri = New-QueryUri "$base/open_api/launch/cpc/meta_data/archive/progress" @{ account_id = $accountId; process_id_list = $processIds }
        $progressResponse = Invoke-MapiJson -Method GET -Uri $progressUri -Headers $headers
        foreach ($item in $batch) {
            $progress = @($progressResponse.result.progress_list) | Where-Object { [string]$_.process_id -eq [string]$item.process_id } | Select-Object -First 1
            if ($null -ne $progress) {
                $item.status = $progress.status
                $item.status_message = Repair-Utf8Mojibake ([string]$progress.error_msg)
                $item.reason = Repair-Utf8Mojibake ([string]$progress.reason)
            }
        }
        Save-Run $run
        Start-Sleep -Seconds 2
    }

    $remaining = @($results | Where-Object { $null -eq $_.status -or [int]$_.status -eq 0 }).Count
    Write-Host "Archives still processing: $remaining"
    if ($remaining -gt 0) { Start-Sleep -Seconds $PollIntervalSeconds }
}

$archivePending = @($results | Where-Object { [int]$_.status -eq 1 -and -not $_.found_in_mid_archive })
for ($round = 1; $round -le 6 -and $archivePending.Count -gt 0; $round++) {
    foreach ($item in $archivePending) {
        $archiveUri = New-QueryUri "$base/open_api/launch/cpc/meta_data/v3/archive/get_archive" @{
            account_id = $accountId
            arc_type = 1
            mid = $mid
            video_md5 = [string]$item.md5
            page = 1
            size = 10
            need_material_tag = 1
        }
        $archiveResponse = Invoke-MapiJson -Method GET -Uri $archiveUri -Headers $headers
        $archive = @($archiveResponse.result.data) | Where-Object { [string]$_.mid -eq $mid -and [string]$_.video_md5 -eq [string]$item.md5 } | Select-Object -First 1
        if ($null -ne $archive) {
            $item.found_in_mid_archive = $true
            $item.archive_title = Repair-Utf8Mojibake ([string]$archive.title)
            $item.archive_mid = [string]$archive.mid
            $item.bvid = [string]$archive.bvid
            $item.avid = [string]$archive.aid
        }
        Save-Run $run
        Start-Sleep -Seconds 1
    }
    $archivePending = @($results | Where-Object { [int]$_.status -eq 1 -and -not $_.found_in_mid_archive })
    if ($archivePending.Count -gt 0 -and $round -lt 6) { Start-Sleep -Seconds 10 }
}

foreach ($item in $results) {
    $item.archive_title = Repair-Utf8Mojibake ([string]$item.archive_title)
    $item.status_message = Repair-Utf8Mojibake ([string]$item.status_message)
    $item.reason = Repair-Utf8Mojibake ([string]$item.reason)
    $item.create_error_msg = Repair-Utf8Mojibake ([string]$item.create_error_msg)
}
$run.up_name = Repair-Utf8Mojibake ([string]$run.up_name)

foreach ($item in @($results | Where-Object { $_.found_in_mid_archive -and -not [string]::IsNullOrWhiteSpace([string]$_.bvid) })) {
    $publicUri = New-QueryUri "https://api.bilibili.com/x/web-interface/view" @{ bvid = [string]$item.bvid }
    try {
        $public = Invoke-RestMethod -Method GET -Uri $publicUri -Headers @{ "User-Agent" = "Mozilla/5.0" } -TimeoutSec 60
        $item.public_code = $public.code
        $item.public_title = [string]$public.data.title
        $item.public_owner_mid = [string]$public.data.owner.mid
        $item.public_accessible = ([int]$public.code -eq 0 -and [string]$public.data.bvid -eq [string]$item.bvid -and [string]$public.data.owner.mid -eq $mid)
    } catch {
        $item.public_code = -1
        $item.public_accessible = $false
    }
    Save-Run $run
    Start-Sleep -Seconds 1
}

$run.completed_at = [DateTimeOffset]::Now.ToString("o")
Save-Run $run

$summary = [pscustomobject]@{
    account_id = $accountId
    mid = $mid
    up_name = $run.up_name
    count = $results.Count
    passed = @($results | Where-Object { [int]$_.status -eq 1 -and $_.found_in_mid_archive -and $_.public_accessible -and [string]$_.archive_title -eq [string]$_.title -and [string]$_.public_title -eq [string]$_.title }).Count
    failed = @($results | Where-Object { [int]$_.status -eq -1 }).Count
    processing = @($results | Where-Object { $null -eq $_.status -or [int]$_.status -eq 0 }).Count
    unverified = @($results | Where-Object { [int]$_.status -eq 1 -and (-not $_.found_in_mid_archive -or -not $_.public_accessible -or [string]$_.archive_title -ne [string]$_.title -or [string]$_.public_title -ne [string]$_.title) }).Count
    output_path = $OutputPath
}
$summary | ConvertTo-Json -Depth 5

if ($summary.passed -ne $summary.count) {
    throw "Not all archives passed final verification. See $OutputPath"
}
