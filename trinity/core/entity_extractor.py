"""
实体识别模块 — 基于正则 + 启发式规则的中英文实体提取器。

每次 ingest 记忆时自动提取，返回实体列表用于构建记忆图谱。
"""
import re
import json
import uuid
from typing import Dict, List, Optional, Set
from datetime import datetime


class EntityExtractor:
    """轻量级实体识别器，基于正则 + 启发式。"""

    # ── 文件名模式 ──
    FILE_PATTERN = re.compile(
        r'\b([a-zA-Z0-9_\-/\\]+\.(?:py|md|json|yaml|yml|toml|ini|cfg|txt|'
        r'csv|tsv|pdf|docx?|pptx?|xlsx?|html?|css|jsx?|tsx?|'
        r'go|rs|java|kt|swift|cpp?|hpp?|sql|sh|ps1|bat|'
        r'dockerfile|makefile|env|gitignore|docker-compose\.yml))\b',
        re.IGNORECASE | re.ASCII,
    )

    # ── 路径模式 ──
    PATH_PATTERN = re.compile(
        r'(?:[A-Za-z]:\\[\w\-\\/\.]+|/(?:[\w\-\.]+/)+[\w\-\.]+)',
    )

    # ── 项目/模块名大写词 ──
    PROJECT_PATTERN = re.compile(
        r'\b([A-Z][a-z]+(?:[A-Z][a-z]+)+)\b', re.ASCII,  # CamelCase
    )

    # ── Agent 模式 ──
    AGENT_PATTERN = re.compile(r'@([a-zA-Z0-9_][a-zA-Z0-9_\-]*)')

    # ── Tag 模式 ──
    TAG_PATTERN = re.compile(r'#([a-zA-Z0-9_][a-zA-Z0-9_\-]*)')

    # ── 英文专有名词 (连续大写) ──
    ACRONYM_PATTERN = re.compile(r'\b([A-Z]{2,}(?:\d+)?)\b')

    # ── 中文实体模式 ──
    CN_PERSON_PATTERN = re.compile(r'([李王张刘陈杨赵黄周吴徐孙马胡朱郭何罗高林]'
                                    r'[^\s，。；、！？\da-zA-Z]{1,2})')
    CN_ORG_PATTERN = re.compile(r'([^\s，。；、！？]{2,8}(?:公司|集团|大学|医院|'
                                 r'研究所|实验室|部门|团队|中心|平台|系统))')

    # ── 版本号 ──
    VERSION_PATTERN = re.compile(r'\bv?(\d+\.\d+(?:\.\d+)?(?:[a-z]+\d*)?)\b')

    def _guess_entity_type(self, name: str, context: str) -> str:
        """推断实体类型。"""
        lower = name.lower()

        # 文件类型
        for ext in ('.py', '.md', '.json', '.yaml', '.yml', '.toml', '.ini',
                     '.cfg', '.txt', '.csv', '.tsv', '.pdf', '.doc', '.docx',
                     '.ppt', '.pptx', '.xls', '.xlsx', '.html', '.css', '.js',
                     '.jsx', '.ts', '.tsx', '.go', '.rs', '.java', '.kt',
                     '.swift', '.c', '.cpp', '.h', '.hpp', '.sql', '.sh',
                     '.ps1', '.bat'):
            if lower.endswith(ext):
                return 'file'

        # 路径
        if '\\' in name or name.startswith('/'):
            return 'file'

        # Agent
        if name.startswith('@'):
            return 'agent'

        # Tag
        if name.startswith('#'):
            return 'tag'

        # 组织
        for kw in ('公司', '集团', '大学', '医院', '研究所', '实验室', '部门', '团队', '中心', '平台', '系统'):
            if kw in name:
                return 'project'

        # 人名
        if re.match(r'^[李王张刘陈杨赵黄周吴徐孙马胡朱郭何罗高林]', name):
            return 'person'

        # 默认概念
        return 'concept'

    def extract(self, content: str) -> List[Dict[str, str]]:
        """从文本内容中提取实体列表。

        Args:
            content: 记忆文本内容。

        Returns:
            实体列表，每项含 name / type / source。
        """
        entities: List[Dict[str, str]] = []
        seen: Set[str] = set()

        def add(name: str, etype: str):
            key = name.lower().strip()
            if key and key not in seen and len(name) >= 2:
                seen.add(key)
                entities.append({
                    "name": name.strip(),
                    "type": etype,
                })

        # ── 文件 ──
        for m in self.FILE_PATTERN.finditer(content):
            add(m.group(1), 'file')

        # ── Agent ──
        for m in self.AGENT_PATTERN.finditer(content):
            add('@' + m.group(1), 'agent')

        # ── Tag ──
        for m in self.TAG_PATTERN.finditer(content):
            add('#' + m.group(1), 'tag')

        # ── CamelCase 项目 ──
        for m in self.PROJECT_PATTERN.finditer(content):
            name = m.group(1)
            # 避免匹配普通英文单词
            if len(name) >= 8 or name[0].isupper() and name[1].isupper():
                add(name, 'project')

        # ── 中文组织 ──
        for m in self.CN_ORG_PATTERN.finditer(content):
            add(m.group(1), 'project')

        # ── 中文人名 ──
        for m in self.CN_PERSON_PATTERN.finditer(content):
            add(m.group(1), 'person')

        # ── 英文专有名词 ──
        for m in self.ACRONYM_PATTERN.finditer(content):
            name = m.group(1)
            # 过滤常见缩写
            if name.lower() not in ('OK', 'HI', 'NO', 'YES', 'MR', 'MS', 'DR',
                                     'AM', 'PM', 'RS', 'TV', 'PC', 'CD', 'DVD',
                                     'USA', 'UK', 'EU', 'UN', 'AI', 'ML', 'DL',
                                     'NLP', 'CV', 'RL', 'API', 'SDK', 'UI', 'UX'):
                add(name, 'concept')

        # ── 版本号 ──
        for m in self.VERSION_PATTERN.finditer(content):
            add('v' + m.group(1), 'concept')

        # ── 路径 ──
        for m in self.PATH_PATTERN.finditer(content):
            name = m.group(0)
            if '\\' in name or '/' in name:
                add(name, 'file')

        return entities
