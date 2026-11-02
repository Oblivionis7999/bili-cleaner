<#
.SYNOPSIS
    真实 B 站账号冒烟测试：默认只读检查本机 BiliCleaner 服务是否正常。

.DESCRIPTION
    先启动 BiliCleaner（真实模式）并在浏览器里扫码登录，再运行本脚本。
    默认只发送 GET 请求，不会改动任何数据。

    加上 -IncludeWriteTest 后，也只对用户显式指定的一个对象执行一次写操作：
      -UnfollowMid <mid>                             取关一个 UP 主
      -FavFolderId <media_id> -UnfavResource <id:type>  取消收藏一个条目
    执行前会打印对象信息，并要求输入 y 确认；输入其他内容则放弃。

    退出码：
      0  全部检查通过（或只读部分通过、未请求写测试）
      1  有只读检查失败
      2  服务未启动 / 端口不对 / 当前是 mock 模式 / 地址不是本机
      3  未登录（请先在浏览器扫码登录）
      4  写测试被取消，或写任务失败
      5  等待任务结果超时（可能仍在跑或在风控冷却）

.PARAMETER BaseUrl
    服务地址，默认 http://127.0.0.1:8770。
    默认只允许 127.0.0.1 / localhost，防止把登录 Cookie 发到别处。

.PARAMETER IncludeWriteTest
    开启写测试。必须同时指定 -UnfollowMid 或 -UnfavResource（二选一）。

.PARAMETER UnfollowMid
    要取关的单个 UP 主 mid（数字）。注意：会真的取关，回滚只能重新关注。

.PARAMETER UnfavResource
    要取消收藏的单个资源，格式 "id:type"，例如 "117528844047788:2"。
    可以从 /api/favorites/<folder_id>/items 或备份文件里查到 id 和 type。

.PARAMETER FavFolderId
    -UnfavResource 所在收藏夹的 media_id（与 -UnfavResource 一起使用）。

.PARAMETER TaskTimeoutSec
    写测试等待任务完成的最长秒数，默认 180。

.PARAMETER AllowMock
    允许对 --mock 模式的服务运行。默认拒绝（mock 是假数据，不是真实冒烟；
    这个开关只用于验证脚本本身能跑通）。

.PARAMETER AllowNonLocalBaseUrl
    允许 -BaseUrl 指向非本机地址。默认拒绝。

.EXAMPLE
    .\scripts\smoke_real.ps1

.EXAMPLE
    .\scripts\smoke_real.ps1 -BaseUrl http://127.0.0.1:8771

.EXAMPLE
    .\scripts\smoke_real.ps1 -IncludeWriteTest -UnfollowMid 12345678

.EXAMPLE
    .\scripts\smoke_real.ps1 -IncludeWriteTest -FavFolderId 916673043 -UnfavResource "117528844047788:2"

.NOTES
    真实账号冒烟只在本地跑，不要在高峰期跑；写测试风险与回滚方式见 docs/SMOKE-TEST.md。
