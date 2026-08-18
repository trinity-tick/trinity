"""
# status: orphan (2026-08-15 audit, not in runtime path)
P10-8: MemPrivacy Edge-Cloud — 对标 arXiv 2605.09530

实现边云协同的隐私保护记忆系统:
  - PrivacySpanDetector: 边缘侧识别隐私敏感片段 (PII / 密钥 / 位置)
  - TypeAwarePlaceholder: 生成语义类型占位符替换敏感内容
  - cloud_process(): 云端基于占位符处理（无明文敏感信息）
  - local_restore(): 本地恢复原始值
  - 四级隐私分类: critical / high / medium / low

Reference:
    arXiv 2605.09530 (2026): "MemPrivacy: Edge-Cloud Collaborative Privacy-Preserving Memory"
"""

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


# ══════════════════════════════════════════════════════════════════════
# 枚举与数据结构
# ══════════════════════════════════════════════════════════════════════

class PrivacyLevel(Enum):
    """隐私敏感等级。"""
    CRITICAL = "critical"   # 最高敏感：密钥、密码、Token
    HIGH = "high"           # 高敏感：身份证、银行卡、地理位置
    MEDIUM = "medium"       # 中敏感：邮箱、手机号、IP 地址
    LOW = "low"             # 低敏感：姓名、用户名


class SpanType(Enum):
    """隐私片段类型。"""
    API_KEY = "api_key"
    PASSWORD = "password"
    TOKEN = "token"
    EMAIL = "email"
    PHONE = "phone"
    ID_CARD = "id_card"
    BANK_CARD = "bank_card"
    IP_ADDRESS = "ip_address"
    LOCATION = "location"
    PERSON_NAME = "person_name"
    CREDENTIAL = "credential"
    CUSTOM = "custom"


@dataclass
class PrivacySpan:
    """隐私敏感片段。"""
    span_id: str
    span_type: SpanType
    privacy_level: PrivacyLevel
    start_pos: int
    end_pos: int
    original_text: str
    placeholder: str = ""
    context_before: str = ""
    context_after: str = ""


@dataclass
class SanitizedContent:
    """脱敏后的内容。"""
    sanitized_text: str                    # 占位符替换后的文本
    spans: list[PrivacySpan] = field(default_factory=list)
    placeholder_map: dict[str, str] = field(default_factory=dict)  # placeholder → original
    metadata: dict = field(default_factory=dict)


@dataclass
class CloudProcessResult:
    """云端处理结果。"""
    result_id: str
    sanitized_query: str                   # 发送到云端的脱敏查询
    cloud_response: str                    # 云端返回（含占位符）
    restored_response: str                 # 本地恢复后的完整响应
    processing_time_ms: float = 0.0
    spans_processed: list[PrivacySpan] = field(default_factory=list)


# ══════════════════════════════════════════════════════════════════════
# Privacy Span Detector (边缘侧)
# ══════════════════════════════════════════════════════════════════════

