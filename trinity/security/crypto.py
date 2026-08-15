"""
Trinity — 存储加密（B5, 2026-08-15）
=====================================
AES-256-GCM 可选加密：保护 SQLite 落盘的敏感正文（memories.content /
memory_versions.content），密钥由环境变量或密钥文件提供。

开关：
    TRINITY_STORAGE_ENCRYPTION=on    # 启用存储加密
    TRINITY_STORAGE_KEY=<hex 64>     # 32 字节密钥（hex）；缺省时自动生成并
                                     # 持久化到 ~/.trinity/secrets/storage.key

设计取舍（与 FTS/哈希链的兼容）：
    - content 列存密文（base64(nonce||ct||tag)）
    - tokenized_content 保持明文 → FTS5 全文检索不受影响
    - sha256_hash/content_hash 基于明文计算 → 去重/一致性链/身份保留不变
    - 高敏部署可关闭 FTS 或仅索引非敏感字段（见 docs/COMPLIANCE_GDPR_20260815.md）

用法：
    from trinity.security.crypto import get_storage_cipher
    cipher = get_storage_cipher()          # None 表示未启用
    enc = cipher.encrypt("秘密内容")
    assert cipher.decrypt(enc) == "秘密内容"
"""

from __future__ import annotations

import base64
import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger("trinity.security.crypto")

ENV_SWITCH = "TRINITY_STORAGE_ENCRYPTION"
ENV_KEY = "TRINITY_STORAGE_KEY"
SECRETS_DIR = Path(os.environ.get("TRINITY_HOME", str(Path.home() / ".trinity"))) / "secrets"
KEY_FILE = SECRETS_DIR / "storage.key"

# 密文格式: b64(nonce(12) || ciphertext || tag(16))
_NONCE_LEN = 12
_TAG_LEN = 16
_PREFIX = "enc:v1:"


class StorageCipher:
    """AES-256-GCM 加解密封装。"""

    def __init__(self, key: bytes):
        if len(key) != 32:
            raise ValueError(f"storage key must be 32 bytes, got {len(key)}")
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        self._aesgcm = AESGCM(key)

    def encrypt(self, plaintext: str) -> str:
        data = plaintext.encode("utf-8")
        nonce = os.urandom(_NONCE_LEN)
        ct = self._aesgcm.encrypt(nonce, data, None)
        return _PREFIX + base64.b64encode(nonce + ct).decode("ascii")

    def decrypt(self, payload: str) -> str:
        if not payload.startswith(_PREFIX):
            # 未加密的历史数据（或非 content 字段）原样返回
            return payload
        raw = base64.b64decode(payload[len(_PREFIX):])
        nonce, ct = raw[:_NONCE_LEN], raw[_NONCE_LEN:]
        return self._aesgcm.decrypt(nonce, ct, None).decode("utf-8")

    def is_encrypted(self, payload: str) -> bool:
        return payload.startswith(_PREFIX)


def _load_or_create_key() -> Optional[bytes]:
    """从环境变量或密钥文件加载 32 字节密钥；都不存在则生成并持久化。"""
    hex_key = os.environ.get(ENV_KEY, "").strip()
    if hex_key:
        try:
            return bytes.fromhex(hex_key)
        except ValueError:
            logger.error("%s 不是合法 hex（需 64 hex 字符 = 32 字节）", ENV_KEY)
            return None
    try:
        if KEY_FILE.exists():
            return KEY_FILE.read_bytes().strip()
        SECRETS_DIR.mkdir(parents=True, exist_ok=True)
        key = os.urandom(32)
        KEY_FILE.write_bytes(key)
        try:
            os.chmod(KEY_FILE, 0o600)
        except OSError:
            pass
        logger.info("generated storage key -> %s", KEY_FILE)
        return key
    except OSError as exc:
        logger.error("storage key file access failed: %s", exc)
        return None


def is_enabled() -> bool:
    return os.environ.get(ENV_SWITCH, "").strip().lower() in ("1", "on", "true", "yes")


def get_storage_cipher() -> Optional[StorageCipher]:
    """返回 StorageCipher；未启用或密钥不可用时返回 None。"""
    if not is_enabled():
        return None
    key = _load_or_create_key()
    if key is None:
        logger.error("TRINITY_STORAGE_ENCRYPTION=on 但无法获取密钥，加密未生效")
        return None
    return StorageCipher(key)
