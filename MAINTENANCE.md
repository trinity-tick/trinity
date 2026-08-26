# Trinity 日常维护指南
# 使用: 每周运行一次，检查项目健康状况

## 每日（可选）
- `git status` — 检查是否有未提交的更改
- 查看 GitHub Issues: https://github.com/trinity-tick/trinity/issues
- 查看 GitHub Discussions: https://github.com/trinity-tick/trinity/discussions

## 每周（推荐）
```
python health_check.py              # 自动健康检查
```

## 每月
- `git pull` — 同步最新代码
- 更新 CHANGELOG.md
- 检查 Dependabot PRs：https://github.com/trinity-tick/trinity/pulls
- 检查依赖安全漏洞

## 发布新版本流程
```
1. 更新 pyproject.toml 版本号
2. 更新 CHANGELOG.md
3. git add -A && git commit -m "vX.Y.Z"
4. git tag vX.Y.Z
5. git push && git push --tags
6. GitHub Actions 自动发布到 PyPI
7. 创建 GitHub Release
```

## 一键构建 + 发布到 PyPI
```bash
python -m build
python -m twine upload dist/*
```

## 自进化闭环（2026-08-25）

三阶段脚本已实现并接入维护链（详见 docs/SELF_EVOLUTION_DESIGN.md 第八节）：

```powershell
# 手动触发一轮自进化闭环（有 LLM 成本：QA + judge3 双轮，n=10 约 20-40min）
powershell -File dsh-ops/trinity-dsh-maintenance.ps1 -Tasks evolve-auto

# 查看已采纳 env（应用器，白名单校验后注入进程环境）
powershell -File dsh-ops/apply_evolve_env.ps1 -Show

# 快速信号采集（跳过 QA，只拿指标+质量）
python scripts/evolve_signal.py --skip-qa
```

要点：
- `evolve-auto` 有意不进 all 链（LLM 成本），默认每周手动触发；
- 连续 3 轮无改进 → interval=paused，需 `--force` 恢复；
- 采纳的 env 在 `~/.trinity/evolve/evolve_env.json`，supervisor 每轮读取注入；
- 产物在 `~/.trinity/evolve/`（signal/ab/base/exp/state/falsified/env）。

## 自进化私有留出集（2026-08-25 修复）

- 私有集（benchmark/private_holdout.json）此前全 UNKNOWN：build 脚本改写 prompt 无语言约束，
  问题被改成中文 vs 英文 haystack → 跨语言检索失败。已修复并重建（0 CJK）；
- question_date 已回填（100/100）；
- judge3 已加票间并发（快 ~3 倍）；evolve_signal/evolve_ab 已改 qtype-aware ingest；
- 旧损坏版本备份在 benchmark/private_holdout_chinese_bug.json。

## 自进化最终状态（2026-08-25 交付）

- 主指标：MRR（连续配对 bootstrap CI，排序敏感抗饱和）；
- 可测参数：search_hybrid 路径内 11 个 env（GRAPH_PPR/RRF_K/IMPORTANCE_BOOST/权重等）；
- 私有评测集：benchmark/private_holdout.json（100 题，防污染）；
- 结论：参数级无显著改进参数（15 轮诚实证伪）；结构级进化需人工；
- 详细：docs/SELF_EVOLUTION_DESIGN.md 第二十五节。
