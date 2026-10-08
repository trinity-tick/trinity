"""SQLite adapter - encryption & PII mixin (split from sqlite.py, 2026-08-17).

Part of the SQLiteAdapter package decomposition. Behavior identical to the
pre-split single-file implementation.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import functools
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from ...security.crypto import get_storage_cipher, StorageCipher  # type: ignore[attr-defined]
from .._util import _safe_write

from ._base import _SQLiteMixinBase

logger = logging.getLogger("trinity.adapters.sqlite")


class _CryptoMixin(_SQLiteMixinBase):
    # PII 检测按优先级排序：长匹配优先，避免身份证中的数字被误当作电话号码
    _PII_PATTERNS = {
        "id_card": r"[1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx]",
        "phone":   r"(?:(?:\+|00)86[\s\-]?)?1[3-9]\d{9}(?!\d)",
        "email":   r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}",
    }

    def _compute_sha256(self, content: str) -> str:
        return hashlib.sha256(content.encode("utf-8")).hexdigest()
    def _encrypt_content(self, content: str) -> str:
        """写入前加密（未启用时原样返回；**已是密文则原样返回**）。

        2026-09-26（事故修复，EXECUTION 本轮）：原实现**无条件**加密 —— 对已经是
        `enc:v1:` 的内容会再包一层，产出**双层密文**。这与另两处同族实现的
        fail-open 口径不一致（`trinity.security.crypto.encrypt_content` 与 PG 侧
        `_pg_write_guard.maybe_encrypt_content` 都会跳过已加密内容）。

        实测代价（只读探针）：生产 PG 最新 1000 条 perception 里 **848 条是双层密文**
        （剥一层仍是 `enc:v1:`）⇒ 单层解密实现把它们交回调用方时是 base64，
        时序巩固因此丢掉 828/2999 行输入。读侧已改为有界剥层（crypto.decrypt_content），
        本处是**写侧根因**：不再制造新的双层行。判据：
        `tests/unit/test_crypto_multi_layer.py`、`tests/unit/test_sqlite_no_double_encrypt.py`。
        """
        if self._cipher is None or not isinstance(content, str) or not content:
            return content
        if self._cipher.is_encrypted(content):
            return content
        return self._cipher.encrypt(content)
    def _decrypt_text_resilient(self, text, *, memory_id=None, tokenized=None,
                                 where: str = "read") -> str:
        """单条文本解密，**失败不抛**：退回该行 tokenized_content（或空）并**指名记录**。

        2026-09-29（外部审计 Round 27）：`_decrypt_content` 在多个逐行循环里被直接调用，
        任一行坏密文（实测 `InvalidTag`）都会让**整次检索**失败 —— 例如
        `sqlite/_search.py:254/318`（`search_memories` 内）就让 `search(mode="graph")` 直接崩。
        本助手把「一行坏数据」的影响限制在它自己，并留下可见痕迹（同 `_decrypt_rows_resilient`
        的策略）。
        """
        try:
            return self._decrypt_content(text)
        except Exception as _e:                       # noqa: BLE001
            _tokenized = tokenized or ""
            logger.warning(
                "%s: 解密失败 memory_id=%s (%s)；%s",
                where, memory_id, type(_e).__name__,
                "已退回 tokenized_content" if _tokenized else "内容置空")
            return _tokenized

    def _decrypt_content(self, content: str) -> str:
        """读取后解密（未加密的历史数据原样返回）。"""
        if self._cipher is None or not content:
            return content
        return self._cipher.decrypt(content)
    def _tokenized_for_storage(self, plain_content: str, tokenized: Optional[str]) -> Optional[str]:
        """确定写入 tokenized_content 列的值。

        - 未加密：保持原逻辑（CJK 分词，非 CJK 为 None 由触发器回退 content）
        - 加密后：content 列是密文，FTS 触发器 COALESCE(tokenized, content)
          会回退到密文 → 检索失效。因此加密模式下非 CJK 内容也写入
          明文 content 作为 tokenized_content，保证 FTS 可搜。
        """
        if self._cipher is not None and not tokenized:
            return plain_content
        return tokenized
    def _detect_pii(self, content: str) -> Dict[str, List[str]]:
        """检测内容中的 PII 并返回脱敏后的内容与检测结果。

        Returns:
            {"redacted": 脱敏后的内容, "found": {"phone": [...], "email": [...], "id_card": [...]}}
        """
        import re

        found: Dict[str, List[str]] = {"phone": [], "email": [], "id_card": []}
        redacted = content

        # 按优先级顺序检测（身份证 > 电话 > 邮箱），避免长内容被短模式误匹配
        for pii_type, pattern in self._PII_PATTERNS.items():
            matches = re.findall(pattern, redacted)
            if matches:
                # 去重并排序（长匹配优先替换）
                unique = sorted(set(matches), key=len, reverse=True)
                found[pii_type] = unique
                for match in unique:
                    # ⭐ G9R-9/t123：口径**对齐 G2/D1 `MASK_CONVENTION`**
                    #   （`trinity/security/redaction_surface.py`）：「保留前 3、去尾号」；邮箱只留 TLD。
                    # 本处**只改掩码口径**，未动调用面（`store_memory(auto_redact_pii=…)`）——
                    # 全仓**无调用方传 True**（实测 grep 命中 0；复现命令见
                    # `evidence/g9r9-no-caller.txt`），默认 False ⇒ 本路径当前不生效，
                    # 改它只为**消除"打开即分叉"的 landmine**。
                    if pii_type == "phone":
                        digits = re.sub(r"\D", "", match)
                        if len(digits) >= 3:
                            # 旧口径：digits[:3] + "****" + digits[-4:]（**留尾 4 位**）⇒ 已废弃
                            replacement = digits[:3] + "*" * (len(digits) - 3)
                        else:
                            replacement = digits + "***"
                    elif pii_type == "email":
                        local, domain = match.split("@", 1)
                        # 旧口径：local[0] + "***@" + domain（**保留完整域名**）⇒ 已废弃
                        tld = domain.rsplit(".", 1)[-1] if "." in domain else domain
                        replacement = "%s***@***.%s" % (local[0] if local else "", tld)
                    elif pii_type == "id_card":
                        # 旧口径：match[:6] + "********" + match[-4:]（**留尾 4 位**）⇒ 已废弃
                        replacement = match[:3] + "*" * max(0, len(match) - 3)
                    else:
                        replacement = "***"
                    # 只替换第一次出现
                    redacted = redacted.replace(match, replacement, 1)

        return {"redacted": redacted, "found": found}
