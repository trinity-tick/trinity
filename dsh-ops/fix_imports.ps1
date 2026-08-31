$ErrorActionPreference = "Continue"
$gw = "D:\smartcos-wms\backend\internal\gateway"
Set-Location "D:\smartcos-wms\backend"
for ($r = 0; $r -lt 30; $r++) {
    $o = go build ./internal/gateway/ 2>&1
    $errs = ($o | Out-String)
    if ($errs -notmatch "imported and not used") {
        Write-Output "BUILD OK round $r"
        break
    }
    $fixed = 0
    foreach ($line in ($errs -split "`n")) {
        if ($line -notmatch "imported and not used") { continue }
        if ($line -match "^(.+?):\d+:\d+: ") { $loc = $Matches[1] } else { continue }
        $fname = ($loc -split "\\")[-1]
        if ($line -match '"([^"]+)"') { $path = $Matches[1] } else { continue }
        $fp = Join-Path $gw $fname
        if (-not (Test-Path $fp)) { continue }
        $lines = [System.IO.File]::ReadAllLines($fp)
        $tgt = '"' + $path + '"'
        for ($i = 0; $i -lt $lines.Count; $i++) {
            if ($lines[$i].Trim() -eq $tgt) {
                $out = $lines[0..($i-1)] + $lines[($i+1)..($lines.Count-1)]
                [System.IO.File]::WriteAllLines($fp, $out)
                $fixed++
                break
            }
        }
    }
    Write-Output "round $r fixed $fixed"
    if ($fixed -eq 0) { Write-Output "STUCK"; ($errs -split "`n")[1..3]; break }
}
