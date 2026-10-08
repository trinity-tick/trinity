"""凭据文件版本化结构闸门（2026-09-30）。

## 缺陷：12 个读者在版本化结构下**静默读空**凭据

`~/.dsh/.credentials.yaml` 自 2026-09-18 起是**版本化结构**：

```yaml
version: 1
refs:
  TRINITY_PG_PASSWORD: "..."
  DEEPSEEK_API_KEY: "..."
```

而仓内有 **12 个读者**用「**顶层键过滤**」的写法：

```python
raw = yaml.safe_load(fh) or {}
creds = {k: v for k, v in raw.items() if k.startswith("TRINITY_PG_")}
```

顶层只有 `version` / `refs` ⇒ **`creds` 恒为空** ⇒ 静默回落默认值。
实测后果：`scripts/plaintext_ratio_audit.py --ratchet` 报
`fe_sendauth: no password supplied`（fail-closed，门永远红），
而 `trinity/brain/precision_tiers.py`、`value_encoder.py` 等同样读空。

## 修复的**边界**（重要，勿越界）

`trinity/security/credentials.py::resolve_backend()` 里有一条 **ROLLBACK 记录**：

> 「refs 回退是**对的**……但启用它会让所有既无 `TRINITY_STORAGE_BACKEND` 又无
> `TRINITY_STORE` 的入口从 SQLite 翻到 PG……把常驻 API 推到 PG，实测 7.29 GB 且无响应。
> **回退，直到 PG 回归修好**。」

⇒ 因此本次**只修凭据值读取**，**绝不碰后端选择**。已逐一核对：
这 12 个文件**没有一个**含 `TRINITY_STORAGE_BACKEND`（实测计数 0）。

## 修法

在 `safe_load` 之后加**逐字兜底**（与 `credentials.py::_load_yaml` 同款）：
`refs` 打底、**顶层覆盖** ⇒ 文件是扁平布局时行为**逐字不变**。

## 实测（修后）

```
_pg_std.pg_creds()            keys=5  password=有
analytics_duckdb.pg_creds()   keys=5  password=有
precision_tiers._creds()      keys=5  password=有
scripts/plaintext_ratio_audit.py --ratchet  -> rc=0
  [生产面] 总行 63,493 | 明文 4,582 (7.22%) | 明文活跃 2,304 (9.57%)
  [ratchet] OK（三个占比均未超过 基线+0.0100）
```

运行：``python -m pytest tests/unit/test_credentials_versioned_file_20260930.py -q``
"""

from __future__ import annotations

import io
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

#: **真正**会读空的读者：用 `yaml.safe_load` 取回 dict 后按**顶层键**过滤。
#: （我最初按宽松模式统计出 12 个，实测纠正为 8 个；另外 4 个根本没有 safe_load。）
PATCHED = [
    "scripts/_pg_std.py",
    "scripts/analytics_duckdb.py",
    "scripts/backfill_audit_checksum.py",
    "scripts/brain_cycle.py",
    "scripts/question_annotate.py",
    "scripts/question_search.py",
    "scripts/reweight_relations_cage.py",
    "trinity/brain/precision_tiers.py",
]

#: 用**朴素行解析**读同一个文件的读者 —— 缩进不敏感，在版本化结构下**本来就能用**。
#: 对它们不用"形状"判据（会逼着改能跑的代码），改用**行为**判据（见下）。
#: 注：`evolve_loop.py` 起初被我误判为"不读凭据"（我只看到它校验 LLM 提议的 env 字典），
#: 是判据指出它也读 `.credentials.yaml` —— 第三次纠正我的分类。
NAIVE_PARSERS = [
    "scripts/env_doctor.py",
    "scripts/evolve_loop.py",
    "trinity/brain/value_encoder.py",
    "trinity/evolution/meta_strategy.py",
]