class PrivacySpanDetector:
    """边缘侧隐私片段检测器。

    检测类型:
      - API Key / Token: 高熵字符串模式
      - 邮箱: xxx@xxx.xxx
      - 手机号: 1[3-9]XXXXXXXXX
      - 身份证: 18 位数字
      - 银行卡: 16-19 位数字
      - IP 地址: xxx.xxx.xxx.xxx
      - 地理位置坐标: lat, lng 对
      - 密码: 上下文关键词 + 赋值模式

    Usage:
        detector = PrivacySpanDetector()
        spans = detector.detect("My API key is sk-abc123def456 and email is test@example.com")
    """

    # ── 检测规则 ────────────────────────────────────────────────────

    _API_KEY_PATTERNS: list[str] = [
        r"(?:api[_-]?key|apikey|access[_-]?token|auth[_-]?token)\s*[:=]\s*['\"]?([A-Za-z0-9._\-+=]{20,})['\"]?",
        r"sk-[A-Za-z0-9]{32,}",
        r"(?:Bearer\s+)([A-Za-z0-9._\-+=]{20,})",
    ]

    _EMAIL_PATTERN: str = r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"

    _PHONE_PATTERNS: list[str] = [
        r"1[3-9]\d{9}",                          # 中国手机号
        r"\+\d{1,3}[\s-]?\d{6,14}",              # 国际电话
    ]

    _ID_CARD_PATTERN: str = r"\b[1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[0-9Xx]\b"

    _BANK_CARD_PATTERN: str = r"\b\d{4}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4,7}\b"

    _IP_PATTERN: str = r"\b(?:\d{1,3}\.){3}\d{1,3}\b"

    _LOCATION_PATTERNS: list[str] = [
        r"(?:latitude|lat|lng|longitude|lon)\s*[:=]\s*([\d.\-]+)",
        r"(?:location|地点|位置)\s*[:=]\s*['\"]?([^'\"]{2,50})['\"]?",
        r"\b(\d{2,3}\.\d{4,})\s*,\s*(\d{2,3}\.\d{4,})\b",
    ]

    _PASSWORD_PATTERNS: list[str] = [
        r"(?:password|passwd|pwd|secret)\s*[:=]\s*['\"]?([^\s'\"]{4,})['\"]?",
        r"(?:密码|口令)\s*[:=]\s*['\"]?([^\s'\"]{4,})['\"]?",
    ]

    _PERSON_NAME_CONTEXT: list[str] = [
        r"(?:name|姓名|用户名|username|user)\s*[:=]\s*['\"]?([A-Za-z\u4e00-\u9fff]{2,20})['\"]?",
    ]

    # ── 检测方法 ────────────────────────────────────────────────────

    def detect(self, text: str) -> list[PrivacySpan]:
        """检测文本中的所有隐私敏感片段。

        返回:
            [PrivacySpan, ...] 按位置排序。
        """
        spans: list[PrivacySpan] = []

        # API Key
        for pattern in self._API_KEY_PATTERNS:
            spans.extend(self._match_pattern(text, pattern, SpanType.API_KEY, PrivacyLevel.CRITICAL))

        # Token 模式（高熵字符串）
        spans.extend(self._detect_high_entropy_tokens(text))

        # 密码
        for pattern in self._PASSWORD_PATTERNS:
            spans.extend(self._match_pattern(text, pattern, SpanType.PASSWORD, PrivacyLevel.CRITICAL))

        # 身份证
        spans.extend(self._match_pattern(text, self._ID_CARD_PATTERN, SpanType.ID_CARD, PrivacyLevel.HIGH))

        # 银行卡
        spans.extend(self._match_pattern(text, self._BANK_CARD_PATTERN, SpanType.BANK_CARD, PrivacyLevel.HIGH))

        # 位置
        for pattern in self._LOCATION_PATTERNS:
            spans.extend(self._match_pattern(text, pattern, SpanType.LOCATION, PrivacyLevel.HIGH))

        # 邮箱
        spans.extend(self._match_pattern(text, self._EMAIL_PATTERN, SpanType.EMAIL, PrivacyLevel.MEDIUM))

        # 手机号
        for pattern in self._PHONE_PATTERNS:
            spans.extend(self._match_pattern(text, pattern, SpanType.PHONE, PrivacyLevel.MEDIUM))

        # IP
        spans.extend(self._match_pattern(text, self._IP_PATTERN, SpanType.IP_ADDRESS, PrivacyLevel.MEDIUM))

        # 人名
        for pattern in self._PERSON_NAME_CONTEXT:
            spans.extend(self._match_pattern(text, pattern, SpanType.PERSON_NAME, PrivacyLevel.LOW))

        # 去重（按位置范围去重，保留最高隐私等级）
        spans = self._deduplicate_spans(spans)

        # 按位置排序
        spans.sort(key=lambda s: s.start_pos)

        # 为每个 span 提取上下文
        for span in spans:
            span.context_before = text[max(0, span.start_pos - 30):span.start_pos]
            span.context_after = text[span.end_pos:min(len(text), span.end_pos + 30)]

        return spans

    def _match_pattern(
        self, text: str, pattern: str, span_type: SpanType, level: PrivacyLevel
    ) -> list[PrivacySpan]:
        spans = []
        for m in re.finditer(pattern, text, re.IGNORECASE):
            original = m.group(1) if m.lastindex else m.group(0)
            span = PrivacySpan(
                span_id=f"{span_type.value}_{m.start()}",
                span_type=span_type,
                privacy_level=level,
                start_pos=m.start() if m.lastindex else m.start(),
                end_pos=m.end() if m.lastindex else m.end(),
                original_text=original,
            )
            spans.append(span)
        return spans

    def _detect_high_entropy_tokens(self, text: str) -> list[PrivacySpan]:
        """检测高熵 Token 字符串。"""
        spans = []
        # 匹配长随机字符串（通常出现在赋值上下文中）
        pattern = r"(?:token|key|secret)\s*[:=]\s*['\"]?([A-Za-z0-9+/=]{32,})['\"]?"
        for m in re.finditer(pattern, text, re.IGNORECASE):
            original = m.group(1)
            # 熵检查
            if self._calc_entropy(original) > 3.5:
                span = PrivacySpan(
                    span_id=f"token_{m.start()}",
                    span_type=SpanType.TOKEN,
                    privacy_level=PrivacyLevel.CRITICAL,
                    start_pos=m.start(1),
                    end_pos=m.end(1),
                    original_text=original,
                )
                spans.append(span)
        return spans

    @staticmethod
    def _calc_entropy(s: str) -> float:
        """计算字符串的 Shannon 熵。"""
        from math import log2
        if not s:
            return 0.0
        counts: dict[str, int] = {}
        for ch in s:
            counts[ch] = counts.get(ch, 0) + 1
        n = len(s)
        return -sum(
            (c / n) * log2(c / n) for c in counts.values()
        )

    def _deduplicate_spans(self, spans: list[PrivacySpan]) -> list[PrivacySpan]:
        """去重：重叠 span 保留最高隐私等级。"""
        if not spans:
            return spans

        # 按 level 优先级排序
        level_order = {
            PrivacyLevel.CRITICAL: 0,
            PrivacyLevel.HIGH: 1,
            PrivacyLevel.MEDIUM: 2,
            PrivacyLevel.LOW: 3,
        }

        spans.sort(key=lambda s: (s.start_pos, level_order.get(s.privacy_level, 99)))
        result: list[PrivacySpan] = []

        for span in spans:
            # 检查是否与已有结果重叠
            overlap = False
            for existing in result:
                if not (span.end_pos <= existing.start_pos or span.start_pos >= existing.end_pos):
                    overlap = True
                    break
            if not overlap:
                result.append(span)

        return result


