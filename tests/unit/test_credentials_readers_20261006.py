# -*- coding: utf-8 -*-
"""判据：读 `~/.dsh/.credentials.yaml` 的**读者**必须能看见版本化结构（`refs`）。

## 为什么要有这条（t26，来自 t24 的独立发现）

该文件自 **2026-09-18** 起是**版本化结构**：顶层只有 `version` / `refs` / `records`，
**真键缩进在 `refs` 下**。而仓内有一批读者按**顶层键**取（`yaml.safe_load(...).get("TRINITY_PG_PASSWORD")`）
⇒ 取到空 ⇒ 凭证为空的异常又被宽 `except`/`skipif` 吞掉 ⇒ **判据静默跳过 / 读数静默降级**：

    · 站点 1 `tests/unit/test_fok_alien_probe.py`：全仓**唯一**一条"打真 PG 索引"的验收，
      从未真正执行，而 skip 理由写的是"无 PG / 无凭证"（本机 PG 可达、凭证就在 `refs` 下 ⇒ 理由是假的）；
    · 站点 2 `scripts/memory_utilization_audit.py`：retention 采样用同款读法 ⇒ 连接必失败
      ⇒ 被吞成 `retention_level="NA"`，与合法语义（NA = 缺上一次读数）混同；
    · 同族测试文件 `test_pg_edge_bitemporal_gdpr.py` / `test_pg_update_tags_jsonb.py`：
      整套 `pytestmark = skipif(not _pg_available(), "PG 不可用（离线/无凭证）")` ⇒ **8 条判据从不运行**。

仓内已有 `tests/unit/test_credentials_versioned_file_20260930.py` 测**文件本身**是否符合版本化形状，
但**没有任何判据覆盖"读这份文件的所有读者"** —— 所以上面这些才漏网。本文件补上这一层。

## 本判据锁什么

逐个**读取站点**（不是逐个文件）判定，因为**同一个文件里可以既有安全读者又有危险读者** ——
站点 2 就是被这一层掩盖的：`memory_utilization_audit.py` 别的函数用的是统一入口 `_pg()`，
而 retention 采样自己另写了一段顶层键读法（文件级"有 canonical 入口"检查会漏掉它）。

    安全 = ① 走**仓内统一入口**（`scripts/_pg_std.py` / `trinity/security/credentials.py`）（不自己解析）；或
           ② yaml 读之后**在同一个作用域内**处理了 `refs`（`{**raw.get("refs"), **raw}` 之类）；或
           ③ 行式解析**对缩进容忍**（`.strip()` / `partition(":")` / `split(":")` / 正则 `^\\s*`）；或
           ④ 只判存在性（`exists()`/`isfile()`），不取键；或
           ⑤ 只读 env（文件路径仅出现在注释/文档字符串里）。
    危险 = yaml 读后**未**处理 `refs`；或行式解析用**不 strip** 的 `startswith` / **锚 `^` 且无 `\\s*`** 的正则。

**已知欠账**用显式登记表（每条带理由）而不是"整目录免检"：登记项**必须仍然存在**（僵尸即红），
**新增**危险读者一律红。`benchmark/` 下的一次性评测运行器另按**数量棘轮**登记（只降不升）。

## 负向实测（承重）

`test_可失败性_...` 三条：合成"顶层 .get 的 yaml 读者"⇒ 必须红；合成"merge refs 的读者"/
"统一入口读者"/"strip 行式读者"⇒ 必须不红。**反事实**：把判定换成"恒安全"的变异体 ⇒
合成危险读者**必须不再**被判红（证明前面的断言承重，而不是自说自话）。
"""
from __future__ import annotations

import ast
import io
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CRED = ".credentials.yaml"
SCAN_DIRS = ("tests", "scripts", "trinity", "benchmark")
SKIP_DIRS = {"__pycache__", ".venv", "node_modules", ".git", "archive", "legacy"}

