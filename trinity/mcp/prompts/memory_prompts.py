"""
提示模板 (Memory Prompts)

提供记忆相关的 LLM 提示模板：
- summarize_memories: 记忆总结
- resolve_conflict:   冲突消解决策
"""

import logging
from typing import Optional

from mcp.server.fastmcp import FastMCP

logger = logging.getLogger("trinity_mcp.prompts")

# ---------------------------------------------------------------------------
# 注册入口
# ---------------------------------------------------------------------------
def register_memory_prompts(mcp: FastMCP) -> None:
    """向 FastMCP 实例注册所有提示模板。

    Args:
        mcp: FastMCP server 实例。
    """
    _register_summarize_memories(mcp)
    _register_resolve_conflict(mcp)
    logger.info("Registered 2 memory prompts.")


# ---------------------------------------------------------------------------
# Prompt: summarize_memories
# ---------------------------------------------------------------------------
def _register_summarize_memories(mcp: FastMCP) -> None:
    """注册 summarize_memories 提示模板。"""

    @mcp.prompt()
    def summarize_memories(
        category: str = "all",
        time_range: str = "last_7_days",
        detail_level: str = "concise",
    ) -> str:
        """记忆总结提示模板。

        根据分类和时间范围筛选记忆，生成结构化总结。

        Args:
            category:     记忆分类过滤。默认 "all" 表示全部。
            time_range:   时间范围，可选 last_7_days / last_30_days / all。默认 last_7_days。
            detail_level: 总结粒度，可选 concise / detailed。默认 concise。

        Returns:
            格式化后的完整 Prompt 字符串。
        """
        detail_instructions: str
        if detail_level == "detailed":
            detail_instructions = (
                "请为每条记忆提供详细总结，包括关键信息点、关联实体和时间背景。"
            )
        else:
            detail_instructions = (
                "请用 1-2 句话概括每条记忆的核心要点，并按主题分组。"
            )

        prompt: str = f"""你是一个记忆分析助手。请基于以下参数总结 Trinity 记忆系统的内容：

## 参数
- **分类过滤**: {category}
- **时间范围**: {time_range}
- **粒度**: {detail_level}

## 输出要求
{detail_instructions}

## 输出格式
1. **总体概览** — 记忆数量、主要主题分布
2. **按主题分组** — 每组列出相关记忆摘要
3. **关键关联** — 记忆之间的交叉引用或因果关系
4. **待跟进事项** — 标记未完成或需要关注的内容（如有）

请基于 `memory_search` 工具的实际返回结果执行以上总结。
"""
        logger.info("summarize_memories prompt generated (category=%s, range=%s).", category, time_range)
        return prompt


# ---------------------------------------------------------------------------
# Prompt: resolve_conflict
# ---------------------------------------------------------------------------
def _register_resolve_conflict(mcp: FastMCP) -> None:
    """注册 resolve_conflict 提示模板。"""

    @mcp.prompt()
    def resolve_conflict(
        memory_id: str,
        current_content: str = "",
        conflicting_content: str = "",
        current_sha256: str = "",
        conflicting_sha256: str = "",
    ) -> str:
        """冲突消解决策提示模板。

        展示新旧版本及 SHA-256 链，引导用户选择保留方案。

        Args:
            memory_id:          冲突记忆的唯一标识。
            current_content:    当前版本内容。
            conflicting_content: 冲突版本内容。
            current_sha256:     当前版本 SHA-256 哈希。
            conflicting_sha256: 冲突版本 SHA-256 哈希。

        Returns:
            格式化后的完整 Prompt 字符串。
        """
        prompt: str = f"""你是一个记忆冲突消解助手。Trinity 记忆系统中检测到版本冲突。

## 冲突详情

| 字段 | 当前版本 | 冲突版本 |
|------|---------|---------|
| **Memory ID** | {memory_id} | {memory_id} |
| **SHA-256** | `{current_sha256}` | `{conflicting_sha256}` |

### 当前版本内容
```
{current_content}
```

### 冲突版本内容
```
{conflicting_content}
```

## 消解决策引导

请帮助用户选择以下方案之一：

1. **保留当前版本** (Keep Current) — 丢弃冲突版本，保留现有内容。
2. **替换为冲突版本** (Accept Incoming) — 用冲突版本覆盖当前内容。
3. **合并两个版本** (Merge) — 尝试智能合并两部分内容，保留互补信息。
4. **保留两者** (Keep Both) — 将冲突版本作为新的独立记忆写入，当前版本不变。

## 输出格式

请输出一个结构化的决策建议：
- **推荐方案**: 方案名称（1/2/3/4）
- **理由**: 基于内容差异分析的推荐原因
- **风险说明**: 所选方案可能的数据损失或冗余

注意：SHA-256 哈希链保证内容完整性，两版本均未被篡改。最终决策由用户确认。
"""
        logger.info("resolve_conflict prompt generated for memory_id=%s.", memory_id)
        return prompt
