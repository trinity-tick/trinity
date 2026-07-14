# 八层防御体系说明

> auto_daemon v1.8.0 | 最后更新: 2026-07-11

---

## 设计原则

1. **安全内建 (Security by Design)**: auto_daemon 是架构原生层，非外挂
2. **纵深防御 (Defense in Depth)**: 八层分层防御，单层被突破不影响整体
3. **最小权限 (Least Privilege)**: 每层仅拥有完成职责所必需的最小权限
4. **失效安全 (Fail-Safe)**: 任何单层故障默认拒绝，不默认放行
5. **全链路可追溯 (Full Audit Trail)**: L5 记录所有请求的完整生命周期

---

## 防御体系总览

```
Layer 8  ████████████████  态势感知 (Situational Awareness)
Layer 7  ███████████████   自愈恢复 (Self-Healing)
Layer 6  ██████████████    熔断降级 (Circuit Breaker)
Layer 5  █████████████     审计日志 (Audit Logging)
Layer 4  ████████████      沙箱隔离 (Sandbox Isolation)
Layer 3  ███████████       行为分析 (Behavior Analysis)
Layer 2  ██████████        模式匹配 (Signature Match)
Layer 1  █████████         输入过滤 (Input Filter)
```

---

## L1: 输入过滤 (Input Filter)

### 职责

实时检测和拦截恶意输入。

### 检测维度

| 类别 | 检测方式 | 延迟 |
|------|----------|------|
| 脏话/仇恨言论 | 词典 + 正则 | < 1ms |
| Prompt Injection | 模式匹配 + 熵检测 | < 2ms |
| Jailbreak 尝试 | 语义分类器 | < 5ms |
| 超长输入 | 长度阈值 (默认 64K chars) | < 1ms |

### 行为

- 命中 → **直接拒绝 (HTTP 400)**，不入后续层
- 白名单路径 → 放行

---

## L2: 模式匹配 (Signature Match)

### 职责

基于签名库和正则规则引擎检测已知攻击模式。

### 签名库覆盖

| 类别 | 签名数 | 更新频率 |
|------|--------|----------|
| SQL 注入 | 1,200+ | 每周 |
| XSS | 800+ | 每周 |
| 路径遍历 | 300+ | 每月 |
| SSRF | 150+ | 每月 |
| 命令注入 | 500+ | 每周 |

### 行为

- 高置信度命中 → 拒绝
- 低置信度 → 标记并传递给 L3

---

## L3: 行为分析 (Behavior Analysis)

### 职责

基于 ML 模型的异常行为检测，识别未知攻击。

### 模型架构

```
Feature Extraction → Isolation Forest → Anomaly Score
                          │
                          ├─── Score < 0.3 → 放行
                          ├─── 0.3 ≤ Score < 0.7 → 标记
                          └─── Score ≥ 0.7 → 拒绝
```

### 特征维度

- 请求频率（时间窗口内）
- 输入熵值
- Token 分布异常度
- 操作序列模式
- API 调用图谱

---

## L4: 沙箱隔离 (Sandbox Isolation)

### 职责

将高风险操作隔离到独立容器中执行，保护宿主系统。

### 隔离级别

| 级别 | 资源限制 | 适用场景 |
|------|----------|----------|
| **L4-Safe** | CPU 20%, RAM 256MB | 脚本执行、文件解析 |
| **L4-Restricted** | CPU 50%, RAM 512MB, 无网络 | 代码执行、插件运行 |
| **L4-Isolated** | CPU 80%, RAM 1GB, 仅回环网络 | 第三方集成 |

### 容器生命周期

```
Create → Execute → Monitor → (Success → Destroy) | (Timeout/Failure → Destroy + Alert)
```

- 每次执行使用**新容器**，无状态残留
- 超时阈值: 30s (Safe), 120s (Restricted), 600s (Isolated)
- 退出后强制清理

---

## L5: 审计日志 (Audit Logging)

### 职责

全链路可追溯的审计日志，满足合规和事后分析需求。

### 日志格式 (JSONL)

```json
{
  "timestamp": "2026-07-11T08:00:00.000Z",
  "trace_id": "trace_abc123",
  "request_id": "req_xyz789",
  "client_ip": "192.168.1.1",
  "method": "trinity.retrieve",
  "layers_visited": ["L1", "L2", "L3"],
  "verdict": "ALLOWED",
  "latency_ms": 42,
  "anomaly_scores": {
    "L3_behavior": 0.15
  }
}
```

