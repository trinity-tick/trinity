# -*- coding: utf-8 -*-
"""Unit tests for the TrustExchange memory market cold-start simulation.

覆盖脚本 scripts/market_sim.py 的核心判定逻辑：
  - 定价单调性（供给↑估值↓ / 成交价↑估值与市场均价↑）
  - 声誉方向（成交↑ / 差评↓ / 多轮收敛）
  - 撮合最优价优先（每轮最低价先成交）
  - 模拟可复现（同 seed 同结果）
  - 临时实例隔离（不落任何生产 orderbook/reputation 存储路径）
"""

import importlib.util
import os
import sys
import tempfile

import pytest

SCRIPT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "scripts",
    "market_sim.py",
)


def _load_market_sim():
    """从绝对路径加载 scripts/market_sim.py（scripts 目录非包，用 importlib）。"""
    spec = importlib.util.spec_from_file_location("market_sim", SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["market_sim"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def sim():
    return _load_market_sim()


def _stable_metrics(report):
    """抽取与时间戳无关的稳定指标，用于可复现性断言。"""
    return {
        "pricing": report["pricing"],
        "matching": report["matching"],
        "verdicts": report["verdicts"],
        "reputation_series": {
            s: list(v["reputation_series"])
            for s, v in report["sellers"].items()
        },
        # 事件里剔除时间无关性，只保留撮合/事件语义字段
        "trade_fills": [e for e in report["events"] if e["event"] == "trade"],
        "bad_review": [e for e in report["events"] if e["event"] == "bad_review"],
    }


def _run_with_clock(sim, monkeypatch, moment: str):
    """把 `market_sim._now_iso` 钉成给定时刻后跑一次模拟（t71/I11 的墙钟反事实用）。"""
    monkeypatch.setattr(sim, "_now_iso", lambda: moment)
    return sim.run_simulation(rounds=5, seed=42)


# ═══════════════════════════════════════════════════════════════════════════
# 定价单调性
# ═══════════════════════════════════════════════════════════════════════════

class TestPricingMonotonicity:
    def test_pricing_supply_pushes_value_down(self, sim):
        """供给↑（同模态卖单变多）→ 估值应下降。"""
        from trinity.market import OrderBook
        orderbook = OrderBook()
        result = sim.run_pricing_checks(orderbook, hist_trades=[])
        assert result["supply_value"] < result["base_value"]
        assert result["supply_down"] is True

    def test_pricing_demand_pushes_value_up(self, sim):
        """需求/成交价↑（历史成交均价抬升）→ 估值与市场均价应上升。"""
        from trinity.market import OrderBook
        orderbook = OrderBook()
        # 构造高于默认(0.5)的文本成交历史。
        hist = [{"modality": "text", "price": 4.5},
                {"modality": "text", "price": 4.0}]
        result = sim.run_pricing_checks(orderbook, hist_trades=hist)
        assert result["demand_value"] > result["supply_value"]
        assert result["market_avg_high"] > result["market_avg_low"]
        assert result["pass"] is True

    def test_pricing_overall_direction(self, sim):
        """整体定价方向三项断言须全通过（需有真实成交历史支撑需求↑）。"""
        from trinity.market import OrderBook
        hist = [{"modality": "text", "price": 3.2},
                {"modality": "text", "price": 3.8}]
        result = sim.run_pricing_checks(OrderBook(), hist_trades=hist)
        assert result["supply_down"] is True
        assert result["demand_up"] is True
        assert result["market_avg_up"] is True
        assert result["pass"] is True

    def test_pricing_no_history_no_fake_demand(self, sim):
        """无历史成交时不应虚假抬价：需求↑判定应为 False（保持诚实）。"""
        from trinity.market import OrderBook
        result = sim.run_pricing_checks(OrderBook(), hist_trades=[])
        assert result["demand_up"] is False
        # 供给方向不受历史影响，仍应成立。
        assert result["supply_down"] is True


# ═══════════════════════════════════════════════════════════════════════════
# 声誉方向
# ═══════════════════════════════════════════════════════════════════════════

class TestReputationDirection:
    def test_reputation_rises_after_trade(self, sim):
        """成交后，未差评卖家声誉应上升或至少持平。"""
        report = sim.run_simulation(rounds=5, seed=42)
        for s, v in report["sellers"].items():
            if s == sim.REPORTED_SELLER:
                continue
            seq = v["reputation_series"]
            assert seq[0] <= seq[-1] + 1e-9, \
                f"{s}: 声誉未保持/上升 {seq}"
        assert report["reputation_checks"]["sellers_rose_after_trade"] is True

    def test_reputation_drops_after_bad_review(self, sim):
        """差评事件后，目标卖家声誉应下降。"""
        report = sim.run_simulation(rounds=5, seed=42)
        be = report["reputation_checks"]["bad_review_event"]
        assert be is not None
        assert be["target"] == sim.REPORTED_SELLER
        assert be["reputation_after"] < be["reputation_before"]
        assert report["reputation_checks"]["dropped_after_bad_review"] is True

    def test_reputation_converges(self, sim):
        """多轮后声誉趋于稳定区间（末段相邻轮差小于阈值）。"""
        report = sim.run_simulation(rounds=6, seed=42)
        assert report["reputation_checks"]["converged"] is True
        assert report["reputation_checks"]["pass"] is True


# ═══════════════════════════════════════════════════════════════════════════
# 撮合最优价优先
# ═══════════════════════════════════════════════════════════════════════════

class TestMatchingBestPrice:
    def test_best_price_first(self, sim):
        """每轮成交价序列须非降（最低价卖单先成交）。"""
        report = sim.run_simulation(rounds=5, seed=42)
        assert report["matching"]["best_price_first"] is True
        for fills in report["matching"]["round_fills"]:
            for a, b in zip(fills, fills[1:]):
                assert a <= b + 1e-9, f"最优价优先破坏: {fills}"

    def test_matching_overall_verdict(self, sim):
        """撮合判定在默认运行下通过。"""
        report = sim.run_simulation(rounds=5, seed=42)
        assert report["verdicts"]["matching_best_price"] is True
        assert report["verdicts"]["pass"] is True


# ═══════════════════════════════════════════════════════════════════════════
# 可复现性
# ═══════════════════════════════════════════════════════════════════════════

class TestReproducibility:
    def test_same_seed_same_result(self, sim, monkeypatch):
        """同 seed → 稳定指标完全一致。

        ⚠️ 2026-10-06（t71/I11）**修既有 flaky**（t65 已定位根因，我按其首选修法只改测试）：

        · **根因 = 墙钟**：`scripts/market_sim.py` 把 `created_at = _now_iso()` 塞进估值探针
          （`:111` 的 pricing probe、`:221` 的待部署记忆），而估值**随年龄衰减**
          ⇒ 两次运行相隔约 0.5s 就会拿到不同的估值（t65 实测反事实：只改 `created_at`
          而不动 seed ⇒ `0s→1.25`、**`0.5s→1.24`**、`1d→1.15`、`30d→0.30`；
          把 `_now_iso` 固定成常量 ⇒ **6/6 相等**，未固定 5/6）。
        · **修法（只改测试）**：把 `_now_iso` 钉成常量 ⇒ "同 seed 同结果"这条**可复现性**判据
          才真的在测"可复现性"，而不是在测"两次运行隔了多久"。
        · **保留生产语义**：`scripts/market_sim.py` 的衰减是**功能**（估值随时间下降），
          **未改动**（本任务硬约束也不允许改它）。判据侧的"年龄"由固定时刻决定 ⇒ 确定性。
        """
        fixed_now = "2026-10-06T00:00:00+00:00"
        monkeypatch.setattr(sim, "_now_iso", lambda: fixed_now)
        a = _stable_metrics(sim.run_simulation(rounds=5, seed=42))
        b = _stable_metrics(sim.run_simulation(rounds=5, seed=42))
        assert a == b

    def test_墙钟是唯一驱动的反事实(self, sim, monkeypatch):
        """**反事实锁**（t71/I11 新增）：只动"墙钟"、不动 seed ⇒ 稳定指标**必须变**。

        这条把"墙钟是 flaky 的唯一驱动"钉在判据里：若哪天生产侧不再用墙钟（例如把
        `created_at` 从估值探针里移除），本判据会红 ⇒ 提醒复核上面那条固定时刻的修法是否还需要。
        """
        a = _stable_metrics(_run_with_clock(sim, monkeypatch, "2026-10-06T00:00:00+00:00"))
        b = _stable_metrics(_run_with_clock(sim, monkeypatch, "2026-10-31T00:00:00+00:00"))
        assert a != b, (
            "只把墙钟从 0s 挪到 25 天后，稳定指标竟然完全相同 ⇒ "
            "要么生产侧不再用 created_at 参与估值（请复核 flaky 修法），要么本判据失效")

    def test_different_seed_differs(self, sim):
        """不同 seed → 成交价序列应不同（价格受种子扰动）。"""
        a = sim.run_simulation(rounds=5, seed=1)["matching"]["round_fills"]
        b = sim.run_simulation(rounds=5, seed=99)["matching"]["round_fills"]
        assert a != b


# ═══════════════════════════════════════════════════════════════════════════
# 临时实例隔离
# ═══════════════════════════════════════════════════════════════════════════

def _is_within(child, parent):
    """`child` 是否落在 `parent` 之下 —— **两个路径不同盘符时也不抛异常**。

    原先这里写的是 `os.path.commonpath([...]) != ...`，而 `commonpath` 在**不同盘符**时
    是**抛** `ValueError: Paths don't have the same drive`（它不返回）。
    单测把 `TRINITY_HOME` 隔离到临时目录之后，这条判据当场炸（2026-09-22 §1247 实测：
    隔离 ON 时全量套件唯一多出来的那 1 条失败就是它）。
    """
    child = os.path.abspath(child)
    parent = os.path.abspath(parent)
    try:
        return os.path.commonpath([child, parent]) == parent
    except ValueError:
        return False


class TestIsolation:
    MARKET_FILES = (
        "memory_market_orderbook.json",
        "memory_market_reputation.json",
        "memory_market_trust_exchange.json",
    )

    @staticmethod
    def _production_home():
        """生产 home，**故意不读 `TRINITY_HOME`**（§1247）。

        本类的判据是「模拟没有落到**生产**的内存市场存储路径」，而
        `tests/unit/conftest.py` 现在会把 `TRINITY_HOME` 指向临时目录 ⇒
        若继续用 `TRINITY_HOME`，这里比的就成了临时目录，**判据会被悄悄削弱**
        （还是一条会变绿的削弱）——所以生产落点必须**另说**：`~/.trinity`
        （三个市场文件实测就在该目录下）。
        """
        return os.path.expanduser("~/.trinity")

    def _real_home_market_files(self):
        home = self._production_home()
        return {f: os.path.join(home, f) for f in self.MARKET_FILES}

    def test_sim_is_isolated_flag_and_env(self, sim):
        """运行期间 TRINITY_TESTING=1 生效，工作目录为临时目录，结束后环境还原。"""
        import os as _os
        orig_testing = _os.environ.get("TRINITY_TESTING")
        report = sim.run_simulation(rounds=3, seed=42)
        assert report["meta"]["isolated"] is True
        # 模拟结束后环境变量应还原到运行前的值（不遗留测试隔离态，也不重写生产值）。
        assert _os.environ.get("TRINITY_TESTING") == orig_testing
        # 工作目录是临时目录。
        assert report["meta"]["work_dir"].startswith(
            tempfile.gettempdir())

    def test_no_production_write(self, sim):
        """临时实例不落任何生产内存市场存储路径。"""
        real_files_before = {
            f: (os.path.exists(p), self._read_opt(p))
            for f, p in self._real_home_market_files().items()
        }
        report = sim.run_simulation(rounds=5, seed=42)

        # 模拟使用的工作目录是临时目录，不在生产 home 下。
        assert not _is_within(report["meta"]["work_dir"], self._production_home()), \
            "模拟工作目录不应落在生产 home（~/.trinity）下"

        real_files_after = {
            f: (os.path.exists(p), self._read_opt(p))
            for f, p in self._real_home_market_files().items()
        }
        assert real_files_after == real_files_before, \
            "真实内存市场持久化文件被意外创建/修改"

    @staticmethod
    def _read_opt(path):
        try:
            with open(path, encoding="utf-8") as fh:
                return fh.read()
        except Exception:
            return None
