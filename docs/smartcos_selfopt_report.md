# Trinity × SmartCos 自优化扫描报告（EXECUTION 418）

> 扫描对象: D:\smartcos-wms\backend（重构后）· 工具: Trinity 静态分析 + 知识对照

## 一、扫描发现
| 维度 | 发现 |
|---|---|
| 包结构 | 110 internal 包 · 50 有测试 · **59 零测试**（含 billing/wave/stocktake/oms 核心） |
| 复杂度热点 | gateway/handler.go **8624 行**（+mock 3517/routes 1413） |
| 技术债 | 54 TODO · **843 处 `_ =` 忽略错误** · 7 panic（非测试） |
| **接线缺口** | **metrics 0 引用 · twinsim 0 引用** · promotionplan/reconciliation 仅测试引用 |

## 二、自优化执行（已落地）
1. **twinsim → promotionplan**: SimulatedCapacityCheck（大促容量评估从静态公式升级为数字孪生仿真——瓶颈识别+P95）✅ 测试 PASS
2. **RLTuner → ai-slotting**: TuneAndSuggest 拣选事件回流（每次拣选完成自动改进库位策略——无需重部署规则）✅ 测试 PASS
3. 提交: SmartCos master（Trinity 扫描发现→代码落地闭环）

## 三、后续优化清单（按价值排序）
1. gateway/handler.go 拆分（8624 行→按域拆分——需专项）
2. 59 个零测试包补测试（优先 billing/wave/stocktake/oms）
3. metrics 接线 gateway（/metrics 端点——需 handler 拆分后）
4. 843 忽略错误清理（分批+lint 规则）