# ══════════════════════════════════════════════════════════════════════
# Type-Aware Placeholder Generator
# ══════════════════════════════════════════════════════════════════════

class TypeAwarePlaceholder:
    """语义类型占位符生成器。

    生成格式: <PRIVACY_{TYPE}_{HASH}> 保留类型语义信息以便云端基于类型优化处理。
    """

    _TYPE_PREFIX_MAP: dict[SpanType, str] = {
        SpanType.API_KEY: "API_KEY",
        SpanType.PASSWORD: "PASSWORD",
        SpanType.TOKEN: "TOKEN",
        SpanType.EMAIL: "EMAIL",
        SpanType.PHONE: "PHONE",
        SpanType.ID_CARD: "ID_CARD",
        SpanType.BANK_CARD: "BANK_CARD",
        SpanType.IP_ADDRESS: "IP_ADDR",
        SpanType.LOCATION: "LOCATION",
        SpanType.PERSON_NAME: "PERSON",
        SpanType.CREDENTIAL: "CREDENTIAL",
        SpanType.CUSTOM: "CUSTOM",
    }

    def generate(self, span: PrivacySpan) -> str:
        """为隐私片段生成语义占位符。"""
        prefix = self._TYPE_PREFIX_MAP.get(span.span_type, "PRIVACY")
        short_hash = hashlib.sha256(span.original_text.encode()).hexdigest()[:8]
        placeholder = f"<PRIVACY_{prefix}_{short_hash}>"
        span.placeholder = placeholder
        return placeholder

    def sanitize(self, text: str, spans: list[PrivacySpan]) -> SanitizedContent:
        """用占位符替换文本中的敏感内容。

        参数:
            text: 原始文本。
            spans: 检测到的隐私片段列表。

        返回:
            SanitizedContent 含脱敏文本和占位符映射。
        """
        # 按位置降序排序以避免偏移问题
        sorted_spans = sorted(spans, key=lambda s: -s.start_pos)

        sanitized = text
        placeholder_map: dict[str, str] = {}

        for span in sorted_spans:
            if not span.placeholder:
                self.generate(span)
            sanitized = (
                sanitized[:span.start_pos] +
                span.placeholder +
                sanitized[span.end_pos:]
            )
            placeholder_map[span.placeholder] = span.original_text

        return SanitizedContent(
            sanitized_text=sanitized,
            spans=spans,
            placeholder_map=placeholder_map,
            metadata={
                "original_length": len(text),
                "sanitized_length": len(sanitized),
                "spans_count": len(spans),
                "privacy_levels": {
                    level.value: sum(
                        1 for s in spans if s.privacy_level == level
                    )
                    for level in PrivacyLevel
                },
            },
        )

    def restore(self, sanitized_text: str, placeholder_map: dict[str, str]) -> str:
        """在本地恢复原始值。

        参数:
            sanitized_text: 含占位符的文本。
            placeholder_map: placeholder → original 映射。

        返回:
            恢复后的原始文本。
        """
        restored = sanitized_text
        for placeholder, original in placeholder_map.items():
            restored = restored.replace(placeholder, original)
        return restored


