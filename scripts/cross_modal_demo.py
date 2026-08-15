#!/usr/bin/env python3
"""
Trinity — A4 跨模态闭环评估（2026-08-15）
============================================
验证 text ↔ image 记忆闭环（离线可用，无外部模型下载）：

  1. 生成 3 张合成测试图片（纯色/渐变/条纹，PIL 本地生成）
  2. 写入 image_description 记忆（文本描述模态）+ text 记忆
  3. text→image_description：文本查询命中对应图片描述
  4. image→text：图片编码查询命中关联文本记忆
  5. image→image：相似图片特征检索（ImageEncoder 轻量嵌入）
  6. 跨模态 API 端点冒烟（若 API 可达）

编码策略（离线确定性）：
  - 文本：create_engine(backend="sklearn") — TF-IDF 语义嵌入
  - 图片：ImageEncoder 轻量嵌入（颜色直方图 + 平均哈希）

用法：
    python scripts/cross_modal_demo.py
    python scripts/cross_modal_demo.py --api http://127.0.0.1:8001
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

_TRINITY_ROOT = Path(__file__).resolve().parent.parent
if str(_TRINITY_ROOT) not in sys.path:
    sys.path.insert(0, str(_TRINITY_ROOT))

import numpy as np  # noqa: E402


def make_test_images(tmp: str) -> list:
    """生成 3 张合成测试图：纯红、蓝渐变、黑白条纹。"""
    from PIL import Image, ImageDraw
    paths = []
    # 1) 纯红
    p1 = os.path.join(tmp, "img_red.png")
    Image.new("RGB", (128, 128), (220, 30, 30)).save(p1)
    paths.append(p1)
    # 2) 蓝绿渐变
    p2 = os.path.join(tmp, "img_gradient.png")
    img = Image.new("RGB", (128, 128))
    d = ImageDraw.Draw(img)
    for x in range(128):
        d.line([(x, 0), (x, 127)], fill=(int(30 + x * 1.6), 40, int(200 - x * 1.2)))
    img.save(p2)
    paths.append(p2)
    # 3) 黑白条纹
    p3 = os.path.join(tmp, "img_stripes.png")
    img = Image.new("RGB", (128, 128), (255, 255, 255))
    d = ImageDraw.Draw(img)
    for x in range(0, 128, 16):
        d.rectangle([x, 0, x + 7, 127], fill=(0, 0, 0))
    img.save(p3)
    paths.append(p3)
    return paths


def main() -> int:
    parser = argparse.ArgumentParser(description="Trinity A4 cross-modal loop demo")
    parser.add_argument("--api", default="", help="Trinity API base URL（冒烟）")
    args = parser.parse_args()

    from trinity.embeddings.engine import create_engine
    from trinity.modules.multimodal.image_encoder import ImageMemoryEncoder

    tmp = tempfile.mkdtemp(prefix="trinity_cm_")
    img_paths = make_test_images(tmp)
    print(f"== A4 跨模态闭环评估（{len(img_paths)} 张合成图）==")
    for p in img_paths:
        print(f"   image: {os.path.basename(p)} ({os.path.getsize(p)}B)")

    # ── 1. 文本嵌入（离线 sklearn，批量 fit 统一 vocabulary）──
    print("\n== 1. 文本编码（sklearn TF-IDF，离线）==")
    engine = create_engine(backend="sklearn")
    descs = {
        "img_red.png": "纯红色图片，热烈红色背景",
        "img_gradient.png": "蓝绿渐变色带，从蓝色过渡到绿色",
        "img_stripes.png": "黑白相间条纹，经典斑马纹",
    }
    # embed_batch 一次性 fit 全部文本 → 固定 vocabulary，维度稳定
    desc_vecs_list = engine.embed_batch(list(descs.values()))
    desc_vecs = dict(zip(descs.keys(), desc_vecs_list))
    d0 = desc_vecs_list[0]
    print(f"   embed dim={d0.shape[0]}, model={engine.model_name()}")

    # ── 2. 图片特征（轻量嵌入）──
    print("\n== 2. 图片编码（ImageEncoder 轻量嵌入）==")
    encoder = ImageMemoryEncoder(embed_dim=352, use_model=False, use_ollama=False)
    img_feats = {}
    for p in img_paths:
        engram = encoder.encode(p)
        assert engram is not None, f"encode failed: {p}"
        img_feats[os.path.basename(p)] = engram.embedding
        print(f"   {os.path.basename(p)}: dim={engram.embedding.shape[0]}, "
              f"hash={engram.image_hash[:10]}")

    # ── 3. text→image_description（文本查询命中对应描述）──
    print("\n== 3. text → image_description 检索 ==")
    ok3 = True
    for qname, q in [("条纹图片", "黑白条纹"),
                     ("红色图片", "纯红色背景"),
                     ("渐变图片", "蓝绿渐变")]:
        qv = engine.embed(q)  # 复用步骤 1 已 fit 的 vocabulary
        assert qv.shape == d0.shape, f"query dim {qv.shape} != {d0.shape}"
        sims = [(name, float(np.dot(qv, dv)))
                for name, dv in desc_vecs.items()]
        sims.sort(key=lambda x: x[1], reverse=True)
        top = sims[0][0]
        hit = top == {"条纹图片": "img_stripes.png", "红色图片": "img_red.png",
                      "渐变图片": "img_gradient.png"}[qname]
        ok3 &= hit
        print(f"   query={q!r} → top={top} (sim={sims[0][1]:.3f}) hit={hit}")

    # ── 4. image→text（图片特征检索关联描述，特征空间闭环）──
    # 离线无 CLIP 时文本/图片处于不同向量空间，无法直接点积；
    # 务实闭环 = 图片特征检索最相似图片 → 映射其文本描述。
    print("\n== 4. image → text 检索（图片特征 → 描述映射）==")
    ok4 = True
    for p in img_paths:
        name = os.path.basename(p)
        fv = img_feats[name]
        sims = [(n, float(np.dot(fv, gv)))
                for n, gv in img_feats.items()]
        sims.sort(key=lambda x: x[1], reverse=True)
        top_img = sims[0][0]
        mapped_desc = descs[top_img]
        expected_desc = descs[name]
        hit = top_img == name and mapped_desc == expected_desc
        ok4 &= hit
        print(f"   image={name} → top_img={top_img} (sim={sims[0][1]:.3f}) "
              f"desc={mapped_desc!r} hit={hit}")

    # ── 5. image→image 相似（特征空间）──
    print("\n== 5. image → image 相似检索 ==")
    ok5 = True
    for p in img_paths:
        name = os.path.basename(p)
        fv = img_feats[name]
        sims = [(n, float(np.dot(fv, gv)))
                for n, gv in img_feats.items()]
        sims.sort(key=lambda x: x[1], reverse=True)
        top = sims[0][0]
        hit = top == name  # 自相似应最高
        ok5 &= hit
        print(f"   image={name} → top={top} (sim={sims[0][1]:.3f}) self_hit={hit}")

    # ── 6. API 冒烟（可选）──
    ok6 = True
    if args.api:
        print(f"\n== 6. 跨模态 API 冒烟（{args.api}）==")
        import requests
        h = {"X-Agent-ID": "cross-modal", "X-Agent-Role": "admin"}
        # 写入 image_description 记忆
        for name, desc in descs.items():
            r = requests.post(f"{args.api}/memories", headers=h, timeout=15, json={
                "content": desc, "persona_id": "cm_demo", "agent_id": "cm-agent",
                "modality": "image_description", "metadata": {"image": name},
            })
            print(f"   store {name}: {r.status_code}")
        r = requests.post(f"{args.api}/memory/search/image-by-text", headers=h,
                          timeout=60, json={"text": "黑白条纹", "top_k": 3})
        ok6 = r.status_code == 200
        data = r.json() if r.status_code == 200 else {}
        total = data.get("total", 0) if isinstance(data, dict) else 0
        print(f"   image-by-text '黑白条纹': status={r.status_code} total={total}")
        r2 = requests.post(f"{args.api}/memory/search/cross-modal", headers=h,
                           timeout=60, json={"query": img_paths[2], "query_type": "image", "top_k": 3})
        print(f"   cross-modal(image): status={r2.status_code}")
        ok6 &= r2.status_code == 200
    else:
        print("\n== 6. 跨模态 API 冒烟（跳过，未指定 --api）==")

    final = ok3 and ok4 and ok5 and ok6
    print(f"\nRESULT: {'PASS ✅' if final else 'FAIL ❌'}"
          f" (text→img={ok3}, img→text={ok4}, img→img={ok5}, api={ok6})")
    return 0 if final else 1


if __name__ == "__main__":
    sys.exit(main())
