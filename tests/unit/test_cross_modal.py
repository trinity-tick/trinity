"""Trinity — 跨模态闭环单元测试（A4, 2026-08-15）。

覆盖（离线、无外部模型下载）：
- sklearn 批量 fit 后文本嵌入维度稳定、语义检索可判别
- ImageEncoder 轻量图片嵌入自相似最高、相似图可区分
- 图片特征 → 描述映射闭环
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import numpy as np
import pytest

from trinity.embeddings.engine import create_engine
from trinity.modules.multimodal.image_encoder import ImageMemoryEncoder


@pytest.fixture()
def test_images(tmp_path: Path) -> list:
    from PIL import Image, ImageDraw
    paths = []
    p1 = tmp_path / "img_red.png"
    Image.new("RGB", (128, 128), (220, 30, 30)).save(p1)
    paths.append(str(p1))
    p2 = tmp_path / "img_gradient.png"
    img = Image.new("RGB", (128, 128))
    d = ImageDraw.Draw(img)
    for x in range(128):
        d.line([(x, 0), (x, 127)], fill=(int(30 + x * 1.6), 40, int(200 - x * 1.2)))
    img.save(p2)
    paths.append(str(p2))
    p3 = tmp_path / "img_stripes.png"
    img = Image.new("RGB", (128, 128), (255, 255, 255))
    d = ImageDraw.Draw(img)
    for x in range(0, 128, 16):
        d.rectangle([x, 0, x + 7, 127], fill=(0, 0, 0))
    img.save(p3)
    paths.append(str(p3))
    return paths


def test_sklearn_batch_fit_stable_dim() -> None:
    """批量 fit 后 embedding_dim 稳定，单条 embed 维度一致。"""
    engine = create_engine(backend="sklearn")
    texts = ["纯红色图片，热烈红色背景",
             "蓝绿渐变色带，从蓝色过渡到绿色",
             "黑白相间条纹，经典斑马纹"]
    vecs = engine.embed_batch(texts)
    dims = {v.shape[0] for v in vecs}
    assert len(dims) == 1  # 统一维度
    q = engine.embed("黑白条纹")
    assert q.shape == vecs[0].shape  # 查询与语料同维度


def test_sklearn_text_retrieval_discriminative() -> None:
    """文本查询能判别对应描述（char_wb n-gram 语义）。"""
    engine = create_engine(backend="sklearn")
    descs = {
        "img_red.png": "纯红色图片，热烈红色背景",
        "img_gradient.png": "蓝绿渐变色带，从蓝色过渡到绿色",
        "img_stripes.png": "黑白相间条纹，经典斑马纹",
    }
    vecs = engine.embed_batch(list(descs.values()))
    name2vec = dict(zip(descs.keys(), vecs))
    for qname, q in [("条纹图片", "黑白条纹"),
                     ("红色图片", "纯红色背景"),
                     ("渐变图片", "蓝绿渐变")]:
        qv = engine.embed(q)
        top = max(name2vec.items(), key=lambda kv: float(np.dot(qv, kv[1])))[0]
        expected = {"条纹图片": "img_stripes.png", "红色图片": "img_red.png",
                    "渐变图片": "img_gradient.png"}[qname]
        assert top == expected, f"{q!r} → {top}, expected {expected}"


def test_image_encoder_lightweight_self_similarity(test_images: list) -> None:
    """轻量嵌入：自相似最高，相似结构图可区分。"""
    enc = ImageMemoryEncoder(embed_dim=352, use_model=False, use_ollama=False)
    feats = {}
    for p in test_images:
        engram = enc.encode(p)
        assert engram is not None
        feats[os.path.basename(p)] = engram.embedding
    for name, fv in feats.items():
        sims = [(n, float(np.dot(fv, gv))) for n, gv in feats.items()]
        sims.sort(key=lambda x: x[1], reverse=True)
        assert sims[0][0] == name  # 自相似最高


def test_image_to_desc_mapping_loop(test_images: list) -> None:
    """图片特征 → 最相似图片 → 描述映射闭环。"""
    enc = ImageMemoryEncoder(embed_dim=352, use_model=False, use_ollama=False)
    descs = {
        "img_red.png": "纯红色图片，热烈红色背景",
        "img_gradient.png": "蓝绿渐变色带，从蓝色过渡到绿色",
        "img_stripes.png": "黑白相间条纹，经典斑马纹",
    }
    feats = {}
    for p in test_images:
        engram = enc.encode(p)
        assert engram is not None
        feats[os.path.basename(p)] = engram.embedding
    for name, fv in feats.items():
        sims = [(n, float(np.dot(fv, gv))) for n, gv in feats.items()]
        sims.sort(key=lambda x: x[1], reverse=True)
        top_img = sims[0][0]
        assert top_img == name
        assert descs[top_img] == descs[name]