# ══════════════════════════════════════════════════════════════════════
# MemPrivacy Edge-Cloud Pipeline
# ══════════════════════════════════════════════════════════════════════

class MemPrivacyPipeline:
    """边云协同隐私保护记忆管道。

    流程:
      1. 边缘侧: detect() 检测隐私片段
      2. 边缘侧: sanitize() 占位符替换
      3. 云端: cloud_process() 基于占位符处理
      4. 边缘侧: local_restore() 恢复原始值
    """

    def __init__(
        self,
        detector: PrivacySpanDetector | None = None,
        placeholder: TypeAwarePlaceholder | None = None,
    ):
        self.detector = detector or PrivacySpanDetector()
        self.placeholder = placeholder or TypeAwarePlaceholder()

    def cloud_process(
        self,
        text: str,
        cloud_handler: Any = None,
    ) -> CloudProcessResult:
        """完整的边云协同处理管道。

        参数:
            text: 需处理的原始文本。
            cloud_handler: 云端处理回调（需实现 process(sanitized_text) -> str）。

        返回:
            CloudProcessResult 含原始/脱敏/云端/恢复四个阶段结果。
        """
        t0 = time.time()
        result_id = hashlib.sha256(f"{text}{t0}".encode()).hexdigest()[:12]

        # 阶段 1: 边缘检测
        spans = self.detector.detect(text)

        # 阶段 2: 脱敏
        sanitized = self.placeholder.sanitize(text, spans)

        # 阶段 3: 云端处理
        if cloud_handler and hasattr(cloud_handler, "process"):
            cloud_response = cloud_handler.process(sanitized.sanitized_text)
        else:
            # Mock: 直接返回脱敏文本
            cloud_response = sanitized.sanitized_text

        # 阶段 4: 本地恢复
        restored = self.placeholder.restore(cloud_response, sanitized.placeholder_map)

        elapsed = (time.time() - t0) * 1000

        return CloudProcessResult(
            result_id=result_id,
            sanitized_query=sanitized.sanitized_text,
            cloud_response=cloud_response,
            restored_response=restored,
            processing_time_ms=elapsed,
            spans_processed=spans,
        )

    def get_privacy_report(self, text: str) -> dict:
        """生成隐私风险评估报告。"""
        spans = self.detector.detect(text)
        critical = sum(1 for s in spans if s.privacy_level == PrivacyLevel.CRITICAL)
        high = sum(1 for s in spans if s.privacy_level == PrivacyLevel.HIGH)
        medium = sum(1 for s in spans if s.privacy_level == PrivacyLevel.MEDIUM)
        low = sum(1 for s in spans if s.privacy_level == PrivacyLevel.LOW)

        return {
            "total_spans": len(spans),
            "critical": critical,
            "high": high,
            "medium": medium,
            "low": low,
            "risk_score": min(1.0, (critical * 10 + high * 5 + medium * 2 + low) / 50),
            "details": [
                {
                    "type": s.span_type.value,
                    "level": s.privacy_level.value,
                    "context": f"{s.context_before}...{s.context_after}",
                }
                for s in spans
            ],
        }

    def get_stats(self) -> dict:
        """获取管道统计。"""
        return {
            "detector_version": "1.0",
            "supported_span_types": [st.value for st in SpanType],
            "privacy_levels": [pl.value for pl in PrivacyLevel],
        }