### 关键字段

| 字段 | 说明 |
|------|------|
| `trace_id` | 全链路追踪 ID，贯穿 8 层 |
| `layers_visited` | 请求经过的层列表 |
| `verdict` | ALLOWED / DENIED / FLAGGED |
| `anomaly_scores` | 各层异常分 |

### 保留策略

- 热存储: 30 天 (Elasticsearch)
- 冷存储: 1 年 (S3/对象存储)
- 归档: 7 年 (合规要求)

---

## L6: 熔断降级 (Circuit Breaker)

### 职责

过载保护和级联故障隔离，防止系统雪崩。

### 三种状态

```
        ┌──────────┐
   ────►│  CLOSED  │──── 正常请求
        └────┬─────┘
             │ 错误率 > 阈值
             ▼
        ┌──────────┐
        │   OPEN   │──── 直接拒绝
        └────┬─────┘
             │ 超时后
             ▼
        ┌──────────┐
        │ HALF_OPEN│──── 探测请求
        └────┬─────┘
             │ 成功 → CLOSED
             │ 失败 → OPEN
```

### 熔断阈值

| 指标 | 阈值 | 窗口 |
|------|------|------|
| 错误率 | > 50% | 60s |
| 慢请求率 | > 30% (>5s) | 120s |
| 并发上限 | > 1000 | 瞬时 |

### 级联熔断

当 chromadb 熔断时，second_brain 自动降级为纯稀疏检索（TF-IDF + BM25），不返回错误。

---

## L7: 自愈恢复 (Self-Healing)

### 职责

自动检测故障并执行恢复操作。

### 恢复策略

| 故障类型 | 检测方式 | 恢复动作 |
|----------|----------|----------|
| 索引损坏 | checksum 校验 | 从备份重建索引 |
| 内存泄漏 | RSS > 阈值 × 1.5 | Graceful restart |
| 磁盘满 | 使用率 > 90% | 触发 LRU 清理 + 告警 |
| 进程僵死 | heartbeat 超时 | Kill + restart |
| 配置漂移 | 配置哈希校验 | 回滚到上一个已知良好配置 |

### 恢复约束

- 单次恢复最长 30 秒
- 连续 3 次恢复失败 → 升级为人工告警
- 恢复操作写入 L5 审计日志

---

## L8: 态势感知 (Situational Awareness)

### 职责

全局威胁建模与实时风险评估，动态调整 L1-L7 的防御阈值。

### 态势维度

| 维度 | 数据源 | 更新频率 |
|------|--------|----------|
| 全局攻击趋势 | L2 签名命中率 | 实时 |
| 异常聚类 | L3 行为分析聚集 | 每 5 分钟 |
| 资源健康度 | CPU/RAM/Disk | 每 30 秒 |
| 外部威胁情报 | 社区 CVE feeds | 每小时 |

### 态势等级

| 等级 | L1 阈值 | L3 阈值 | L6 超时 | 适用场景 |
|------|---------|---------|---------|----------|
| 🟢 GREEN | 正常 | 0.7 | 5s | 日常运行 |
| 🟡 YELLOW | 收紧 20% | 0.5 | 3s | 轻微异常 |
| 🟠 ORANGE | 收紧 50% | 0.3 | 1s | 攻击迹象 |
| 🔴 RED | 最大防御 | 0.1 | 即时熔断 | 确认攻击 |

### 自动响应

- 🟡 → 增加 L5 采样率至 100%
- 🟠 → 启用 L3 增强模型 (GPU inference)
- 🔴 → L6 全熔断、仅白名单放行、L7 待命

---

## 安全通信

- **内部通信**: mTLS (双向 TLS 1.3)
- **密钥管理**: 自持密钥，不依赖外部 KMS
- **密钥轮换**: 自动轮换，每 24 小时

---

## 合规声明

| 标准 | 覆盖层 |
|------|--------|
| SOC 2 Type II | L5 审计日志 |
| GDPR Art. 32 | L4 沙箱 + L5 审计 |
| ISO 27001 | L1-L8 全覆盖 |
| OWASP Top 10 | L1-L3 |