#>
#Requires -Version 5.1
[CmdletBinding()]
param(
    [string]$BaseUrl = "http://127.0.0.1:8770",
    [switch]$IncludeWriteTest,
    [long]$UnfollowMid = 0,
    [string]$UnfavResource = "",
    [long]$FavFolderId = 0,
    [int]$TaskTimeoutSec = 180,
    [switch]$AllowMock,
    [switch]$AllowNonLocalBaseUrl
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$script:BaseUrl = $BaseUrl.TrimEnd("/")
$script:Checks = New-Object System.Collections.ArrayList
$script:Timer = [System.Diagnostics.Stopwatch]::StartNew()

try {
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    $OutputEncoding = [System.Text.Encoding]::UTF8
}
catch {
    # 控制台编码设置失败不影响检查逻辑
}

# ---------------------------------------------------------------- 输出辅助

function Write-Section([string]$Title) {
    Write-Host ""
    Write-Host ("== " + $Title) -ForegroundColor Cyan
}

function Write-Info([string]$Text) {
    Write-Host $Text -ForegroundColor DarkGray
}

function Write-Ok([string]$Text) {
    Write-Host $Text -ForegroundColor Green
}

function Write-Warn([string]$Text) {
    Write-Host ("[警告] " + $Text) -ForegroundColor Yellow
}

function Write-Err([string]$Text) {
    Write-Host ("[错误] " + $Text) -ForegroundColor Red
}

function Get-ShortText([string]$Text, [int]$Max = 30) {
    if ([string]::IsNullOrWhiteSpace($Text)) { return "" }
    $oneLine = ($Text -replace "[\r\n\t]+", " ").Trim()
    if ($oneLine.Length -le $Max) { return $oneLine }
    return $oneLine.Substring(0, $Max) + "…"
}

function Format-UnixTime($Value) {
    try {
        if (-not $Value) { return "未知" }
        $time = [DateTimeOffset]::FromUnixTimeSeconds([long]$Value)
        return $time.ToLocalTime().ToString("yyyy-MM-dd HH:mm")
    }
    catch {
        return "未知"
    }
}

function Format-ApiError($Result) {
    if ($Result.Json -and $Result.Json.error) {
        $code = [string]$Result.Json.error.code
        $message = [string]$Result.Json.error.message
        if ($code) { return ("HTTP " + $Result.Status + " " + $code + "：" + $message) }
        return ("HTTP " + $Result.Status + "：" + $message)
    }
    if ($Result.Status -gt 0) {
        $extra = [string]$Result.Error
        return ("HTTP " + $Result.Status + " " + $extra).Trim()
    }
    return ("无法连接：" + [string]$Result.Error)
}

function Add-CheckResult {
    param(
        [string]$Name,
        [bool]$Ok,
        [string]$Detail,
        [long]$ElapsedMs
    )
    $null = $script:Checks.Add([pscustomobject]@{
        Name = $Name
        Ok = $Ok
        Detail = $Detail
        ElapsedMs = $ElapsedMs
    })
    if ($Ok) {
        Write-Host ("[ OK ] {0,-30} {1,6} ms  {2}" -f $Name, $ElapsedMs, $Detail) -ForegroundColor Green
    }
    else {
        Write-Host ("[FAIL] {0,-30} {1,6} ms  {2}" -f $Name, $ElapsedMs, $Detail) -ForegroundColor Red
    }
}

# ---------------------------------------------------------------- HTTP 封装

function Invoke-Api {
    <#
        发一个 API 请求并返回统一结构：
        @{ Url; Status; Ok; Json; Text; Error; ElapsedMs }
        4xx/5xx 不抛异常，返回 Ok=$false 让调用方汇总。
    #>
    param(
        [ValidateSet("GET", "POST")]
        [string]$Method = "GET",
        [string]$Path,
        [object]$Body = $null,
        [int]$TimeoutSec = 30
    )

    $url = $script:BaseUrl + $Path
    $watch = [System.Diagnostics.Stopwatch]::StartNew()
    $status = 0
    $text = ""
    $errorText = ""

    $requestParams = @{
        Uri = $url
        Method = $Method
        TimeoutSec = $TimeoutSec
        UseBasicParsing = $true
        Headers = @{ "Accept" = "application/json" }
    }
    if ($null -ne $Body) {
        $requestParams["ContentType"] = "application/json; charset=utf-8"
        $requestParams["Body"] = ($Body | ConvertTo-Json -Depth 10 -Compress)
    }

    try {
        $response = Invoke-WebRequest @requestParams
        $status = [int]$response.StatusCode
        $text = [string]$response.Content
    }
    catch {
        $caught = $_
        $exception = $caught.Exception
        $errorText = [string]$exception.Message
        $response = $null
        if ($exception.PSObject.Properties.Name -contains "Response") {
            $response = $exception.Response
        }
        if ($null -ne $response -and $response.PSObject.Properties.Name -contains "StatusCode") {
            try { $status = [int]$response.StatusCode } catch { $status = 0 }
        }
        # PowerShell 7：body 通常在 ErrorDetails.Message
        if ($caught.ErrorDetails -and $caught.ErrorDetails.Message) {
            $text = [string]$caught.ErrorDetails.Message
        }
        # Windows PowerShell 5.1：从响应流里读 body
        if ([string]::IsNullOrEmpty($text) -and $null -ne $response) {
            try {
                if ($response.PSObject.Methods["GetResponseStream"]) {
                    $stream = $response.GetResponseStream()
                    if ($null -ne $stream) {
                        $reader = New-Object System.IO.StreamReader($stream)
                        $text = $reader.ReadToEnd()
                        $reader.Dispose()
                    }
                }
            }
            catch {
                # 读不到 body 就只保留状态码
            }
        }
    }

    $watch.Stop()
    $json = $null
    if (-not [string]::IsNullOrWhiteSpace($text)) {
        try { $json = $text | ConvertFrom-Json } catch { $json = $null }
    }
    $ok = ($status -ge 200 -and $status -lt 300)
    return [pscustomobject]@{
        Url = $url
        Status = $status
        Ok = $ok
        Json = $json
        Text = $text
        Error = $errorText
        ElapsedMs = [long]$watch.ElapsedMilliseconds
    }
}

function Invoke-ReadCheck {
    param(
        [string]$Name,
        [string]$Path,
        [scriptblock]$Describe
    )
    $result = Invoke-Api -Method "GET" -Path $Path
    $ok = [bool]$result.Ok
    $detail = ""
    if ($ok) {
        try {
            $detail = [string](& $Describe $result.Json)
        }
        catch {
            $ok = $false
            $detail = "响应结构异常：" + [string]$_.Exception.Message
        }
    }
    else {
        $detail = Format-ApiError $result
    }
    Add-CheckResult -Name $Name -Ok $ok -Detail $detail -ElapsedMs $result.ElapsedMs
    return $result
}

function Start-ApiTask {
    param(
        [string]$Type,
        [hashtable]$Params
    )
    $result = Invoke-Api -Method "POST" -Path "/api/tasks" -Body @{ type = $Type; params = $Params }
    if (-not $result.Ok) {
        Write-Err ("创建任务失败：" + (Format-ApiError $result))
        return $null
    }
    return $result.Json
}

function Wait-ApiTask {
    param(
        [string]$TaskId,
        [int]$TimeoutSec = 180
    )
    $watch = [System.Diagnostics.Stopwatch]::StartNew()
    $lastLine = ""
    while ($watch.Elapsed.TotalSeconds -lt $TimeoutSec) {
        $result = Invoke-Api -Method "GET" -Path ("/api/tasks/" + $TaskId)
        if ($result.Ok -and $null -ne $result.Json) {
            $task = $result.Json
            $line = "status={0}  done={1}/{2}  failed={3}  {4}" -f `
                $task.status, $task.done, $task.total, $task.failed, $task.message
            if ($line -ne $lastLine) {
                Write-Host ("  [任务] " + $line) -ForegroundColor DarkGray
                $lastLine = $line
            }
            if (@("done", "failed", "stopped") -contains [string]$task.status) {
                return $task
            }
            if ([string]$task.status -eq "cooldown") {
                Write-Warn ("触发 B 站风控，任务冷却到 " + [string]$task.cooldown_until + "，继续等待…")
            }
        }
        Start-Sleep -Seconds 1
    }
    return $null
}

# ---------------------------------------------------------------- 写测试用数据查找

function Find-FollowItem {
    param(
        [long]$Mid,
        [int]$PageSize = 200,
        [int]$MaxPages = 20
    )
    $offset = 0
    for ($page = 0; $page -lt $MaxPages; $page++) {
        $result = Invoke-Api -Method "GET" -Path ("/api/follows?offset={0}&limit={1}" -f $offset, $PageSize)
        if (-not $result.Ok) {
            Write-Err ("读取关注列表失败：" + (Format-ApiError $result))
            return $null
        }
        $items = @($result.Json.items)
        foreach ($item in $items) {
            if ([long]$item.mid -eq $Mid) { return $item }
        }
        $total = [int]$result.Json.total
        $offset += $PageSize
        if ($items.Count -eq 0 -or $offset -ge $total) { break }
    }
    return $null
}

function Find-FavItem {
    param(
        [long]$FolderId,
        [string]$Resource,
        [int]$PageSize = 200,
        [int]$MaxPages = 50
    )
    $offset = 0
    for ($page = 0; $page -lt $MaxPages; $page++) {
        $path = "/api/favorites/{0}/items?offset={1}&limit={2}" -f $FolderId, $offset, $PageSize
        $result = Invoke-Api -Method "GET" -Path $path
        if (-not $result.Ok) {
            Write-Err ("读取收藏夹条目失败：" + (Format-ApiError $result))
            return $null
        }
        $items = @($result.Json.items)
        foreach ($item in $items) {
            $key = "{0}:{1}" -f $item.id, $item.type
            if ($key -eq $Resource) { return $item }
        }
        $total = [int]$result.Json.total
        $offset += $PageSize
        if ($items.Count -eq 0 -or $offset -ge $total) { break }
    }
    return $null
}

# ---------------------------------------------------------------- 写测试

function Invoke-UnfollowWriteTest {
    param([long]$Mid)

    Write-Section "写测试：取关单个 UP 主（真实写操作，不可撤销）"
    Write-Warn "接下来会对真实 B 站账号执行一次取关；只建议用测试号或不再需要关注的对象。"
    Write-Warn "不要在高峰期跑；遇到风控会冷却，脚本可能等待较久。"

    $item = Find-FollowItem -Mid $Mid
    if ($null -eq $item) {
        Write-Err ("在关注列表里没找到 mid=" + $Mid + "，已放弃（未发送写请求）。")
        exit 4
    }

    $official = [string]$item.official
    if ([string]::IsNullOrWhiteSpace($official)) { $official = "无" }
    $groups = @($item.groups)
    $groupText = "无"
    if ($groups.Count -gt 0) { $groupText = ($groups -join "、") }
    $mutualText = "否"
    if ([bool]$item.mutual) { $mutualText = "是（互关）" }

    Write-Host ""
    Write-Host "  即将取关的对象：" -ForegroundColor White
    Write-Host ("    mid      : {0}" -f $item.mid)
    Write-Host ("    昵称     : {0}" -f $item.name)
    Write-Host ("    签名     : {0}" -f (Get-ShortText ([string]$item.sign) 60))
    Write-Host ("    认证     : {0}" -f $official)
    Write-Host ("    分组     : {0}" -f $groupText)
    Write-Host ("    互关     : {0}" -f $mutualText)
    Write-Host ("    关注时间 : {0}" -f (Format-UnixTime $item.mtime))
    Write-Host ("    空间页   : https://space.bilibili.com/{0}" -f $item.mid)
    if ([bool]$item.mutual) {
        Write-Warn "这是互关对象。若设置里 protect_mutual=true，任务会跳过它（不会真的取关）。"
    }

    $answer = Read-Host "确认执行【取关】？输入 y 继续，其他任意输入取消"
    if (([string]$answer).Trim().ToLowerInvariant() -ne "y") {
        Write-Warn "已取消，没有发送任何写请求。"
        exit 4
    }

    $task = Start-ApiTask -Type "unfollow" -Params @{
        mids = @($Mid)
        reason = "manual"
        ack_risk = $true
    }
    if ($null -eq $task) { exit 4 }
    Write-Host ("  已入队任务：{0}（status={1}）" -f $task.id, $task.status)

    $finished = Wait-ApiTask -TaskId $task.id -TimeoutSec $TaskTimeoutSec
    if ($null -eq $finished) {
        Write-Err ("等待任务超时（" + $TaskTimeoutSec + " 秒）。任务可能仍在跑或正在冷却，可打开 " + $script:BaseUrl + " 查看任务列表。")
        exit 5
    }
    $status = [string]$finished.status
    if ($status -ne "done") {
        Write-Err ("取关任务没有成功：status=" + $status + " message=" + [string]$finished.message)
        Write-Info "  排查：%APPDATA%\BiliCleaner\logs\operations.jsonl 与任务操作日志。"
        exit 4
    }

    Write-Host ""
    Write-Ok ("取关任务完成：done={0}/{1} failed={2}" -f $finished.done, $finished.total, $finished.failed)
    if ([int]$finished.done -eq 0) {
        Write-Warn "任务完成但实际取关 0 条：该对象可能是互关且 protect_mutual=true，或任务被跳过。"
    }
    if ($finished.backup_path) {
        Write-Host ("  备份路径 : {0}" -f $finished.backup_path) -ForegroundColor DarkGray
    }
    Write-Host "  回滚方式 : 打开 https://space.bilibili.com/$Mid 点“关注”重新关注。"
    Write-Host "             注意：关注顺序与自定义分组不会恢复，重新关注后 mtime 会变。"
}

function Invoke-UnfavWriteTest {
    param(
        [long]$FolderId,
        [string]$Resource
    )

    Write-Section "写测试：取消收藏单个资源（真实写操作，不可撤销）"
    Write-Warn "接下来会对真实 B 站账号执行一次取消收藏；只建议用自己刚收藏的测试视频。"
    Write-Warn "不要在高峰期跑；遇到风控会冷却，脚本可能等待较久。"

    if ($FolderId -le 0) {
        Write-Err "取消收藏测试需要 -FavFolderId <media_id>（收藏夹 id）。"
        exit 4
    }
    if ($Resource -notmatch '^\d+:\d+$') {
        Write-Err ('-UnfavResource 格式应为 "id:type"，例如 "117528844047788:2"，当前值：' + $Resource)
        exit 4
    }

    $foldersResult = Invoke-Api -Method "GET" -Path "/api/favorites/folders"
    if (-not $foldersResult.Ok) {
        Write-Err ("读取收藏夹列表失败：" + (Format-ApiError $foldersResult))
        exit 4
    }
    $folder = $null
    foreach ($candidate in @($foldersResult.Json.items)) {
        if ([long]$candidate.media_id -eq $FolderId) { $folder = $candidate; break }
    }
    if ($null -eq $folder) {
        Write-Err ("没找到 media_id=" + $FolderId + " 的收藏夹，已放弃（未发送写请求）。")
        exit 4
    }
    Write-Host ("  目标收藏夹：{0}（media_id={1}，共 {2} 条）" -f $folder.title, $folder.media_id, $folder.count)

    $item = Find-FavItem -FolderId $FolderId -Resource $Resource
    if ($null -eq $item) {
        Write-Err ("在收藏夹里没找到资源 " + $Resource + "，已放弃（未发送写请求）。")
        exit 4
    }

    $upper = [string]$item.upper
    if ([string]::IsNullOrWhiteSpace($upper)) { $upper = "未知" }
    $bvid = [string]$item.bvid
    $link = "（无 bvid，视频可能已失效）"
    if ($bvid) { $link = "https://www.bilibili.com/video/" + $bvid }

    Write-Host ""
    Write-Host "  即将取消收藏的资源：" -ForegroundColor White
    Write-Host ("    资源 key : {0}:{1}" -f $item.id, $item.type)
    Write-Host ("    标题     : {0}" -f (Get-ShortText ([string]$item.title) 70))
    Write-Host ("    UP 主    : {0}" -f $upper)
    Write-Host ("    bvid     : {0}" -f $bvid)
    Write-Host ("    链接     : {0}" -f $link)
    Write-Host ("    收藏时间 : {0}" -f (Format-UnixTime $item.fav_time))
    if ([bool]$item.invalid) {
        Write-Warn "该条目已被标记为“失效内容”，取消收藏不会丢失可播放内容。"
    }

    $answer = Read-Host "确认执行【取消收藏】？输入 y 继续，其他任意输入取消"
    if (([string]$answer).Trim().ToLowerInvariant() -ne "y") {
        Write-Warn "已取消，没有发送任何写请求。"
        exit 4
    }

    $task = Start-ApiTask -Type "favorite_clear" -Params @{
        folder_id = $FolderId
        ids = @($Resource)
        ack_risk = $true
    }
    if ($null -eq $task) { exit 4 }
    Write-Host ("  已入队任务：{0}（status={1}）" -f $task.id, $task.status)

    $finished = Wait-ApiTask -TaskId $task.id -TimeoutSec $TaskTimeoutSec
    if ($null -eq $finished) {
        Write-Err ("等待任务超时（" + $TaskTimeoutSec + " 秒）。任务可能仍在跑或正在冷却，可打开 " + $script:BaseUrl + " 查看任务列表。")
        exit 5
    }
    $status = [string]$finished.status
    if ($status -ne "done") {
        Write-Err ("取消收藏任务没有成功：status=" + $status + " message=" + [string]$finished.message)
        Write-Info "  排查：%APPDATA%\BiliCleaner\logs\operations.jsonl 与任务操作日志。"
        exit 4
    }

    Write-Host ""
    Write-Ok ("取消收藏任务完成：done={0}/{1} failed={2}" -f $finished.done, $finished.total, $finished.failed)
    if ($finished.backup_path) {
        Write-Host ("  备份路径 : {0}" -f $finished.backup_path) -ForegroundColor DarkGray
    }
    Write-Host "  回滚方式 : 打开上面的视频链接，点“收藏”重新收藏（收藏时间会变成当前时间）。"
    Write-Host "             如需放回原来的收藏夹，请手动选择；视频已失效则无需恢复。"
}

# ================================================================ 主流程

Write-Host ""
Write-Host "BiliCleaner 真实账号冒烟测试" -ForegroundColor White
Write-Host ("服务地址：{0}" -f $script:BaseUrl) -ForegroundColor DarkGray
Write-Host "默认只读；写操作需要 -IncludeWriteTest，且只会操作一个显式指定的对象。" -ForegroundColor DarkGray

if (-not $AllowNonLocalBaseUrl) {
    if ($script:BaseUrl -notmatch '^https?://(127\.0\.0\.1|localhost)(:\d+)?$') {
        Write-Err "-BaseUrl 默认只允许本机地址（127.0.0.1 / localhost），避免把登录 Cookie 发到别处。"
        exit 2
    }
}

# ---------------------------------------------------------------- 基础连通性

Write-Section "基础连通性"

$health = Invoke-Api -Method "GET" -Path "/api/health"
$healthOk = $health.Ok -and ($null -ne $health.Json) -and ($health.Json.ok -eq $true)
if ($healthOk) {
    $healthDetail = "version={0} mock={1}" -f $health.Json.version, $health.Json.mock
}
else {
    $healthDetail = Format-ApiError $health
}
Add-CheckResult -Name "/api/health" -Ok $healthOk -Detail $healthDetail -ElapsedMs $health.ElapsedMs

if (-not $healthOk) {
    Write-Err "服务没有响应。先在本机启动 BiliCleaner："
    Write-Info "    python -m bilicleaner --no-browser --port 8770"
    exit 2
}

$isMock = [bool]($health.Json.mock)
if ($isMock -and -not $AllowMock) {
    Write-Err "当前服务是 --mock 模式（假数据），不能用于真实账号冒烟。"
    Write-Info "    请去掉 --mock 重启服务；如只想验证本脚本本身，可加 -AllowMock。"
    exit 2
}
if ($isMock) {
    Write-Warn "当前是 mock 模式（-AllowMock 已开启），结果只能证明脚本能跑通，不代表真实账号。"
}

$me = Invoke-Api -Method "GET" -Path "/api/me"
$meOk = $me.Ok -and ($null -ne $me.Json) -and ($me.Json.logged_in -eq $true)
if ($meOk) {
    $user = $me.Json.user
    $meDetail = "mid={0} name={1}" -f $user.mid, $user.name
}
else {
    $meDetail = Format-ApiError $me
}
Add-CheckResult -Name "/api/me" -Ok $meOk -Detail $meDetail -ElapsedMs $me.ElapsedMs

if (-not $meOk) {
    Write-Err ("尚未登录。请用浏览器打开 " + $script:BaseUrl + " 扫码登录后重新运行本脚本。")
    exit 3
}

# ---------------------------------------------------------------- 只读数据检查

Write-Section "只读接口（第一页 / 列表）"

$null = Invoke-ReadCheck -Name "/api/follows" -Path "/api/follows?offset=0&limit=50" -Describe {
    param($json)
    if ($null -eq $json.PSObject.Properties["total"]) { throw "响应缺少 total 字段" }
    if ($null -eq $json.PSObject.Properties["items"]) { throw "响应缺少 items 字段" }
    $items = @($json.items)
    $text = "total=" + [int]$json.total + "，本页 " + $items.Count + " 条"
    if ($items.Count -gt 0) {
        $text += "；首条：" + [string]$items[0].name + "（mid=" + [string]$items[0].mid + "）"
    }
    if ([int]$json.total -eq 0) {
        $text += "；关注数为 0，确认账号是否为空"
    }
    $text
}

$null = Invoke-ReadCheck -Name "/api/follows/groups" -Path "/api/follows/groups" -Describe {
    param($json)
    if ($null -eq $json.PSObject.Properties["items"]) { throw "响应缺少 items 字段" }
    $items = @($json.items)
    $text = "分组 " + $items.Count + " 个"
    $names = @($items | Select-Object -First 5 | ForEach-Object { [string]$_.name })
    if ($names.Count -gt 0) {
        $text += "：" + ($names -join "、")
    }
    $text
}

$null = Invoke-ReadCheck -Name "/api/dynamics" -Path "/api/dynamics?offset=&limit=30" -Describe {
    param($json)
    if ($null -eq $json.PSObject.Properties["items"]) { throw "响应缺少 items 字段" }
    $items = @($json.items)
    $labels = @($items | ForEach-Object { [string]$_.type_label } | Where-Object { $_ } | Select-Object -Unique)
    $text = "本页 " + $items.Count + " 条，has_more=" + [string]$json.has_more
    if ($labels.Count -gt 0) {
        $text += "，类型：" + ($labels -join " / ")
    }
    if ($items.Count -gt 0) {
        $text += "；首条：" + [string]$items[0].type_label + "「" + (Get-ShortText ([string]$items[0].text) 24) + "」"
    }
    $text
}

$null = Invoke-ReadCheck -Name "/api/favorites/folders" -Path "/api/favorites/folders" -Describe {
    param($json)
    if ($null -eq $json.PSObject.Properties["items"]) { throw "响应缺少 items 字段" }
    $folders = @($json.items)
    $text = "收藏夹 " + $folders.Count + " 个"
    $names = @($folders | Select-Object -First 5 | ForEach-Object { [string]$_.title + "(" + [string]$_.count + ")" })
    if ($names.Count -gt 0) {
        $text += "：" + ($names -join "、")
    }
    $text
}

$null = Invoke-ReadCheck -Name "/api/toview" -Path "/api/toview?offset=0&limit=50" -Describe {
    param($json)
    if ($null -eq $json.PSObject.Properties["total"]) { throw "响应缺少 total 字段" }
    if ($null -eq $json.PSObject.Properties["items"]) { throw "响应缺少 items 字段" }
    $items = @($json.items)
    $text = "total=" + [int]$json.total + "，本页 " + $items.Count + " 条"
    if ($items.Count -gt 0) {
        $text += "；首条：" + (Get-ShortText ([string]$items[0].title) 24)
    }
    $text
}

# ---------------------------------------------------------------- 汇总

$script:Timer.Stop()
$allChecks = @($script:Checks)
$failedChecks = @($allChecks | Where-Object { -not $_.Ok })
$passedCount = $allChecks.Count - $failedChecks.Count

Write-Section "只读冒烟结果"
$summaryColor = "Green"
if ($failedChecks.Count -gt 0) { $summaryColor = "Red" }
Write-Host ("通过 {0}/{1} 项，总耗时 {2:N1} 秒" -f $passedCount, $allChecks.Count, $script:Timer.Elapsed.TotalSeconds) -ForegroundColor $summaryColor

if ($failedChecks.Count -gt 0) {
    Write-Host ""
    Write-Host "失败的检查项：" -ForegroundColor Red
    foreach ($check in $failedChecks) {
        Write-Host ("  - " + $check.Name + "：" + $check.Detail) -ForegroundColor Red
    }
    Write-Host ""
    Write-Info "常见原因：未登录（401）、B 站风控冷却（429）、服务端内部错误（502）。"
    Write-Info "详细日志：%APPDATA%\BiliCleaner\logs\operations.jsonl"
    exit 1
}

# ---------------------------------------------------------------- 写测试（可选）

if (-not $IncludeWriteTest) {
    if ($UnfollowMid -gt 0 -or -not [string]::IsNullOrWhiteSpace($UnfavResource)) {
        Write-Warn "检测到写测试参数，但没有加 -IncludeWriteTest，已忽略（不会执行任何写操作）。"
    }
    Write-Host ""
    Write-Ok "只读冒烟完成，未修改任何数据。"
    Write-Info "写测试请加 -IncludeWriteTest 并显式指定单个对象，风险与回滚见 docs/SMOKE-TEST.md。"
    exit 0
}

Write-Section "写测试（真实写操作，不可撤销）"
Write-Host "写测试一次只允许一个对象，且执行前需要输入 y 确认。" -ForegroundColor White

$hasUnfollow = ($UnfollowMid -gt 0)
$hasUnfav = -not [string]::IsNullOrWhiteSpace($UnfavResource)

if ($hasUnfollow -eq $hasUnfav) {
    Write-Err "写测试必须且只能指定一个对象："
    Write-Info '    取关：        -IncludeWriteTest -UnfollowMid <mid>'
    Write-Info '    取消收藏：    -IncludeWriteTest -FavFolderId <media_id> -UnfavResource "<id:type>"'
    exit 4
}

if ($hasUnfollow) {
    Invoke-UnfollowWriteTest -Mid $UnfollowMid
}
else {
    Invoke-UnfavWriteTest -FolderId $FavFolderId -Resource $UnfavResource
}

exit 0
