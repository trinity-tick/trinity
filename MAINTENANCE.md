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