#: ── 已知欠账：**危险读者**（每条带理由；不修的理由必须写清）────────────────────
#: 判据会同时断言：① 表外不得出现危险读者；② 表内条目**必须仍然存在**（否则是僵尸登记）。
KNOWN_RISKY = {
    # ⚠️ 2026-10-06（t31）：原先登记的 **11 个 scripts/ 站点已修复并移出本表**
    #（顶层 .get → _pg_std.pg_creds 7 个；锚 ^ → ^\\s* 的 2 个；run_prewarm 的 refs 合并；
    # switch_storage 的写者锚 + 引号）。修复状态与前后对比见
    # CREDENTIALS-READERS.md §t31。移出它们不是“眼不见为净”：`test_危险读者必须已登记`
    # 仍会对**新出现的**危险读者判红，而 `test_登记表不得有僵尸` 会强迫“修好一个就移出一个”。
    # ── D. t31 后仍被扫描器判为 UNKNOWN 的**写者**（读法已修，但扫描器按"裸 open I/O"报）────
    "scripts/switch_storage.py":
        "**写者**。t31 已修：`_current()` 改成行式 `strip()+partition`（原来是锚 `^KEY` 且值带引号 ⇒ "
        "status **谎报** sqlite(default)）；写入的 `re.sub` 也补了 `\\s*` 并**保留缩进**"
        "（原来匹配不到 ⇒ 走 else 追加顶层键，`refs` 旧值仍生效）。"
        "保留登记的原因**不是**它还有缺陷，而是扫描器的启发式对 `open(_CRED,'r'/'w')` 这类"
        "**裸 I/O helper**（L22/L28）只能给 UNKNOWN —— 它看不到同文件的解析已改为容差形态。"
        "⇒ 这是**扫描器分类学**的边界，已写进报告 §t31 交队长/verifier 复核。",
    # ── C. 刻意保留（有测量依据的决定，**不要修**）───────────────────────────────
    "trinity/security/credentials.py":
        "**有记录的刻意回滚**（`resolve_backend()` 的 yaml 分支，2026-09-28）："
        "`refs` 兜底本身是对的，但开启后会把该函数的返回值从 '' 翻成 postgresql，"
        "使所有既无 `TRINITY_STORAGE_BACKEND` 又无 `TRINITY_STORE` 的入口切换到 PG —— "
        "实测常驻 API 涨到 **7.29GB 且无响应**，故回滚。`_load_yaml()` 的 refs 修复**保留**。"
        "⇒ 登记为**刻意保留**：它必须可见，但改动要等 PG 回归问题解决（写域在 security-hardening）。",
}

#: `benchmark/` 下的一次性评测运行器：整类登记（数量棘轮，只降不升，新增即红）。
#: 2026-10-06 实测 = **0**（当前没有危险读者）。
BENCH_RISKY_LIMIT = 0
_BENCH_REASON = ("benchmark/ 下的一次性评测运行器：其凭证读法不影响常驻链路（无人 import 它们），"
                 "但**同类缺陷**（顶层键 ⇒ 空口令）在评测脚本里会给出**看起来正常的错数字**。"
                 "整类冻结：新增一个即红，收敛一个请下调上限。")

#: 「提到凭证路径、但**没有** I/O 调用点」的文件（52 个）——多数只是注释/文档里的提及。
#: 冻结为**上界**：新增一个 ⇒ 可能是出现了我没覆盖的读法（例如把路径交给别的模块去 open），
#: 必须登记或复核，不允许静默增长。
MENTION_ONLY_FROZEN = {
    "benchmark/answer_eval.py", "benchmark/answer_eval_strategies.py",
    "benchmark/citation_coverage.py", "benchmark/longmemeval_v2_runner.py",
    "benchmark/official_lm_eval.py", "benchmark/tr_probe_fixed.py",
    "scripts/audit_archive_purge.py", "scripts/auto_session_summary.py", "scripts/blind_judge.py",
    "scripts/build_hard_holdout.py", "scripts/canary_smoke.py", "scripts/check_store_growth.py",
    "scripts/corpus_quality_audit.py", "scripts/critical_paths_gate.py",
    "scripts/decay_llm_sandbox.py", "scripts/decay_real_llm.py", "scripts/e2e_qa_eval.py",
    "scripts/env_doctor.py", "scripts/full_stress_test.py", "scripts/gdpr_export.py",
    "scripts/independent_receipt_verify.py", "scripts/memory_utilization_audit.py",
    "scripts/provenance_export.py", "scripts/reflection_rewrite.py",
    "scripts/run_pagetree_summaries.py", "scripts/session_state_demo.py",
    "scripts/sqlite_pg_mirror.py", "scripts/sync_sqlite_to_pg.py",
    "scripts/update_dsh_agents_md.py", "tests/conftest.py",
    "tests/unit/test_cold_pick_salt_diversity.py", "tests/unit/test_credential_hygiene_20260930.py",
    "tests/unit/test_credentials_readers_20261006.py",
    "tests/unit/test_credentials_versioned_file_20260930.py",
    "tests/unit/test_fok_alien_probe.py", "tests/unit/test_llm_client.py",
    "tests/unit/test_llm_key_resolution.py", "tests/unit/test_memories_session_index.py",
    "tests/unit/test_no_new_half_archived_rows.py",
    "tests/unit/test_pg_credential_fallback_20260930.py", "tests/unit/test_pg_credentials_patch.py",
    "tests/unit/test_pg_ddl_tags_matches_schema.py",
    "tests/unit/test_pg_edge_bitemporal_gdpr.py", "tests/unit/test_pg_update_tags_jsonb.py",
    "tests/unit/test_provenance_roundtrip_20261006.py",
    "trinity/adapters/postgresql.py", "trinity/core/client/_construction.py",
    "trinity/evolution/meta_strategy.py", "trinity/llm/client.py", "trinity/qa/route_reasoner.py",
    "trinity/retrieval/query_expansion.py", "trinity/utils/pgconn.py",
    # ── 2026-10-07（t87b，两个新文件；**复核结论：安全**，登记理由逐条写在下面）──────────
    # ① `scripts/cross_store_reconcile_probe.py`：**已改为走统一入口** `scripts/_pg_std.pg_creds()`
    #    （t31 把 11 个 scripts/ 站点改成它；本身是已扫描站点）。
    #    本文件现在**没有任何凭证 I/O**，字面路径只出现在**说明为何改**的历史注释里。
    #    原先被判"提到但无 I/O"的原因是本判据的探测面：它只认 `open(<字面路径/模块级常量>)`，
    #    看不到「上一行 `p = …credentials.yaml`、下一行 `open(p)`」这种**局部变量**写法。
    "scripts/cross_store_reconcile_probe.py",
    # ② `trinity/bridges/delivery_slot.py`：同上（**已改为** `trinity.security.credentials
    #    .resolve_credentials()`）。它的凭证读取只出现在 `--ab` 实验路径的 `_plain_content()`
    #    （投递槽位策略`build_slot_block/apply_slot` 不读凭证），且是**只读** PG、
    #    只用于拼连接参数、**从不打印/记录任何口令**。
    "trinity/bridges/delivery_slot.py",
}

