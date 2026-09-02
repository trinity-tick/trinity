# 2026-09-02 (round 12/15): 修复 editable finder 注册顺序（pip 重装后需重跑本脚本）
# 背景：pip editable 安装生成的 __editable___trinity_memory_*_finder.py 用
# sys.meta_path.append 注册，排在 PathFinder 之后——从含 trinity 目录的 cwd 启动时
# PathFinder 先命中 namespace 包（unknown location 报错）。需改为 insert(0)。
$ErrorActionPreference = 'Stop'
$sp = 'C:\Users\Administrator\AppData\Local\Programs\Python\Python314\Lib\site-packages'
$finder = Get-ChildItem $sp -Filter '__editable___trinity_memory_*_finder.py' | Select-Object -First 1
if (-not $finder) {
  Write-Host "no editable finder found — trinity 未以 editable 方式安装？" -ForegroundColor Yellow
  exit 1
}
$content = Get-Content $finder.FullName -Raw
if ($content -match 'sys\.meta_path\.insert\(0, _EditableFinder\)') {
  Write-Host "finder already patched: $($finder.Name)" -ForegroundColor Green
  exit 0
}
$patched = $content -replace 'sys\.meta_path\.append\(_EditableFinder\)', 'sys.meta_path.insert(0, _EditableFinder)  # 2026-09-02: 真实包优先于 cwd namespace'
if ($patched -eq $content) {
  Write-Host "append pattern not found — manual review needed" -ForegroundColor Yellow
  exit 2
}
[IO.File]::WriteAllText($finder.FullName, $patched, [Text.UTF8Encoding]::new($false))
Write-Host "patched: $($finder.Name) (append -> insert(0))" -ForegroundColor Green
# verify
$py = 'C:\Users\Administrator\AppData\Local\Programs\Python\Python314\python.exe'
$out = & $py -c "import trinity; print(trinity.__file__)" 2>&1 | Select-Object -Last 1
Write-Host "import trinity -> $out"
