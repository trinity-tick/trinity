#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TrustExchange 记忆市场冷启动模拟。

模拟 5 卖家挂单（每次挂 1-2 个记忆资产卖单，价格/质量分各不相同）+
3 买家连续出价的多轮撮合流程（含 1 次差评/退货事件），并据此验证：

  ① 定价随供需方向正确变化 —— estimate_value 对同类挂单变多（供给↑）估值↓，
     成交均价抬升（需求/成交价↑）估值与市场均价↑；
  ② 声誉收敛 —— 成交后卖家声誉↑、差评后↓、多轮后趋于稳定区间；
  ③ 撮合最优价优先 —— 每轮最低价卖单先成交（成交价序列非降）。

隔离保证：构造时设置 TRINITY_TESTING=1（OrderBook / ReputationEngine /
TrustExchange 三类持久化全部变为 no-op）并把 TRINITY_HOME 指向临时目录，
全程不写任何生产库文件（真实存储路径不会被读取或写入）。

可独立运行（--rounds / --seed），也可 import 复用 run_simulation()。

用法:
    python scripts/market_sim.py --rounds 5 --seed 42
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import tempfile
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

# 允许脚本被直接执行（`python scripts/market_sim.py`）时也能找到 trinity 包。
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# 过滤 trinity 导入时的 60 处 "[Pxxx] initialized" 横幅，保持输出干净。
os.environ.setdefault("TRINITY_QUIET_IMPORT", "1")

from trinity.market import (  # noqa: E402
    OrderBook,
    ReputationEngine,
    TrustExchange,
    create_asset,
    estimate_value,
    get_market_price,
)

# ── 默认参数 ────────────────────────────────────────────────────────────
DEFAULT_ROUNDS = 5
DEFAULT_SEED = 42
SUPPLY_PER_ROUND = 2   # 每轮补充的卖单数
EPSILON_CONVERGE = 0.01  # 声誉收敛判定阈值（相邻两轮评分差）

# 卖方基础质量分（用于模拟初始背书量的多少，差评事件作用在低质量卖方上）。
SELLER_PROFILE = {
    "seller_1": {"quality": 0.95, "price_base": 1.10},
    "seller_2": {"quality": 0.88, "price_base": 1.35},
    "seller_3": {"quality": 0.74, "price_base": 1.60},
    "seller_4": {"quality": 0.60, "price_base": 1.90},
    "seller_5": {"quality": 0.42, "price_base": 2.30},
}
BUYERS = ["buyer_1", "buyer_2", "buyer_3"]
REPORTED_SELLER = "seller_5"  # 第 2 轮后收到差评/退货的卖方


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── 供给池：确定性生成卖单 ──────────────────────────────────────────────
def _build_supply_pool(rng: random.Random) -> List[Dict[str, Any]]:
    """依据种子生成一个确定性的卖单池（卖家/价格/质量分/内容各不相同）。"""
    pool: List[Dict[str, Any]] = []
    idx = 0
    for seller, profile in SELLER_PROFILE.items():
        # 每个卖家挂 1-2 个卖单
        n_assets = rng.randint(1, 2)
        for k in range(n_assets):
            idx += 1
            # 价格在该卖家基价附近小幅扰动，保证全市场最低价卖单唯一且清晰可判。
            price = round(profile["price_base"] + rng.uniform(-0.12, 0.12), 2)
            mods = ["text", "text", "structured", "code"]
            modality = mods[profile["quality"] > 0.7]
            if modality == "code":
                modality = "text"
            content = (
                f"冷启动记忆 #{seller}#{k}: 关于模态市场的定价与声誉观测记录 "
                f"{'x' * int(30 + profile['quality'] * 60)}"
            )
            pool.append({
                "seller": seller,
                "price": max(0.4, price),
                "modality": modality,
                "content": content,
                "quality": profile["quality"],
            })
    return pool