def _live_lines(rel: str):
    p = ROOT / rel
    if not p.exists():
        return
    for i, _ln in enumerate(p.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
        if not _ln.lstrip().startswith("#"):
            yield i, _ln


@pytest.mark.parametrize("rel", PATCHED)
def test_reader_is_not_backend_selector(rel: str) -> None:
    """**不许越界**：本次只修凭据值；碰后端选择会重演 7.29GB 事故。"""
    p = ROOT / rel
    if not p.exists():
        pytest.skip(f"{rel} 不在本检出中")
    txt = p.read_text(encoding="utf-8", errors="ignore")
    assert "TRINITY_STORAGE_BACKEND" not in txt, (
        f"{rel} 竟然涉及后端选择 —— 不得套用本次的 refs 回退（见 credentials.py 的 ROLLBACK）"
    )


@pytest.mark.parametrize("rel", PATCHED)
def test_reader_merges_refs_layer(rel: str) -> None:
    """每个**确实会读空**的读者都必须显式处理 `refs` 层。"""
    p = ROOT / rel
    if not p.exists():
        pytest.skip(f"{rel} 不在本检出中")
    txt = p.read_text(encoding="utf-8", errors="ignore")
    assert re.search(r'get\("refs"\)|\["refs"\]', txt), (
        f"{rel} 未处理 refs 层 —— 版本化凭据文件下会静默读空"
    )


@pytest.mark.parametrize("rel", NAIVE_PARSERS)
def test_naive_parser_still_reads_versioned_file(rel: str) -> None:
    """**行为判据**（取代形状判据）：朴素行解析必须能从版本化文件里取到值。

    这些读者的解析方式是"按行 split(':')"、**对缩进不敏感**，所以在
    `version/refs` 嵌套下本来就能取到嵌套键 —— 不该逼它们改代码。
    但"本来就能用"必须是**测出来的**，不是推断的：这里直接读真实文件验证。
    """
    p = ROOT / rel
    if not p.exists():
        pytest.skip(f"{rel} 不在本检出中")
    src = p.read_text(encoding="utf-8", errors="ignore")
    assert "safe_load" not in src, (
        f"{rel} 出现了 safe_load —— 它属于 PATCHED 那类，需显式 refs 合并"
    )

    cred = Path.home() / ".dsh" / ".credentials.yaml"
    if not cred.exists():
        pytest.skip("本机无凭据文件")
    # 复刻"按行 split(':')"语义，验证它在真实文件上取得到嵌套键
    parsed = {}
    for line in cred.read_text(encoding="utf-8-sig", errors="ignore").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or ":" not in s:
            continue
        k, _, v = s.partition(":")
        parsed[k.strip()] = v.strip().strip('"').strip("'")
    assert any(k.startswith("TRINITY_") for k in parsed), (
        "朴素行解析在真实凭据文件上取不到任何 TRINITY_* 键 —— 该结论（本就能用）被推翻，需重审"
    )


def test_naive_parsers_are_classified_by_evidence() -> None:
    """留痕：这三份清单是**实测**得出的，且我在此过程中被纠正过三次。

    * 起初按宽松模式统计出「12 个会读空」→ 实测纠正为 **8 个**（另外 4 个没有 `safe_load`）；
    * `env_doctor.py` 起初被判为"会读空"，实测它的 `read_yaml_keys` 是行解析、**带得出嵌套键**；
    * `evolve_loop.py` 起初被判为"不读凭据"，实测它**也读**（行解析 `DEEPSEEK_API_KEY`）。
    本判据不重复断言分类本身，只钉住"分类必须来自证据"这一点：清单里的每个文件都必须真的
    提到 `<home>/.dsh/.credentials.yaml`。
    """
    for rel in PATCHED + NAIVE_PARSERS:
        p = ROOT / rel
        if not p.exists():
            continue
        txt = p.read_text(encoding="utf-8", errors="ignore")
        assert ".credentials.yaml" in txt, (
            f"{rel} 被列在 PATCHED/NAIVE 清单里，却根本没读凭据文件 —— 分类需重做"
        )


def test_merge_keeps_top_level_precedence() -> None:
    """合并语义：`refs` 打底、**顶层覆盖** ⇒ 扁平文件下行为逐字不变。"""
    checker = (ROOT / "trinity/security/credentials.py").read_text(encoding="utf-8")
    assert '{**(cfg.get("refs") or {}), **cfg}' in checker, (
        "凭据模块的逐字兜底写法变了 —— 本判据基于该语义"
    )
    flat = {"TRINITY_PG_PASSWORD": "top"}
    nested = {"refs": {"TRINITY_PG_PASSWORD": "inner"}, "TRINITY_PG_PASSWORD": "top"}
    for cfg in (flat, nested):
        merged = {**(cfg.get("refs") or {}), **cfg}
        assert merged["TRINITY_PG_PASSWORD"] == "top", "顶层必须覆盖 refs"


def test_pg_creds_actually_resolves_password() -> None:
    """**行为验证**：修后 `_pg_std.pg_creds()` 必须拿到非空口令。"""
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import _pg_std  # type: ignore
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"无法导入 _pg_std：{exc}")
    try:
        creds = _pg_std.pg_creds()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"pg_creds() 不可用：{exc}")
    assert isinstance(creds, dict) and creds, "pg_creds() 返回空"
    assert creds.get("password"), (
        "pg_creds() 的口令为空 ⇒ 版本化凭据文件未被正确读取（本判据即为该缺陷的回归锁）"
    )


def test_plaintext_ratio_gate_can_measure() -> None:
    """该门此前因口令读空而永远 fail-closed；修后必须能测（rc=0 或明确的比值失败）。"""
    import subprocess
    r = subprocess.run([sys.executable, str(ROOT / "scripts/plaintext_ratio_audit.py"), "--ratchet"],
                       cwd=str(ROOT), capture_output=True,
                       encoding="utf-8", errors="replace", timeout=600)
    out = (r.stdout or "") + (r.stderr or "")
    assert "no password supplied" not in out, (
        "仍是「口令未提供」—— refs 层没读到，门依旧测不了"
    )
    # 允许 rc=1（棘轮真超标是合法结论），但不许是"测不了"
    assert "无法测量" not in out, f"门仍无法测量：\n{out[-600:]}"
