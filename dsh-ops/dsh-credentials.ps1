<#
.SYNOPSIS
    DSH 凭证读取模块（dot-source 用）— 从 $HOME\.dsh\.credentials.yaml 读取简单 key: value 凭证。

.DESCRIPTION
    DeepSeek Harness 的凭证文件是 `~/.dsh/.credentials.yaml`（provider 拥有值）。
    本模块提供 Get-DshCredential，供 dsh-ops 脚本在启动 Trinity 服务/任务前
    注入 TRINITY_PG_* / TRINITY_API_KEY 等敏感配置，避免在脚本或 trinity.yaml
    中硬编码明文（尤其避免明文进入 git 仓库）。

    用法：
        . (Join-Path $PSScriptRoot "dsh-credentials.ps1")
        $pw = Get-DshCredential "TRINITY_PG_PASSWORD"

    解析规则：`key: value` 行，value 支持引号包裹；# 注释行忽略。
    文件缺失或 key 不存在返回 $null。
#>
function Get-DshCredential {
    param([string]$Name)
    $credFile = Join-Path $env:USERPROFILE ".dsh\.credentials.yaml"
    if (-not (Test-Path $credFile)) { return $null }
    $pattern = "^$([regex]::Escape($Name))\s*:\s*(.*)$"
    foreach ($line in Get-Content $credFile) {
        if ($line -match $pattern) {
            $v = $Matches[1].Trim()
            if ($v -match '^"(.*)"$' -or $v -match "^'(.*)'$") { return $Matches[1] }
            return $v
        }
    }
    return $null
}
