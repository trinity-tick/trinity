# Trinity 记忆市场协议（Memory Market, roadmap C1, 2026-08-15）

> 11 端点已实现（trinity/api/server.py，tags=["Memory Market"]）并经完整生命周期验证
> （挂单→搜索→订单簿→撤单 200）。本协议供第三方接入参考。

## 端点清单

| 端点 | 方法 | 说明 | 关键参数 |
|---|---|---|---|
| `/market/list` | POST | 挂单（封装记忆为资产进订单簿） | body: `{memory:{content,importance,...}, owner, price?, license?}` |
| `/market/delist` | POST | 撤单 | body: `{asset_id}` |
| `/market/search` | GET | 搜索活跃挂单 | `query`, `modality`, `max_price` |
| `/market/orderbook` | GET | 全部活跃挂单 | — |
| `/market/buy` | POST | 下单购买 | body: `{asset_id, buyer, offer?}` |
| `/market/transactions/{agent_id}` | GET | 某 agent 交易记录 | — |
| `/market/reputation/{agent_id}` | GET | 某 agent 信誉分 | — |
| `/market/endorse` | POST | 背书（提升信誉） | body: `{target, endorser?, amount?}` |
| `/market/report` | POST | 举报 | body: `{target, reporter?, reason?}` |
| `/market/price/{modality}` | GET | 该类记忆均价 | — |
| `/market/estimate` | POST | 记忆估值 | body: `{memory:{content,importance,...}, modality?}` |

## 数据结构

- **MemoryAsset**：`memory_id / owner_agent / modality / tags / license / content`
- **挂单（OrderBookEntry）**：`asset_id / price / currency(默认 trust_score) / listed_at`
- **信誉**：`ReputationScore`（背书/举报影响，见 `trinity/market/reputation.py`）
- **定价**：`get_market_price(modality)` 基于成交记录移动平均（`pricing.py`）
- **估值**：`estimate_value(memory)` → 0-1 分（importance/recency/稀缺性加权）

## 生命周期示例（已验证）

```bash
# 挂单
POST /market/list
{"memory":{"content":"可售记忆资产示例","importance":0.8},"owner":"agent-demo","price":1.5}
→ {"status":"listed","asset_id":"ast_xxx","price":1.5,"currency":"trust_score"}

# 搜索 / 订单簿
GET /market/search?query=可售   → {"count":1,"results":[...]}
GET /market/orderbook            → {"count":1,"orders":[...]}

# 撤单
POST /market/delist {"asset_id":"ast_xxx"} → {"status":"delisted"}
```

## 交易流

buy → 校验 buyer 信誉（reputation）与余额（trust_score）→ 资产转移
（owner 变更 + 交易记录 + 卖家信誉+）→ 落交易账（transactions）。

## 备注

- 当前为本地单进程订单簿（`_market_orderbook` 模块级单例），跨实例需外部存储（后续联邦/市场层）。
- 验证环境：trinity-api :8001，需 `X-Agent-ID` 头（RBAC）。
