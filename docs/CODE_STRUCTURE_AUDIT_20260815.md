# Trinity 代码/结构梳理报告（2026-08-15）

> 依据：全量模块审计（scripts/audit_modules.py）+ 根目录盘点 + 加载链核查。
> 报告 JSON：~/.trinity/logs/module_audit.json

## 一、审计结论：303 模块的真实运行路径

| 分类 | 数量 | 说明 |
|---|---|---|
| **ACTIVE** | 38 | 被 engine 聚合链（engine.py→engine_core/engine_*）或全库代码 import，真实运行路径 |
| **EXPERIMENTAL** | 1 | loader.py：懒加载基建（与 registry 成对），设计就绪但未接入运行路径 |
| **ORPHAN** | 264 | 全库零引用（无 import/字符串/registry 引用），论文对齐的算法储备 |

**关键发现**：
1. **90% 模块（264/303）不在运行路径**——是论文对齐的"算法储备"（P21 系列、CB 系列、
   adversarial 防御等），已加 `# status: orphan` 标注保留（不删除）。
2. **registry.py 的懒加载从未接入**：`ModuleRegistry.register()` 只在 loader.py 的
   `_register_all()` 被调用（12 模块），而 loader 本身零外部引用——"解决 9693 行单文件"
   的设计意图未落地。
3. **好消息**：单文件问题已被 P0 refactor 解决——engine.py 现在是 131 行 facade，
   re-export 56 个类（全部验证有效），大模块分布在 engine_core(924)/engine_data_pipeline(2572)/
   engine_observability(1789)/engine_retrieval(1158) 等 12 个拆分文件。
4. **engine.py facade 完整性**：56 个 __all__ 导出全部可解析，loader 引用的 12 个类
   （M101-104, CB45-52）全部存在——未来启用懒加载无断链风险。

## 二、根目录清理

| 动作 | 对象 |
|---|---|
| 删除（调试残留） | proc_test_out.txt、sig.txt、simple_test.txt、temp_test_proc.py、test_import.py、test_mcp_err.txt、test_proc3.py、test_proc3_out.txt、test_stderr.txt、test123.txt（4B "test"） |
| 归档（历史工具） | trinity_init.py、trinity_work.py → scripts/legacy/（零实时引用） |
| 归档（旧文档源） | docs_site/（12 个 md，早期 mkdocs 源副本，已被 docs/ + site/ 取代）→ scripts/legacy/docs_site/ |
| 保留 | health_check.py（被维护脚本引用）、*.bat 运维脚本、start_api.bat |
| 已确认忽略 | temp/ output/ site/ logs/ egg-info 均在 .gitignore（不会误提交） |

## 三、决策记录

| 项 | 决策 | 理由 |
|---|---|---|
| 264 孤儿模块 | 保留 + status: orphan 标注，不删除 | 论文对齐的算法储备，有历史/未来价值；删除风险大于收益 |
| registry/loader | status: experimental 保留 | 懒加载是可选未来优化；引用的类全部有效，启用无断链 |
| engine facade | 不动（已健康） | 131 行 facade + 56 类全通，P0 refactor 已完成 |
| 模块生命周期 | 文件头 status 标注（orphan/experimental） | 让"宣称规模"与"运行路径"可区分、可审计 |

## 四、后续可选（非本轮）

1. **启用懒加载**：把 SecondBrainLoader 接入 engine_core 构造路径（收益：启动提速；
   风险：需全量回归）。
2. **孤儿模块归档**：对 264 个 orphan 建 `trinity/modules/second_brain/archive/` 分类
   （实验/论文/未完成），或按状态建索引。
3. **CI 集成**：audit_modules.py 加进 maintenance selftest，检测"新增模块未接入"。
4. **覆盖审计**：对 38 个 active 模块核对测试覆盖（当前 626 测试主要覆盖引擎/存储/检索）。
