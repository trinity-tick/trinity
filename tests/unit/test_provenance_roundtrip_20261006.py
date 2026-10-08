# -*- coding: utf-8 -*-
"""PROV-O 导出/校验往返的判据（2026-10-06 复评 G6）。

## 修的是什么

1. `scripts/provenance_export.py::_conn()` 原先**硬编码空口令**
   （`password=os.environ.get("TRINITY_PG_PASSWORD", "")`），且不读
   `~/.dsh/.credentials.yaml` ⇒ 实测 `fe_sendauth: no password supplied`
   ⇒ **PROV-O 端到端往返在本机根本跑不起来**。
2. 导出侧只写 `ANCHOR_FIELDS`，**不含 `anchor_checksum`**；而
   `verify_provenance.py` 正是用 `"anchor_checksum" in g` 来筛选锚节点的
   ⇒ `anchors` 恒为空 ⇒ `if heads and anchors:` 为假 ⇒
   **`head_vs_anchor`、`anchor_checksum[...]`、`anchor_link[...]` 三条校验被整段跳过**，
   而输出仍然显示"通过"。

## 修的过程中又暴露两条**判据本身**的错误（同样是"门禁没有判别力"）

3. `content_sha256`：库里 `sha256_hash` 为 NULL 时导出 `contentSha256: null`，
   旧代码仍去复算并与 `None` 比对 ⇒ **必然 FAIL**（不可证被当成已证伪）。
   现补 SKIP 分支并写明原因。
4. `head_vs_anchor`：锚是**时点**见证，链在被锚后必然继续增长 ⇒
   要求"当前链头 == 某条锚头"只在"锚定后零写入"时成立 ⇒ **永久假红**。
   而本导出产物**结构上不含锚定行**（只有该记忆的活动 + 最后 3 条锚），
   "链被改写"与"链正常增长"给出**相同签名** ⇒ 这一条在本产物内
   **不可判定**，正确处理是显式 SKIP 并指向 `audit_anchor.py --verify`。

## 本文件怎么测

不喊口号，直接**跑真实 CLI**：用合成 jsonld 驱动 `verify_provenance.py`，
断言它分别产出 PASS / SKIP 且退出码正确。判据有牙齿的关键在
`test_wrong_hash_is_still_red` —— 证明"修好的是误报，不是把校验器改成恒绿"。
"""
from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
EXPORT_PY = ROOT / "scripts" / "provenance_export.py"
VERIFY_PY = ROOT / "scripts" / "verify_provenance.py"
PY = str(ROOT / ".venv" / "Scripts" / "python.exe")