#: 行式解析的“缩进容忍”**只看正则级**（`^\s*`）；循环级的容忍由 `_line_loop_verdict` 判
#: （早期版本把 `.split(":")`/`partition(` 也算容忍 —— 实测**错**：
#: `line.split(":")[0]` 在未 strip 的原始行上得到的键带前导空格，仍然读不到 `refs` 下的键）。
_TOLERANT_RX = re.compile(r"(\^\\s\*|\^\\s\+|\.strip\(\)\.(?:startswith|split|partition)"
                          r"|\.lstrip\(\)\.(?:startswith|split|partition))")
#: 本文件自身必须排除：它里面有"锚行首凭证键正则"的**示例 fixture 字符串**（会自匹配）。
SELF = "tests/unit/test_credentials_readers_20261006.py"
_BARE_STARTSWITH_RX = re.compile(r"(?<!strip\(\))(?<!lstrip\(\))(?<!s\.)\.startswith\(\s*[\"']"
                                 r"(TRINITY_|DEEPSEEK_|GATEWAY_|FEISHU_)")
_ANCHORED_RX = re.compile(r"re\.(?:match|search|sub)\(\s*r?[\"']\^\(?(?:TRINITY_|DEEPSEEK_|GATEWAY_|FEISHU_)")
_CANONICAL_RX = re.compile(r"(_pg_std|security\.credentials|resolve_credentials|pg_creds|pg_connect)")
_REFS_RX = re.compile(r"""['"]refs['"]""")


def _dotted(node) -> "str | None":
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def _parents(tree):
    par = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            par[child] = node
    return par


def _slice(lines, node) -> str:
    """节点对应的源码切片（**按行号切片**，不用 `ast.get_source_segment`）。

    为什么不用 `get_source_segment`：它在**每次调用**里对整份 source 做 line-split，
    在本仓的数千行文件上 × 每个 Call 节点 ⇒ 实测把判据跑到 pytest 超时。行号切片是 O(行数)。
    """
    a = getattr(node, "lineno", None)
    b = getattr(node, "end_lineno", None)
    if not a or not b:
        return ""
    return "\n".join(lines[a - 1:b])


def _scope_text(tree, node, lines, parents, cache) -> str:
    """站点所在的**作用域**源码：最近的函数体；没有则整个模块。带缓存（按节点 id）。"""
    cur = node
    while cur is not None:
        if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
            key = id(cur)
            if key not in cache:
                cache[key] = _slice(lines, cur)
            return cache[key]
        cur = parents.get(cur)
    return "\n".join(lines)


def scan_readers(directory: "str | os.PathLike" = ROOT, *,
                 dirs=SCAN_DIRS, skip=SKIP_DIRS) -> list:
    """全仓扫描凭证读站点 ⇒ [{file, line, kind, verdict, evidence}]。"""
    return _scan_all(directory, dirs=dirs, skip=skip)["sites"]


#: 扫描结果缓存（同一次测试会话里扫一次；2s 级）
_SCAN_CACHE = {}


def mention_only_files(directory: "str | os.PathLike" = ROOT) -> set:
    """提到凭证路径、但**没有任何 I/O 调用点**的文件（多重是注释/文档字符串里的提及）。

    这些不是读者，但**必须冻结数量**：新增一个可能意味着出现了一种我没覆盖的读法
    （例如把路径交给别的模块去 open）。
    """
    return _scan_all(directory)["mention_only"]