# ── 定价健康检查（①）───────────────────────────────────────────────────
def run_pricing_checks(orderbook: OrderBook, hist_trades: List[Dict[str, Any]]) -> Dict[str, Any]:
    """验证定价随供需方向正确变化。"""
    probe = {
        "memory_id": "probe_value",
        "content": "用于定价方向校验的标准内容文本。",
        "category": "text",
        "tags": ["pricing", "check"],
        "created_at": _now_iso(),
    }
    # 空市场基准价（无供给竞争）。
    base_value = estimate_value(probe, market_data=[], hist_trades=None)

    # 供给↑：人为构造大量同模态(text)卖单进入市场 → 稀有度被竞争摊薄 → 估值应下降。
    crowded_market = [{"modality": "text", "owner_agent": f"s{i}"} for i in range(40)]
    supply_value = estimate_value(probe, market_data=crowded_market, hist_trades=None)

    # 需求/成交价↑：相对空历史，注入高价成交记录 → hist_price 因子上升 → 估值应上升。
    # 使用相同的拥挤供给，只改变历史成交价，隔离"需求↑"对估值的影响。
    demand_value = estimate_value(
        probe, market_data=crowded_market, hist_trades=hist_trades)
    # 同时用实际撮合历史校验 get_market_price 的市场均价抬升。
    market_avg_low = get_market_price("text", hist_trades=[])
    market_avg_high = get_market_price("text", hist_trades=hist_trades)

    supply_down = round(supply_value, 4) < round(base_value, 4) - 1e-4
    demand_up = round(demand_value, 4) > round(supply_value, 4) + 1e-4
    market_avg_up = market_avg_high > market_avg_low

    return {
        "base_value": round(base_value, 4),
        "supply_value": round(supply_value, 4),
        "demand_value": round(demand_value, 4),
        "market_avg_low": round(market_avg_low, 4),
        "market_avg_high": round(market_avg_high, 4),
        "supply_down": supply_down,
        "demand_up": demand_up,
        "market_avg_up": market_avg_up,
        "pass": bool(supply_down and demand_up and market_avg_up),
    }


def _hist_trades_from_txs(txs: List[Any]) -> List[Dict[str, Any]]:
    """把已成交的 Transaction 转成 pricing 需要的 {modality, price} 历史记录。"""
    return [{"modality": "text", "price": t.price} for t in txs]


# ── 撮合辅助 ────────────────────────────────────────────────────────────
def _cheapest_active(orderbook: OrderBook):
    """返回订单簿中价格最低的活跃卖单，返回 None 表示无活跃卖单。"""
    actives = orderbook.search_market()
    if not actives:
        return None
    return min(actives, key=lambda e: e.price)


def _seller_reputation(reputation: ReputationEngine, seller: str) -> float:
    return reputation.calculate_reputation(seller).score


