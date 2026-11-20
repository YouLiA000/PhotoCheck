# YuNet 模型目录（K-07 兜底路径）

这个目录**默认是空的**，不是漏打包。人脸检测有两条路径，这条是兜底：

| 优先级 | 后端 | 前置条件 |
|---|---|---|
| 1 | mediapipe `FaceDetection` | `mediapipe` 能 import 且能在当前 venv 路径下初始化 |
| 2 | `cv2.FaceDetectorYN`（本目录的 ONNX） | mediapipe 不可用时才走，需要 `face_detection_yunet_2023mar.onnx` |

## 为什么模型文件不随包分发

1. 体积（约 230 KB）虽小，但它是**第三方模型权重**，随交付包再分发要单独确认许可；
2. 本机 mediapipe 可用，这条兜底路径**从未在真实人脸上验证过**，不宜让接收方误以为它已可用。

## 需要兜底时怎么准备

1. 下载 OpenCV Zoo 的 YuNet：`face_detection_yunet_2023mar.onnx`
   （<https://github.com/opencv/opencv_zoo/tree/main/models/face_detection_yunet>）；
2. 放到本目录，文件名保持不变；
3. 或在代码里显式指定绝对路径：

```python
from dataclasses import replace

from core.config import DEFAULT_CONFIG
from core.detect import analyze

cfg = replace(
    DEFAULT_CONFIG,
    face=replace(DEFAULT_CONFIG.face, yunet_model_path=r"C:/models/face_detection_yunet_2023mar.onnx"),
)
result = analyze("photo.jpg", cfg=cfg)
```

## 缺失时的行为（重要）

**会抛 `DetectorUnavailable`，不是静默降级。** 错误消息里带被查找的完整路径。

这条是刻意设计的：人脸检测不可用时如果安静地返回"没检出人脸"，
上层会把这张照片判成"无可处置内容"，用户就带着一张露脸的照片导出了。
宁可报错让人补模型，也不能给一个看起来正常的错答案。

`detector_status()["face"]` 也会如实返回 `unavailable`，`probe_region` 的
`unavailable` 元组会出现 `face`——**此时 `residual_face == 0` 不代表该区域没有人脸**。
