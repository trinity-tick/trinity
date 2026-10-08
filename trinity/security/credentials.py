"""Unified PG credential resolution + psycopg2.connect patch (2026-09-02).

背景：brain/*、api/* 等 90+ 模块硬编码 psycopg2.connect(host="127.0.0.1", ...,
user=os.environ.get("TRINITY_PG_USER", "trinity"), password=os.environ.get("TRINITY_PG_PASSWORD", ""))。此处集中解析凭证并全局补丁 psycopg2.connect：
仅当调用点参数等于默认兜底值时用解析值覆盖（尊重显式定制的非默认参数）。

解析优先级：环境变量 TRINITY_PG_* → ~/.dsh/.credentials.yaml → 默认值。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Optional
try:
    from trinity._swallow import swallow  # L1 静默失败治理（2026-09-13）
except Exception:
    def swallow(site: str, exc: Any = None, *, detail: str = "") -> None:
        # 2026-09-13（659.40）：本块可能位于模块级 sys.path 操纵**之前**，
        # 此时 from trinity._swallow import 会失败 → 埋点静默退化为空操作。
        # 改为**首次调用时惰性重导入**：异常真正发生时 sys.path 早已就绪。
        try:
            from trinity._swallow import swallow as _real
            globals()["swallow"] = _real
            return _real(site, exc, detail=detail)
        except Exception:
            return None

_FALLBACK: Dict[str, Any] = {
    "host": "127.0.0.1",
    "port": 5432,
    "dbname": "trinity",
    "user": "trinity",
    "password": "trinity",
}
_ENV_MAP = {
    "TRINITY_PG_HOST": "host",
    "TRINITY_PG_PORT": "port",
    "TRINITY_PG_DB": "dbname",
    "TRINITY_PG_USER": "user",
    "TRINITY_PG_PASSWORD": "password",
}


def pg_env_or_resolved(env_key: str, default: str = "") -> str:
    """PG 参数的**统一回落**：`os.environ` 优先 → 统一凭据解析（env → yaml → 默认）。

    2026-09-30（外部审计 · 系统性缺陷修复）：全仓实测有 **171 处**形如

        password=os.environ.get("TRINITY_PG_PASSWORD", "")

    的直连写法 —— 它们**只读环境变量**，**不查 `~/.dsh/.credentials.yaml`**，
    口令默认**空串**。而口令只由**监督器**注入进程环境 ⇒ 任何**不是监督器拉起**的
    入口（脚本 / MCP / `python -m` / 定时任务）都会
    `psycopg2.OperationalError: fe_sendauth: no password supplied`
    （实测：`trinity/brain/self_axioms.py`、`metamemory.py` 等一批模块的测试即因此变红）。

    语义保证：**环境变量存在时逐字返回它** ⇒ 监督器路径（唯一会设该变量的路径）
    的行为**完全不变**；缺失时才回落到与库内其它模块一致的解析链。
    """
    v = os.environ.get(env_key)
    if v:
        return v
    _env_to_key = {
        "TRINITY_PG_HOST": "host",
        "TRINITY_PG_PORT": "port",
        "TRINITY_PG_DB": "dbname",
        "TRINITY_PG_USER": "user",
        "TRINITY_PG_PASSWORD": "password",
    }
    key = _env_to_key.get(env_key)
    if not key:
        return default
    try:
        got = resolve_credentials().get(key)
    except Exception as _e:  # noqa: BLE001
        swallow(__name__, _e)
        return default
    if got is None or got == "":
        return default
    return str(got)


def _load_yaml() -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    try:
        import yaml
        p = Path.home() / ".dsh" / ".credentials.yaml"
        if not p.exists():
            return out
        cfg = yaml.safe_load(p.read_text(encoding="utf-8-sig")) or {}
        # 2026-09-28（体检口径修复）：凭证文件自 2026-09-18 起已是**版本化结构**
        # （顶层 version / refs / records，13 个键**缩进在 `refs` 之下**）。
        # 原实现只读**扁平**顶层键（`if envk in cfg`）⇒ 本函数恒返回 {}，
        # 于是 resolve_credentials() **静默回落 _FALLBACK**（不报错）。
        # 当前之所以没出事故，是因为 _FALLBACK 恰好等于真实值
        # （host/port/dbname/user/password = 127.0.0.1/5432/trinity/trinity/trinity）
        # —— 凭证一旦轮换，这里会静默拿旧默认值继续跑（§13.5 写侧哑线同族）。
        # 逐字兜底：`refs` 打底，顶层覆盖（显式顶层值优先）。
        cfg = {**(cfg.get("refs") or {}), **cfg}
        for envk, key in _ENV_MAP.items():
            if envk in cfg:
                out[key] = int(cfg[envk]) if key == "port" else cfg[envk]
    except Exception as _e:
        swallow(__name__, _e)
    return out


def resolve_credentials() -> Dict[str, Any]:
    """返回解析后的连接参数（env → yaml → 默认）。"""
    creds = dict(_FALLBACK)
    creds.update(_load_yaml())
    for envk, key in _ENV_MAP.items():
        v = os.environ.get(envk)
        if v:
            creds[key] = int(v) if key == "port" else v
    return creds


_CREDS: Dict[str, Any] = resolve_credentials()


def pg_connect(*args: Any, **kwargs: Any):
    """psycopg2.connect with resolved credentials as defaults."""
    import psycopg2
    for k, v in _CREDS.items():
        kwargs.setdefault(k, v)
    return psycopg2.connect(*args, **kwargs)


def patch_psycopg2() -> bool:
    """全局补丁 psycopg2.connect：存量硬编码默认值自动替换为解析凭证。幂等。

    2026-09-30（外部审计 · **一类 171 处缺陷的单点修复**）：

    本补丁的设计意图（见本文件与 `test_pg_credentials_patch.py` 的说明）就是
    「`brain/*`、`api/*` 存量大量 `psycopg2.connect` 字面量由全局补丁在运行时替换」。
    `trinity/__init__.py:27` 已 `import` 本模块 ⇒ 补丁**本应全局生效**。

    但原判据只认 `None` 与 `_FALLBACK` 值（`password` 的兜底是 `"trinity"`）：

        if cur is None or cur == _FALLBACK.get(k):

    而**大量站点传的是空串** `password=os.environ.get("TRINITY_PG_PASSWORD", "")`
    ⇒ `""` 既不等于 `None` 也不等于 `"trinity"` ⇒ **不被替换** ⇒ 非监督器入口
    （脚本 / MCP / `python -m` / 定时任务 / 测试）全部
    `psycopg2.OperationalError: fe_sendauth: no password supplied`（实测命中）。

    **修法**：把**空串**同样视为「未设置」。语义上安全 —— 空口令**不可能**是
    有意的选择（它无法通过认证），而**任何非空显式值仍然逐字保留**
    （5430 docker 桥 / 自定义 host 等照旧不被覆盖）。
    """
    try:
        import psycopg2
    except Exception:
        return False
    if getattr(psycopg2.connect, "_trinity_patched", False):
        return True
    _orig = psycopg2.connect

    def _patched(*args: Any, **kwargs: Any):
        for k, v in _CREDS.items():
            cur = kwargs.get(k)
            # 空串也当"未设置"（2026-09-30）：见 docstring。
            if cur is None or cur == "" or cur == _FALLBACK.get(k):
                kwargs[k] = v
        return _orig(*args, **kwargs)

    _patched._trinity_patched = True  # type: ignore[attr-defined]
    psycopg2.connect = _patched
    return True


def resolve_backend() -> str:
    """TRINITY_STORAGE_BACKEND 解析：环境变量 → ~/.dsh/.credentials.yaml → ''。

    2026-09-02（API 自举）：未注入 env 时回退 credentials 文件，使
    python -m trinity.api.server 等任意入口默认走 PG 主存储而非 SQLite 镜像。

    **静默降级复核（2026-10-06 T5）**：本函数有三处「看起来有解析、实际会静默改写/
    静默回落」的行为，均已实测确认，且**不改变行为**（行为由 D28 拍板），只把它们
    变成可读数——`backend_resolution_trace()`：
      ① 环境变量**强制改写**：`TRINITY_STORAGE_BACKEND` 一旦存在即逐字返回，
         覆盖 yaml（这是设计意图，但也是"任何进程都能改后端"的开关）；
      ② SQLite 守卫：`TRINITY_STORE` / `TRINITY_DB_PATH` 任一存在 ⇒ 直接返回 ''，
         **不再看 yaml**（否则隔离评测会误连 PG，见下方注释）；
      ③ yaml 分支**读顶层键**，而凭证文件 2026-09-18 起已版本化（键缩进在 `refs` 下）；
         `refs` 兜底**已刻意回滚**（2026-09-28，会静默把常驻 API 从 SQLite 切到 PG，
         实测 7.29GB + 无响应），因此该分支当前恒返回 ''。
    ③ 的失败面是「凭证轮换/键位变化后**静默**拿默认值继续跑」——trace 把
    「yaml 里其实有值」与「实际生效值」并排暴露出来，避免再出现哑线。
    """
    v = os.environ.get("TRINITY_STORAGE_BACKEND", "").strip().lower()
    if v:
        return v
    # 2026-09-02 fix：TRINITY_DB_PATH / TRINITY_STORE 显式指定 SQLite 时不得被 yaml 的 PG 后端覆盖
    # （pytest fixture 与 LongMemEval runner 只设其一即表达 SQLite 隔离意图；缺守卫曾致
    #  基准 ingest 误写 PG 主库 lme_* agent 8,029 条污染——见第 23 轮）
    if os.environ.get("TRINITY_DB_PATH") or os.environ.get("TRINITY_STORE"):
        return ""
    try:
        import yaml
        p = Path.home() / ".dsh" / ".credentials.yaml"
        if p.exists():
            cfg = yaml.safe_load(p.read_text(encoding="utf-8-sig")) or {}
            # 2026-09-28 ROLLBACK: the refs fallback below is CORRECT (the key really is
            # under `refs`), but enabling it flips resolve_backend() from "" to
            # "postgresql" for every entry point that has neither TRINITY_STORAGE_BACKEND
            # nor TRINITY_STORE set -> those processes would switch SQLite->PG. Combined
            # with the dsh-credentials.ps1 fix this put the resident API on PG, which
            # measured 7.29GB and unresponsive. Reverted until the PG regression is fixed.
            # (The `_load_yaml()` refs fix above is KEPT: it changes no current value,
            #  since the file's values equal _FALLBACK.)
            return str(cfg.get("TRINITY_STORAGE_BACKEND", "")).strip().lower()
    except Exception as _e:
        swallow(__name__, _e)
    return ""


def backend_resolution_trace() -> Dict[str, Any]:
    """`resolve_backend()` 的**可解释读数**（只读；不改变任何行为）。

    返回 {
      "value": 实际生效值,
      "source": 取值来源（env / sqlite_guard / yaml / default(empty)）,
      "env_value", "guard_keys", "yaml_top_level", "yaml_refs", "yaml_error",
      "notes": [人话解释], "silent_risks": [静默降级点],
    }
    """
    env_value = os.environ.get("TRINITY_STORAGE_BACKEND", "").strip().lower()
    guard_keys = [k for k in ("TRINITY_STORE", "TRINITY_DB_PATH") if os.environ.get(k)]
    yaml_top: Optional[str] = None
    yaml_refs: Optional[str] = None
    yaml_error: Optional[str] = None
    try:
        import yaml
        p = Path.home() / ".dsh" / ".credentials.yaml"
        if p.exists():
            cfg = yaml.safe_load(p.read_text(encoding="utf-8-sig")) or {}
            yaml_top = str(cfg.get("TRINITY_STORAGE_BACKEND", "") or "").strip().lower() or None
            yaml_refs = str((cfg.get("refs") or {}).get("TRINITY_STORAGE_BACKEND", "")
                            or "").strip().lower() or None
    except Exception as _e:  # noqa: BLE001
        yaml_error = "%s: %s" % (type(_e).__name__, _e)

    value = resolve_backend()
    if env_value:
        source = "env"
    elif guard_keys:
        source = "sqlite_guard"
    elif value:
        source = "yaml"
    else:
        source = "default(empty)"

    notes: List[str] = []
    silent: List[str] = []
    if env_value:
        notes.append("环境变量优先：%s 被逐字采用（任何进程都可用它改后端）" % "TRINITY_STORAGE_BACKEND")
    if guard_keys:
        notes.append("SQLite 守卫命中（%s）⇒ 不看 yaml，返回 ''" % ",".join(guard_keys))
    if yaml_error:
        notes.append("yaml 读取失败 ⇒ 静默回落空串")
        silent.append("yaml_read_error")
    if yaml_refs and not yaml_top:
        notes.append("凭证文件里该键在 `refs` 下（值=%s），但 refs 兜底**已刻意回滚** "
                     "⇒ 实际生效值为 ''（静默：文件里有值而进程看不到）" % yaml_refs)
        silent.append("refs_fallback_rolled_back")
    if not value and not guard_keys and not env_value and not yaml_top:
        notes.append("无 env / 无守卫 / yaml 无顶层值 ⇒ 返回 ''（落 SQLite 默认路径）")
    return {
        "value": value,
        "source": source,
        "env_value": env_value or None,
        "guard_keys": guard_keys,
        "yaml_top_level": yaml_top,
        "yaml_refs": yaml_refs,
        "yaml_error": yaml_error,
        "notes": notes,
        "silent_risks": silent,
    }


# ── 统一凭据入口：DSN / 连接（2026-10-06 T5 明文凭据收口）─────────────────
# 背景：`scripts/sync_sqlite_to_pg.py` 曾写死
#   PG_URL = os.environ.get("TRINITY_PG_URL", "postgresql://<user>:<口令>@127.0.0.1:5432/trinity")
# —— 明文口令进源码（且带一个"看起来能连"的默认值）。维护脚本/导出工具一律改走
# 本模块：**env → ~/.dsh/.credentials.yaml → 失败即报错**（fail-closed，不再有弱默认）。
def credential_provenance() -> Dict[str, str]:
    """逐键给出**取值来源**：`env` / `yaml` / `fallback`（静默降级的可见化）。

    动机（实测）：`resolve_credentials()` 的兜底 `_FALLBACK` 里仍带一个**弱默认口令**，
    于是「凭证文件读不到」与「凭证文件读到了」在返回值上**看不出区别** ——
    这正是 §"静默降级" 那一族。本函数让调用方（含维护脚本）能区分：
    只有 `yaml`/`env` 才算"已配置凭据"，`fallback` 必须显式接受才可使用。
    """
    y = _load_yaml()
    out: Dict[str, str] = {}
    for envk, key in _ENV_MAP.items():
        if os.environ.get(envk):
            out[key] = "env"
        elif key in y:
            out[key] = "yaml"
        else:
            out[key] = "fallback"
    return out


def resolve_pg_dsn(dbname: Optional[str] = None, *, allow_fallback: bool = False) -> Optional[str]:
    """从统一凭据解析出 PG DSN；**口令为空或是内置兜底值则返回 None**（fail-closed）。

    改前的形态（`scripts/sync_sqlite_to_pg.py`）：明文 DSN 常量作为默认值 ——
    源码里带口令，且永远"看起来能连"。本入口拒绝那种语义：
    `env`（TRINITY_PG_*）→ `~/.dsh/.credentials.yaml` → **fail-closed**，
    内置弱兜底（`_FALLBACK`）只有在 `allow_fallback=True` 时才被接受。

    注意：返回值**包含口令**，不得写进日志/报告/提交信息；需要落盘时请只记
    `dsn_redacted()` 的形态。
    """
    creds = resolve_credentials()
    prov = credential_provenance()
    pw = str(creds.get("password") or "")
    if not pw:
        return None
    if prov.get("password") == "fallback" and not allow_fallback:
        return None
    from urllib.parse import quote
    user = quote(str(creds.get("user") or ""), safe="")
    return "postgresql://%s:%s@%s:%s/%s" % (
        user, quote(pw, safe=""), creds.get("host"), creds.get("port"),
        dbname or creds.get("dbname"))


def dsn_redacted(dsn: str) -> str:
    """把 DSN 里的口令替换为 ***（用于日志/报告）。"""
    try:
        scheme, _, rest = str(dsn).partition("://")
        creds, _, host = rest.partition("@")
        user = creds.split(":", 1)[0]
        return "%s://%s:***@%s" % (scheme, user, host)
    except Exception:  # noqa: BLE001
        return "***"


# ── LLM API key 解析（2026-09-18 事故修复）────────────────────────────
# 背景：2026-09-17 21:58 harness 轮换了 DEEPSEEK_API_KEY，Trinity 侧凭证文件
# 仍留旧 key；而 trinity/llm/client.py::resolve_api_key() **只读环境变量**，
# 导致未被注入 env 的入口（API 服务 / MCP / 直接脚本）全链 401，推理脑停摆。
# 此处把 key 解析与 PG 凭证统一到同一套"env → 凭证文件"优先级，
# 并增加第二级回退（harness 自己的凭证文件，轮换时新 key 先落那里）。
_LLM_KEY_NAMES = ("TRINITY_LLM_API_KEY", "DEEPSEEK_API_KEY")
_LLM_KEY_FALLBACK_OFF = ("0", "off", "false", "no")


def _credential_paths() -> List[Path]:
    """LLM key 凭证文件搜索链（顺序即优先级）。

    TRINITY_CREDENTIAL_FILE 覆盖第一级（供测试隔离）；
    TRINITY_HARNESS_CREDENTIAL_FILE 覆盖第二级。
    """
    paths: List[Path] = []
    override = os.environ.get("TRINITY_CREDENTIAL_FILE", "").strip()
    paths.append(Path(override) if override else Path.home() / ".dsh" / ".credentials.yaml")
    harness_override = os.environ.get("TRINITY_HARNESS_CREDENTIAL_FILE", "").strip()
    if harness_override:
        paths.append(Path(harness_override))
    else:
        appdata = os.environ.get("APPDATA", "").strip()
        if appdata:
            paths.append(Path(appdata) / "DeepSeek Harness" / "harness-home" / ".credentials.yaml")
    return paths


def _read_llm_key_from_file(path: Path) -> Optional[str]:
    """从简单 `key: value` 凭证文件读 LLM key（忽略注释行；支持引号包裹）。"""
    try:
        if not path.exists():
            return None
        for raw in path.read_text(encoding="utf-8-sig", errors="ignore").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or ":" not in line:
                continue
            name, _, value = line.partition(":")
            if name.strip() in _LLM_KEY_NAMES:
                v = value.strip().strip('"').strip("'").rstrip(",").strip()
                if v:
                    return v
    except Exception as _e:
        swallow(__name__, _e)
    return None


def resolve_llm_api_key() -> Optional[str]:
    """解析 LLM API key（env → ~/.dsh/.credentials.yaml → harness 凭证文件）。

    回滚开关：TRINITY_LLM_KEY_FILE_FALLBACK=0 时退回"纯 env"旧语义。
    """
    for name in _LLM_KEY_NAMES:
        v = os.environ.get(name, "").strip()
        if v:
            return v
    if os.environ.get("TRINITY_LLM_KEY_FILE_FALLBACK", "1").strip().lower() in _LLM_KEY_FALLBACK_OFF:
        return None
    for p in _credential_paths():
        v = _read_llm_key_from_file(p)
        if v:
            return v
    return None


_patch_applied: bool = patch_psycopg2()
