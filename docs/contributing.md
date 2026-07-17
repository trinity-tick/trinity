# Contributing

欢迎贡献代码！请遵循以下流程：

1. Fork 本仓库
2. 创建特性分支 (`git checkout -b feature/amazing-feature`)
3. 提交更改 (`git commit -m 'Add amazing feature'`)
4. 推送到分支 (`git push origin feature/amazing-feature`)
5. 创建 Pull Request

## 开发环境

```bash
git clone https://github.com/trinity-tick/trinity.git
cd trinity
pip install -e ".[dev,test]"
```

## 运行测试

```bash
python -m pytest tests/ -v
```

## 代码风格

代码使用 `ruff` 进行格式化和 lint：

```bash
ruff check trinity/ tests/
```

## 许可证

MIT License — 详见 [LICENSE](LICENSE)
