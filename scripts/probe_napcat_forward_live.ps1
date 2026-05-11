param(
    [string]$UserId = "1507833383",
    [int]$Count = 30,
    [string]$HttpUrl = "http://127.0.0.1:3000",
    [string]$FastUrl = "http://127.0.0.1:6099/plugin/napcat-plugin-qq-data-fast/api",
    [string]$MessageId = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Invoke-JsonPost {
    param(
        [string]$Uri,
        [hashtable]$Body
    )
    return Invoke-RestMethod -Method Post -Uri $Uri -ContentType "application/json" -Body ($Body | ConvertTo-Json -Compress -Depth 12)
}

function Get-OptionalText {
    param(
        [object]$Source,
        [string]$Name
    )
    if (-not $Source) {
        return ""
    }
    if (-not $Source.PSObject.Properties[$Name]) {
        return ""
    }
    return [string]$Source.$Name
}

function Get-ForwardCandidateRows {
    param(
        [object[]]$Messages
    )

    $rows = @()
    foreach ($message in ($Messages | Where-Object { $_ -ne $null })) {
        $raw = $null
        if ($message.PSObject.Properties["rawMessage"]) {
            $raw = $message.rawMessage
        }
        if ($raw -and $raw.elements) {
            foreach ($element in $raw.elements) {
                $forwardElement = $null
                if ($element.PSObject.Properties["multiForwardMsgElement"]) {
                    $forwardElement = $element.multiForwardMsgElement
                }
                if (-not $forwardElement) {
                    continue
                }
                $rows += [pscustomobject]@{
                    source = "fast_raw"
                    message_id = [string]$message.message_id
                    message_seq = [string]$message.message_seq
                    time = [string]$message.time
                    user_id = [string]$message.user_id
                    forward_id = [string]$message.message_id
                    peer_uid = [string]$raw.peerUid
                    peer_uin = [string]$raw.peerUin
                    chat_type = [string]$raw.chatType
                    element_id = [string]$element.elementId
                    res_id = [string]$forwardElement.resId
                    file_name = [string]$forwardElement.fileName
                    xml_brief = [string]$forwardElement.xmlContent
                }
            }
            continue
        }

        $segments = @()
        if ($message.PSObject.Properties["message"] -and $message.message) {
            $segments = @($message.message)
        } elseif ($message.PSObject.Properties["data"] -and $message.data) {
            $segments = @($message.data)
        }
        foreach ($segment in $segments) {
            if (-not $segment) {
                continue
            }
            $segmentType = [string]$segment.type
            $segmentData = $segment.data
            if ($segmentType -ne "forward" -or -not $segmentData) {
                continue
            }
            $forwardId = [string]$segmentData.id
            if (-not $forwardId) {
                $forwardId = [string]$segmentData.res_id
            }
            if (-not $forwardId) {
                $forwardId = [string]$segmentData.resId
            }
            $rows += [pscustomobject]@{
                source = "native_public"
                message_id = [string]$message.message_id
                message_seq = [string]$message.message_seq
                time = [string]$message.time
                user_id = [string]$message.user_id
                forward_id = $forwardId
                peer_uid = ""
                peer_uin = ""
                chat_type = ""
                element_id = ""
                res_id = $forwardId
                file_name = (Get-OptionalText -Source $segmentData -Name "fileName")
                xml_brief = (Get-OptionalText -Source $segmentData -Name "content")
            }
        }
    }
    return $rows
}

function Print-JsonLine {
    param([object]$Value)
    $Value | ConvertTo-Json -Compress -Depth 12
}

$nativeRecent = Invoke-JsonPost -Uri "$HttpUrl/get_friend_msg_history" -Body @{
    user_id = [int64]$UserId
    count = $Count
    disable_get_url = $true
    parse_mult_msg = $false
}

Write-Output "native_status=$($nativeRecent.status)"
if ($nativeRecent.data.messages.Count -gt 0) {
    Write-Output ("native_first_message=" + (Print-JsonLine ($nativeRecent.data.messages | Select-Object -First 1)))
}
$nativeCandidates = Get-ForwardCandidateRows -Messages $nativeRecent.data.messages
Write-Output "native_forward_count=$($nativeCandidates.Count)"
foreach ($row in $nativeCandidates) {
    Write-Output ("native_forward=" + (Print-JsonLine $row))
}

$target = $null
if ($MessageId) {
    $target = $nativeCandidates | Where-Object {
        $_.message_id -eq $MessageId -or $_.forward_id -eq $MessageId -or $_.res_id -eq $MessageId
    } | Select-Object -First 1
}
if (-not $target) {
    $target = $nativeCandidates | Select-Object -First 1
}
if (-not $target) {
    throw "No forward message found for friend $UserId"
}

Write-Output ("selected_forward=" + (Print-JsonLine $target))

$nativeParsed = Invoke-JsonPost -Uri "$HttpUrl/get_friend_msg_history" -Body @{
    user_id = [int64]$UserId
    count = 5
    message_seq = [string]$target.message_seq
    reverse_order = $false
    disable_get_url = $true
    parse_mult_msg = $true
}

$nativeParsedMessage = $nativeParsed.data.messages | Where-Object { [string]$_.message_id -eq [string]$target.message_id } | Select-Object -First 1
if (-not $nativeParsedMessage) {
    $nativeParsedMessage = $nativeParsed.data.messages | Select-Object -First 1
}
Write-Output "native_parse_mult_status=$($nativeParsed.status)"
Write-Output ("native_parse_mult_message=" + (Print-JsonLine $nativeParsedMessage))

$nativeForwardAction = Invoke-JsonPost -Uri "$HttpUrl/get_forward_msg" -Body @{
    message_id = [string]$target.forward_id
}
Write-Output "native_get_forward_status=$($nativeForwardAction.status)"
Write-Output ("native_get_forward=" + (Print-JsonLine $nativeForwardAction.data))

$fastHistory = Invoke-JsonPost -Uri "$FastUrl/history" -Body @{
    chat_type = "private"
    chat_id = [string]$UserId
    count = $Count
}

Write-Output "fast_history_code=$($fastHistory.code)"
if ($fastHistory.data.messages.Count -gt 0) {
    Write-Output ("fast_first_message=" + (Print-JsonLine ($fastHistory.data.messages | Select-Object -First 1)))
}
$fastCandidates = Get-ForwardCandidateRows -Messages $fastHistory.data.messages
Write-Output "fast_forward_count=$($fastCandidates.Count)"
foreach ($row in $fastCandidates) {
    Write-Output ("fast_forward=" + (Print-JsonLine $row))
}

$fastTarget = $fastCandidates | Where-Object {
    $_.message_id -eq $target.forward_id -or
    $_.message_id -eq $target.message_id -or
    $_.res_id -eq $target.res_id
} | Select-Object -First 1
if (-not $fastTarget) {
    throw "Fast plugin history did not return target forward $($target.message_id)"
}

$fastDetail = Invoke-JsonPost -Uri "$FastUrl/hydrate-forward-detail" -Body @{
    message_id_raw = [string]$fastTarget.message_id
    element_id = [string]$fastTarget.element_id
    peer_uid = [string]$fastTarget.peer_uid
    chat_type_raw = [int]$fastTarget.chat_type
}

Write-Output "fast_detail_code=$($fastDetail.code)"
Write-Output ("fast_detail=" + (Print-JsonLine $fastDetail.data))
