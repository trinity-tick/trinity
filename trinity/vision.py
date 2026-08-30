# -*- coding: utf-8 -*-
"""trinity/vision.py — 本地视觉描述（2026-09，EXECUTION 147）

轻量视觉感知：无需外部 vision 模型，用 PIL 提取图像基础特征生成
描述文本（尺寸/主色调/亮度/复杂度）。作为 /memory/perceive 的
image 通道描述器；未来可替换为真多模态模型（接口不变）。

describe_image(image: PIL.Image) -> str
"""
import math


def describe_image(image) -> str:
    """生成图像描述（基础视觉特征）。"""
    try:
        from PIL import Image as _PIL
        import numpy as _np
        if image.mode != "RGB":
            image = image.convert("RGB")
        w, h = image.size
        # 缩略采样（性能）
        small = image.resize((32, 32))
        arr = _np.asarray(small, dtype=_np.float32)
        mean_rgb = arr.mean(axis=(0, 1))
        brightness = float(arr.mean() / 255.0)
        # 复杂度：颜色方差
        variance = float(arr.std() / 255.0)
        # 主色调
        r, g, b = int(mean_rgb[0]), int(mean_rgb[1]), int(mean_rgb[2])
        if brightness < 0.2:
            tone = "暗色"
        elif brightness > 0.8:
            tone = "亮色"
        else:
            tone = "中等亮度"
        if variance < 0.08:
            complexity = "内容单一"
        elif variance < 0.2:
            complexity = "内容适中"
        else:
            complexity = "内容丰富"
        return (f"截图 {w}x{h}px，{tone}，{complexity}，"
                f"主色调 RGB({r},{g},{b})")
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
