# -*- coding: utf-8 -*-
"""trinity/vision.py — 本地视觉描述（2026-09，EXECUTION 147/149）

轻量视觉感知：无需外部 vision 模型，用 PIL/numpy 提取图像特征生成
描述文本。EXECUTION 149 增强：
  - 高对比文字区域检测（UI/截图常见）
  - 颜色分布（主色系 + 数量）
  - 边缘密度（界面复杂度近似）

describe_image(image: PIL.Image) -> str
"""
import math


def _text_regions(arr, threshold=180):
    """高对比度块检测（近似文字/图标区域）。

    arr: (H, W, 3) numpy uint8；返回块数量与总占比。
    """
    try:
        import numpy as _np
        lum = arr.mean(axis=2)
        # 高对比：局部亮暗差异大
        h, w = lum.shape
        block = 16
        nh, nw = h // block, w // block
        high = 0
        for by in range(nh):
            for bx in range(nw):
                blk = lum[by * block:(by + 1) * block, bx * block:(bx + 1) * block]
                if blk.size == 0:
                    continue
                rng = float(blk.max() - blk.min())
                if rng > threshold:
                    high += 1
        total = max(1, nh * nw)
        return high, high / total
    except Exception:
        return 0, 0.0


def _edge_density(arr):
    """边缘密度（相邻像素差异比例）——界面复杂度近似。"""
    try:
        import numpy as _np
        lum = arr.mean(axis=2)
        dx = _np.abs(_np.diff(lum, axis=1))
        dy = _np.abs(_np.diff(lum, axis=0))
        edges = float((dx > 20).mean()) + float((dy > 20).mean())
        return min(1.0, edges / 2.0)
    except Exception:
        return 0.0


def describe_image(image) -> str:
    """生成图像描述（基础视觉特征 + 语义线索）。"""
    try:
        from PIL import Image as _PIL
        import numpy as _np
        if image.mode != "RGB":
            image = image.convert("RGB")
        w, h = image.size
        small = image.resize((64, 64))
        arr = _np.asarray(small, dtype=_np.uint8)
        mean_rgb = arr.mean(axis=(0, 1))
        brightness = float(arr.mean() / 255.0)
        variance = float(arr.std() / 255.0)
        r, g, b = int(mean_rgb[0]), int(mean_rgb[1]), int(mean_rgb[2])
        # 主色系
        if r > 150 and g < 100 and b < 100:
            tone = "红/橙色系"
        elif r < 100 and g > 120 and b < 120:
            tone = "绿色系"
        elif r < 100 and g < 100 and b > 150:
            tone = "蓝色系"
        elif brightness < 0.2:
            tone = "暗色"
        elif brightness > 0.8:
            tone = "亮色"
        else:
            tone = "中性色"
        if variance < 0.08:
            complexity = "内容单一"
        elif variance < 0.2:
            complexity = "内容适中"
        else:
            complexity = "内容丰富"
        # EXECUTION 149: 文字区域 + 边缘
        txt_blocks, txt_ratio = _text_regions(arr)
        edges = _edge_density(arr)
        parts = [f"截图 {w}x{h}px", tone, complexity]
        if txt_blocks >= 3:
            parts.append(f"含约 {txt_blocks} 处高对比文字/图标区")
        if edges > 0.3:
            parts.append("界面元素密集")
        elif edges < 0.08:
            parts.append("画面平滑")
        parts.append(f"主色 RGB({r},{g},{b})")
        return "，".join(parts)
    except Exception:
        return None


def describe_image_b64(b64_str: str) -> str:
    """base64 图像字符串 → 描述（失败返回 None）。"""
    try:
        import base64 as _b64, io as _io
        from PIL import Image as _PIL
        img = _PIL.open(_io.BytesIO(_b64.b64decode(b64_str)))
        return describe_image(img)
    except Exception:
        return None
