# trinity-memory-guard.ps1 - E1 commit-memory watchdog (scheduled every 1min)
$log = 'C:\Users\Administrator\.trinity\logs\trinity-memory-guard.log'
function Guard-Log($msg){ Add-Content -Path $log -Value ((Get-Date -Format 'yyyy-MM-dd HH:mm:ss') + ' ' + $msg) -Encoding UTF8 }
try {
  $os = Get-CimInstance Win32_OperatingSystem
  $totalGB = [math]::Round($os.TotalVirtualMemorySize/1MB,1)
  $freeGB = [math]::Round($os.FreeVirtualMemory/1MB,1)
  $managedRe = 'trinity\.api\.server|trinity\.mcp\.server|collector|gateway\.server|memory_stream_server|engine_worker'
  $procs = @(Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -and $_.CommandLine -match $managedRe })
  $maxProc = $procs | Sort-Object PrivateMemorySize64 -Descending | Select-Object -First 1
  $biggestGB = 0.0
  if($maxProc){ $biggestGB = [math]::Round($maxProc.PrivateMemorySize64/1GB,2) }
  if ($freeGB -lt 10.0) {
    Guard-Log ('WARN commit-free ' + $freeGB + 'GB of ' + $totalGB + 'GB < 10GB - restart biggest managed proc PID ' + $maxProc.ProcessId + ' (' + $biggestGB + 'GB)')
    if($maxProc){ Stop-Process -Id $maxProc.ProcessId -Force -ErrorAction SilentlyContinue }
  } elseif ($biggestGB -gt 12.0) {
    Guard-Log ('WARN biggest managed proc PID ' + $maxProc.ProcessId + ' ' + $biggestGB + 'GB > 12GB - restarting')
    if($maxProc){ Stop-Process -Id $maxProc.ProcessId -Force -ErrorAction SilentlyContinue }
  } else {
    $tick = [int]([math]::Floor((Get-Date).ToUniversalTime().Ticks / 1e7) % 600)
    if ($tick -lt 20) { Guard-Log ('OK commit-free ' + $freeGB + 'GB biggest ' + $biggestGB + 'GB') }
  }
} catch { Guard-Log ('ERROR ' + $_.Exception.Message) }
