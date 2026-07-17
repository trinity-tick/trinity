# 知识图谱查询使用示例

```python
import sys; sys.path.insert(0, r"c:\Users\Administrator\trinity")
from trinity.kgraph import KnowledgeGraph

# 初始化
kg = KnowledgeGraph(storage_path=r"c:\Users\Administrator\trinity\data\kgraph\warehouse.jsonl")

# 添加实体
kg.add_entity("caitang", "brand", {"name": "彩棠", "desc": "珀莱雅子品牌"})
kg.add_entity("proya", "brand", {"name": "珀莱雅"})
kg.add_entity("heavy_rule", "rule", {"name": "重品层规则", "desc": "重品0.1kg-0.3kg放第一层"})
kg.add_entity("position_1", "location", {"name": "第一层货架"})
kg.add_entity("x_priority", "strategy", {"name": "X轴优先扩展", "desc": "货架布局时优先扩展X轴"})

# 添加关系
kg.add_relation("caitang", "belongs_to", "proya")
kg.add_relation("heavy_rule", "applies_to", "caitang")
kg.add_relation("heavy_rule", "located_at", "position_1")
kg.add_relation("caitang", "uses", "x_priority")

# 查询“彩棠”的关联网络
results = kg.query_relations("caitang", max_depth=2)
for r in results:
    print(f"{r['relation']}: {r['subject']} -> {r['object']}")

# 保存
kg.save()
```
