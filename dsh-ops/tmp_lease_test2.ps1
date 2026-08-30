$env:TRINITY_STORE = "D:\trinity-data\store"
$env:TRINITY_MEMORY_ENABLED = "0"
$env:PYTHONPATH = "D:\trinity-code"
$Py = "C:\Users\Administrator\AppData\Local\Programs\Python\Python314\python.exe"
# 模拟 maintenance 的 $tmpPy：写临时 py 文件（runpy 方式）
$tmpPy = Join-Path $env:TEMP "tmp_lease_test.py"
@"
import sys, os
sys.path.insert(0, r"D:\trinity-code")
os.environ.setdefault("HF_HUB_OFFLINE", "1")
import runpy
sys.argv = ["pg_integrity_monitor"]
runpy.run_path(r"D:\trinity-code\scripts\pg_integrity_monitor.py", run_name="__main__")
"@ | Set-Content -Path $tmpPy -Encoding UTF8
$out = & $Py "D:\trinity-code\scripts\with_lease.py" --job integrity-monitor --db "$env:TRINITY_STORE\trinity_store.db" -- $Py $tmpPy 2>&1
Write-Output "OUT: $out"
Remove-Item $tmpPy -Force
