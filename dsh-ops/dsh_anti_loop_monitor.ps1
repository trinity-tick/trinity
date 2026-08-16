# dsh_anti_loop_monitor.ps1
# 功能：监控 dsh 进程，识别并清理空转实例，保留正常服务
# 判定规则：
#   - dsh web 服务进程（命令行含 @deepseek-ai\dsh\lib\bin.js）：
#       * 监听 127.0.0.1:3080 且有活跃连接 -> 正常服务，保留
#       * 不监听 3080 且 CPU 时间 > 阈值   -> 空转，终止（连同其 npx 父进程）
#       * 不监听 3080 且 CPU 时间 < 阈值   -> 启动中，保留（下次再判）
#   - npx 包装进程（命令行含 npx-cli.js 且含 dsh）：
#       * 其子进程（dsh web）正常 -> 保留
#       * 其子进程被判定为空转   -> 一并终止
# 用法：powershell -ExecutionPolicy Bypass -File dsh_anti_loop_monitor.ps1

param(
    [int]$CpuThresholdSec = 60,    # CPU 时间阈值（秒），超过且无服务特征视为空转
    [int]$CheckIntervalSec = 30,   # 检查间隔（秒）
    [string]$LogFile = "C:\Users\Administrator\.trinity\logs\dsh_anti_loop.log"
)

# 确保日志目录存在
$logDir = Split-Path $LogFile -Parent
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir -Force | Out-Null }

function Write-Log {
    param([string]$Message)
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    "$ts $Message" | Out-File -FilePath $LogFile -Append -Encoding UTF8
    Write-Host "$ts $Message"
}

Write-Log "dsh 空转监控启动，CPU阈值=${CpuThresholdSec}s，检查间隔=${CheckIntervalSec}s"

while ($true) {
    try {
        $dshProcs = Get-CimInstance Win32_Process | Where-Object {
            $_.Name -eq 'node.exe' -and $_.CommandLine -match 'dsh'
        }

        # 分类进程
        $webProcs = @()  # dsh web 服务进程
        $npxProcs = @()  # npx 包装进程

        foreach ($p in $dshProcs) {
            $cmd = $p.CommandLine
            if ($cmd -match 'dsh\\lib\\bin\.js') {
                $webProcs += $p
            } elseif ($cmd -match 'npx-cli\.js' -and $cmd -match 'dsh') {
                $npxProcs += $p
            }
        }

        # 检查 dsh web 服务进程
        foreach ($p in $webProcs) {
            $cpu = [math]::Round(($p.KernelModeTime + $p.UserModeTime) / 1e7, 1)
            $mem = [math]::Round($p.WorkingSetSize / 1MB, 1)
            $listen = Get-NetTCPConnection -OwningProcess $p.ProcessId -State Listen -ErrorAction SilentlyContinue | Where-Object { $_.LocalPort -eq 3080 }
            $est = (Get-NetTCPConnection -OwningProcess $p.ProcessId -State Established -ErrorAction SilentlyContinue | Measure-Object).Count

            if ($listen) {
                # 监听 3080 -> 正常服务，保留
                Write-Log "dsh web PID=$($p.ProcessId) 正常（监听3080，连接=$est，CPU=${cpu}s）"
            } elseif ($cpu -gt $CpuThresholdSec) {
                # 不监听 3080 且 CPU 时间高 -> 空转，终止
                Write-Log "检测到空转 dsh PID=$($p.ProcessId) CPU=${cpu}s Mem=${mem}MB，终止"
                Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
                # 同时终止其 npx 父进程
                $parent = $npxProcs | Where-Object { $_.ProcessId -eq $p.ParentProcessId }
                if ($parent) {
                    Write-Log "终止 npx 包装进程 PID=$($parent.ProcessId)"
                    Stop-Process -Id $parent.ProcessId -Force -ErrorAction SilentlyContinue
                }
            } else {
                # 不监听 3080 且 CPU 时间低 -> 启动中，保留
                Write-Log "dsh PID=$($p.ProcessId) 启动中（CPU=${cpu}s），保留"
            }
        }
    } catch {
        Write-Log "监控异常: $_"
    }

    Start-Sleep -Seconds $CheckIntervalSec
}
