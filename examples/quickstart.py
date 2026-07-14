"""
Trinity Quickstart — 5-minute introduction.

Usage:
    python examples/quickstart.py

    Or if installed:
    trinity search --query "user preferences" --top-k 5
"""

import sys
sys.path.insert(0, "..")

from trinity import Trinity


def main():
    print("=" * 60)
    print("Trinity Quickstart — 三位一体智能记忆系统")
    print("=" * 60)

    # 1. Initialize
    memory = Trinity()
    print("\n[1/5] ✅ Trinity initialized")

    # 2. Diagnostics
    diag = memory.diagnostics()
    ver = diag.get("trinity_version", "unknown")
    modules = diag.get("total_modules", 0)
    guardian = diag.get("guardian_chain", {}).get("length", 0)
    channels = diag.get("retrieval_channels", 0)
    print(f"[2/5] ✅ System: v{ver} | {modules} modules | {guardian}-tier guard | {channels} channels")

    # 3. Ingest memories
    memories = [
        "用户偏好深色主题，每周五下午三点开会",
        "用户不喜欢自动播放视频，偏好点击播放",
        "用户是 Python 开发者，主要使用 FastAPI 和 React",
        "用户居住在深圳南山区，喜欢徒步和摄影",
    ]
    for i, m in enumerate(memories):
        result = memory.ingest(m, tags=["demo", "preference"], importance=0.7)
        print(f"[3/5] ✅ Ingested memory {i+1}: {result.get('memory_id', 'ok')}")

    # 4. Search
    results = memory.search("用户喜欢什么主题？", top_k=3)
    print(f"\n[4/5] Search results for '用户喜欢什么主题？':")
    for r in results.get("results", results if isinstance(results, list) else []):
        score = r.get("score", r.get("final_score", 0))
        preview = r.get("content_preview", r.get("content", ""))[:60]
        print(f"  [{score:.3f}] {preview}...")

    # 5. Contradiction detection
    result = memory.detect_contradiction(
        "用户偏好深色主题",
        "用户偏好亮色主题"
    )
    print(f"\n[5/5] Contradiction detection: {result}")

    print("\n" + "=" * 60)
    print("✅ Trinity Quickstart complete!")
    print("=" * 60)


if __name__ == "__main__":
    main()
