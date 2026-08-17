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

logger = logging.getLogger("trinity.adapters.sqlite")


class _CryptoMixin:
    # PII 检测按优先级排序：长匹配优先，避免身份证中的数字被误当作电话号码
    _PII_PATTERNS = {
        "id_card": r"[1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx]",
        "phone":   r"(?:(?:\+|00)86[\s\-]?)?1[3-9]\d{9}(?!\d)",
        "email":   r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}",
    }

    def _compute_sha256(self, content: str) -> str:
        return hashlib.sha256(content.encode("utf-8")).hexdigest()
    def _encrypt_content(self, content: str) -> str:
        """写入前加密（未启用时原样返回）。"""
        if self._cipher is None:
            return content
        return self._cipher.encrypt(content)
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
                    if pii_type == "phone":
                        # 138****1234 保留首尾3+4位
                        digits = re.sub(r"\D", "", match)
                        if len(digits) >= 7:
                            replacement = digits[:3] + "****" + digits[-4:]
                        elif len(digits) >= 3:
                            replacement = digits[:3] + "****"
                        else:
                            replacement = digits + "****"
                    elif pii_type == "email":
                        # email: a***@domain.com
                        local, domain = match.split("@", 1)
                        if len(local) >= 3:
                            replacement = local[0] + "***@" + domain
                        elif len(local) >= 1:
                            replacement = local[0] + "***@" + domain
                        else:
                            replacement = "***@" + domain
                    elif pii_type == "id_card":
                        # 身份证: 保留前6后4
                        replacement = match[:6] + "********" + match[-4:]
                    else:
                        replacement = "***"
                    # 只替换第一次出现
                    redacted = redacted.replace(match, replacement, 1)

        return {"redacted": redacted, "found": found}
