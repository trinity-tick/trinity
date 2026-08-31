# Trinity 大脑模块依赖图（EXECUTION 383 固化）

> 来源：自动审计（scripts/audit_dependencies.py 可重新生成）
> 模块总数：189 · 依赖环：0

## 基础服务（被 >=2 模块引用——接口冻结）
| 模块 | 被引用数 | 冻结说明 |
|---|---|---|
| affect | 7 | 情绪评估 API（assess）——6 模块依赖，签名冻结 |
| gist_extraction | 5 | 保持稳定——签名勿随意更改 |
| dopamine_reward | 3 | 奖赏水平 API——3 模块依赖，签名冻结 |
| associative_memory | 3 | 联想检索——2 模块依赖，签名冻结 |
| value_encoder | 3 | 价值编码 API——3 模块依赖，签名冻结 |
| source_credibility | 2 | 来源可信度——2 模块依赖，签名冻结 |
| surprise_encoding | 2 | 保持稳定——签名勿随意更改 |

## 依赖方向（基础层 → 高层）
```
基础服务（affect/dopamine/value_encoder/source_credibility）
  ↑ 被各机制模块引用
机制模块（178+ 个——每轮新增）
  ↑ 被注册表（brain_capabilities）引用
注册表（_advanced.py——203 项）
```

## 依赖规则（冻结约定）
1. **基础服务签名冻结**：affect/dopamine_reward/value_encoder 等 API 不可随意更改（依赖多）
2. **新机制只依赖基础服务**：新模块只 import 基础服务，不反向依赖高层
3. **注册表自动同步**：新模块加入 scripts/register_mechanisms.py 自动注册