# ══════════════════════════════════════════════════════════════════════
# 自检
# ══════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("MemPrivacy Edge-Cloud — 自检")
    print("=" * 60)

    # 模拟用户对话含敏感信息
    test_text = (
        "用户查询: 用 API key sk-abc123def456ghi789jkl 访问服务器。\n"
        "邮箱: alice@example.com, 手机: 13800138000\n"
        "身份证: 110101199001011234, 银行卡: 6222021234567890\n"
        "IP: 192.168.1.100, 位置: lat=39.9042, lng=116.4074\n"
        "密码: MySecureP@ssw0rd, 姓名: 张三\n"
    )

    pipeline = MemPrivacyPipeline()

    # 隐私检测
    print("\n[阶段 1: 边缘检测]")
    spans = pipeline.detector.detect(test_text)
    for s in spans:
        print(f"  [{s.privacy_level.value}] {s.span_type.value}: '{s.original_text}' at {s.start_pos}-{s.end_pos}")

    # 脱敏
    print(f"\n[阶段 2: 脱敏] {len(spans)} 个隐私片段")
    sanitized = pipeline.placeholder.sanitize(test_text, spans)
    print(f"  脱敏文本: {sanitized.sanitized_text[:120]}...")

    # 云端处理 (Mock)
    print("\n[阶段 3: 云端处理]")

    class MockCloudHandler:
        def process(self, text: str) -> str:
            return f"[云端处理完成] 已处理查询: {text}"

    result = pipeline.cloud_process(test_text, MockCloudHandler())
    print(f"  云查询: {result.sanitized_query[:100]}...")
    print(f"  云响应: {result.cloud_response[:100]}...")

    # 本地恢复
    print(f"\n[阶段 4: 本地恢复]")
    print(f"  恢复后: {result.restored_response[:120]}...")

    # 隐私报告
    print(f"\n[隐私报告]")
    report = pipeline.get_privacy_report(test_text)
    print(json.dumps({
        "total_spans": report["total_spans"],
        "critical": report["critical"],
        "high": report["high"],
        "medium": report["medium"],
        "low": report["low"],
        "risk_score": report["risk_score"],
    }, indent=2, ensure_ascii=False))

    # 验证恢复完整性
    restored = pipeline.placeholder.restore(
        sanitized.sanitized_text, sanitized.placeholder_map
    )
    assert restored == test_text, "恢复完整性验证失败!"
    print("\n恢复完整性验证: PASS")

    print("\n所有测试通过!")
