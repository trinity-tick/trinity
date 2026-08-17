# Trinity 采集插件生态 (C2) — Harvester 插件规范 v0

> 任何"外部源 → 结构化记忆"的采集器，按本规范实现即可注册进 Trinity 生态。

## 1. 插件接口

一个插件是一个 Python 模块，导出：

```python
PLUGIN = {
    "id": "bilibili-harvester",          # 全局唯一
    "name": "B站视频采集器",
    "version": "1.0.0",
    "source": "bilibili",                # 数据源标识
    "capabilities": ["video", "text"],
}

def harvest(config: dict) -> list[dict]:
    """返回记忆条目列表，每条:
    {
        "content": "文本内容",
        "category": "video_harvested",
        "tags": ["bilibili", "bvid:xxx"],
        "importance": 0.7,
        "metadata": {"url": "...", "author": "..."},
    }
    """
    raise NotImplementedError
```

## 2. 注册与发现

- 插件放入 `harvesters/plugins/<id>.py`，`harvesters/registry.json` 登记
- 运行时发现：扫描 `harvesters/plugins/`，校验 `PLUGIN` 结构（复用 `auto_discovery` 模式）
- 采集产物统一走 `POST /memories`（或 `bulk_write`）写入，带上 `category=*_harvested`

## 3. 示例插件（example_plugin.py）

```bash
python harvesters/example_plugin.py --config '{"url":"https://example.com"}'  # 采集并写入
python harvesters/example_plugin.py --dry-run                                 # 只打印产物
```

## 4. 质量要求

- 每个插件必须声明 `capabilities` 与 `source`
- 产物必须可溯源（metadata.url/author）
- 自动脱敏：写入时 PII 检测默认开启
- 幂等：同一 URL 重复采集需去重（content hash 或 url 索引）

## 5. 插件市场（远期）

- `harvesters/market.json`：插件清单 + 评分（配合 C1 market 信誉）
- 一键安装：`trinity harvest install <plugin-id>`
