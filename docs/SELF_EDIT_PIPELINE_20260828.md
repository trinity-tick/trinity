# 代码自改管线（阶段1，2026-08-28）

## 闭环流程

```
目标缺口 → evolve_patch.py（LLM 生成文本替换补丁）
→ 唯一匹配校验 + py_compile 冒烟 → temp/patches/ 留档
→ 人工审查（默认闸门）→ --apply 合入 → git commit
→ fulltest 门禁（pytest 全量 + eval 12）
```

## 首次实战（2026-08-28）

- 目标：tune_judge.py argparse 中文 help；
- LLM 生成补丁 → 人工审查通过 → 合入 → 编译 OK + --help 生效；
- **意义：Trinity 第一次"自己生成代码改动（经人工确认）"真实落地**。

## 安全闸门

1. 白名单：仅 scripts/ 下 .py（≤20KB）；
2. 唯一匹配校验（count==1）——防止 LLM 截断/重复替换；
3. py_compile 冒烟——语法错误自动拒绝；
4. 默认不 apply（人工确认）；
5. 补丁留档（temp/patches/evolve_*.txt）；
6. fulltest 门禁：全量测试通过才认为合入安全。

## 已知边界

- LLM diff 模式（hunk 行号不可靠）→ 已弃用，文本替换模式稳定；
- old 块须 1-3 行（长块易截断）；
- 核心代码（trinity/ 下）不在白名单（阶段 3 才开放）。

## 命令

```bash
python scripts/evolve_patch.py --target scripts/tune_judge.py --goal "..."      # 生成+校验
python scripts/evolve_patch.py --target scripts/tune_judge.py --goal "..." --apply  # 合入
powershell -File dsh-ops\trinity-dsh-maintenance.ps1 -Tasks fulltest          # 门禁
```

*生成 2026-08-28*


---

## 阶段2（2026-08-28）：自动合入闭环（无人值守）✅

```
目标 → LLM 补丁 → 校验 → 写入 → git commit → fulltest 门禁（1261+）
→ 全绿保留 / 失败自动 revert
```

**首次无人值守实战**（2026-08-28）：
- 目标：tune_report.py --queries 加 help 文本；
- 全程：APPLIED → auto committed: True → **GATE PASSED (1261+ tests)** → OK；
- git log：`091e2bb auto-evolve: 给 --queries 参数添加 help 文本说明用途`——
  **Trinity 第一次全自动改自己的代码并通过门禁**；
- 启用方式：`evolve_patch.py --target ... --goal ... --apply --auto`。

## 安全边界（阶段2）

- 白名单仍限 scripts/ .py（≤20KB）；
- 门禁失败自动 git revert（git 基线兜底）；
- 每次自改独立 commit（可 revert 精确回退）；
- 核心代码（trinity/）仍不在白名单（阶段 3）。