# ── 主模拟 ──────────────────────────────────────────────────────────────
def run_simulation(
    rounds: int = DEFAULT_ROUNDS,
    seed: int = DEFAULT_SEED,
    snapshot_path: Optional[str] = None,
) -> Dict[str, Any]:
    """运行 TrustExchange 冷启动多轮模拟。

    全程隔离：TRINITY_TESTING=1 关闭一切持久化；TRINITY_HOME 指向临时目录。
    返回结构化报告（含断言布尔、声誉序列、撮合/定价指标与最终 PASS/FAIL）。
    """
    rng = random.Random(seed)

    # 保存原始环境，执行完毕恢复。
    _orig_testing = os.environ.get("TRINITY_TESTING")
    _orig_home = os.environ.get("TRINITY_HOME")
    _work_dir = tempfile.mkdtemp(prefix="trinity_market_sim_")
    os.environ["TRINITY_TESTING"] = "1"  # 关闭全部持久化（_load/_save 变 no-op）
    os.environ["TRINITY_HOME"] = _work_dir  # 临时存储路径（纵深防御，不使用）
    try:
        orderbook = OrderBook()
        reputation = ReputationEngine()
        exchange = TrustExchange(orderbook=orderbook, reputation=reputation)

        # 初始背书：用质量分决定每个卖方的初始背书数量，建立差异化声誉基线。
        for seller, profile in SELLER_PROFILE.items():
            for _ in range(int(profile["quality"] * 10)):
                reputation.endorse_agent("system", seller, "cold-start baseline")

        supply_pool = _build_supply_pool(rng)
        deploy_ptr = 0
        all_txs: List[Any] = []
        sold_asset_ids: List[str] = []

        reputation_series: Dict[str, List[float]] = {s: [] for s in SELLER_PROFILE}
        round_report: List[Dict[str, Any]] = []
        bad_review_done = False
        event_log: List[Dict[str, Any]] = []

        def _base_reputation() -> Dict[str, float]:
            return {s: _seller_reputation(reputation, s) for s in SELLER_PROFILE}

        # 第 0 轮前记录基线声誉（无成交）。
        baseline_reputation = _base_reputation()

        for rnd in range(1, rounds + 1):
            # 1. 部署本轮卖单（确定性补充供给）。
            newly_listed: List[str] = []
            for _ in range(SUPPLY_PER_ROUND):
                if deploy_ptr >= len(supply_pool):
                    supply_pool.extend(_build_supply_pool(rng))
                spec = supply_pool[deploy_ptr]
                deploy_ptr += 1
                memory = {
                    "memory_id": f"ast_{rnd}_{deploy_ptr:03d}",
                    "content": spec["content"],
                    "category": "text",
                    "tags": [f"tag_{deploy_ptr}", "coldstart"],
                    "created_at": _now_iso(),
                }
                asset = create_asset(memory, spec["seller"], price=spec["price"])
                orderbook.list_asset(asset, price=spec["price"])
                newly_listed.append(asset.memory_id)

            # 2. 差评/退货事件：第 2 轮结束后作用在低质量卖方。
            if rnd == 2 and not bad_review_done:
                rep_before = _base_reputation()[REPORTED_SELLER]
                reputation.report_agent(
                    BUYERS[0], REPORTED_SELLER,
                    "quality mismatch — asset returned (cold-start bad review)",
                )
                rep_after = _base_reputation()[REPORTED_SELLER]
                bad_review_done = True
                event_log.append({
                    "round": rnd,
                    "event": "bad_review",
                    "target": REPORTED_SELLER,
                    "reputation_before": round(rep_before, 4),
                    "reputation_after": round(rep_after, 4),
                })

            # 3. 买家撮合：每个买家优先买当前最低价活跃卖单（最优价优先）。
            round_fills: List[str] = []
            for buyer in BUYERS:
                target = _cheapest_active(orderbook)
                if target is None:
                    break
                tx = exchange.buy_asset(
                    buyer_agent=buyer,
                    asset_id=target.asset_id,
                    offer_price=target.price,
                )
                all_txs.append(tx)
                sold_asset_ids.append(target.asset_id)
                round_fills.append(round(tx.price, 2))
                event_log.append({
                    "round": rnd,
                    "event": "trade",
                    "buyer": buyer,
                    "seller": target.asset.owner_agent,
                    "asset_id": target.asset_id,
                    "price": round(tx.price, 2),
                })

            # 4. 记录本轮声誉快照。
            rep_now = _base_reputation()
            for s in SELLER_PROFILE:
                reputation_series[s].append(round(rep_now[s], 4))

            round_report.append({
                "round": rnd,
                "newly_listed": newly_listed,
                "fills": round_fills,
                "reputation": {s: round(v, 4) for s, v in rep_now.items()},
            })

        # ▶ 结果判定 ─────────────────────────────────────────────────────
        # ① 定价方向
        hist_trades = _hist_trades_from_txs(all_txs)
        pricing = run_pricing_checks(orderbook, hist_trades)

        # ② 声誉方向与收敛
        direction_ok = []
        # 2a. 无差评卖方成交后声誉应上升或持平（trade_rate↑）。
        for s in SELLER_PROFILE:
            if s == REPORTED_SELLER:
                continue
            base = baseline_reputation[s]
            latest = reputation_series[s][-1] if reputation_series[s] else base
            direction_ok.append(latest >= base - 1e-9)
        # 2b. 差评后声誉应下降。
        bad_review_event = next(
            (e for e in event_log if e.get("event") == "bad_review"), None)
        bad_dropped = bool(
            bad_review_event
            and bad_review_event["reputation_after"] < bad_review_event["reputation_before"]
            - 1e-9
        )
        # 2c. 多轮后收敛：相邻两轮评分差最终小于阈值。
        converged = True
        for s in SELLER_PROFILE:
            seq = reputation_series[s]
            if len(seq) < 3:
                continue
            tail = seq[-3:]
            if max(tail) - min(tail) > EPSILON_CONVERGE:
                converged = False

        reputation_pass = bool(all(direction_ok) and bad_dropped and converged)

        # ③ 撮合最优价优先：每轮成交价序列应非降（最低价先成交）。
        matching_ok = True
        for rr in round_report:
            fills = rr["fills"]
            # 最优价优先要求价格非降；下降即破坏排序。
            if any(a > b + 1e-9 for a, b in zip(fills, fills[1:])):
                matching_ok = False
                break

        pass_all = bool(pricing["pass"] and reputation_pass and matching_ok)

        report: Dict[str, Any] = {
            "meta": {
                "rounds": rounds,
                "seed": seed,
                "isolated": os.environ.get("TRINITY_TESTING") == "1",
                "work_dir": _work_dir,
            },
            "sellers": {
                s: {
                    "profile": p,
                    "reputation_series": reputation_series[s],
                    "final_reputation": round(
                        reputation_series[s][-1], 4) if reputation_series[s]
                        else None,
                }
                for s, p in SELLER_PROFILE.items()
            },
            "pricing": pricing,
            "matching": {
                "round_fills": [rr["fills"] for rr in round_report],
                "best_price_first": matching_ok,
            },
            "reputation_checks": {
                "sellers_rose_after_trade": all(direction_ok),
                "bad_review_event": bad_review_event,
                "dropped_after_bad_review": bad_dropped,
                "converged": converged,
                "pass": reputation_pass,
            },
            "events": event_log,
            "round_summary": round_report,
            "verdicts": {
                "pricing": pricing["pass"],
                "reputation": reputation_pass,
                "matching_best_price": matching_ok,
                "pass": pass_all,
            },
        }

        if snapshot_path:
            with open(snapshot_path, "w", encoding="utf-8") as fh:
                json.dump(report, fh, ensure_ascii=False, indent=2)
        return report
    finally:
        # 恢复环境变量，避免影响当前进程后续行为。
        if _orig_testing is None:
            os.environ.pop("TRINITY_TESTING", None)
        else:
            os.environ["TRINITY_TESTING"] = _orig_testing
        if _orig_home is None:
            os.environ.pop("TRINITY_HOME", None)
        else:
            os.environ["TRINITY_HOME"] = _orig_home


