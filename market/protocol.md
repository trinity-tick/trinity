# Trinity 记忆市场协议 (C1)

> 把 `/market/*` 从"功能"升级为"协议"：任何第三方 agent/系统可按本规范接入，
> 上架/估价/交易/信誉 全流程可编程。

## 1. 角色

- **卖家 (Owner)**：持有记忆/知识包，调用 `POST /market/list` 上架
- **买家 (Buyer)**：调用 `POST /market/search` 检索，`POST /market/buy` 下单
- **见证者 (Endorser)**：调用 `POST /market/endorse` 对资产背书，累积信誉

## 2. 资产模型

```json
{
  "memory": {"content": "WMS 行业知识包 v1 ...", "tags": ["wms", "knowledge-pack"], "category": "knowledge"},
  "owner": "agent-wms",
  "price": 10.0,
  "license": "CC-BY",
  "currency": "trust_score"
}
```

## 3. 流程

```
上架:  POST /market/list        {memory, owner, price, license, currency}
估价:  POST /market/estimate    (查询估值模型)
搜索:  POST /market/search      按内容/标签检索在售资产
下单:  POST /market/buy         {buyer_agent, asset_id, offer_price, currency}
背书:  POST /market/endorse     {asset_id, endorser_agent}
报告:  GET  /market/report      交易与信誉总览
下架:  POST /market/delist      {asset_id}
```

## 4. 计价与信誉

- `currency=trust_score`：信誉积分制（买家给分、见证者背书累积）
- `GET /market/reputation/{agent_id}` 查询任何 agent 信誉
- `POST /market/estimate` 参考定价（可接入 A1 基准质量分）

## 5. 协议约束（v0）

- 资产内容写入时需脱敏（PII 检测自动执行）
- 交易全程记入审计链（`/audit/*`），防止伪造交易
- 上架上限：单 agent ≤ 100 个资产（防刷）

## 6. 三方接入示例

```bash
# 卖家
python market/demo.py list --owner agent-wms --content "WMS 库位优化知识包" --price 10
# 买家搜索+下单
python market/demo.py search --q "库位优化"
python market/demo.py buy --buyer agent-buyer --asset <asset_id> --offer 10
# 查询信誉
python market/demo.py reputation --agent agent-wms
```
