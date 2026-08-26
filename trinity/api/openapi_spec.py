# -*- coding: utf-8 -*-
"""OpenAPI 规范（Budibase 公开 API 模式借鉴，Phase 3）。

生成 Trinity REST API 的 OpenAPI 3.0 文档（GET /openapi.json），
覆盖主要端点（health/memories/search/hybrid/audit/graph/evolution/
structure/views/automation），供外部工具（Postman/代码生成）消费。
"""
from __future__ import annotations

from typing import Dict, Any

VERSION = "8.2.0"


def build_spec(server_url: str = "http://127.0.0.1:8001") -> Dict[str, Any]:
    spec: Dict[str, Any] = {
        "openapi": "3.0.3",
        "info": {
            "title": "Trinity Memory API",
            "description": (
                "Trinity 长程记忆系统 REST API（CRDT 版本链 / SHA-256 审计 / "
                "多租户隔离 / 47 通道检索 / 页树 / reason / 自动化规则）"
            ),
            "version": VERSION,
        },
        "servers": [{"url": server_url}],
        "tags": [
            {"name": "health", "description": "健康检查"},
            {"name": "memories", "description": "记忆 CRUD"},
            {"name": "search", "description": "检索（keyword/hybrid/pagetree/reason/view）"},
            {"name": "audit", "description": "审计与可证明性"},
            {"name": "graph", "description": "知识图谱"},
            {"name": "evolution", "description": "自进化"},
            {"name": "structure", "description": "DSH 结构层（会话/goal/todo）"},
            {"name": "automation", "description": "事件驱动自动化（Budibase 借鉴）"},
        ],
        "paths": {
            "/health": {
                "get": {
                    "tags": ["health"],
                    "summary": "健康检查（含 engine 组件与降级状态）",
                    "responses": {"200": {"description": "ok | degraded"}},
                }
            },
            "/memories": {
                "post": {
                    "tags": ["memories"],
                    "summary": "写入记忆（CRDT 版本化 + SHA-256 审计；触发 automation memory.write 事件）",
                    "requestBody": {
                        "required": True,
                        "content": {"application/json": {"schema": {"$ref": "#/components/schemas/MemoryWrite"}}},
                    },
                    "responses": {"200": {"description": "写入结果（memory_id/version_id/sha256_hash）"}},
                },
                "get": {
                    "tags": ["memories"],
                    "summary": "查询记忆列表",
                    "parameters": [
                        {"name": "q", "in": "query", "required": False, "schema": {"type": "string"}},
                        {"name": "top_k", "in": "query", "schema": {"type": "integer", "default": 10}},
                        {"name": "category", "in": "query", "schema": {"type": "string"}},
                    ],
                    "responses": {"200": {"description": "记忆列表"}},
                },
            },
            "/memory/search": {
                "get": {
                    "tags": ["search"],
                    "summary": "混合检索（FTS 默认；mode 切换语义/图谱/hybrid）",
                    "parameters": [
                        {"name": "q", "in": "query", "required": True, "schema": {"type": "string"}},
                        {"name": "mode", "in": "query", "schema": {"type": "string", "default": "keyword",
                                                                  "enum": ["keyword", "semantic", "graph", "hybrid", "reason"]}},
                        {"name": "top_k", "in": "query", "schema": {"type": "integer", "default": 10}},
                        {"name": "persona_id", "in": "query", "schema": {"type": "string"}},
                        {"name": "agent_id", "in": "query", "schema": {"type": "string"}},
                        {"name": "view", "in": "query", "schema": {"type": "string"},
                         "description": "命名记忆视图（views.yaml）"},
                        {"name": "visibility_rule", "in": "query", "schema": {"type": "string"},
                         "description": "行级可见性规则，如 importance>=0.6 AND category!='lme'"},
                    ],
                    "responses": {"200": {"description": "检索结果"}},
                }
            },
            "/memory/search/hybrid": {
                "post": {
                    "tags": ["search"],
                    "summary": "5 通道 RRF 融合检索（vector/bm25/graph/aggregator/procedural [+pagetree]）",
                    "responses": {"200": {"description": "融合结果"}},
                }
            },
            "/memories/{memory_id}": {
                "get": {
                    "tags": ["memories"],
                    "summary": "按 ID 取记忆",
                    "parameters": [{"name": "memory_id", "in": "path", "required": True, "schema": {"type": "string"}}],
                    "responses": {"200": {"description": "记忆详情"}},
                },
                "put": {
                    "tags": ["memories"],
                    "summary": "更新记忆（版本链 +1）",
                    "responses": {"200": {"description": "更新结果"}},
                },
                "delete": {
                    "tags": ["memories"],
                    "summary": "删除记忆（软删除）",
                    "responses": {"200": {"description": "删除结果"}},
                },
            },
            "/audit/receipt/{memory_id}": {
                "get": {
                    "tags": ["audit"],
                    "summary": "可证明记忆回执（哈希/版本链/审计链完整性）",
                    "responses": {"200": {"description": "回执"}},
                }
            },
            "/audit/integrity": {
                "get": {
                    "tags": ["audit"],
                    "summary": "全链 SHA-256 校验",
                    "responses": {"200": {"description": "完整性报告"}},
                }
            },
            "/graph/relations/at": {
                "get": {
                    "tags": ["graph"],
                    "summary": "图谱时点查询（edge bi-temporal）",
                    "responses": {"200": {"description": "时点关系"}},
                }
            },
            "/evolution/cycle/run": {
                "post": {
                    "tags": ["evolution"],
                    "summary": "触发一轮自进化周期",
                    "responses": {"200": {"description": "周期结果"}},
                }
            },
            "/structure/goals": {
                "get": {
                    "tags": ["structure"],
                    "summary": "活跃目标列表（automation goal.updated 事件源）",
                    "responses": {"200": {"description": "目标列表"}},
                }
            },
            "/automation/stats": {
                "get": {
                    "tags": ["automation"],
                    "summary": "自动化引擎统计（emitted/matched/executed/failed）",
                    "responses": {"200": {"description": "统计"}},
                }
            },
            "/openapi.json": {
                "get": {
                    "tags": ["health"],
                    "summary": "本 OpenAPI 规范",
                    "responses": {"200": {"description": "OpenAPI 3.0 JSON"}},
                }
            },
        },
        "components": {
            "securitySchemes": {
                "bearerAuth": {"type": "http", "scheme": "bearer",
                               "description": "TRINITY_API_KEY（可选鉴权）"},
            },
            "schemas": {
                "MemoryWrite": {
                    "type": "object",
                    "required": ["content"],
                    "properties": {
                        "content": {"type": "string", "description": "记忆内容"},
                        "category": {"type": "string", "default": "general"},
                        "importance": {"type": "number", "default": 0.5},
                        "tags": {"type": "array", "items": {"type": "string"}},
                        "persona_id": {"type": "string", "default": "default"},
                        "agent_id": {"type": "string", "default": "default"},
                        "session_id": {"type": "string"},
                        "metadata": {"type": "object"},
                    },
                }
            },
        },
        "security": [{"bearerAuth": []}],
    }
    return spec