# ── 可读报告输出（默认 5 轮，关注核心结论）─────────────────────────────
def format_report(report: Dict[str, Any], rounds: int) -> str:
    lines: List[str] = []
    lines.append("=" * 62)
    lines.append("TrustExchange 记忆市场冷启动模拟报告")
    lines.append(f"  轮数={report['meta']['rounds']}  种子={report['meta']['seed']}"
                 f"  隔离={report['meta']['isolated']}")
    lines.append("=" * 62)

    lines.append("\n[① 定价随供需方向]")
    p = report["pricing"]
    lines.append(
        f"  基准估值={p['base_value']}  供给拥挤后估值={p['supply_value']}"
        f"  (供给↑→估值{'↓' if p['supply_down'] else '未↓'})")
    lines.append(
        f"  成交均价抬升后估值={p['demand_value']}"
        f"  (需求↑→估值{'↑' if p['demand_up'] else '未↑'})")
    lines.append(
        f"  市场均价({p['market_avg_low']} → {p['market_avg_high']})"
        f"  {'↑' if p['market_avg_up'] else '未↑'}")
    lines.append(f"  → 判定: {'PASS' if p['pass'] else 'FAIL'}")

    lines.append("\n[② 声誉方向与收敛]")
    rc = report["reputation_checks"]
    for s in report["sellers"]:
        series = report["sellers"][s]["reputation_series"]
        final = report["sellers"][s]["final_reputation"]
        if len(series) > 2:
            tail = series[-3:]
            spread = round(max(tail) - min(tail), 4)
            lines.append(
                f"  {s:<10} 序列={series}  末段跨度={spread}  最终={final}")
        else:
            lines.append(f"  {s:<10} 序列={series}  最终={final}")
    if report["sellers"][REPORTED_SELLER]["reputation_series"]:
        lines.append("  → 成交后卖方声誉单调上升/持平: "
                     f"{'PASS' if rc['sellers_rose_after_trade'] else 'FAIL'}")
    if rc["bad_review_event"]:
        be = rc["bad_review_event"]
        lines.append(
            f"  差评事件(第{be['round']}轮, {be['target']}): "
            f"{be['reputation_before']} → {be['reputation_after']}  "
            f"{'↓PASS' if rc['dropped_after_bad_review'] else 'FAIL'}")
    lines.append(f"  多轮收敛: {'PASS' if rc['converged'] else 'FAIL'}")
    lines.append(f"  → 判定: {'PASS' if rc['pass'] else 'FAIL'}")

    lines.append("\n[③ 撮合最优价优先]")
    lines.append(f"  每轮成交价序列 = {report['matching']['round_fills']}")
    lines.append(f"  → 判定: {'PASS' if report['matching']['best_price_first'] else 'FAIL'}")

    lines.append("\n[每轮关键数据]")
    for rs in report["round_summary"]:
        lines.append(
            f"  第{rs['round']}轮  新挂={rs['newly_listed']}  "
            f"成交价={rs['fills']}  声誉={rs['reputation']}")

    verdicts = report["verdicts"]
    lines.append("\n" + "=" * 62)
    lines.append("最终判定: "
                 f"{'PASS' if verdicts['pass'] else 'FAIL'}"
                 f"  (定价:{verdicts['pricing']} / 声誉:{verdicts['reputation']} / "
                 f"撮合:{verdicts['matching_best_price']})")
    lines.append("=" * 62)
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="TrustExchange 记忆市场冷启动模拟（全隔离，不写生产库）。")
    parser.add_argument("--rounds", type=int, default=DEFAULT_ROUNDS,
                        help=f"撮合轮数（默认 {DEFAULT_ROUNDS}）")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED,
                        help=f"随机种子（默认 {DEFAULT_SEED}，可复现）")
    parser.add_argument("--snapshot", type=str, default=None,
                        help="JSON 快照输出路径（默认不写文件）")
    args = parser.parse_args(argv)

    report = run_simulation(
        rounds=args.rounds, seed=args.seed, snapshot_path=args.snapshot)
    print(format_report(report, args.rounds))
    return 0 if report["verdicts"]["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
