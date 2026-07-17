# Multimodal

Trinity 支持多模态记忆——文本、图像、音频统一存储和检索。

## 图像记忆

```python
from trinity.modules.multimodal import ImageEncoder

encoder = ImageEncoder()
embedding = encoder.encode("path/to/image.jpg")
```

## 音频记忆

```python
from trinity.modules.multimodal import AudioEncoder

encoder = AudioEncoder()
embedding = encoder.encode("path/to/audio.wav")
```

## 多模态检索

所有模态统一使用 `Trinity.search()` 接口：

```python
from trinity import Trinity

mem = Trinity()
results = mem.search("包含猫的图片")
# 返回图像、文本、音频中匹配的结果
```