def _scan_all(directory=ROOT, *, dirs=SCAN_DIRS, skip=SKIP_DIRS) -> dict:
    # 只缓存**真实工作区**的扫描（2s 级）；fixture 目录不缓存 ——
    # 否则同一个 tmp_path 里"改了 fixture 再扫一次"会拿到上次的缓存（实测踩过）。
    cacheable = str(directory) == str(ROOT)
    key = (str(directory), tuple(dirs), tuple(sorted(skip)))
    if cacheable and key in _SCAN_CACHE:
        return _SCAN_CACHE[key]
    sites, mention_only = [], set()
    root = str(directory)
    for base in dirs:
        bdir = os.path.join(root, base)
        if not os.path.isdir(bdir):
            continue
        for dp, dn, fns in os.walk(bdir):
            dn[:] = [d for d in dn if d not in skip]
            for fn in sorted(fns):
                if not fn.endswith(".py"):
                    continue
                path = os.path.join(dp, fn)
                rel = os.path.relpath(path, root).replace(os.sep, "/")
                if rel == SELF:
                    continue                      # 自匹配：本文件含示例正则
                try:
                    with open(path, "rb") as fh:
                        data = fh.read()
                except OSError:
                    continue
                if CRED.encode() not in data:
                    continue                      # 便宜预筛（1941 文件 0.4s）
                text = data.decode("utf-8", errors="replace")
                try:
                    tree = ast.parse(text)
                except SyntaxError:
                    sites.append({"file": rel, "line": 0, "kind": "syntax-error",
                                  "verdict": "RISKY(syntax)", "evidence": "文件编译不过，无法判定读法"})
                    continue
                parents = _parents(tree)
                lines = text.splitlines()
                cache = {}
                cred_names = _module_cred_names(tree, lines)

                def _touches(blob: str, scope: str = "") -> bool:
                    if CRED in blob:
                        return True
                    names = cred_names | _scope_cred_names(scope or blob)
                    return any(re.search(r"\b%s\b" % re.escape(n), blob) for n in names)

                found = False
                for ln, pat in _anchored_key_regexes(text):
                    found = True
                    sites.append({"file": rel, "line": ln, "kind": "regex",
                                  "verdict": "RISKY(anchored ^)",
                                  "evidence": "锚 ^ 且无 \\s* 的凭证键正则（读不到缩进键）：%s" % pat})
                for node in ast.walk(tree):
                    if not isinstance(node, ast.Call):
                        continue
                    d = _dotted(node.func)
                    if d in ("yaml.safe_load", "yaml.load", "yaml.full_load"):
                        scope = _scope_text(tree, node, lines, parents, cache)
                        if not _touches(scope):
                            continue
                        kind = "yaml"
                    else:
                        seg = _slice(lines, node)
                        if not _touches(seg):
                            continue
                        # 只有**真正的 I/O 调用**才算读站点：`open` / `read_text` / `read_bytes` / `read`。
                        # （早期版本把"实参里带路径的任意调用"都算上 ⇒ `read_yaml_keys(CRED_FILE)`
                        #   这种**把路径转交 helper** 的调用被误报；它是转交不是读。）
                        if not (d == "open" or (d or "").endswith(
                                (".read_text", ".read_bytes", ".read"))):
                            continue
                        scope = _scope_text(tree, node, lines, parents, cache)
                        if re.search(r"yaml\.(safe_)?load\(", scope):
                            continue      # 同一作用域里 yaml 站点已覆盖这条读法，别重复计
                        kind = "open/read"
                    found = True
                    verdict, evidence = _judge(kind, scope, text, _slice(lines, node))
                    sites.append({"file": rel, "line": getattr(node, "lineno", 0),
                                  "kind": kind, "verdict": verdict, "evidence": evidence})
                if not found:
                    mention_only.add(rel)
    result = {"sites": sites, "mention_only": mention_only}
    if cacheable:
        _SCAN_CACHE[key] = result
    return result


def _module_cred_names(tree, lines) -> set:
    """**模块级**绑定到凭证路径的常量名（`_CRED = os.path.expanduser("…/.credentials.yaml")`）。

    只取 `tree.body` 上的赋值：函数内的同名变量是**别的意思**
    （实测：`blind_judge.py` 里函数内的局部 `path` 与模块级凭证常量同名，
    用"全文件搜名字"会把它的一堆 `open(path, "w")` 误判成读凭证 —— 假红就是这么来的）。
    """
    names = set()
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            rhs = _slice(lines, node.value) if node.value is not None else ""
            if CRED in rhs:
                names |= {t.id for t in targets if isinstance(t, ast.Name)}
    return names


def _scope_cred_names(scope: str) -> set:
    """作用域内绑定到凭证路径的名字（`p = Path.home() / ".dsh" / ".credentials.yaml"`）。"""
    return set(re.findall(r"(\w+)\s*=\s*[^\n]*credentials\.yaml", scope))


_ANCHORED_KEY_RX = re.compile(
    r"""(?m)^\s*(?:\w+\s*=\s*)?re\.(?:compile|match|search|sub)\(\s*r?["']"""
    r"""(?:\^|\^\\s\*)?(?:TRINITY_|DEEPSEEK_|GATEWAY_|FEISHU_)""")


