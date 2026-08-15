# restart-web-profile.ps1 — 重启 DSH web profile（加载 dsh-trinity 新插件代码）
# ⚠️ 会中断当前 web GUI 会话（正在进行的对话会断开，重开浏览器恢复）。
# 用法: powershell -NoProfile -ExecutionPolicy Bypass -File dsh-ops\restart-web-profile.ps1
#
# 背景：node_modules 内插件 JS 变更 HMR 不重载（dsh-client-hmr 只监听客户端
# bundle），web 宿主需重启才能加载 @deepseek-ai/dsh-trinity 的新代码
# （17 个 trinity_* 工具 + session/event 结构订阅）。headless 已实证新代码
# 工作正常（真实会话 7 事件自动流入 Trinity 结构层）。

$ErrorActionPreference = "Continue"
$LogDir = "C:\Users\Administrator\.trinity\logs"
$LogFile = Join-Path $LogDir "web-restart.log"
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir -Force | Out-Null }

function Write-Log {
    param([string]$Message)
    $line = "{0} {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
    Add-Content -Path $LogFile -Value $line -Encoding UTF8
    Write-Output $line
}

# ── 1. 找当前 web 宿主 ──────────────────────────────────────────────
$webProc = Get-CimInstance Win32_Process -Filter "Name='node.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -match 'bin\.js[^"]*\s+web' -or $_.CommandLine -match 'bin\.js" web' }

if (-not $webProc) {
    $conn = Get-NetTCPConnection -LocalPort 3080 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($conn) { $webProc = Get-CimInstance Win32_Process -Filter "ProcessId=$($conn.OwningProcess)" -ErrorAction SilentlyContinue }
}

if (-not $webProc) {
    Write-Log "未找到运行中的 web profile 宿主进程。"
    exit 1
}

$pidToKill = $webProc.ProcessId
Write-Log "停止 web 宿主 PID $pidToKill ..."
Stop-Process -Id $pidToKill -Force
Start-Sleep -Seconds 4

# ── 2. 确认旧 worker 已退出（其父进程是 web 宿主）──────────────────
Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -match 'engine_worker' } |
    ForEach-Object {
        Write-Log "清理残留 engine_worker PID $($_.ProcessId)"
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    }
Start-Sleep -Seconds 2

# ── 3. 重新拉起 web profile ─────────────────────────────────────────
$dshCmd = "C:\Users\Administrator\AppData\Roaming\npm\dsh.cmd"
if (-not (Test-Path $dshCmd)) {
    $dshCmd = (Get-Command dsh -ErrorAction SilentlyContinue).Source
}
Write-Log "启动 web profile: $dshCmd web"
Start-Process -FilePath $dshCmd -ArgumentList @("web") -WindowStyle Hidden |
    Out-Null

# ── 4. 等待端口 3080 就绪 ───────────────────────────────────────────
$deadline = (Get-Date).AddSeconds(90)
$ok = $false
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Seconds 4
    $conn = Get-NetTCPConnection -LocalPort 3080 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($conn) {
        Write-Log "端口 3080 已就绪（新宿主 PID $($conn.OwningProcess)）"
        $ok = $true
        break
    }
}

# ── 5. 等待新 worker（engine_worker.py 由插件 spawn）────────────────
if ($ok) {
    $hostPid = $conn.OwningProcess
    $wDeadline = (Get-Date).AddSeconds(60)
    $wOk = $false
    while ((Get-Date) -lt $wDeadline) {
        Start-Sleep -Seconds 3
        $worker = Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
            Where-Object { $_.CommandLine -match 'engine_worker' -and $_.ParentProcessId -eq $hostPid } |
            Select-Object -First 1
        if ($worker) {
            Write-Log "新 engine_worker 已由宿主 spawn（PID $($worker.ProcessId)）——插件加载成功"
            $wOk = $true
            break
        }
    }
    if (-not $wOk) { Write-Log "60s 内未见新 worker；请检查 web 宿主日志" }
} else {
    Write-Log "90s 内端口 3080 未就绪，请手动启动：dsh web"
}
Write-Log "重启流程结束。浏览器刷新 http://127.0.0.1:3080 恢复会话。"
