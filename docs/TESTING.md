# Trinity 测试口径（EXECUTION 458C 统一，2026-09-02）

- **fast（默认）**：pytest.ini testpaths=trinity/tests → **168 passed / ~22-42s**（日常门禁、maintenance fulltest 用）；
- **full**：仓库根另有大套 tests/（122 文件，含 market/e2e/adapters 等）→ 命令：python -m pytest tests（无默认配置前缀）；
- 458 定向子集参考：tests/unit/test_market_sim.py tests/unit/test_market_finish.py tests/test_market_persistence_20260901.py tests/test_multimodal.py（53 passed）；
- 归档口径说明：历史文档中的 "815 / 1261 全绿" 指不同时期的 full 套件统计，与 fast=168 非同一口径——引用数字时须注明套件名与日期。