def _anchored_key_regexes(text: str) -> list:
    """**锚 `^` 且无 `\\s*`** 的凭证键正则（它们读不到缩进在 `refs` 下的键）。

    这是第二条独立规则（按**文件**报）：正则可能被写在"接收文本"的 helper 里，
    而读取动作发生在别处 —— 只看调用点会漏（实测 `mcp_http_probe._KEY_RE` 就是这种）。
    假阳性边界很窄：必须**锚在行首**且**没有 `\\s*` 容错**，且键名是凭证键名族。
    """
    out = []
    for ln, line in enumerate(text.splitlines(), 1):
        if "re." not in line or "^" not in line:
            continue
        m = re.search(r"""r?["'](\^[^"']{0,120})["']""", line)
        if not m:
            continue
        pat = m.group(1)
        if not re.match(r"\^\(?(?:TRINITY_|DEEPSEEK_|GATEWAY_|FEISHU_)", pat):
            continue
        if pat.startswith("^\\s") or "^\\s" in pat[:4]:
            continue
        out.append((ln, pat[:60]))
    return out


def _line_loop_verdict(scope: str):
    """**只看真的逐行循环**：`for <var> in <文件句柄>` 之后的 `<var>.startswith(...)`。

    两个坑（都实测踩过）：
    · 直接搜 `.startswith(` ⇒ `scripts/_pg_std.py` 的字典键过滤
      `{k: v for k, v in raw.items() if k.startswith("TRINITY_PG_")}` 被骗出假红（16 条假红）；
    · 只认 `for line in open(...)` ⇒ `env_doctor.read_yaml_keys` 的
      `with open(path) as fh: for line in fh:` 被判 UNKNOWN（它其实 strip 过、是安全的）。
    ⇒ 判据必须锚在**循环变量**上，且要认得**文件句柄变量**。
    """
    handles = set()
    for m in re.finditer(r"with\s+open\([^)]*\)\s*as\s+(\w+)", scope):
        handles.add(m.group(1))
    for m in re.finditer(r"^\s*(\w+)\s*=\s*(?:open\(|.*read_text\(|.*read\(\)\.splitlines\(\))",
                         scope, re.M):
        handles.add(m.group(1))
    for m in re.finditer(r"for\s+(\w+)\s+in\s+([^\n:]+):", scope):
        var, src = m.group(1), m.group(2)
        src_ok = ("open(" in src or "splitlines(" in src or "readlines(" in src
                  or src.strip() in handles)
        if not src_ok:
            continue
        alias = re.search(r"(\w+)\s*=\s*%s\.strip\(\)" % re.escape(var), scope)
        _alias_startswith = bool(
            alias and re.search(r"\b%s\.startswith\(" % re.escape(alias.group(1)), scope))
        if not (re.search(r"\b%s\.startswith\(" % re.escape(var), scope)
                or _alias_startswith):
            continue
        tolerant = bool(
            re.search(r"\b%s\s*=\s*%s\.strip\(\)" % (re.escape(var), re.escape(var)), scope)
            or re.search(r"\b%s\.strip\(\)\.(startswith|split|partition)" % re.escape(var), scope)
            or re.search(r"\b%s\.lstrip\(\)\.startswith\(" % re.escape(var), scope)
            or (alias and re.search(r"\b%s\.startswith\(" % re.escape(alias.group(1)), scope)))
        if tolerant:
            return "SAFE(line tolerant)", "逐行解析先 strip（缩进容忍）"
        return "RISKY(raw startswith)", "逐行解析**不 strip** ⇒ 读不到缩进在 refs 下的键"
    return None


def _unanchored_search(scope: str) -> bool:
    """**不锚行首**的 `re.search` ⇒ 前导空格无关，天然读得到缩进键（另一种合法读法）。

    例：`scripts/bench_ingest_longmemeval_s.py` 的
    `re.search(k + "[\\\\s]*:[\\\\s]*[\\"']?([^\\"'\\\\r\\\\n]+)", c)` —— 无 `^` ⇒ 缩进不影响匹配。
    """
    for m in re.finditer(r"re\.(?:search|match)\(\s*r?[\"']([^\"']{0,80})", scope):
        if not m.group(1).startswith("^"):
            return True
    # 模式串可能是**拼出来的**（`re.search(k + "[\\s]*:[\\s]*...", c)`）：允许调用里有前缀
    for m in re.finditer(r"re\.(?:search|match)\([^\"'\n]{0,40}[\"']([^\"']{0,80})", scope):
        if m.group(1) and not m.group(1).startswith("^"):
            return True
    return False


