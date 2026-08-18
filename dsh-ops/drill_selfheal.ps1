# Trinity 故障演练脚本（2026-08-18, SRE 制度化）
# 用法: powershell -NoProfile -ExecutionPolicy Bypass -File dsh-ops\drill_selfheal.ps1
# 行为: kill api/mcp/gateway/collector → 等待 supervisor 自愈 → 验证恢复 → 输出演练报告
param([int]$MaxWaitSec = 600, [string]$LogDir = "C:\Users\Administrator\.trinity\logs")

$ErrorActionPreference = "Continue"
$start = Get-Date
Write-Output "=== Trinity 故障演练开始: $start ==="

# 1. kill 全服务
$targets = Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -match 'trinity\.api\.server|trinity\.mcp\.server|gateway\.server' }
foreach ($t in $targets) { taskkill /F /PID $t.ProcessId 2>&1 | Out-Null }
$cp = (Get-Content 'C:\Users\Administrator\trinity\data\collector.pid' -ErrorAction SilentlyContinue).Trim()
if ($cp) { taskkill /F /PID $cp 2>&1 | Out-Null }
Write-Output "killed $($targets.Count) services + collector"

# 2. 等待自愈（supervisor 5 分钟轮询）
$apiOk = $false; $mcpOk = $false; $gwOk = $false
$deadline = (Get-Date).AddSeconds($MaxWaitSec)
$gwKey = (Get-Content 'C:\Users\Administrator\.dsh\.credentials.yaml' | Select-String '^GATEWAY_API_KEY:' | ForEach-Object { $_.Line.Split(':')[1].Trim() })
while ((Get-Date) -lt $deadline -and -not ($apiOk -and $mcpOk -and $gwOk)) {
    Start-Sleep -Seconds 8
    try { $apiOk = ((Invoke-WebRequest -Uri 'http://127.0.0.1:8001/health' -TimeoutSec 3 -UseBasicParsing -ErrorAction Stop).StatusCode -eq 200) } catch { $apiOk = $false }
    try { $gwOk = ((Invoke-WebRequest -Uri 'http://127.0.0.1:8002/v1/models' -TimeoutSec 3 -UseBasicParsing -Headers @{Authorization=('Bearer ' + $gwKey)} -ErrorAction Stop).StatusCode -eq 200) } catch { $gwOk = $false }
    $mcpOk = (Test-NetConnection -ComputerName 127.0.0.1 -Port 8000 -WarningAction SilentlyContinue -InformationLevel Quiet)
}
$elapsed = [math]::Round(((Get-Date) - $start).TotalSeconds, 1)
$result = if ($apiOk -and $mcpOk -and $gwOk) { "PASS" } else { "FAIL" }

# 3. 报告
Write-Output "=== Trinity 故障演练报告 ==="
Write-Output "started: $start"
Write-Output "recovery_seconds: $elapsed"
Write-Output "api: $apiOk"
Write-Output "mcp: $mcpOk"
Write-Output "gateway: $gwOk"
Write-Output "result: $result"
try { Add-Content -Path (Join-Path $LogDir "drill-selfheal.log") -Value ((Get-Date -Format "yyyy-MM-dd HH:mm:ss") + " | $result | $($elapsed)s") -Encoding UTF8 } catch { }
if ($result -eq "FAIL") { exit 1 } else { exit 0 }
