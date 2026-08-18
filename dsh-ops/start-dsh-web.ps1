<#
.SYNOPSIS
    DSH web 宿主守卫启动（2026-08-18）
.DESCRIPTION
    若 127.0.0.1:3080 已被监听（已有 web 宿主）则跳过；否则拉起 dsh web
    并记录日志 ~/.dsh/logs/dsh-web-autostart.log。
    由 dsh-web-autostart.vbs 在登录时以隐藏窗口调用。
#>
$ErrorActionPreference = "Continue"
$LogFile = "C:\Users\Administrator\.dsh\logs\dsh-web-autostart.log"
function Write-Log {
    param([string]$m)
    try { Add-Content -Path $LogFile -Value ("{0} {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $m) -Encoding UTF8 } catch {}
}

$listening = Get-NetTCPConnection -LocalPort 3080 -State Listen -ErrorAction SilentlyContinue
if ($listening) {
    Write-Log "port 3080 already listening (pid=$($listening[0].OwningProcess)) - skip autostart"
    exit 0
}

$dshCmd = "C:\Users\Administrator\AppData\Roaming\npm\dsh.cmd"
if (-not (Test-Path $dshCmd)) {
    Write-Log "dsh CLI not found at $dshCmd - skip"
    exit 1
}

try {
    # 隐藏窗口启动 dsh web，输出重定向到日志
    $out = "C:\Users\Administrator\.dsh\logs\web.autostart.out.log"
    $err = "C:\Users\Administrator\.dsh\logs\web.autostart.err.log"
    $quoted = '"' + $dshCmd + '"'
    $p = Start-Process -FilePath "cmd.exe" -ArgumentList '/c', $quoted, 'web' -WindowStyle Hidden -RedirectStandardOutput $out -RedirectStandardError $err -PassThru
    Write-Log "dsh web autostarted (pid=$($p.Id))"
} catch {
    Write-Log "autostart failed: $_"
}