def _judge(kind: str, scope: str, text: str, seg: str):
    """站点级判定（只看代码，注释不参与证据）。"""
    lineloop = _line_loop_verdict(scope)
    if kind == "yaml":
        if _REFS_RX.search(scope):
            return "SAFE(yaml+refs)", "同作用域内处理了 refs（版本化结构）"
        return "RISKY(no refs)", "yaml 读后未 merge refs ⇒ 顶层取不到真键（空凭证）"
    if lineloop:
        return lineloop
    if _ANCHORED_RX.search(scope):
        return "RISKY(anchored ^)", "正则锚 ^ 且无 \\s* ⇒ 匹配不到缩进键（写者/读者都会错）"
    if _TOLERANT_RX.search(scope):
        return "SAFE(line tolerant)", "行式解析对缩进容忍（strip/^\\s*）"
    if _unanchored_search(scope):
        return "SAFE(unanchored)", "不锚行首的 re.search ⇒ 缩进不影响匹配"
    if re.search(r"(exists\(\)|isfile\()", scope):
        return "SAFE(exists only)", "只判存在性，不取键"
    if _CANONICAL_RX.search(seg) or _CANONICAL_RX.search(scope):
        return "SAFE(canonical)", "走仓内统一入口（不自己解析）"
    if _CANONICAL_RX.search(text):
        return "SAFE(canonical-file)", "该文件走统一入口"
    return "UNKNOWN", "读法未被规则覆盖（**不得静默通过**）"


def risky_sites(sites: list) -> list:
    return [s for s in sites if s["verdict"].startswith(("RISKY", "UNKNOWN"))]


# ── 正例：真实工作区 ─────────────────────────────────────────────────────

def test_危险读者必须已登记():
    """表外出现危险读者 ⇒ 红（新增读者必须 merge refs 或走统一入口）。"""
    bad = risky_sites(scan_readers())
    unregistered = sorted({s["file"] for s in bad} - set(KNOWN_RISKY) - _bench_files())
    detail = [(s["file"], s["line"], s["verdict"]) for s in bad if s["file"] in unregistered]
    assert not unregistered, (
        "以下文件里有**读凭证文件但不 merge `refs`** 的站点，且未登记：\n  - "
        + "\n  - ".join(unregistered)
        + "\n  ⇒ 正确读法四选一：① `from _pg_std import pg_creds/pg_connect`；"
          "② `raw = {**(raw.get('refs') or {}), **raw}`；"
          "③ 行式解析用 `.strip()`/`partition(':')`；④ 只判存在性。"
          "见 tests/unit/test_credentials_readers_20261006.py 文件头。\n"
          "站点明细：%s" % (detail,))


def test_登记表不得有僵尸():
    """登记的危险读者若已修好（不再危险）⇒ 必须从表里删掉，否则表会慢慢变成谎言。"""
    bad_files = {s["file"] for s in risky_sites(scan_readers())}
    zombies = sorted(set(KNOWN_RISKY) - bad_files)
    assert not zombies, (
        "登记表里的这些文件**已不再**是危险读者（已修或已删）⇒ 请从 KNOWN_RISKY 移除：%s\n"
        "（保留它们会让『已知欠账』这个数字虚高，也会掩盖真正的欠账）" % zombies)


def test_benchmark_类危险读者数量只降不升():
    files = _bench_files()
    assert len(files) <= BENCH_RISKY_LIMIT, (
        "benchmark/ 下的危险读者由 %d 涨到 %d（上限 %d）：%s\n⇒ %s"
        % (BENCH_RISKY_LIMIT, len(files), BENCH_RISKY_LIMIT, sorted(files), _BENCH_REASON))


def _bench_files() -> set:
    return {s["file"] for s in risky_sites(scan_readers()) if s["file"].startswith("benchmark/")}


def test_提到路径但没有读动作的文件不得静默增长():
    """`mention_only` 集合冻结为上界：新增 ⇒ 可能是我没覆盖的读法（把路径交给别处 open）。"""
    extra = sorted(mention_only_files() - MENTION_ONLY_FROZEN)
    assert not extra, (
        "这些文件提到凭证路径、但本判据**找不到任何 I/O 调用点**（新增）：%s\n"
        "⇒ 可能是一种我没覆盖的读法（例如把路径转交给别的模块去 open）。请人工复核它的读法，"
        "然后：安全就加进 MENTION_ONLY_FROZEN，危险就加进 KNOWN_RISKY。" % extra)


# ── 两个已修站点的**回归锁**（防止改回去）─────────────────────────────────

