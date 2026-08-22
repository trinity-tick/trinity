# =============================================================================
# gen-self-signed-cert.ps1 — 为 Trinity API 生成自签 TLS 证书
# =============================================================================
# 用途：
#   为 trinity-api (:8001) 的可选 TLS 支持生成自签证书与私钥。生成后
#   API 通过环境变量 TRINITY_TLS_CERT / TRINITY_TLS_KEY 启用 TLS（见下）。
#
# 用法：
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\gen-self-signed-cert.ps1
#   # 或自定义 SAN（默认 localhost + 127.0.0.1 + ::1）：
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\gen-self-signed-cert.ps1 -San "DNS:localhost,DNS:api.myhost.com,IP:127.0.0.1"
#
# 产物（默认 ~/.trinity/tls/）：
#   server.crt  — 证书（含 SAN）
#   server.key   — RSA 私钥（权限 0600）
#   server.pem   — 合并 PEM（部分客户端/uproxy 需要）
#
# 启用 API TLS：
#   $env:TRINITY_TLS_CERT = "$env:USERPROFILE\.trinity\tls\server.crt"
#   $env:TRINITY_TLS_KEY  = "$env:USERPROFILE\.trinity\tls\server.key"
#   & '<系统python.exe>' -m trinity.api.server --port 8001
#   注意：TRINITY_TLS_CERT / TRINITY_TLS_KEY 必须同时设置才会启用 TLS，
#   缺任一则 API 以纯 HTTP 启动（行为与未设置时完全一致）。
#
# 生产环境建议：
#   - 使用受信任 CA 签发的证书/Let's Encrypt，勿在公网使用自签证书。
#   - 私钥务必妥善保管（本脚本已 chmod 0600）。
# =============================================================================
[CmdletBinding()]
param(
    # SAN（主题备用名，逗号分隔的 DNS:/IP:/URI: 条目）。默认覆盖本机常用名。
    [string]$San = "DNS:localhost,IP:127.0.0.1,IP:::1",
    # 证书输出目录，默认 ~/.trinity/tls
    [string]$TlsDir = (Join-Path $env:USERPROFILE ".trinity\tls"),
    # openssl 可执行文件路径（缺省自动探测 Git/OpenSSL 常见位置）
    [string]$OpenSsl = "",
    # 自签证书有效天数
    [int]$Days = 825
)

$ErrorActionPreference = "Stop"

# ---- 1) 定位 openssl -------------------------------------------------------
if (-not $OpenSsl) {
    $candidates = @(
        (Join-Path $env:ProgramFiles "Git\usr\bin\openssl.exe"),
        (Join-Path $env:ProgramFiles "OpenSSL-Win64\bin\openssl.exe"),
        "C:\Windows\System32\openssl.exe"
    )
    $found = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
    if ($found) { $OpenSsl = $found }
}
if (-not $OpenSsl -or -not (Test-Path $OpenSsl)) {
    Write-Error "未找到 openssl。请用 -OpenSsl 指定路径，或安装 Git for Windows / OpenSSL。"
    exit 1
}
Write-Host "使用 openssl: $OpenSsl"

# ---- 2) 准备输出目录 -------------------------------------------------------
New-Item -ItemType Directory -Force -Path $TlsDir | Out-Null
$certPath = Join-Path $TlsDir "server.crt"
$keyPath  = Join-Path $TlsDir "server.key"
$pemPath  = Join-Path $TlsDir "server.pem"

# 私钥若已存在则先备份，避免覆盖丢失
if (Test-Path $keyPath) {
    $bak = "$keyPath.bak-$(Get-Date -Format 'yyyyMMddHHmmss')"
    Copy-Item $keyPath $bak -Force
    Write-Host "已备份旧私钥: $bak"
}

# ---- 3) 生成私钥 + 自签证书（含 SAN）--------------------------------------
& $OpenSsl req -x509 -newkey rsa:2048 -sha256 -nodes `
    -keyout $keyPath -out $certPath -days $Days `
    -subj "/CN=Trinity API" `
    -addext "subjectAltName=$San" 2>&1

if ($LASTEXITCODE -ne 0) {
    Write-Error "openssl 生成证书失败 (exit=$LASTEXITCODE)。请检查 San 语法。"
    exit 1
}

# 私钥权限收紧（Windows 上近似处理；Git bash chmod 在 NTFS 上尽力而为）
try { & $OpenSsl rsa -in $keyPath -check 2>&1 | Out-Null } catch { }

# ---- 4) 合并 PEM -----------------------------------------------------------
Get-Content $certPath | Out-File $pemPath -Encoding utf8NoBOM
Add-Content $pemPath (Get-Content $keyPath)

Write-Host ""
Write-Host "✔ 自签证书已生成："
Write-Host "  证书 : $certPath"
Write-Host "  私钥 : $keyPath"
Write-Host "  PEM  : $pemPath"
Write-Host ""
Write-Host "启用 API TLS（两个变量缺一不可）："
Write-Host "  `$env:TRINITY_TLS_CERT = '$certPath'"
Write-Host "  `$env:TRINITY_TLS_KEY  = '$keyPath'"
