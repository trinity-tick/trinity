# -*- coding: utf-8 -*-
"""C3 榜单渲染器 — 从 submissions/*.json 生成 leaderboard.html。

用法:
    python benchmark/leaderboard/build.py [--out benchmark/leaderboard.html]
"""
import argparse
import json
import os
import sys

SUBMISSIONS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "submissions")
DEFAULT_OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "leaderboard.html")

# 展示指标映射: metric -> (列名, 取大/取小)
METRICS = [
    ("r_at_5", "SQuAD R@5", "max"),
    ("recall_at_5_session", "LoCoMo Recall@5", "max"),
    ("composite_judge", "MemSyco Composite", "max"),
    ("e2e_p50_ms", "端到端 P50(ms)", "min"),
    ("max_qps", "QPS@200", "max"),
    ("sycophancy_rate", "谄媚率", "min"),
]

TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>Trinity MemBench Leaderboard</title>
<style>
  body {{ font-family:"Microsoft YaHei",system-ui,sans-serif; background:#0f1420; color:#dbe4f0; padding:24px; }}
  h1 {{ font-size:20px; }} h1 small {{ color:#7d8aa0; }}
  table {{ border-collapse:collapse; width:100%; margin-top:12px; font-size:13px; }}
  th,td {{ border-bottom:1px solid #26304a; padding:8px 10px; text-align:left; }}
  th {{ color:#8fb3ff; }}
  .rank1 {{ color:#ffd700; }} .rank2 {{ color:#c0c0c0; }} .rank3 {{ color:#cd7f32; }}
  .bar {{ display:inline-block; height:10px; background:#2f7dff; vertical-align:middle; border-radius:3px; }}
  code {{ background:#182036; padding:1px 6px; border-radius:4px; }}
  .note {{ color:#7d8aa0; font-size:12px; }}
</style>
</head>
<body>
<h1>Trinity MemBench Leaderboard <small>公开评测基准 · 自动生成于 {generated}</small></h1>
<table>
  <thead><tr><th>#</th><th>提交者</th><th>版本</th><th>日期</th>{heads}</tr></thead>
  <tbody>{rows}</tbody>
</table>
<h2 style="font-size:15px;margin-top:24px">提交方式</h2>
<pre style="background:#182036;padding:12px;border-radius:8px;font-size:12px;overflow:auto">{submit_fmt}</pre>
<p class="note">校验：<code>python benchmark/leaderboard/validate.py</code>（固定数据集、固定 top_k、禁用缓存预热）。</p>
</body>
</html>
"""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=DEFAULT_OUT)
    args = ap.parse_args()

    subs = []
    for f in sorted(os.listdir(SUBMISSIONS_DIR)):
        if not f.endswith(".json"):
            continue
        sub = json.load(open(os.path.join(SUBMISSIONS_DIR, f), encoding="utf-8"))
        metric_map = {(r["suite"], r["metric"]): r["value"] for r in sub["results"]}
        subs.append({
            "submitter": sub.get("submitter", "?"),
            "version": sub.get("trinity_version", "?"),
            "date": sub.get("date", "?"),
            "metrics": metric_map,
        })

    # 排序：按主指标（SQuAD R@5）降序
    subs.sort(key=lambda s: s["metrics"].get(("squad", "r_at_5"), -1), reverse=True)

    heads = "".join(f"<th>{label}</th>" for _label, _dir in [] for label in []) + \
            "".join(f"<th>{label}</th>" for label, _d in [(m[1], m[2]) for m in METRICS])
    rows = []
    for i, s in enumerate(subs, 1):
        cls = f"rank{i}" if i <= 3 else ""
        tds = "".join(
            f"<td>{_fmt(s['metrics'].get((_suite, _metric)), _dir)}</td>"
            for _suite, _metric, _dir in [("squad", "r_at_5", "max"), ("locomo", "recall_at_5_session", "max"),
                                           ("memsyco", "composite_judge", "max"), ("latency", "e2e_p50_ms", "min"),
                                           ("concurrency", "max_qps", "max"), ("memsyco", "sycophancy_rate", "min")]
        )
        rows.append(f"<tr><td class='{cls}'>{i}</td><td>{s['submitter']}</td><td>{s['version']}</td><td>{s['date']}</td>{tds}</tr>")

    submit_fmt = (
        '{\n  "submitter": "your-name",\n  "trinity_version": "v8.2.0",\n'
        '  "date": "2026-08-14",\n  "results": [\n'
        '    {"suite": "squad", "metric": "r_at_5", "value": 0.9833},\n'
        '    {"suite": "memsyco", "metric": "composite_judge", "value": 0.88}\n  ]\n}'
    )
    import datetime
    html = TEMPLATE.format(generated=datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
                           heads=heads, rows="".join(rows), submit_fmt=submit_fmt)
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"leaderboard 已生成: {args.out} ({len(subs)} 个提交)")


def _fmt(v, direction):
    if v is None:
        return "-"
    if isinstance(v, float):
        if direction == "max" and v <= 1:
            return f"{v:.3f}"
        return f"{v:,.1f}"
    return str(v)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