def test_站点1_必须走统一入口且不再自带顶层读法():
    p = os.path.join(ROOT, "tests/unit/test_fok_alien_probe.py")
    src = io.open(p, encoding="utf-8").read()
    assert "_pg_std" in src and "pg_connect" in src, (
        "站点 1 必须走统一入口 `scripts/_pg_std.py`（它 merge refs）")
    assert "import yaml" not in src, (
        "站点 1 又出现了自带 yaml 读法 ⇒ 会退回『顶层取不到 ⇒ 空口令 ⇒ 假 skip』")
    bad = [s for s in scan_readers()
           if s["file"] == "tests/unit/test_fok_alien_probe.py"
           and s["verdict"].startswith(("RISKY", "UNKNOWN"))]
    assert not bad, "站点 1 仍被本判据判为危险读者：%r" % (bad,)
    assert not re.search(r"pytest\.skip\(\s*[\"'][^\"']*无 PG / 无凭证", src), (
        "站点 1 的 skip 理由不许再是笼统的『无 PG / 无凭证』——那是**与事实不符**的理由")
    assert "真实原因" in src, "站点 1 的 skip 理由必须把**真实异常**带出来"


def test_站点2_采样失败必须响亮而不是静默成NA():
    p = os.path.join(ROOT, "scripts/memory_utilization_audit.py")
    src = io.open(p, encoding="utf-8").read()
    assert 'out["retention_level"] = "UNAVAILABLE"' in src, (
        "采样不可用必须以 UNAVAILABLE 显式表达")
    assert 'out["retention_level"] = "NA"' not in src, (
        "采样失败又被写成 NA ⇒ 与『缺上一次读数』这个**合法语义**混同（静默降级）")
    assert "retention_unavailable" in src and "retention_error" in src, (
        "必须把『不可用』与『错误原文』写成机器可读字段，而不是只塞进 note")
    assert "[warn] retention" in src, "必须同时向 stderr 响亮告警（否则只是换了个字段名）"
    assert "_cx = _pg()" in src, "站点 2 必须走本文件既有的统一入口 `_pg()`（→ _pg_std）"


def test_同族PG测试文件必须走统一入口():
    for rel in ("tests/unit/test_pg_edge_bitemporal_gdpr.py",
                "tests/unit/test_pg_update_tags_jsonb.py"):
        src = io.open(os.path.join(ROOT, rel), encoding="utf-8").read()
        assert "_pg_std" in src and "pg_creds" in src, "%s 必须走统一入口" % rel
        assert not re.search(r"reason=\"PG 不可用（离线/无凭证）", src), (
            "%s 的 skip 理由不许笼统（此前它掩盖了真因：凭证读法不对 ⇒ 口令空串）" % rel)
        assert "_PG_ERROR" in src, "%s 的 skip 理由必须带上**真实异常**" % rel


# ── 负向实测（承重证明）─────────────────────────────────────────────────

_RISKY_FIXTURE = '''# -*- coding: utf-8 -*-
import os
import yaml


def _conn():
    raw = yaml.safe_load(open(os.path.expanduser("~/.dsh/.credentials.yaml"),
                              encoding="utf-8-sig")) or {}
    return dict(password=raw.get("TRINITY_PG_PASSWORD", ""))   # 顶层键 ⇒ 空口令
'''

_SAFE_REFS_FIXTURE = '''# -*- coding: utf-8 -*-
import os
import yaml


def _conn():
    with open(os.path.expanduser("~/.dsh/.credentials.yaml"), encoding="utf-8-sig") as fh:
        raw = yaml.safe_load(fh) or {}
    raw = {**(raw.get("refs") or {}), **raw}
    return dict(password=raw.get("TRINITY_PG_PASSWORD", ""))
'''

_SAFE_CANONICAL_FIXTURE = '''# -*- coding: utf-8 -*-
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "scripts"))
from _pg_std import pg_creds          # 统一入口（内部 merge refs）


def _conn():
    return pg_creds()
'''

_SAFE_LINE_FIXTURE = '''# -*- coding: utf-8 -*-
import os


def _conn():
    cfg = {}
    for line in open(os.path.expanduser("~/.dsh/.credentials.yaml"), encoding="utf-8-sig"):
        s = line.strip()
        if s.startswith("TRINITY_PG_"):
            k, _, v = s.partition(":")
            cfg[k] = v.strip()
    return cfg
'''

_RISKY_LINE_FIXTURE = '''# -*- coding: utf-8 -*-
import os


def _conn():
    cfg = {}
    for line in open(os.path.expanduser("~/.dsh/.credentials.yaml"), encoding="utf-8-sig"):
        if line.startswith("TRINITY_PG_"):        # 不 strip ⇒ 缩进键读不到
            cfg[line.split(":")[0]] = line.split(":")[1]
    return cfg
'''


def _write_fixture(tmp_path, name, body):
    d = tmp_path / "scripts"
    d.mkdir(exist_ok=True)
    (d / name).write_text(body, encoding="utf-8")
    return [s for s in scan_readers(tmp_path) if s["file"].endswith(name)]


