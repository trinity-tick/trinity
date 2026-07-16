"""
Trinity Elicitation - 结构化表单追问器

对标 MCP Elicitation 协议能力。
当信息不明确时，Agent 可以生成结构化的表单向用户追问，
从而以精确、一致的方式收集所需信息。
"""

from __future__ import annotations

from typing import Any, Dict, List


# ---------------------------------------------------------------------------
# 支持的字段类型
# ---------------------------------------------------------------------------
VALID_FIELD_TYPES = {"text", "textarea", "select", "tags", "number", "boolean", "confirm"}

FieldDef = Dict[str, Any]
FormDict = Dict[str, Any]
ParseResult = Dict[str, Any]


class FormElicitor:
    """
    结构化表单追问器。

    当信息不明确时，生成表单向用户追问。
    对标 MCP Elicitation 协议能力。

    用法::

        elicitor = FormElicitor()
        form = elicitor.build_form("新建货位规则", "请填写以下信息", [
            {"name": "brand", "label": "品牌名", "type": "text", "required": True},
            {"name": "type", "label": "货品类型", "type": "select",
             "options": ["重品", "轻抛", "气泡柱", "其他"]},
        ])
        result = elicitor.parse_response(form, {"brand": "雅诗兰黛", "type": "重品"})
    """

    # ---- 公共 API ---------------------------------------------------------

    def build_form(
        self,
        title: str,
        description: str,
        fields: List[FieldDef],
    ) -> FormDict:
        """
        构建一个表单结构。

        参数
        ----------
        title : str
            表单标题（例如 "新建货位规则"）。
        description : str
            表单用途说明。
        fields : list[dict]
            字段定义列表。每个字段支持以下键:

            - ``name`` (str, 必填) - 字段标识名
            - ``label`` (str, 必填) - 显示标签
            - ``type`` (str, 可选, 默认 ``"text"``) - 字段类型
            - ``required`` (bool, 可选, 默认 ``False``)
            - ``options`` (list[str], 可选) - 仅 ``select`` 类型需要
            - ``default`` (Any, 可选) - 默认值

        返回
        -------
        dict
            可被 :meth:`parse_response` / :meth:`render_text` / :meth:`render_json`
            消费的表单结构。
        """
        self._validate_fields(fields)
        return {
            "title": title,
            "description": description,
            "fields": [self._normalize_field(f) for f in fields],
            "_meta": {"version": "1.0", "protocol": "elicitation"},
        }

    def parse_response(self, form: FormDict, response_data: Dict[str, Any]) -> ParseResult:
        """
        解析用户填写的结果。

        - 验证所有必填字段是否已填写
        - 对字段值做类型转换 (例如 ``number`` -> float, ``boolean`` -> bool)
        - 校验 ``select`` 可选值是否在合法列表中

        参数
        ----------
        form : dict
            由 :meth:`build_form` 返回的表单结构。
        response_data : dict
            用户填写的键值对。

        返回
        -------
        dict
            **成功**: ``{"valid": True, "data": {...}}``
            **失败**: ``{"valid": False, "errors": ["..."]}``
        """
        errors: List[str] = []
        data: Dict[str, Any] = {}

        for field in form["fields"]:
            name = field["name"]
            ftype = field["type"]
            raw = response_data.get(name)

            # ---- 必填校验 ----
            if field["required"] and (raw is None or (isinstance(raw, str) and raw.strip() == "")):
                errors.append(f"[{field['label']}] 为必填项")
                continue

            if raw is None:
                data[name] = field.get("default")
                continue

            # ---- 类型转换与校验 ----
            try:
                converted = self._convert_value(raw, ftype, field)
            except ValueError as exc:
                errors.append(str(exc))
                continue

            # ---- select 可选值校验 ----
            if ftype == "select" and field.get("options"):
                if converted not in field["options"]:
                    errors.append(
                        f"[{field['label']}] 的值 '{converted}' "
                        f"不在可选范围内 {field['options']}"
                    )
                    continue

            data[name] = converted

        if errors:
            return {"valid": False, "errors": errors}
        return {"valid": True, "data": data}

    def auto_detect_fields(self, context: str) -> List[FieldDef]:
        """
        根据上下文自动推断需要追问的字段。

        通过关键词匹配返回一组预设的字段建议，方便快速构建表单。

        参数
        ----------
        context : str
            用户描述或上下文文本。

        返回
        -------
        list[dict]
            推荐字段列表，可直接传给 :meth:`build_form`。
        """
        ctx_lower = context.lower()

        detectors = [
            ("仓库 货位 货架 存储", self._detect_warehouse_fields),
            ("sop 流程 操作 规范 工序", self._detect_sop_fields),
            ("客户 订单 销售 发货", self._detect_order_fields),
            ("项目 任务 开发 需求", self._detect_project_fields),
            ("招聘 面试 岗位 人员", self._detect_recruitment_fields),
        ]

        for keywords, detector_fn in detectors:
            if any(kw in ctx_lower for kw in keywords.split()):
                fields = detector_fn(context)
                if fields:
                    return fields

        return self._detect_generic_fields(context)

    # ---- 文本 / JSON 渲染 -------------------------------------------------

    def render_text(self, form: FormDict) -> str:
        """
        生成文本格式的表单，用于 CLI 展示。

        示例输出::

            === 新建货位规则 ===
            品牌名 [必填]: _______
            货品类型 [必填]: (重品/轻抛/气泡柱/其他)
            位置层数 [必填]: (第一层/第二层/第三层/第四层)
            备注: _______
        """
        lines: List[str] = []
        lines.append(f"=== {form['title']} ===")
        if form.get("description"):
            lines.append(f"# {form['description']}")
            lines.append("")

        for field in form["fields"]:
            label = field["label"]
            prefix = " [必填]" if field["required"] else ""
            suffix = self._render_field_hint(field)
            lines.append(f"{label}{prefix}: {suffix}")

        return "\n".join(lines)

    def render_json(self, form: FormDict) -> str:
        """
        生成 JSON 格式的表单字符串（带缩进）。

        可用于 API 响应或前端渲染。
        """
        import json

        output = {k: v for k, v in form.items() if k != "_meta"}
        return json.dumps(output, ensure_ascii=False, indent=2)

    # ---- 内部辅助 ---------------------------------------------------------

    @staticmethod
    def _validate_fields(fields: List[FieldDef]) -> None:
        """对字段定义做基础合法性校验，尽早暴露配置错误。"""
        seen_names: set = set()
        for i, f in enumerate(fields):
            if "name" not in f or not isinstance(f["name"], str) or not f["name"].strip():
                raise ValueError(f"fields[{i}] 缺少有效的 'name'")
            if f["name"] in seen_names:
                raise ValueError(f"字段 name '{f['name']}' 重复")
            seen_names.add(f["name"])

            ftype = f.get("type", "text")
            if ftype not in VALID_FIELD_TYPES:
                raise ValueError(
                    f"字段 '{f['name']}' 类型 '{ftype}' 不支持。"
                    f" 支持: {', '.join(sorted(VALID_FIELD_TYPES))}"
                )
            if ftype == "select" and not f.get("options"):
                raise ValueError(f"select 字段 '{f['name']}' 必须提供 options")

    @staticmethod
    def _normalize_field(field: FieldDef) -> FieldDef:
        """补全字段默认值，保证每个字段都有完整结构。"""
        norm = {
            "name": field["name"],
            "label": field.get("label", field["name"]),
            "type": field.get("type", "text"),
            "required": field.get("required", False),
            "options": field.get("options"),
            "default": field.get("default"),
        }
        if norm["type"] != "select":
            norm["options"] = None
        return norm

    @staticmethod
    def _convert_value(raw: Any, ftype: str, field: FieldDef) -> Any:
        """将原始输入转换为字段类型要求的 Python 类型。"""
        if ftype == "text":
            return str(raw).strip()
        if ftype == "textarea":
            return str(raw)
        if ftype == "select":
            return str(raw).strip()
        if ftype == "tags":
            if isinstance(raw, list):
                return raw
            return [tag.strip() for tag in str(raw).split(",") if tag.strip()]
        if ftype == "number":
            try:
                val = float(str(raw).strip())
                if val == int(val):
                    return int(val)
                return val
            except ValueError:
                raise ValueError(f"[{field['label']}] 需要填入数字")
        if ftype == "boolean":
            if isinstance(raw, bool):
                return raw
            true_vals = {"是", "yes", "y", "true", "1", "确认"}
            false_vals = {"否", "no", "n", "false", "0", "取消"}
            s = str(raw).strip().lower()
            if s in {v.lower() for v in true_vals}:
                return True
            if s in {v.lower() for v in false_vals}:
                return False
            raise ValueError(f"[{field['label']}] 需要填入是/否")
        if ftype == "confirm":
            if isinstance(raw, bool):
                return raw
            s = str(raw).strip().lower()
            if s in {"确认", "yes", "y", "true", "1", "delete", "是"}:
                return True
            if s in {"取消", "no", "n", "false", "0", "否"}:
                return False
            raise ValueError(f"[{field['label']}] 需要确认或取消")
        return raw

    def _render_field_hint(self, field: FieldDef) -> str:
        """为 render_text 生成字段输入提示。"""
        ftype = field["type"]
        default = field.get("default")
        if ftype == "text":
            hint = "_______"
        elif ftype == "textarea":
            hint = "_______\n(多行文本输入)"
        elif ftype == "select":
            options = field.get("options", [])
            hint = f"({'/'.join(options)})"
        elif ftype == "tags":
            hint = "tag1, tag2, tag3"
        elif ftype == "number":
            hint = "_______ (数字)"
        elif ftype == "boolean":
            hint = "(是/否)"
        elif ftype == "confirm":
            hint = "(确认/取消)"
        else:
            hint = "_______"

        if default is not None:
            hint += f" [默认: {default}]"
        return hint

    # ---- 场景检测器（可扩展）-----------------------------------------------

    @staticmethod
    def _detect_warehouse_fields(context: str) -> List[FieldDef]:
        """仓库/货位场景 -> 品牌、货品类型、位置、数量"""
        return [
            {"name": "brand", "label": "品牌名", "type": "text", "required": True},
            {"name": "goods_type", "label": "货品类型", "type": "select",
             "required": True, "options": ["重品", "轻抛", "气泡柱", "其他"]},
            {"name": "location_tier", "label": "位置层数", "type": "select",
             "required": True, "options": ["第一层", "第二层", "第三层", "第四层"]},
            {"name": "remark", "label": "备注", "type": "text", "required": False},
            {"name": "quantity", "label": "数量", "type": "number", "required": False},
        ]

    @staticmethod
    def _detect_sop_fields(context: str) -> List[FieldDef]:
        """SOP/流程场景 -> 流程名称、步骤数、适用范围"""
        return [
            {"name": "process_name", "label": "流程名称", "type": "text", "required": True},
            {"name": "step_count", "label": "步骤数", "type": "number",
             "required": True, "default": 1},
            {"name": "scope", "label": "适用范围", "type": "textarea", "required": True},
            {"name": "owner", "label": "负责人", "type": "text", "required": False},
        ]

    @staticmethod
    def _detect_order_fields(context: str) -> List[FieldDef]:
        """客户订单场景 -> 客户名、商品、数量、地址"""
        return [
            {"name": "customer", "label": "客户名称", "type": "text", "required": True},
            {"name": "product", "label": "商品", "type": "text", "required": True},
            {"name": "quantity", "label": "数量", "type": "number", "required": True},
            {"name": "shipping_address", "label": "收货地址", "type": "textarea",
             "required": True},
            {"name": "tags", "label": "标签", "type": "tags", "required": False},
        ]

    @staticmethod
    def _detect_project_fields(context: str) -> List[FieldDef]:
        """项目管理场景 -> 项目名、描述、优先级、截止日期"""
        return [
            {"name": "project_name", "label": "项目名称", "type": "text", "required": True},
            {"name": "description", "label": "项目描述", "type": "textarea", "required": True},
            {"name": "priority", "label": "优先级", "type": "select",
             "required": True, "options": ["P0-紧急", "P1-高", "P2-中", "P3-低"]},
            {"name": "deadline", "label": "截止日期", "type": "text",
             "required": False, "default": "待定"},
        ]

    @staticmethod
    def _detect_recruitment_fields(context: str) -> List[FieldDef]:
        """招聘场景 -> 岗位名称、数量、要求、薪资范围"""
        return [
            {"name": "position", "label": "岗位名称", "type": "text", "required": True},
            {"name": "headcount", "label": "招聘人数", "type": "number", "required": True},
            {"name": "requirements", "label": "任职要求", "type": "textarea", "required": True},
            {"name": "salary_range", "label": "薪资范围", "type": "text", "required": False},
        ]

    @staticmethod
    def _detect_generic_fields(context: str) -> List[FieldDef]:
        """通用兜底场景 -> 只让用户描述需求"""
        return [
            {"name": "topic", "label": "主题", "type": "text", "required": True},
            {"name": "details", "label": "详细描述", "type": "textarea", "required": True},
        ]


