#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""experiment_review.py — 评测审阅循环（Claude Science 借鉴 Phase 2）。

对比两次评测结果（baseline vs new）：
  - 总览：R@5 / AnswerAcc / gaps 的 delta
  - 逐类目 delta 表 + 异常波动标记（|delta| > 0.05）
  - 归因：retr_gap（检索缺口）/ gen_gap（生成缺口）变化
  - 工件审计：从 manifest 读环境差异（code_hash / params / dataset）

用法:
    python scripts/experiment_review.py --base output/ae_500_reason_v3.json --new output/ae_500_reason_v5.json
    python scripts/experiment_review.py --base A.json --new B.json --out review.md --threshold 0.05
"""
import argparse
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
os.environ.setdefault("TRINITY_MEMORY_ENABLED", "0")


def _load(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _read_manifest(result_path):
    mpath = result_path + ".manifest.json"
    if os.path.exists(mpath):
        try:
            with open(mpath, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="", help="baseline 结果 JSON（--latest 时忽略）")
    ap.add_argument("--new", default="", help="新结果 JSON（--latest 时忽略）")
    ap.add_argument("--latest", action="store_true",
                    help="自动选 output/ 下最近两次 ae_500_reason_*.json（按修改时间）")
    ap.add_argument("--out", default="", help="审阅报告输出（默认 stdout）")
    ap.add_argument("--threshold", type=float, default=0.05, help="异常波动阈值")
    args = ap.parse_args()

    if args.latest:
        out_dir = os.path.join(REPO, "output")
        cands = sorted(
            [os.path.join(out_dir, f) for f in os.listdir(out_dir)
             if f.startswith("ae_500_reason_") and f.endswith(".json")
             and not f.endswith(".manifest.json")],
            key=lambda p: os.path.getmtime(p),
        )
        if len(cands) < 2:
            print("ERROR: --latest needs >=2 ae_500_reason_*.json results in output/")
            return 1
        args.base, args.new = cands[-2], cands[-1]
        print(f"--latest: base={os.path.basename(args.base)} new={os.path.basename(args.new)}")

    base = _load(args.base)
    new = _load(args.new)
    lines = []

    # ── 总览 ──
    lines.append("# 实验审阅报告（Claude Science 借鉴 Phase 2）")
    lines.append("")
    lines.append(f"- base: {args.base}")
    lines.append(f"- new : {args.new}")
    lines.append(f"- 阈值: ±{args.threshold}")
    lines.append("")
    lines.append("| 指标 | base | new | Δ | 异常 |")
    lines.append("|---|---|---|---|---|")
    for key, label in (("R@5", "R@5"), ("AnswerAcc", "AnswerAcc"),
                       ("generation_gap", "gen_gap"), ("retrieval_gap", "retr_gap")):
        b = base.get(key)
        n = new.get(key)
        if b is None or n is None:
            continue
        d = n - b
        flag = "⚠" if abs(d) > args.threshold else ""
        lines.append(f"| {label} | {b:.4f} | {n:.4f} | {d:+.4f} | {flag} |")

    # ── 逐类目 ──
    bc = base.get("by_category", {})
    nc = new.get("by_category", {})
    if bc or nc:
        lines.append("")
        lines.append("### 逐类目")
        lines.append("")
        lines.append("| 类目 | base R@5 | new R@5 | ΔR | base Acc | new Acc | ΔAcc | 异常 |")
        lines.append("|---|---|---|---|---|---|---|---|")
        for c in sorted(set(bc) | set(nc)):
            b = bc.get(c, {})
            n = nc.get(c, {})
            br = b.get("R@5", "-")
            nr = n.get("R@5", "-")
            ba = b.get("AnswerAcc", "-")
            na = n.get("AnswerAcc", "-")
            dr = (nr - br) if isinstance(br, float) and isinstance(nr, float) else "-"
            da = (na - ba) if isinstance(ba, float) and isinstance(na, float) else "-"
            flag = ""
            if isinstance(da, float) and abs(da) > args.threshold:
                flag = "⚠"
            elif isinstance(dr, float) and abs(dr) > args.threshold:
                flag = "⚠"
            lines.append(f"| {c} | {br} | {nr} | {dr if isinstance(dr, str) else f'{dr:+.3f}'} | "
                         f"{ba} | {na} | {da if isinstance(da, str) else f'{da:+.3f}'} | {flag} |")

    # ── 工件审计（manifest） ──
    lines.append("")
    lines.append("### 工件审计（manifest）")
    lines.append("")
    for tag, path in (("base", args.base), ("new", args.new)):
        m = _read_manifest(path)
        if m is None:
            lines.append(f"- **{tag}**: 无 manifest（旧结果或未接入）")
        else:
            lines.append(f"- **{tag}**: code_hash={m.get('code_hash', '?')} | "
                         f"python={m.get('env', {}).get('python', '?')} | "
                         f"params={json.dumps(m.get('params', {}), ensure_ascii=False)}")
    m1 = _read_manifest(args.base)
    m2 = _read_manifest(args.new)
    if m1 and m2:
        same_code = m1.get("code_hash") == m2.get("code_hash")
        lines.append(f"- 代码一致性: {'同（可复现对比）' if same_code else '**不同（对比跨代码版本，谨慎解读）**'}")
        p1 = m1.get("params", {})
        p2 = m2.get("params", {})
        if p1 != p2:
            lines.append(f"- 参数差异: base={json.dumps(p1, ensure_ascii=False)} vs "
                         f"new={json.dumps(p2, ensure_ascii=False)}")
    lines.append("")
    report = chr(10).join(lines)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(report)
        print(f"review saved -> {args.out}")
    print(report)
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