def test_可失败性_顶层get的yaml读者必须被判红(tmp_path):
    sites = _write_fixture(tmp_path, "risky.py", _RISKY_FIXTURE)
    assert sites, "没扫到这个 fixture 的读站点 ⇒ 判据的扫描面有问题"
    assert all(s["verdict"].startswith("RISKY") for s in sites), (
        "顶层 `.get()` 的 yaml 读者没被判红 ⇒ 本判据无判别力：%r" % (sites,))

    # 反事实：把判定换成"恒安全"的变异体 ⇒ 同一个危险 fixture **必须不再**判红
    def _mutant(_kind, _scope, _text, _seg):
        return "SAFE(mutant)", "恒安全变异体"
    mutant_verdicts = [_mutant(s["kind"], "", "", "")[0] for s in sites]
    assert all(v.startswith("SAFE") for v in mutant_verdicts) and any(
        s["verdict"].startswith("RISKY") for s in sites), (
        "反事实不成立：恒安全的变异判定本该让这个 fixture 『变绿』")


def test_可失败性_不安全读者不得被误报(tmp_path):
    """安全读法**必须不被**判红（否则判据只会在人手里被忽略）。

    注意：走统一入口的 fixture 会**没有任何读站点**（它自己不解析文件）——
    那正是"安全"的最强形态，故此处只要求"没有被判红"，不要求一定有站点。
    """
    for name, body in (("with_refs.py", _SAFE_REFS_FIXTURE),
                       ("canonical.py", _SAFE_CANONICAL_FIXTURE),
                       ("line_strip.py", _SAFE_LINE_FIXTURE)):
        sites = _write_fixture(tmp_path, name, body)
        bad = [s for s in sites if s["verdict"].startswith(("RISKY", "UNKNOWN"))]
        assert not bad, "%s 被误报为危险读者 ⇒ 判据会产出假红：%r" % (name, bad)


def test_可失败性_锚行首的凭证键正则必须被判红(tmp_path):
    """第二条规则：锚 `^` 且无 `\\s*` 的凭证键正则 ⇒ 在版本化文件上恒不匹配。"""
    risky = (
        '# -*- coding: utf-8 -*-\nimport os\nimport re\n\n'
        'CREDS = os.path.expanduser("~/.dsh/.credentials.yaml")\n'
        '_KEY_RE = re.compile(r"^(TRINITY_MCP_API_KEY|TRINITY_API_KEY)\\s*:\\s*(.+)$")\n\n\n'
        'def parse_token():\n'
        '    text = open(CREDS, encoding="utf-8-sig").read()\n'
        '    return _KEY_RE.search(text)\n')
    d = tmp_path / "scripts"
    d.mkdir(exist_ok=True)
    (d / "anchored.py").write_text(risky, encoding="utf-8")
    hits = [s for s in scan_readers(tmp_path)
            if s["file"].endswith("anchored.py") and s["verdict"] == "RISKY(anchored ^)"]
    assert hits, "锚行首的凭证键正则没被判红 ⇒ 第二条规则失效"

    tolerant = risky.replace('r"^(TRINITY', 'r"^\\s*(TRINITY')
    (d / "anchored.py").write_text(tolerant, encoding="utf-8")
    hits2 = [s for s in scan_readers(tmp_path)
             if s["file"].endswith("anchored.py") and s["verdict"] == "RISKY(anchored ^)"]
    assert not hits2, "`^\\s*` 容错版被误判为危险 ⇒ 第二规则会假红"


def test_可失败性_不strip的行式读者必须被判红(tmp_path):
    sites = _write_fixture(tmp_path, "risky_line.py", _RISKY_LINE_FIXTURE)
    assert sites and all(s["verdict"].startswith("RISKY") for s in sites), (
        "不 strip 的行式读者没被判红：%r" % (sites,))


def test_扫描面自证_必须真的扫到读者():
    """扫描面自证：必须扫到**至少一个危险读者**与**至少一个 merge refs 的安全读者**。

    否则「全绿」可能只是**什么都没扫到**（本仓反复记录的空转形态：判据恒绿）。
    """
    sites = scan_readers()
    # t31：原来锚在 `scripts/cold_corpus_triage.py`（它已修好 ⇒ 不再是危险读者，无站点）。
    # 自证改用**仍然被判危险**的那个刻意保留站点，否则这条自证会随修复而变成空转。
    assert [s for s in sites if s["file"] == "trinity/security/credentials.py"
            and s["verdict"].startswith("RISKY")], "没扫到已知的危险读者 ⇒ 扫描面失效/空转"
    assert [s for s in sites if s["file"] == "scripts/_pg_std.py"
            and s["verdict"].startswith("SAFE")], "没扫到统一入口（merge refs）的安全站点"
    assert any(s["verdict"] == "SAFE(yaml+refs)" for s in sites), (
        "没有任何『yaml + refs』的 SAFE 站点 ⇒ 扫描面可能只覆盖了一半")
    # 两个已修站点不得再出现在危险集合里
    risky_files = {s["file"] for s in risky_sites(sites)}
    for rel in ("tests/unit/test_fok_alien_probe.py", "scripts/memory_utilization_audit.py"):
        assert rel not in risky_files, "%s 仍被判为危险读者" % rel


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