def _load_verifier():
    spec = importlib.util.spec_from_file_location("_vp_20261006", VERIFY_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def vp():
    return _load_verifier()


def _anchor(vp, **over):
    body = {
        "ts": "2026-10-06T06:02:00+00:00",
        "chain_head_id": "head-1",
        "chain_head_checksum": "deadbeef" * 8,
        "row_count": 10,
        "prev_anchor_checksum": "",
        "prefix_count": 10,
        "prefix_xor": "00" * 32,
        "backend": "postgresql",
    }
    body.update(over)
    fields = {k: body[k] for k in vp.ANCHOR_FIELDS if k in body}
    node = {
        "@id": "trinity:anchor/0",
        "@type": "prov:Entity",
        "anchor_checksum": vp._sha(json.dumps(fields, sort_keys=True, ensure_ascii=False)),
    }
    node.update(fields)
    return node


def _graph(vp, *, content="hello", sha=None, anchors=None):
    ent = {
        "@id": "trinity:memory/m1",
        "@type": "prov:Entity",
        "trinity:content": content,
        "trinity:contentSha256": sha,
        "trinity:contentTruncated": False,
        "trinity:contentLength": len(content),
    }
    return {"@context": {}, "@graph": [ent] + (anchors or [])}


def _run(tmp_path, doc):
    p = tmp_path / "prov.jsonld"
    p.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    # t133/G10R1（根因修）：**必须显式 encoding**。
    # 缺它时父进程按 locale 解码子进程的 UTF-8 输出；本机（无 PYTHONUTF8/PYTHONIOENCODING）locale=cp936
    # ⇒ 捕获**为空** ⇒ 3 条判据假红（实测 `assert ('SKIP' in '')`；full8 的三条红即此）。
    # ⚠️ 反向教训：若跑测试的 shell 导出了 `PYTHONIOENCODING=utf-8`，本缺陷会被**掩盖**（我第一轮就被自己的环境骗了）。
    r = subprocess.run([PY, str(VERIFY_PY), str(p)],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", cwd=str(ROOT))
    return r.returncode, (r.stdout or "") + (r.stderr or "")


# ── content_sha256：三种"不可证"必须 SKIP，而不是 FAIL ─────────────────────

def test_null_claimed_hash_is_skipped_not_failed(tmp_path, vp):
    """库里未落 sha256_hash ⇒ 导出 null ⇒ 无对账对象 ⇒ SKIP（修 G6 前是 FAIL）。"""
    code, out = _run(tmp_path, _graph(vp, content="plaintext", sha=None))
    assert "SKIP" in out and "sha256_hash 为空" in out
    assert "FAIL" not in out, "不可证被当成已证伪：\n%s" % out
    assert code == 0


def test_ciphertext_row_is_skipped(tmp_path, vp):
    code, out = _run(tmp_path, _graph(vp, content="enc:v1:abc", sha="whatever"))
    assert "SKIP" in out and "密文行" in out
    assert code == 0


def test_correct_hash_passes(tmp_path, vp):
    code, out = _run(tmp_path, _graph(vp, content="hello", sha=vp._sha("hello")))
    assert "PASS" in out
    assert code == 0


def test_wrong_hash_is_still_red(tmp_path, vp):
    """**判据有牙齿**：claimed 值存在且不符时必须 FAIL（修的是误报，不是把校验器改成恒绿）。"""
    code, out = _run(tmp_path, _graph(vp, content="hello", sha="0" * 64))
    assert "FAIL" in out
    assert code == 2


# ── anchor_checksum：必须真的被校验（修 G6 前这条腿恒不执行） ──────────────

def test_valid_anchor_checksum_passes(tmp_path, vp):
    code, out = _run(tmp_path, _graph(vp, sha=vp._sha("hello"), anchors=[_anchor(vp)]))
    assert "PASS  anchor_checksum[0]" in out or "anchor_checksum[0]" in out
    assert code == 0


def test_tampered_anchor_checksum_is_red(tmp_path, vp):
    bad = _anchor(vp)
    bad["anchor_checksum"] = "f" * 64
    code, out = _run(tmp_path, _graph(vp, sha=vp._sha("hello"), anchors=[bad]))
    assert "FAIL" in out and "anchor_checksum[0]" in out
    assert code == 2


# ── head_vs_anchor：不可判定必须 SKIP，且必须指向可判定的工具 ──────────────

def test_head_vs_anchor_skips_when_unprovable(tmp_path, vp):
    doc = _graph(vp, sha=vp._sha("hello"), anchors=[_anchor(vp)])
    doc["@graph"].append({
        "@id": "trinity:activity/a1", "@type": "prov:Activity",
        "trinity:checksum": "cafe" * 16, "trinity:isChainHead": True,
        "trinity:id": "a1", "trinity:memoryId": "m1", "trinity:action": "create",
        "trinity:agentId": "x", "trinity:personaId": "default",
        "trinity:timestamp": "2026-10-06T06:00:00+00:00", "trinity:details": {},
        "trinity:prevChecksum": "",
    })
    code, out = _run(tmp_path, doc)
    assert "head_vs_anchor" in out
    assert "SKIP" in out, "不可判定项必须显式 SKIP：\n%s" % out
    assert "audit_anchor.py --verify" in out, "必须指向真正能判定的工具"


# ── 源码级防回退 ────────────────────────────────────────────────────────

def test_export_conn_no_longer_hardcodes_empty_password():
    src = EXPORT_PY.read_text(encoding="utf-8")
    assert "_pg_std" in src and "pg_connect" in src, "未复用共享凭据解析"
    assert 'password=os.environ.get("TRINITY_PG_PASSWORD", "")' not in src.split("def _conn")[1].split("def ")[1], (
        "回退分支之外的默认连接仍硬编码空口令"
    )


def test_export_emits_anchor_checksum():
    src = EXPORT_PY.read_text(encoding="utf-8")
    assert 'node["anchor_checksum"]' in src, "锚节点必须导出 anchor_checksum（否则 verify 的锚腿恒空）"


def test_verifier_keeps_the_three_way_head_logic():
    src = VERIFY_PY.read_text(encoding="utf-8")
    assert "anchored_heads" in src
    assert "链已增长" in src
    assert "本产物无法判定" in src
