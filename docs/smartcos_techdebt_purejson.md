# SmartCos WMS 技术债补充：纯 json 字段分析（EXECUTION 450）

## 扫描结果
纯 json 字段（无 db 无 gorm）: 4373 个 / 分布于 service.go（DTO）与 model.go

## 工程判断（分寸）
1. **大部分是 API DTO**（json tag 用于响应序列化）——不需要 db tag
2. **需要 db tag 的**: 同一 struct 既被 gorm 写又被 sqlx 读的混合使用场景
   （如 SplitRule 案例——gorm Create + sqlx Select 共存）
3. **暴露方式**: 只能靠 sqlx scan 测试逐个暴露（运行时才能确认）
   ——已验证模式: stocktake/inventory/billing/oms_rule 各 1-2 个

## 策略
- 不做 4373 盲改（大量误伤风险）
- 测试驱动: 每覆盖一个 repo 测试 → 暴露 → 修复（已修 4 个真实 bug 的模式）
- 优先写测试的包: outbound service / picking repository / billing repository

## 已修复清单
- oms SplitRule（444）· oms_rule SplitRule 纯 json 5 字段（449）
