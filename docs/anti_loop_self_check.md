---
AIGC:
    Label: "1"
    ContentProducer: 001191440300708461136T1XGW3
    ProduceID: 1ed0c9b5065b1819decccf0a8be25f33_045f8091985411f19bec525400826444
    ReservedCode1: Q8XnrdR0j9ywwI7t6jX/hk0/JAP+0cwSdC6D8A1Y/eLXfIsRwOGUXKLllBKzfZ4TV4/K866NlzA5JmpF6XCG/QeKSifcER/yYBmsl5rsIyVAgjkVDL0rCtuZix6qAO97KWXnJkgpfuIk9AnqW+hm9fMh5dnJ7s6TlFGN+vCtZMr/n22Sb4jmSlnw4Jo=
    ContentPropagator: 001191440300708461136T1XGW3
    PropagateID: 1ed0c9b5065b1819decccf0a8be25f33_045f8091985411f19bec525400826444
    ReservedCode2: Q8XnrdR0j9ywwI7t6jX/hk0/JAP+0cwSdC6D8A1Y/eLXfIsRwOGUXKLllBKzfZ4TV4/K866NlzA5JmpF6XCG/QeKSifcER/yYBmsl5rsIyVAgjkVDL0rCtuZix6qAO97KWXnJkgpfuIk9AnqW+hm9fMh5dnJ7s6TlFGN+vCtZMr/n22Sb4jmSlnw4Jo=
---

# Trinity 防循环自检与修复机制（Anti-Loop Self-Check）

> 建立日期：2026-08-15
> 目的：从机制上根治"子 Agent / 主 Agent 死循环"问题，防止重复派发、重复读取、盲目重试、杀守护进程假循环等恶性循环再次发生。

## 一、死循环根因清单（历史复盘）

| # | 循环形态 | 根因 | 后果 |
|---|---------|------|------|
| 1 | 派发循环 | Trinity 本地检查/优化类任务反复 `dispatch_task` 派发 file-agent，子任务被系统中断后反复重派 | 派发→中断→再派发，无限空转 |
| 2 | 读取循环 | 主 Agent 对同一文件同一范围重复读取（曾对 stream_ingest.py 521-641 行重复约 18 次） | 冗余空转，产生大量重复 memory_id |
| 3 | 重试循环 | 脚本报错后不定位根因，盲目重试（select 在 Windows 管道不可用、字段名错误等反复报错） | 反复失败重试，浪费资源 |
| 4 | 杀守护假循环 | 手动终止守护进程管理的子进程（engine_worker / hermes_cli），守护立即重启 | 杀→重启→再杀，形成假循环 |
| 5 | 双库混淆 | 无统一存储路径配置，data/trinity_store.db 与 ~/.trinity/store/trinity_store.db 混淆 | 误删/清理错库 |

## 二、防循环行为规则（强制）

### R1 派发去重
- Trinity 本地检查/优化/加固类任务：**直接执行，不派发 file-agent**（file-agent 子任务在本环境反复被中断）。
- 派发子 Agent 前自检：任务是否已执行过？是否必要？同一目标不重复派发。

### R2 读取去重
- 同一文件同一行范围：**只读取一次**，结果复用；确需再读必须基于新线索（新行号/新问题）。

### R3 重试上限
- 同类失败最多重试 **2 次**；超出后必须降级（换工具/换路径/交还用户决策），严禁仅微调参数绕过。

### R4 守护识别（防假循环）
- 已知守护进程：`Hermes.exe`（管理 hermes_cli serve）、`node.exe`（dsh，管理 engine_worker / trinity-mcp）、`MarvisNode.exe`。
- **禁止手动终止守护进程管理的子进程**，否则守护会立即重启，形成假循环。
- 确需停止某服务时，先停守护进程本身（或走服务正常关闭流程），再处理子进程。

### R5 存储路径统一
- 涉及 Trinity 数据操作前，先确认目标库路径（`data/trinity_store.db` vs `~/.trinity/store/trinity_store.db`），删除/清理前必须核对 audit_log 并确认。

### R6 自检入口
- 每次巡检/优化任务开始前，运行防循环自检：
  ```powershell
  cd C:\Users\Administrator\trinity
  .\.venv\Scripts\python.exe scripts\anti_loop_self_check.py
  ```
- 输出 `PASS` → 继续任务；`WARN` → 先处理告警项；`FAIL` → 停止并定位根因。

## 三、自检脚本说明

`scripts/anti_loop_self_check.py` 检测三类问题：

- **[A] 重复进程检测**：聚焦 Trinity/hermes/Marvis 业务脚本，检测同一脚本被启动多份（豁免系统多实例进程与"主+worker"父子结构）。
- **[B] 守护关系识别**：列出守护进程及其管理的子进程，标记"勿手动终止"，防止杀守护假循环。
- **[C] 循环迹象检测**：近 5 分钟启动进程数异常、近 10 分钟同一脚本启动 ≥3 次（疑似重启循环）。

退出码：0 = PASS，1 = WARN，2 = FAIL。支持 `--json` 输出便于程序解析。

## 四、修复流程（遇到循环时）

1. 运行 `anti_loop_self_check.py` 定位问题类型（A/B/C）。
2. 按 R1-R5 对应规则处理：
   - 派发循环 → 停止派发，直接执行（R1）
   - 读取循环 → 停止重复读取，复用已有结果（R2）
   - 重试循环 → 停止重试，定位根因（R3）
   - 杀守护假循环 → 停止杀子进程，识别守护（R4）
   - 双库混淆 → 核对存储路径（R5）
3. 修复后重新运行自检确认 PASS。
*（内容由AI生成，仅供参考）*