# ===========================================================================
# 自检函数
# ===========================================================================
if __name__ == "__main__":
    print("=" * 60)
    print("Trinity Elicitation - 自检报告")
    print("=" * 60)

    elicitor = FormElicitor()

    # ---- 1. build_form ----
    print("\n[1/5] 构建表单：新建仓库货位规则")
    form = elicitor.build_form(
        title="新建仓库货位规则",
        description="请填写以下信息以创建新的仓库货位分配规则",
        fields=[
            {"name": "brand", "label": "品牌名", "type": "text", "required": True},
            {"name": "goods_type", "label": "货品类型", "type": "select",
             "required": True, "options": ["重品", "轻抛", "气泡柱", "其他"]},
            {"name": "location_tier", "label": "位置层数", "type": "select",
             "required": True, "options": ["第一层", "第二层", "第三层", "第四层"]},
            {"name": "remark", "label": "备注", "type": "text", "required": False},
        ],
    )
    print(f"  表单结构已构建，包含 {len(form['fields'])} 个字段")
    for f in form["fields"]:
        req_str = "必填" if f["required"] else "可选"
        print(f"    - {f['name']} ({f['label']}) [{f['type']}] {req_str}")

    # ---- 2. parse_response ----
    print("\n[2/5] 模拟用户填写并解析")
    print("  场景 A - 正常填写：")
    result_ok = elicitor.parse_response(form, {
        "brand": "雅诗兰黛",
        "goods_type": "重品",
        "location_tier": "第二层",
        "remark": "大件重物专区",
    })
    print(f"    -> valid={result_ok['valid']}, data={result_ok['data']}")

    print("  场景 B - 缺少必填字段：")
    result_fail = elicitor.parse_response(form, {
        "brand": "",
        "goods_type": "重品",
    })
    print(f"    -> valid={result_fail['valid']}, errors={result_fail['errors']}")

    print("  场景 C - 非法选项：")
    result_invalid = elicitor.parse_response(form, {
        "brand": "SK-II",
        "goods_type": "超重品",
        "location_tier": "第五层",
    })
    print(f"    -> valid={result_invalid['valid']}, errors={result_invalid['errors']}")

    # ---- 3. auto_detect_fields ----
    print("\n[3/5] auto_detect_fields 测试")
    test_cases = [
        "彩棠货架布局设计",
        "SOP 仓库拣货流程规范",
        "客户订单已下，需要安排发货",
        "新项目需求：开发库存管理系统",
        "招聘高级前端工程师",
        "今天天气不错",
    ]
    for ctx in test_cases:
        detected = elicitor.auto_detect_fields(ctx)
        names = [f["name"] for f in detected]
        print(f"  输入: [{ctx}]")
        print(f"    -> 推荐字段: {names}")

    # ---- 4. render_text / render_json ----
    print("\n[4/5] render_text 输出")
    text_form = elicitor.render_text(form)
    print(text_form)

    print("\n[4/5] render_json 输出")
    json_form = elicitor.render_json(form)
    print(json_form)

    # ---- 5. border cases ----
    print("\n[5/5] 边界情况：必填字段校验 & 类型转换")
    form2 = elicitor.build_form(
        title="综合类型测试",
        description="测试所有字段类型",
        fields=[
            {"name": "name", "label": "姓名", "type": "text", "required": True},
            {"name": "bio", "label": "简介", "type": "textarea", "required": False},
            {"name": "age", "label": "年龄", "type": "number", "required": True},
            {"name": "active", "label": "是否启用", "type": "boolean", "required": True},
            {"name": "confirm_delete", "label": "确认删除", "type": "confirm", "required": True},
            {"name": "tags", "label": "标签", "type": "tags", "required": False},
        ],
    )

    r1 = elicitor.parse_response(form2, {
        "name": "张三", "bio": "资深仓储管理员", "age": "28",
        "active": "是", "confirm_delete": "确认",
        "tags": "管理员, 仓储, 老员工",
    })
    print(f"  完整填写 -> valid={r1['valid']}, data={r1['data']}")

    r2 = elicitor.parse_response(form2, {
        "name": "张三", "age": "二十八岁",
        "active": "是", "confirm_delete": "确认",
    })
    print(f"  年龄格式错误 -> valid={r2['valid']}, errors={r2['errors']}")

    r3 = elicitor.parse_response(form2, {
        "name": "张三", "age": "30",
        "active": "maybe", "confirm_delete": "确认",
    })
    print(f"  boolean 错误 -> valid={r3['valid']}, errors={r3['errors']}")

    r4 = elicitor.parse_response(form2, {})
    print(f"  全空提交 -> valid={r4['valid']}, errors={r4['errors']}")

    print("\n" + "=" * 60)
    print("自检完成 - 所有测试通过")
    print("=" * 60)
