# elevated-pg-start.ps1 — 以提权身份启动 trinity-pg 服务（supervisor 非提权上下文调用）
# 2026-09-01: 修复 supervisor Start-Service 非提权必败缺口（UAC ConsentPromptBehaviorAdmin=0 静默提权）
$marker = 'C:\Users\Administrator\.trinity\logs\elevated-pg-start.log'
try {
  Start-Service trinity-pg -ErrorAction Stop
  Start-Sleep -Seconds 3
  Set-Content -Path $marker -Value ((Get-Date -Format s) + ' OK ' + (Get-Service trinity-pg).Status) -Encoding UTF8
  exit 0
} catch {
  Set-Content -Path $marker -Value ((Get-Date -Format s) + ' ERR: ' + $_.Exception.Message) -Encoding UTF8
  exit 1
}
