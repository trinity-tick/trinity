<#
.SYNOPSIS
    DSH credential reader (dot-source this) - reads simple key: value credentials
    from $HOME\.dsh\.credentials.yaml.

.DESCRIPTION
    The DeepSeek Harness credential file is `~/.dsh/.credentials.yaml` (owned by the
    provider). This module provides Get-DshCredential so dsh-ops scripts can inject
    sensitive settings (TRINITY_PG_* / TRINITY_API_KEY ...) before starting Trinity
    services or tasks, instead of hardcoding plaintext in scripts or trinity.yaml.

    Usage:
        . (Join-Path $PSScriptRoot "dsh-credentials.ps1")
        $pw = Get-DshCredential "TRINITY_PG_PASSWORD"

    Parse rule: `key: value` lines; values may be quoted; '# comment' lines ignored.
    Missing file or missing key returns $null.

    NOTE: this file is deliberately ASCII-only so it stays readable and parseable
    even when the UTF-8 BOM is missing. PowerShell 5.1 reads a .ps1 without a BOM as
    ANSI, which turns CJK comments into mojibake and can produce parse errors
    (see ps1-bom-guard.ps1). Do not add non-ASCII text here.

    ==========================================================================
    2026-09-28 healthcheck fix - two parts, read both before changing this file.
    ==========================================================================

    PART 1 - indentation bug (re-applied after a rollback, see PART 2).
    The pattern used to be "^NAME\s*:" - the leading `^` left NO room for
    indentation. But ~/.dsh/.credentials.yaml was migrated on 2026-09-18 to a
    versioned layout (top-level keys: version / refs / records) and all 13 refs
    live 2-space INDENTED under `refs:`. So this function returned $null for
    EVERY key. Two independently measured consequences:

      (a) trinity-dsh-maintenance.ps1:354-359 resolves PG credentials with
          Get-DshCredential and falls back to the literal "postgres" / "postgres"
          when it gets $null. Actual PG creds are trinity/trinity. Measured result
          (daily-chain-20260928.out.log:46,69):
              PG: 127.0.0.1:5432/trinity as postgres
              psycopg2.OperationalError: FATAL: password authentication failed
                  for user "postgres"
          => the `mirror` task (SQLite -> PG) FAILED on every 4h chain run
             (2026-09-27 17:32 / 21:37, 2026-09-28 01:44 / 06:02), so PG never
             received the SQLite rows it was missing.
      (b) trinity-supervisor.ps1:56-61 injects 12 keys from this file; with $null
          for all of them NOTHING was injected.
    Fix: allow leading whitespace ("^\s*"). Rollback: change "^\s*" back to "^".

    PART 2 - why TRINITY_STORAGE_BACKEND is deliberately withheld.
    Re-applying PART 1 alone ALSO restores supervisor's injection of
    TRINITY_STORAGE_BACKEND=postgresql (it is in supervisor.ps1:56's list), which
    silently switches the resident API's storage backend from SQLite to PG. That
    was tried on 2026-09-28 and measured BADLY:
        - API PID 37848 ballooned to 7.29GB (vs 3.19GB on SQLite)
        - became fully unresponsive (even /livez timed out)
        - supervisor logged "api UNHEALTHY beyond grace - kill + restart" twice
          but could not kill it (supervisor is Medium, the API is High), leaving
          port 8001 held by a hung process for ~16 minutes.
    Per D28 (DECISIONS_PENDING.md) the storage backend is an explicit DECISION,
    not something that should ride along as a side effect of fixing a credential
    reader. So this reader WITHHOLDS TRINITY_STORAGE_BACKEND: callers get $null,
    the env var is simply never set, and resolve_backend()
    (trinity/security/credentials.py) falls through to the SQLite default path
    that D28-A selected.
    Rollback: remove the entry from $WithheldKeys below.
    Evidence: dsh-ops/evidence/EXEC-4D-20260928.md
#>

# Keys this reader must never hand out, with the reason. Keep short and justified.
$WithheldKeys = @{
    'TRINITY_STORAGE_BACKEND' = 'explicit D28 decision; injecting it from the credential file silently switches the resident API to PG (measured 2026-09-28: 7.29GB + unresponsive)'
}

function Get-DshCredential {
    param([string]$Name)
    if ($WithheldKeys.ContainsKey($Name)) { return $null }
    $credFile = Join-Path $env:USERPROFILE ".dsh\.credentials.yaml"
    if (-not (Test-Path $credFile)) { return $null }
    $pattern = "^\s*$([regex]::Escape($Name))\s*:\s*(.*)$"
    foreach ($line in Get-Content $credFile) {
        if ($line -match $pattern) {
            $v = $Matches[1].Trim()
            $val = $null
            if ($v -match '^"(.*)"$') {
                # YAML double-quoted scalar: backslash escapes ARE processed -> unescape.
                $val = $Matches[1] -replace '\\\\', '\'
            } elseif ($v -match "^'(.*)'$") {
                # YAML single-quoted scalar: taken literally, EXCEPT that this file is
                # written with DOUBLED backslashes inside single quotes
                #   TRINITY_STORE: 'C:\\Users\\Administrator\\.trinity\\store-restored'
                # Literal reading yields a path whose components are `C:` + `` + `Users` ...
                # which does not exist on disk => every consumer resolves to a MISSING
                # directory, silently degrades to the wrong default (~/.trinity/store) or to
                # a relative cwd path. Measured 2026-10-02: this is why the disaster-recovery
                # store was not picked up. Collapse the separator runs for path-like values.
                $val = $Matches[1] -replace '\\\\', '\'
            } else {
                $val = $v
            }
            # 2026-10-02: the 2026-09-28 "double backslash" note below was about a
            # DOUBLE-quoted scalar; a SINGLE-quoted one was never normalized, so the
            # comment's guarantee did not hold for the current file. Normalizing both
            # branches (above) makes it hold. Rollback: delete the two -replace calls.
            return $val
        }
    }
    return $null
}
