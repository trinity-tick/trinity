# SmartCos WMS 技术债清单：缺 db tags 的 gorm structs（EXECUTION 445 记录）

> 扫描发现（445 轮）: **98 个 struct / 1034 字段**只有 gorm tag 缺 sqlx db tag
> 其中 **11 个高风险包**（repository 用 sqlx Get/Select 扫描这些 struct）
> 含核心: inventory VirtualStock/StockLockRecord（防超卖）、oms OMSRule/SplitRule 已修、
> stocktake、picking、billing、transfer、transaction 等

## 已修复
- oms SplitRule（444 轮——ListSplitRules 生产失败实证修复）

## 待批量修复建议（后续专项）
1. 自动迁移脚本（字段名→snake_case db tag——445 轮尝试的脚本方向，
   需解决 Python 转义/partial-write 问题后重写）
2. 修复后逐包 go build+test 回归（sqlmock 测试锁定）
3. 优先级: inventory（防超卖核心）/ stocktake / picking > billing > 其他

## 442-445 轮验证状态
- 全仓 build 0 · 全量 FAIL_COUNT 0（SplitRule 修复 + 现场恢复后）
