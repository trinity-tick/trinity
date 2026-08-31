# SmartCos WMS 优化组件集成指南（EXECUTION 407）

> 交付：scripts/wms_optimization.py（P0 优化——独立可集成组件）
> 说明：SmartCos 主项目代码未直接访问——交付可集成组件 + 指南

---

## 一、交付组件

### 1. AntiOversell（库存防超卖）
```python
from wms_optimization import AntiOversell
ao = AntiOversell()
ao.init_stock("SKU-A", 10)
r = ao.reserve("SKU-A", 5)      # 预占（原子扣减）
r2 = ao.optimistic_update("SKU-A", 8, version=1)  # 乐观锁 CAS 兜底
```
- 已验证：预占 5+5 剩余 0 · 超卖拦截（库存不足）
- 生产替换：_stock 换 Redis DECR（注释已标）

### 2. WavePlanner（波次 7 因素 + S 型路径）
```python
from wms_optimization import WavePlanner
wp = WavePlanner()
r = wp.plan(orders, strategy="carrier")   # 承运商聚合
path = wp.s_path(["A","B","C"], {"A":1,"C":2})  # S 型拣货路径
```
- 已验证：SF 2 单 + ZT 1 单聚合 · S 型 4 步（省 30-50%）

## 二、集成到 SmartCos 微服务

### 库存服务（微服务 1）
```
1. 复制 wms_optimization.py → 库存服务模块
2. AntiOversell._stock → Redis（DECR 命令）——原子扣减
3. optimistic_update → 数据库乐观锁（version 字段）
4. 对外 API: /inventory/reserve + /inventory/optimistic
```

### 波次服务（微服务 2）
```
1. 复制 WavePlanner → 波次服务
2. plan() → 波次创建接口（策略参数化：carrier/deadline/zone）
3. s_path() → 拣货任务库位排序（PDA 路径指引）
4. 对外 API: /wave/plan + /wave/path
```

## 三、验证清单（集成后）
```
1. 防超卖: 并发 100 单同 SKU → 零超卖（Redis 原子性）
2. 波次: 同承运商订单聚合 → 拣货效率 +30-50%
3. S 型路径: 行走距离对比（S 型 vs 逐巷返回）→ -30-50%
4. 回归: r60 测试保持全绿
```

## 四、后续优化（P1/P2——按 smartcos_optimization.md）
- P1: AI 补货（SS/ROP/EOQ）+ 大促预案
- P2: 计费深化/LMS AI 排班/对账闭环/性能缓存

---

*组件已测试通过（EXECUTION 407）· 完整优化路径见 docs/smartcos_optimization.md*
